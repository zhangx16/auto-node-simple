"""One-shot SSH bootstrap; no saved SSH credentials or arbitrary command endpoint."""
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import socket
import threading
import time
import paramiko
from fastapi import HTTPException
from pydantic import BaseModel, Field
from . import vpsnat

LOCK = threading.Lock()

class SSHLogin(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[A-Za-z0-9:.\-]+$')
    forward_id: str = Field(pattern=r'^[1-9][0-9]*$')
    fingerprint: str = Field(default='', max_length=100)
    password: str = Field(default='', max_length=4096, repr=False)
    private_key: str = Field(default='', max_length=32768, repr=False)
    passphrase: str = Field(default='', max_length=4096, repr=False)

class SSHProbe(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=r'^[A-Za-z0-9:.\-]+$')
    forward_id: str = Field(pattern=r'^[1-9][0-9]*$')

def init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS remote_installs(
        id TEXT PRIMARY KEY,vm_id TEXT NOT NULL,status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',started REAL NOT NULL,finished REAL NOT NULL DEFAULT 0)""")

def recover():
    from . import main
    with main.conn() as c:
        c.execute("UPDATE remote_installs SET status='unknown',message='面板已重启，远端安装结果待核对；请查看节点与安装记录',finished=? WHERE status='running'",(time.time(),))

def reconcile(c):
    c.execute("UPDATE remote_installs SET status='done',message='安装成功，节点已保存' WHERE status IN ('unknown','finished') AND id IN (SELECT id FROM installs WHERE status='done')")

def status(c, ident):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='remote_installs'").fetchone():
        return None
    reconcile(c)
    row = c.execute('SELECT status,message,started,finished FROM remote_installs WHERE id=?',(ident,)).fetchone()
    return dict(row) if row else None

def endpoint(vm_id, b):
    vpsnat.vm(vm_id)  # Verify ownership and current existence via the user's key.
    mapping = next((p for p in vpsnat.forwards(vm_id) if p['id'] == b.forward_id), None)
    if not mapping or mapping['protocol'] != 'tcp' or mapping['internal_port'] != 22:
        raise HTTPException(422, '请选择映射到内部 TCP 22 的 SSH 端口')
    try:
        answers = socket.getaddrinfo(b.host, mapping['public_port'],type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(a[4][0] for a in answers))
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise HTTPException(422,'SSH 入口必须解析为公网地址')
    except socket.gaierror:
        raise HTTPException(422,'SSH 入口域名解析失败') from None
    # Pin the resolved address; never resolve again while authenticating.
    return addresses[0], mapping['public_port']

def fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')

def transport(address, port):
    sock = socket.create_connection((address, port), timeout=12)
    try:
        t = paramiko.Transport(sock)
        t.banner_timeout = 12
        t.auth_timeout = 15
        t.start_client(timeout=12)
        t.set_keepalive(20)
        return t
    except Exception:
        sock.close()
        raise

def probe(vm_id,b):
    t = None
    try:
        address,port = endpoint(vm_id,b)
        t = transport(address,port)
        return {'fingerprint':fingerprint(t.get_remote_server_key()),'port':port}
    except vpsnat.APIError as exc:
        raise HTTPException(exc.status,str(exc)) from None
    except (OSError,paramiko.SSHException,EOFError):
        raise HTTPException(502,'SSH 握手失败，请核对公网入口、SSH 映射及服务状态') from None
    finally:
        if t:t.close()

def private_key(text,passphrase):
    for kind in (paramiko.Ed25519Key,paramiko.ECDSAKey,paramiko.RSAKey):
        try:return kind.from_private_key(io.StringIO(text), password=passphrase or None)
        except (paramiko.SSHException,ValueError):pass
    raise HTTPException(422,'无法读取 SSH 私钥，请检查格式和私钥口令')

def connect(vm_id,b):
    if not b.fingerprint.startswith('SHA256:'):
        raise HTTPException(422,'请先读取并核对 SSH 主机指纹')
    if not b.password and not b.private_key:
        raise HTTPException(422,'自动安装需要 root 密码或 SSH 私钥')
    t = None
    try:
        address,port = endpoint(vm_id,b)
        t = transport(address,port)
        if not hmac.compare_digest(fingerprint(t.get_remote_server_key()),b.fingerprint):
            raise HTTPException(409,'SSH 主机指纹已变化，未发送登录凭据；请核对入口和服务器身份')
        if b.private_key:t.auth_publickey('root',private_key(b.private_key,b.passphrase))
        else:t.auth_password('root',b.password)
        with t.open_session(timeout=12) as channel:
            channel.settimeout(12)
            channel.exec_command('id -u')
            if channel.recv(128).strip() != b'0' or channel.recv_exit_status() != 0:
                raise HTTPException(403,'自动安装需要 root 权限')
        return t
    except BaseException:
        if t:t.close()
        raise

def worker(ident,t,command):
    from . import main
    message='';state='unknown'
    try:
        with t.open_session(timeout=15) as channel:
            channel.settimeout(15)
            channel.exec_command(command)
            deadline=time.monotonic()+900
            while True:
                # Installer output may contain tokens and node links: drain but never log/store it.
                while channel.recv_ready():channel.recv(65536)
                while channel.recv_stderr_ready():channel.recv_stderr(65536)
                if channel.exit_status_ready():
                    code=channel.recv_exit_status()
                    state='unknown' if code==0 else 'failed'
                    message='脚本已执行，等待安装结果回传' if code==0 else '远端脚本执行失败，请查看安装结果或服务器状态'
                    break
                if time.monotonic()>deadline:
                    message='SSH 等待超过 15 分钟，远端结果待核对；请勿直接重复安装'
                    break
                time.sleep(.2)
    except Exception:
        message='SSH 连接中断，远端结果待核对；请查看节点与安装记录'
    finally:
        t.close()
        with main.conn() as c:
            row=c.execute('SELECT status FROM installs WHERE id=?',(ident,)).fetchone()
            if row and row['status']=='done':state='done';message='安装成功，节点已保存'
            elif row and row['status']=='failed':state='failed';message='安装未完成，请查看安装结果'
            c.execute('UPDATE remote_installs SET status=?,message=?,finished=? WHERE id=?',(state,message,time.time(),ident))

def start(vm_id,install,b,request):
    from . import main
    if install.platform_vm_id != vm_id:
        raise HTTPException(422,'安装目标与所选实例不一致')
    with LOCK:
        with main.conn() as c:
            reconcile(c)
            from . import platform_automation
            if platform_automation.active(c,vm_id):raise HTTPException(409,'此实例正在执行批量自动化安装，请等待完成')
            if c.execute("SELECT 1 FROM remote_installs WHERE vm_id=? AND status IN ('running','unknown')",(vm_id,)).fetchone():
                raise HTTPException(409,'此实例已有安装任务或未确认结果，请先查看安装记录并核对远端状态')
            if c.execute("SELECT count(*) FROM remote_installs WHERE status='running'").fetchone()[0]>=2:
                raise HTTPException(409,'已有两个远程安装任务，请等待完成')
        t = None
        try:
            t=connect(vm_id,b)
            # Authentication secrets are never included in the persisted install configuration.
            b.password='';b.private_key='';b.passphrase=''
            result=main.create(install,request)
            with main.conn() as c:
                c.execute('INSERT INTO remote_installs(id,vm_id,status,started) VALUES (?,?,?,?)',(result['id'],vm_id,'running',time.time()))
            thread=threading.Thread(target=worker,args=(result['id'],t,result['command']),daemon=True)
            thread.start()
            return {'id':result['id'],'remote':True,'hostname':result.get('hostname','')}
        except (paramiko.AuthenticationException, paramiko.PasswordRequiredException):
            if t:t.close()
            raise HTTPException(422,'SSH 登录失败，请检查 root 凭据及密码登录设置') from None
        except (OSError,paramiko.SSHException,EOFError):
            if t:t.close()
            raise HTTPException(502,'SSH 连接失败，请核对入口和 SSH 服务') from None
        except vpsnat.APIError as exc:
            if t:t.close()
            raise HTTPException(exc.status,str(exc)) from None
        except BaseException:
            if t:t.close()
            raise
        finally:
            b.password='';b.private_key='';b.passphrase=''
