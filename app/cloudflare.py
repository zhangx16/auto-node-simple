"""Small, on-demand fixed tunnel provisioning with private server defaults."""
import json,os,re,sqlite3,threading,urllib.request,urllib.error
from pathlib import Path
from urllib.parse import urlencode
DATA=Path(os.getenv('DATA_DIR','/app/data'));LOCK=threading.Lock()
class CFError(Exception):pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None

def db():
 c=sqlite3.connect(DATA/'simple.db',timeout=10);c.row_factory=sqlite3.Row;return c

def init(c):
 c.execute("""CREATE TABLE IF NOT EXISTS fixed_tunnels(install_id TEXT PRIMARY KEY,hostname TEXT NOT NULL UNIQUE,tunnel_id TEXT NOT NULL DEFAULT '',dns_id TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'reserved')""")

def settings():
 try:
  cfg=json.loads((DATA/'cloudflare.json').read_text())
  if not all(cfg.get(k) for k in ('account_id','zone_id','zone_name','api_token')):raise ValueError()
  return cfg
 except (OSError,ValueError,TypeError):raise CFError('未配置默认 Cloudflare 账户，请使用手动 Token 模式')

def status():
 try:
  cfg=settings();return {'configured':True,'zone_name':cfg['zone_name']}
 except CFError:return {'configured':False,'zone_name':''}

def api(method,path,body=None,missing_ok=False):
 cfg=settings();req=urllib.request.Request('https://api.cloudflare.com/client/v4'+path,method=method,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer '+cfg['api_token'],'Content-Type':'application/json'})
 try:
  with urllib.request.build_opener(NoRedirect()).open(req,timeout=15) as r:
   raw=r.read(1024*1024+1)
   if len(raw)>1024*1024:raise CFError('Cloudflare 响应过大')
   data=json.loads(raw)
 except urllib.error.HTTPError as e:
  if missing_ok and e.code==404:return None
  raise CFError(f'Cloudflare 请求失败（HTTP {e.code}），请检查默认 Token 权限')
 except (OSError,ValueError):raise CFError('Cloudflare API 暂不可用，请稍后重试')
 if not data.get('success'):raise CFError('Cloudflare 未确认操作成功')
 return data.get('result')

def verify():
 cfg=settings();zone=api('GET','/zones/'+cfg['zone_id'])
 if zone.get('status')!='active' or zone.get('name')!=cfg['zone_name'] or zone.get('account',{}).get('id')!=cfg['account_id']:raise CFError('默认账户与 Zone 不匹配')
 api('GET',f"/accounts/{cfg['account_id']}/cfd_tunnel?per_page=1&is_deleted=false")
 return status()

def binding(ident):
 with db() as c:
  row=c.execute('SELECT * FROM fixed_tunnels WHERE install_id=?',(ident,)).fetchone()
 return dict(row) if row else None

def patch(ident,**values):
 with db() as c:c.execute('UPDATE fixed_tunnels SET '+','.join(k+'=?' for k in values)+' WHERE install_id=?',(*values.values(),ident))

def hostname(value,ident):
 cfg=settings();value=(value or f"node-{ident}.{cfg['zone_name']}").strip().lower().rstrip('.')
 if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.'+re.escape(cfg['zone_name']),value):raise CFError('自动配置域名必须是 '+cfg['zone_name']+' 下的一级子域名')
 return value

def cleanup(ident):
 """Only remove IDs created by this project, after checking DNS ownership."""
 b=binding(ident)
 if not b:return
 cfg=settings()
 if b['dns_id']:
  path=f"/zones/{cfg['zone_id']}/dns_records/{b['dns_id']}";record=api('GET',path,missing_ok=True)
  if record:
   if record.get('name')!=b['hostname'] or record.get('type')!='CNAME' or record.get('content')!=b['tunnel_id']+'.cfargotunnel.com':raise CFError('DNS 已变化，未删除其他配置')
   api('DELETE',path,missing_ok=True)
  patch(ident,dns_id='')
 if b['tunnel_id']:
  api('DELETE',f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}",missing_ok=True)
 with db() as c:c.execute('DELETE FROM fixed_tunnels WHERE install_id=?',(ident,))

def provision(ident,value,port):
 with LOCK:
  cfg=settings();host=hostname(value,ident)
  records=api('GET',f"/zones/{cfg['zone_id']}/dns_records?"+urlencode({'name':host,'per_page':100}))
  if records:raise CFError('该域名已有 DNS 记录，请换一个未使用的子域名')
  with db() as c:
   try:c.execute('INSERT INTO fixed_tunnels(install_id,hostname) VALUES (?,?)',(ident,host))
   except sqlite3.IntegrityError:raise CFError('域名已被另一条安装任务占用')
  try:
   created=api('POST',f"/accounts/{cfg['account_id']}/cfd_tunnel",{'name':'auto-node-simple-'+ident,'config_src':'cloudflare'})
   tid=created['id'];patch(ident,tunnel_id=tid)
   token=api('GET',f"/accounts/{cfg['account_id']}/cfd_tunnel/{tid}/token")
   if not isinstance(token,str) or len(token)<20:raise CFError('Cloudflare 未返回有效的隧道 Token')
   api('PUT',f"/accounts/{cfg['account_id']}/cfd_tunnel/{tid}/configurations",{'config':{'ingress':[{'hostname':host,'service':f'http://127.0.0.1:{port}'},{'service':'http_status:404'}]}})
   records=api('GET',f"/zones/{cfg['zone_id']}/dns_records?"+urlencode({'name':host,'per_page':100}))
   if records:raise CFError('域名在配置过程中被占用，未覆盖现有 DNS')
   dns=api('POST',f"/zones/{cfg['zone_id']}/dns_records",{'type':'CNAME','name':host,'content':tid+'.cfargotunnel.com','proxied':True,'ttl':1,'comment':'Created by auto-node-simple; install '+ident})
   patch(ident,dns_id=dns['id'],status='ready')
   return {'hostname':host,'tunnel_token':token}
  except Exception as exc:
   try:cleanup(ident)
   except Exception:patch(ident,status='cleanup_required')
   if isinstance(exc,CFError):raise
   raise CFError('固定隧道配置未完成，请重试；已保留资源处理记录')

def _route_config(ident):
 b=binding(ident)
 if not b:raise CFError('未找到本项目固定隧道记录')
 cfg=settings();path=f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/configurations"
 data=api('GET',path);config=data['config'];matches=[n for n in config['ingress'] if n.get('hostname')==b['hostname']]
 if len(matches)!=1:raise CFError('隧道路由已变化，请先核对 Cloudflare 配置')
 return b,path,config,matches[0]

def read_port(ident):
 from urllib.parse import urlsplit
 _,_,_,entry=_route_config(ident);url=urlsplit(entry['service'])
 if url.scheme!='http' or url.hostname!='127.0.0.1' or not url.port:raise CFError('隧道未指向本地监听，不能自动识别端口')
 return url.port

def connector_token(ident):
 b=binding(ident)
 if not b:raise CFError('固定隧道不由本项目管理')
 cfg=settings();token=api('GET',f"/accounts/{cfg['account_id']}/cfd_tunnel/{b['tunnel_id']}/token")
 if not isinstance(token,str) or len(token)<20:raise CFError('未取得有效隧道运行凭据')
 return token

def update_port(ident,port):
 with LOCK:
  _,path,config,entry=_route_config(ident);entry['service']=f'http://127.0.0.1:{port}'
  api('PUT',path,{'config':config})
