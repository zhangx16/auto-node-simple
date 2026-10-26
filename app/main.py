"""Three protocols, command installation and results. No agent or background worker."""
import base64,hashlib,hmac,json,os,secrets,shlex,sqlite3,time,re,ipaddress
from pathlib import Path
from typing import Literal
from uuid import uuid4
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import FileResponse,PlainTextResponse,Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer,BadSignature
from pydantic import BaseModel,Field,field_validator
from . import cloudflare,node_management,auto_repair,node_upgrade
ROOT=Path(__file__).parent;DATA=Path(os.getenv('DATA_DIR','/app/data'));DATA.mkdir(parents=True,exist_ok=True)
ASSETS=Path(os.getenv('ASSET_DIR','/app/assets'))
PANEL=os.getenv('PANEL_URL','http://localhost:18901').rstrip('/')
DEFAULT_ARGO_ADDRESS='www.cloudflare.com'
AUTH=DATA/'auth.json'
if not AUTH.exists():
 password=secrets.token_urlsafe(18);salt=secrets.token_hex(16)
 AUTH.write_text(json.dumps({'username':'admin','salt':salt,'hash':hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),120000).hex()}));AUTH.chmod(0o600)
 (DATA/'credentials.txt').write_text('Username: admin\nPassword: '+password+'\n');(DATA/'credentials.txt').chmod(0o600)
KEY=DATA/'session.key'
if not KEY.exists():KEY.write_text(secrets.token_hex(32));KEY.chmod(0o600)
signer=URLSafeTimedSerializer(KEY.read_text(),salt='simple-session')
def conn():
 c=sqlite3.connect(DATA/'simple.db',timeout=10);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
with conn() as c:
 c.executescript('CREATE TABLE IF NOT EXISTS installs(id TEXT PRIMARY KEY,token_hash TEXT NOT NULL,config TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,result TEXT NOT NULL DEFAULT \'\',error TEXT NOT NULL DEFAULT \'\');CREATE TABLE IF NOT EXISTS attempts(ip TEXT,at REAL);')
 c.executescript("""CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,name TEXT NOT NULL,protocol TEXT NOT NULL,uri TEXT NOT NULL,created REAL NOT NULL);
 CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,name TEXT NOT NULL,token TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1);
 CREATE TABLE IF NOT EXISTS subscription_nodes(subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,position INTEGER NOT NULL,PRIMARY KEY(subscription_id,node_id));""")
 node_management.init(c)
 auto_repair.init(c)
 if 'target_node' not in {r[1] for r in c.execute('PRAGMA table_info(installs)')}:c.execute("ALTER TABLE installs ADD COLUMN target_node TEXT NOT NULL DEFAULT ''")
 cloudflare.init(c)
 from . import formats
 for row in c.execute("SELECT * FROM installs WHERE status='done' AND result!='' AND target_node=''").fetchall():
  try:
   p=formats.parse(row['result']);c.execute('INSERT OR IGNORE INTO nodes(id,name,protocol,uri,created) VALUES (?,?,?,?,?)',(row['id'],p['name'],p['kind'],row['result'],row['created']))
  except (ValueError,TypeError):pass
 node_management.migrate_argo_suffix(c)
os.chmod(DATA/'simple.db',0o600)
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)
app.add_event_handler('startup',auto_repair.start)
app.add_event_handler('shutdown',auto_repair.stop)
def owner(r):
 try:signer.loads(r.cookies.get('simple_session',''),max_age=86400)
 except BadSignature:raise HTTPException(401,'请先登录')
 if r.method not in ('GET','HEAD') and r.headers.get('origin') and r.headers['origin'].rstrip('/')!=PANEL:raise HTTPException(403,'请求来源不匹配')
class Login(BaseModel):
 username:str=Field(max_length=80);password:str=Field(max_length=1024)
@app.post('/api/login')
def login(b:Login,r:Request):
 ip=r.headers.get('x-real-ip') or r.client.host
 with conn() as c:
  c.execute('DELETE FROM attempts WHERE at<?',(time.time()-900,))
  if c.execute('SELECT count(*) FROM attempts WHERE ip=?',(ip,)).fetchone()[0]>=8:raise HTTPException(429,'尝试过多，请稍后再试')
  a=json.loads(AUTH.read_text());digest=hashlib.pbkdf2_hmac('sha256',b.password.encode(),a['salt'].encode(),120000).hex()
  if b.username!=a['username'] or not hmac.compare_digest(digest,a['hash']):
   c.execute('INSERT INTO attempts VALUES (?,?)',(ip,time.time()));c.commit();raise HTTPException(401,'用户名或密码错误')
 response=PlainTextResponse('ok');response.set_cookie('simple_session',signer.dumps({'user':b.username}),secure=True,httponly=True,samesite='strict',max_age=86400);return response
@app.post('/api/logout')
def logout(r:Request):
 owner(r);res=PlainTextResponse('ok');res.delete_cookie('simple_session');return res
class Install(BaseModel):
 name:str=Field(default='我的节点',min_length=1,max_length=60)
 protocol:Literal['argo','reality','hysteria2']='argo'
 port:int=Field(default=18080,ge=1024,le=65535)
 address:str=Field(default='',max_length=253)
 client_port:int=Field(default=443,ge=1,le=65535)
 sni:str=Field(default='www.amd.com',min_length=1,max_length=253)
 tunnel:Literal['fixed']='fixed'
 cf_mode:Literal['default','manual']='default'
 tunnel_token:str=Field(default='',max_length=4096)
 tunnel_hostname:str=Field(default='',max_length=253)
 @field_validator('address','sni','tunnel_hostname')
 @classmethod
 def host(cls,v):
  if not v:return v
  if not re.fullmatch(r'[A-Za-z0-9:.\-]+',v) or v.startswith('-'):raise ValueError('请输入域名或 IP，不含协议及路径')
  return v
 @field_validator('name')
 @classmethod
 def clean_name(cls,v):
  if any(ord(x)<32 for x in v):raise ValueError('名称不能含控制字符')
  return v
@app.get('/api/cloudflare')
def cf_status(r:Request):owner(r);return cloudflare.status()
@app.get('/api/session')
def session(r:Request):owner(r);return {'username':json.loads(AUTH.read_text())['username']}
def download_command(url):
 fetch='if command -v curl >/dev/null 2>&1; then curl -fsSL --connect-timeout 10 --max-time 60 '+shlex.quote(url)+' -o "$f"; elif command -v wget >/dev/null 2>&1; then wget -q -T 60 -O "$f" '+shlex.quote(url)+'; else echo "需要 curl 或 wget；Alpine 可执行 apk add curl，Debian/Ubuntu 可执行 apt-get install curl" >&2; exit 1; fi'
 return 'sh -c '+shlex.quote('umask 077; f=$(mktemp); trap '+shlex.quote('rm -f "$f"')+' EXIT; '+fetch+' && sh "$f"')

@app.post('/api/installs')
def create(b:Install,r:Request):
 owner(r)
 if b.protocol!='argo' and not b.address:raise HTTPException(422,'直连协议必须填写公网连接地址')
 if b.protocol=='argo' and b.client_port not in (443,2053,2083,2087,2096,8443):raise HTTPException(422,'Argo 客户端需使用 Cloudflare HTTPS 端口')
 automatic=b.protocol=='argo' and b.cf_mode=='default' and not b.tunnel_token
 if b.protocol=='argo' and not automatic and (not b.tunnel_token or not b.tunnel_hostname):raise HTTPException(422,'手动模式需要 Token 和已配置的隧道域名')
 if automatic and not cloudflare.status()['configured']:raise HTTPException(422,'未配置默认 Cloudflare 账户，请使用手动 Token 模式')
 if b.tunnel_token and not re.fullmatch(r'[A-Za-z0-9_+/=.-]+',b.tunnel_token):raise HTTPException(422,'隧道 Token 格式无效')
 ident=secrets.token_hex(8);token=secrets.token_urlsafe(32);cfg=b.model_dump();cfg.update(id=ident,credential=str(uuid4()),path='/'+secrets.token_hex(12))
 if b.protocol=='argo' and not cfg['address'].strip():cfg['address']=DEFAULT_ARGO_ADDRESS
 if b.protocol!='argo' or b.tunnel!='fixed':cfg['tunnel_token']=''
 with conn() as c:
  c.execute("DELETE FROM installs WHERE created<?",(time.time()-86400,))
  if c.execute('SELECT count(*) FROM installs').fetchone()[0]>=100:raise HTTPException(409,'安装记录已满，请稍后重试')
  c.execute('INSERT INTO installs(id,token_hash,config,status,created) VALUES (?,?,?,?,?)',(ident,hashlib.sha256(token.encode()).hexdigest(),json.dumps(cfg),'ready',time.time()))
 if automatic:
  try:
   provisioned=cloudflare.provision(ident,b.tunnel_hostname,b.port)
   cfg.update(tunnel_hostname=provisioned['hostname'],tunnel_token=provisioned['tunnel_token'])
   with conn() as c:c.execute('UPDATE installs SET config=? WHERE id=?',(json.dumps(cfg),ident))
  except cloudflare.CFError as exc:
   with conn() as c:c.execute("UPDATE installs SET status='failed',error=? WHERE id=?",(str(exc),ident))
   raise HTTPException(502,str(exc))
 url=f'{PANEL}/install/{ident}/{token}.sh'
 command=download_command(url)
 return {'id':ident,'command':command,'hostname':cfg.get('tunnel_hostname','')}
def authorized(ident,token):
 with conn() as c:row=c.execute('SELECT * FROM installs WHERE id=?',(ident,)).fetchone()
 if not row or time.time()-row['created']>86400 or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),row['token_hash']):raise HTTPException(404,'命令不存在或已过期')
 return dict(row)
@app.get('/install/{ident}/{token}.sh')
def script(ident:str,token:str):
 row=authorized(ident,token)
 if row['status']=='done':raise HTTPException(410,'安装已完成，请在页面查看节点链接')
 if row['status']=='cancelled':raise HTTPException(410,'更新命令已取消')
 if json.loads(row['config']).get('protocol')=='argo' and json.loads(row['config']).get('tunnel')!='fixed':raise HTTPException(410,'临时隧道已移除，请重新生成固定隧道安装命令')
 cfg=json.loads(row['config']);manifest=json.loads((ASSETS/'manifest.json').read_text())
 if cfg.get('protocol')=='argo' and not cfg.get('address','').strip():cfg['address']=DEFAULT_ARGO_ADDRESS
 with conn() as c:keep_repair=bool(c.execute('SELECT 1 FROM node_repair WHERE node_id=?',(str(cfg.get('id',ident)),)).fetchone())
 values={'PANEL':PANEL,'INSTALL_ID':ident,'NODE_ID':str(cfg.get('id',ident)),'CALLBACK_TOKEN':token,'PAYLOAD':base64.b64encode(json.dumps(cfg).encode()).decode(),'MANIFEST':base64.b64encode(json.dumps(manifest).encode()).decode(),'REPORT_TOKEN':auto_repair.install_token(str(cfg.get('id',ident)),token),'REPAIR_KEEP_TOKEN':'1' if keep_repair else '0','REPAIR_INSTALLER':base64.b64encode(auto_repair.installer().encode()).decode(),'NODE_RELEASE':node_upgrade.release()}
 text='#!/bin/sh\nset -eu\numask 077\n'+'\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n'+(ROOT/'install.sh').read_text()
 return PlainTextResponse(text,headers={'Cache-Control':'no-store'})
class Result(BaseModel):
 link:str=Field(default='',max_length=16000);error:str=Field(default='',max_length=300)
 repair_installed:bool=False
 repair_token_kept:bool=False
 installed_release:str=Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')
@app.post('/result/{ident}/{token}')
def result(ident:str,token:str,b:Result):
 with node_management.LOCK:return accept_result(ident,token,b)

def accept_result(ident,token,b):
 row=authorized(ident,token)
 if row['status']=='done':return {'ok':True}
 if row['status']=='cancelled':raise HTTPException(409,'更新已取消，请核对远端状态')
 if b.link:
  try:parsed=formats.parse(b.link)
  except (ValueError,TypeError):raise HTTPException(422,'节点链接无效')
 if b.link and parsed['kind']=='argo':b.link=node_management.argo_uri(b.link);parsed=formats.parse(b.link)
 cfg=json.loads(row['config']);target=row.get('target_node','')
 if target:
  with conn() as c:
   old=node_management.get(c,target);update=c.execute('SELECT * FROM node_updates WHERE node_id=? AND job_id=?',(target,ident)).fetchone()
   if not update or old['uri']!=update['base_uri']:raise HTTPException(409,'更新命令已失效，请核对节点状态')
  if b.link:
   before=formats.parse(old['uri'])
   if (parsed['kind']!=cfg['protocol'] or parsed['credential']!=cfg['credential'] or parsed['port']!=cfg['client_port'] or parsed['server'].lower()!=cfg['address'].lower() or parsed['sni']!=cfg['sni']):raise HTTPException(409,'回传连接参数与更新配置不一致')
   if parsed['kind']=='reality' and (parsed['public_key']!=before['public_key'] or parsed['short_id']!=before['short_id']):raise HTTPException(409,'Reality 身份密钥发生变化，未覆盖原节点')
   if parsed['kind']=='argo' and cloudflare.binding(target) and not cfg.get('_upgrade_release'):
    try:cloudflare.update_port(target,cfg['port'])
    except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
 with conn() as c:
  current=c.execute('SELECT status FROM installs WHERE id=?',(ident,)).fetchone()
  if current['status']=='done':return {'ok':True}
  if b.link:
   name=node_management.display_name(cfg) if cfg.get('name') else parsed['name']
   stored={k:v for k,v in cfg.items() if not k.startswith('_')}
   if cloudflare.binding(target or ident):stored['tunnel_token']=''
   if target:
    c.execute('UPDATE nodes SET name=?,uri=?,config=? WHERE id=?',(name,b.link,json.dumps(stored),target));c.execute('DELETE FROM node_updates WHERE node_id=?',(target,))
   else:c.execute('INSERT OR IGNORE INTO nodes(id,name,protocol,uri,created,config) VALUES (?,?,?,?,?,?)',(ident,name,parsed['kind'],b.link,time.time(),json.dumps(stored)))
   if b.repair_installed:
    auto_repair.enroll(c,target or ident,token,b.repair_token_kept)
    if b.installed_release:c.execute("UPDATE node_repair SET installed_release=?,upgrade_status='ok',upgrade_error='' WHERE node_id=?",(b.installed_release,target or ident))
  c.execute('UPDATE installs SET status=?,result=?,error=?,config=? WHERE id=?',('done' if b.link else 'failed',b.link,b.error,'{}' if b.link else row['config'],ident))
 return {'ok':True}
@app.get('/api/installs')
def listing(r:Request):
 owner(r)
 with conn() as c:rows=c.execute('SELECT id,status,created,result,error FROM installs WHERE created>? ORDER BY created DESC LIMIT 20',(time.time()-86400,)).fetchall()
 return [dict(x) for x in rows]

@app.get('/api/nodes')
def nodes(r:Request):
 owner(r)
 with conn() as c:return [node_management.describe(c,dict(n)) for n in c.execute('SELECT * FROM nodes ORDER BY created DESC')]
class NodeEdit(BaseModel):
 name:str=Field(min_length=1,max_length=60)
 port:int=Field(ge=1024,le=65535)
 client_port:int=Field(ge=1,le=65535)
 address:str=Field(default='',max_length=253)
 sni:str=Field(min_length=1,max_length=253)
 _name=field_validator('name')(Install.clean_name.__func__)
 _hosts=field_validator('address','sni')(Install.host.__func__)

@app.get('/api/nodes/{ident}')
def node_details(ident:str,r:Request):
 owner(r)
 with node_management.LOCK:
  try:return node_management.details(ident)
  except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
@app.put('/api/nodes/{ident}')
def edit_node(ident:str,b:NodeEdit,r:Request):
 owner(r)
 with node_management.LOCK:
  try:return node_management.edit(ident,b)
  except cloudflare.CFError as exc:raise HTTPException(502,str(exc))
@app.post('/api/nodes/{ident}/cancel-update')
def cancel_node_update(ident:str,r:Request):
 owner(r)
 with node_management.LOCK:return node_management.cancel(ident)

@app.delete('/api/nodes/{ident}')
def delete_node(ident:str,r:Request):
 owner(r)
 with node_management.LOCK:return node_management.delete(ident)


@app.post('/api/nodes/{ident}/repair/setup')
def repair_setup(ident:str,r:Request):
 owner(r);return auto_repair.setup(ident)

@app.post('/api/nodes/{ident}/repair/{action}')
def repair_control(ident:str,action:Literal['disable','resume'],r:Request):
 owner(r);return auto_repair.control(ident,action)

@app.get('/repair/{ident}/{token}.sh')
def repair_script(ident:str,token:str):
 return PlainTextResponse(auto_repair.script(ident,token),headers={'Cache-Control':'no-store'})

@app.post('/api/nodes/{ident}/upgrade/{action}')
def upgrade_control(ident:str,action:Literal['enable','disable','retry'],r:Request):
 owner(r);return node_upgrade.control(ident,action)

def repair_bearer(r):
 auth=r.headers.get('authorization','')
 if not auth.startswith('Bearer '):raise HTTPException(401,'缺少节点凭据')
 return auth[7:]

@app.get('/repair/{ident}/bootstrap')
def upgrade_bootstrap(ident:str,r:Request):
 return PlainTextResponse(node_upgrade.bootstrap(ident,repair_bearer(r)),headers={'Cache-Control':'no-store'})

@app.get('/repair/{ident}/upgrade')
def upgrade_bundle(ident:str,r:Request):
 return node_upgrade.bundle(ident,repair_bearer(r))

class UpgradeResult(BaseModel):
 release:str=Field(pattern=r'^[a-f0-9]{64}$')
 ok:bool

@app.post('/repair/{ident}/upgrade-result')
def upgrade_finished(ident:str,b:UpgradeResult,r:Request):
 return node_upgrade.finished(ident,repair_bearer(r),b.release,b.ok)

@app.post('/repair/{ident}/report')
def repair_report(ident:str,b:auto_repair.Report,r:Request):
 auth=r.headers.get('authorization','')
 if not auth.startswith('Bearer '):raise HTTPException(401,'缺少巡检凭据')
 return auto_repair.report(ident,auth[7:],b)

class Subscription(BaseModel):
 name:str=Field(min_length=1,max_length=60)
 node_ids:list[str]=Field(default_factory=list,max_length=1000)
 enabled:bool=True
 @field_validator('name')
 @classmethod
 def valid_name(cls,v):
  if not v.strip() or any(ord(x)<32 for x in v):raise ValueError('订阅名称无效')
  return v.strip()
 @field_validator('node_ids')
 @classmethod
 def valid_ids(cls,v):
  if len(set(v))!=len(v):raise ValueError('节点不能重复选择')
  return v

def describe_sub(c,s):
 result=dict(s);token=result.pop('token');result['enabled']=bool(result['enabled'])
 result['node_ids']=[n[0] for n in c.execute('SELECT node_id FROM subscription_nodes WHERE subscription_id=? ORDER BY position',(s['id'],))]
 result['urls']={fmt:f"{PANEL}/subscribe/{s['id']}/{token}?format={fmt}" for fmt in ('base64','raw','clash','singbox')}
 return result
@app.get('/api/subscriptions')
def subscriptions(r:Request):
 owner(r)
 with conn() as c:return [describe_sub(c,s) for s in c.execute('SELECT * FROM subscriptions ORDER BY rowid DESC')]

def save_subscription(b,ident=None):
 with conn() as c:
  if ident and not c.execute('SELECT 1 FROM subscriptions WHERE id=?',(ident,)).fetchone():raise HTTPException(404,'订阅不存在')
  if any(not c.execute('SELECT 1 FROM nodes WHERE id=?',(nid,)).fetchone() for nid in b.node_ids):raise HTTPException(422,'所选节点不存在')
  if not ident:
   if c.execute('SELECT count(*) FROM subscriptions').fetchone()[0]>=100:raise HTTPException(409,'最多创建 100 个订阅')
   ident=secrets.token_hex(8);c.execute('INSERT INTO subscriptions VALUES (?,?,?,?)',(ident,b.name,secrets.token_urlsafe(32),int(b.enabled)))
  else:c.execute('UPDATE subscriptions SET name=?,enabled=? WHERE id=?',(b.name,int(b.enabled),ident))
  c.execute('DELETE FROM subscription_nodes WHERE subscription_id=?',(ident,))
  c.executemany('INSERT INTO subscription_nodes VALUES (?,?,?)',[(ident,nid,i) for i,nid in enumerate(b.node_ids)])
  return describe_sub(c,c.execute('SELECT * FROM subscriptions WHERE id=?',(ident,)).fetchone())
@app.post('/api/subscriptions')
def add_subscription(b:Subscription,r:Request):owner(r);return save_subscription(b)
@app.put('/api/subscriptions/{ident}')
def edit_subscription(ident:str,b:Subscription,r:Request):owner(r);return save_subscription(b,ident)
@app.delete('/api/subscriptions/{ident}')
def delete_subscription(ident:str,r:Request):
 owner(r)
 with conn() as c:c.execute('DELETE FROM subscriptions WHERE id=?',(ident,))
 return {'ok':True}
@app.api_route('/subscribe/{ident}/{token}',methods=['GET','HEAD'])
def subscribe(ident:str,token:str,r:Request,format:Literal['base64','raw','clash','singbox']|None=None):
 with conn() as c:
  sub=c.execute('SELECT * FROM subscriptions WHERE id=?',(ident,)).fetchone()
  if not sub or not sub['enabled'] or not hmac.compare_digest(token,sub['token']):raise HTTPException(404,'订阅不存在或已停用')
  rows=[dict(n) for n in c.execute('SELECT n.* FROM subscription_nodes m JOIN nodes n ON n.id=m.node_id WHERE m.subscription_id=? ORDER BY m.position',(ident,))]
 if not rows:raise HTTPException(409,'订阅中暂无节点')
 ua=r.headers.get('user-agent','').lower();fmt=format or ('clash' if ua.startswith(('clash','mihomo')) else 'singbox' if ua.startswith(('sing-box','singbox')) else 'base64')
 if fmt in ('raw','base64'):
  text='\n'.join(n['uri'] for n in rows)+'\n'
  if fmt=='base64':text=base64.b64encode(text.encode()).decode()+'\n'
 else:text=formats.configuration(rows,fmt)
 ext='yaml' if fmt=='clash' else 'json' if fmt=='singbox' else 'txt'
 return Response(text,media_type='text/plain' if fmt in ('raw','base64') else 'application/yaml' if fmt=='clash' else 'application/json',headers={'Cache-Control':'no-store','Vary':'User-Agent','X-Subscription-Node-Count':str(len(rows)),'profile-title':'base64:'+base64.b64encode(sub['name'].encode()).decode(),'profile-update-interval':'24','Content-Disposition':f'inline; filename="subscription-{ident}.{ext}"'})

@app.get('/assets/{name}')
def asset(name:str):
 manifest=json.loads((ASSETS/'manifest.json').read_text())
 if name not in manifest:raise HTTPException(404)
 return FileResponse(ASSETS/name,media_type='application/octet-stream',headers={'Cache-Control':'public,max-age=86400'})
@app.get('/upgrade.sh')
def upgrade_entry():return FileResponse(ROOT/'upgrade-bootstrap.sh',media_type='text/plain',headers={'Cache-Control':'no-store'})

@app.get('/health')
def health():return {'ok':True}
@app.get('/')
def home():return FileResponse(ROOT/'static/index.html',headers={'Cache-Control':'no-store'})
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
