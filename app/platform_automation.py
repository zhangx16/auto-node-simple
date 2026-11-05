"""Durable, explicit VPSNAT Argo batches. Passwords live only in private recovery files."""
import hashlib
import json
import os
import re
import secrets
import shlex
import threading
import time
import paramiko
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from . import vpsnat,remote_install,cloudflare

LOCK=threading.Lock()
PORT_CONFLICTS={'PORT_IN_USE','PORT_CONFLICT','PORT_ALREADY_EXISTS','HOST_PORT_IN_USE','PORT_OCCUPIED','PORT_ALREADY_IN_USE'}

def init(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS automation_batches(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,digest TEXT,status TEXT,base_port INTEGER,created REAL,finished REAL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS automation_items(batch_id TEXT,vm_id TEXT,name TEXT,state TEXT,stage TEXT,message TEXT DEFAULT '',port INTEGER DEFAULT 0,public_port INTEGER DEFAULT 0,install_id TEXT DEFAULT '',verified INTEGER DEFAULT 0,updated REAL,PRIMARY KEY(batch_id,vm_id));''')

def recover():
    from . import main
    with main.conn() as c:
        c.execute("UPDATE automation_items SET state='unknown',message='面板重启，操作结果待核对；重试批次会复用已有凭据和映射' WHERE state IN ('queued','running')")
        c.execute("UPDATE automation_batches SET status='interrupted',finished=? WHERE status='running'",(time.time(),))

def active(c,vm_id):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_items'").fetchone():return False
    return bool(c.execute("SELECT 1 FROM automation_items WHERE vm_id=? AND state IN ('queued','running')",(vm_id,)).fetchone())

def update(batch,vm,**values):
    from . import main
    values['updated']=time.time()
    with main.conn() as c:c.execute('UPDATE automation_items SET '+','.join(k+'=?' for k in values)+' WHERE batch_id=? AND vm_id=?',(*values.values(),batch,vm))

def profile_path(vm,base):
    from . import main
    folder=main.DATA/'automation-secrets';folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
    return folder/f'vm-{vpsnat.identifier(vm)}-argo-{base}.json'

def save_profile(path,data):
    temp=path.with_suffix('.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    os.replace(temp,path);path.chmod(0o600)

def read_profile(path):
    if not path.exists():return {}
    data=json.loads(path.read_text())
    if not isinstance(data,dict):raise ValueError('自动化恢复记录格式无效')
    return data

def select_port(base,mappings,listening):
    used={p['internal_port'] for p in mappings}|{p['public_port'] for p in mappings}|set(listening)
    for port in range(base,min(base+1000,65536)):
        if port not in used:return port
    raise HTTPException(409,'起始端口之后没有可用端口（最多检查 1000 个）')

def execute(t,command,timeout=20):
    with t.open_session(timeout=timeout) as ch:
        ch.settimeout(timeout);ch.exec_command(command);output=bytearray();deadline=time.monotonic()+timeout
        while True:
            while ch.recv_ready():
                output.extend(ch.recv(65536))
                if len(output)>131072:raise RuntimeError('远端检查响应过大')
            while ch.recv_stderr_ready():ch.recv_stderr(65536)
            if ch.exit_status_ready():return ch.recv_exit_status(),output.decode(errors='replace')
            if time.monotonic()>deadline:raise TimeoutError('远端检查超时')
            time.sleep(.05)

def listeners(t):
    # /proc/net is available on supported Linux guests, without requiring ss first.
    code,out=execute(t,"awk 'FNR>1 {split($2,a,\":\"); print a[length(a)]}' /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6 2>/dev/null")
    if code:raise RuntimeError('无法读取实例已占用端口，停止猜测端口')
    return {int(line,16) for line in out.splitlines() if re.fullmatch(r'[0-9A-Fa-f]{4}',line)}

def discover_ssh(machine,ports,profile):
    ssh=[p for p in ports if p['protocol']=='tcp' and p['internal_port']==22]
    if not ssh:raise HTTPException(422,'实例没有 TCP 22 的 SSH 映射，请先添加 SSH 映射')
    hosts=list(dict.fromkeys([profile.get('host',''),machine.get('public_ip',''),*machine.get('entry_candidates',[])]))
    results=[]
    for host in hosts:
        if not host:continue
        for p in ssh:
            try:
                found=remote_install.probe(machine['id'],remote_install.SSHProbe(host=host,forward_id=p['id']))
                results.append({'host':host,'forward_id':p['id'],'fingerprint':found['fingerprint'],'ssh_port':p['public_port']})
            except HTTPException:continue
    if len(results)!=1:raise HTTPException(409,'未找到唯一可用的 SSH 入口，未重置密码；请核对平台地域入口')
    chosen=results[0]
    if profile.get('fingerprint') and profile['fingerprint']!=chosen['fingerprint']:
        raise HTTPException(409,'SSH 指纹与已保存记录不同，停止自动化，请核对服务器身份')
    return chosen

def reset_password(vm,profile,path,replay=False):
    if profile.get('reset_state') == 'confirmed' and profile.get('password'):return
    if profile.get('reset_state') == 'pending' and profile.get('password') and not replay:return
    if not (replay and profile.get('password') and profile.get('reset_key')):
        profile.update(password='An9!'+secrets.token_urlsafe(24),reset_state='pending',reset_key=str(uuid4()),reset_at=time.time())
    save_profile(path,profile)  # Persist BEFORE mutation so timeouts never lose the new credential.
    try:result=vpsnat.request('POST',f'/vms/{vm}/reset-password',{'password':profile['password']},profile['reset_key'])
    except vpsnat.APIError:
        # A response/transport failure may occur after the platform changed the password.
        # Next execution tries this saved credential; it never blindly rotates it again.
        raise
    root=result.get('root_password') or (result.get('data',{}).get('root_password') if isinstance(result.get('data'),dict) else None)
    if root:profile['password']=root
    profile['reset_state']='confirmed';save_profile(path,profile)

def managed_mapping(vm,base,profile,path,ports,used):
    tag=f'auto-node-simple:argo:{vm}:{base}'
    raw=vpsnat.collection(vpsnat.request('GET',f'/vms/{vm}/port-forwards'),('port_forwards','items'))
    ours=[p for p in raw if p.get('description')==tag and str(p.get('protocol')).lower()=='tcp']
    if profile.get('mapping_id'):
        match=next((p for p in ports if p['id']==str(profile['mapping_id'])),None)
        if match:return match
        raise HTTPException(409,'此前创建的映射已删除，请先核对恢复记录和远端状态')
    if len(ours)>1:raise HTTPException(409,'找到重复的自动化映射，请先在平台核对')
    if ours:
        p=ours[0]
        result={'id':str(p['id']),'protocol':'tcp','internal_port':int(p['guest_port']),'public_port':int(p['host_port']),'public_ip':''}
        profile.update(mapping_id=result['id'],port=result['internal_port'],public_port=result['public_port']);save_profile(path,profile)
        return result
    allowed=profile.get('allowed_port_range')
    port=select_port(max(base,allowed[0] if allowed else base),ports,used)
    for _ in range(1000):
        if port>65535:break
        profile.update(port=port,mapping_state='pending');save_profile(path,profile)
        try:
            result=vpsnat.mutate('POST',f'/vms/{vm}/port-forwards',{'protocol':'tcp','guest_port':port,'host_port':port,'description':tag},f'automapping-{vm}-{base}-{port}')
        except vpsnat.APIError as exc:
            if getattr(exc,'code',None)=='PORT_OUT_OF_RANGE' and exc.port_range:
                profile['allowed_port_range']=list(exc.port_range);save_profile(path,profile)
                port=select_port(max(port+1,exc.port_range[0]),ports,used)
                if port>exc.port_range[1]:raise HTTPException(409,'平台允许端口范围内没有可用端口')
                continue
            if getattr(exc,'code',None) in PORT_CONFLICTS:
                used.add(port);port=select_port(port+1,ports,used);continue
            raise
        row=vpsnat.object_data(result,'port_forward')
        mapping={'id':str(row['id']),'protocol':'tcp','internal_port':int(row['guest_port']),'public_port':int(row['host_port']),'public_ip':''}
        profile.update(mapping_id=mapping['id'],port=mapping['internal_port'],public_port=mapping['public_port'],mapping_state='confirmed');save_profile(path,profile)
        return mapping
    raise HTTPException(409,'未能分配可用映射端口')

def install_one(batch,machine,base,request):
    from . import main,node_probe
    vm=machine['id'];path=profile_path(vm,base);profile=read_profile(path);t=None;executing=False
    try:
        update(batch,vm,state='running',stage='preflight',message='检查实例与 SSH 入口')
        with main.conn() as c:
            remote_install.reconcile(c)
            if c.execute("SELECT 1 FROM remote_installs WHERE vm_id=? AND status IN ('running','unknown')",(vm,)).fetchone():
                raise HTTPException(409,'实例已有运行中或结果未知的 SSH 安装任务，请先核对')
            existing=c.execute('SELECT id,uri FROM nodes WHERE id=?',(profile.get('install_id',''),)).fetchone()
        if existing:
            verified=bool(profile.get('verified',False))
            if not verified:
                update(batch,vm,stage='verify',message='复用已安装节点，重新验证实际转发')
                try:node_probe.probe(existing['uri']);verified=True
                except (ValueError,RuntimeError):pass
                profile['verified']=verified;save_profile(path,profile)
            update(batch,vm,state='done',stage='done',message='复用已完成的自动化节点，未再次重置密码或创建映射' if verified else '节点已安装，实际转发仍待核对；未重复安装',port=profile['port'],public_port=profile['public_port'],install_id=existing['id'],verified=int(verified))
            return
        if profile.get('completed'):raise HTTPException(409,'先前安装已完成，但节点记录已被删除；请核对服务器服务后处理')
        if machine['status']!='running':raise HTTPException(409,'实例未运行，未执行重置或安装')
        ports=vpsnat.forwards(vm)
        identity=discover_ssh(machine,ports,profile);profile.update(identity);save_profile(path,profile)
        update(batch,vm,stage='reset_password',message='API 重置 root 密码，凭据保存在本机私有恢复文件')
        reset_password(vm,profile,path)
        login=remote_install.SSHLogin(host=profile['host'],forward_id=profile['forward_id'],fingerprint=profile['fingerprint'],password=profile['password'])
        update(batch,vm,stage='ssh',message='使用已保存的新密码验证 SSH root 登录')
        try:t=remote_install.connect(vm,login)
        except paramiko.AuthenticationException:
            if profile.get('reset_state')!='pending':raise
            # Verify first; only if authentication fails, replay the SAME password/key.
            reset_password(vm,profile,path,replay=True);login.password=profile['password'];t=remote_install.connect(vm,login)
        login.password='';profile['reset_state']='confirmed';save_profile(path,profile)
        used=listeners(t)
        update(batch,vm,stage='port_forward',message='从起始端口检查占用并创建独立 TCP 映射')
        mapping=managed_mapping(vm,base,profile,path,ports,used)
        update(batch,vm,port=mapping['internal_port'],public_port=mapping['public_port'])
        ident=profile.get('install_id','');command=profile.get('command','')
        if ident:
            with main.conn() as c:old=c.execute('SELECT status,created FROM installs WHERE id=?',(ident,)).fetchone()
            if not old or time.time()-old['created']>86400:
                raise HTTPException(409,'旧安装任务已过期或删除，请核对原隧道和服务后处理')
        else:
            update(batch,vm,stage='cloudflare',message='创建独立 Cloudflare 固定隧道与安装任务')
            config=main.Install(name=machine['name'][:40]+f'-Argo-{mapping["internal_port"]}',protocol='argo',port=mapping['internal_port'],client_port=443,platform_vm_id=vm,platform_forward_id=mapping['id'])
            result=main.create_install(config) if request is None else main.create(config,request)
            ident=result['id'];command=result['command']
            profile.update(install_id=ident,command=command);save_profile(path,profile)
        update(batch,vm,install_id=ident,stage='install',message='正在通过 SSH 安装 Argo，等待成功回传')
        # Re-check the live socket to catch services started since preflight.
        code,_=execute(t,'test ! -f /usr/local/etc/auto-node-simple/configs/'+shlex.quote(ident)+'.json')
        if not code and profile['port'] in listeners(t):raise HTTPException(409,'端口已被其他服务占用，停止覆盖；请核对映射')
        with main.conn() as c:
            c.execute("INSERT OR REPLACE INTO remote_installs(id,vm_id,status,started) VALUES (?,?,?,?)",(ident,vm,'running',time.time()))
        executing=True;remote_install.worker(ident,t,command);t=None;executing=False
        with main.conn() as c:
            row=c.execute('SELECT status,error FROM installs WHERE id=?',(ident,)).fetchone()
            node=c.execute('SELECT uri FROM nodes WHERE id=?',(ident,)).fetchone()
        if not row or row['status']!='done' or not node:raise HTTPException(409,'远端安装未确认成功，请查看安装记录；再次尝试会复用已保存的任务')
        profile.update(completed=True);profile.pop('command',None);save_profile(path,profile)
        update(batch,vm,stage='verify',message='节点已保存，正在检测实际转发')
        verified=False
        for attempt in range(2):
            try:node_probe.probe(node['uri']);verified=True;break
            except (ValueError,RuntimeError):
                if attempt==0:time.sleep(5)
        profile['verified']=verified;save_profile(path,profile)
        update(batch,vm,state='done',stage='done',verified=int(verified),message='Argo 安装及实际转发验证成功' if verified else 'Argo 已安装并回传，实际转发暂未通过，请查看节点巡检')
    except Exception as exc:
        if isinstance(exc,HTTPException):message=str(exc.detail)
        elif isinstance(exc,vpsnat.APIError):message=str(exc)
        else:message='自动化阶段失败（'+type(exc).__name__+'），凭据和已完成步骤保留；请核对实例状态'
        # Never include raw exception text: SSH/library errors may contain secrets.
        update(batch,vm,state='unknown' if executing else 'failed',message=message)
    finally:
        if t:t.close()

def run(batch,machines,base,request):
    from . import main
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda m:install_one(batch,m,base,request),machines))
    finally:
        with main.conn() as c:
            remaining=c.execute("SELECT count(*) FROM automation_items WHERE batch_id=? AND state IN ('queued','running')",(batch,)).fetchone()[0]
            c.execute('UPDATE automation_batches SET status=?,finished=? WHERE id=?',('interrupted' if remaining else 'finished',time.time(),batch))
        from . import platform_watch
        platform_watch.reconcile_batch(batch)

def start(vm_ids,base,key,request):
    from . import main
    if not key or not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}',key):raise HTTPException(422,'批次需要有效 Idempotency-Key')
    digest=hashlib.sha256(json.dumps([sorted(vm_ids),base]).encode()).hexdigest()
    with LOCK:
        with main.conn() as c:
            row=c.execute('SELECT id,digest FROM automation_batches WHERE request_key=?',(key,)).fetchone()
            if row:
                if row['digest']!=digest:raise HTTPException(409,'相同幂等键不能用于不同批次参数')
                return {'id':row['id']}
            if c.execute("SELECT 1 FROM automation_batches WHERE status='running'").fetchone():raise HTTPException(409,'已有自动化批次运行中，请等待完成')
        if not cloudflare.status()['configured']:raise HTTPException(422,'请先配置默认 Cloudflare 账户')
        machines=vpsnat.vms();ids={vpsnat.identifier(v) for v in vm_ids}
        if ids and ids-{m['id'] for m in machines}:raise HTTPException(422,'包含不属于当前账户的实例 ID')
        machines=[m for m in machines if not ids or m['id'] in ids]
        if not machines:raise HTTPException(409,'没有可处理的平台实例')
        batch=secrets.token_hex(8)
        with main.conn() as c:
            c.execute('INSERT INTO automation_batches(id,request_key,digest,status,base_port,created) VALUES (?,?,?,?,?,?)',(batch,key,digest,'running',base,time.time()))
            for m in machines:c.execute('INSERT INTO automation_items(batch_id,vm_id,name,state,stage,updated) VALUES (?,?,?,?,?,?)',(batch,m['id'],m['name'],'queued','queued',time.time()))
        threading.Thread(target=run,args=(batch,machines,base,request),daemon=True).start()
        return {'id':batch,'count':len(machines)}

def listing():
    from . import main
    with main.conn() as c:
        batches=[dict(r) for r in c.execute('SELECT id,status,base_port,created,finished FROM automation_batches ORDER BY created DESC LIMIT 10')]
        for b in batches:b['items']=[dict(r) for r in c.execute('SELECT vm_id,name,state,stage,message,port,public_port,install_id,verified,updated FROM automation_items WHERE batch_id=? ORDER BY rowid',(b['id'],))]
    return batches
