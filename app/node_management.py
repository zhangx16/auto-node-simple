"""Link editing and explicit command-based updates; no node agent or SSH."""
import hashlib,json,secrets,time,threading
LOCK=threading.RLock()
from urllib.parse import urlsplit,urlunsplit,quote,unquote
from fastapi import HTTPException
from . import formats,cloudflare

def init(c):
 if 'config' not in {r[1] for r in c.execute('PRAGMA table_info(nodes)')}:c.execute("ALTER TABLE nodes ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
 c.execute("CREATE TABLE IF NOT EXISTS node_updates(node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,job_id TEXT NOT NULL,base_uri TEXT NOT NULL,config TEXT NOT NULL)")

def get(c,ident):
 r=c.execute('SELECT * FROM nodes WHERE id=?',(ident,)).fetchone()
 if not r:raise HTTPException(404,'节点不存在')
 return dict(r)

def config(n):
 cfg=json.loads(n['config'])
 if cfg:
  if n['protocol']=='argo':cfg['sni']=formats.parse(n['uri'])['sni']
  return cfg
 p=formats.parse(n['uri']);u=urlsplit(n['uri']);suffix={'argo':'CF','reality':'Vless','hysteria2':'hy2'}[p['kind']]
 name=n['name'][:-len(suffix)-1] if n['name'].endswith('-'+suffix) or (p['kind']=='argo' and n['name'].endswith('-cf')) else n['name']
 return {'id':n['id'],'name':name,'protocol':p['kind'],'port':None,'address':p['server'],'client_port':p['port'],'sni':p['sni'],'credential':p['credential'],'path':p.get('path','/'),'tunnel':'fixed','cf_mode':'default' if cloudflare.binding(n['id']) else 'manual','tunnel_hostname':p['sni'] if p['kind']=='argo' else '','tunnel_token':''}

def pending(c,ident):
 row=c.execute('SELECT u.*,j.status,j.created,j.error FROM node_updates u LEFT JOIN installs j ON j.id=u.job_id WHERE u.node_id=?',(ident,)).fetchone()
 if not row:return None
 r=dict(row)
 return {'job_id':r['job_id'],'status':r['status'] or 'expired','expired':not r['created'] or time.time()-r['created']>86400,'error':r['error'] or '', 'port':json.loads(r['config'])['port']}

def describe(c,n):
 cfg=config(n)
 return {k:n[k] for k in ['id','name','protocol','uri','created']}|{'edit_name':cfg['name'],'port':cfg['port'],'address':cfg['address'],'client_port':cfg['client_port'],'sni':cfg['sni'],'pending_update':pending(c,n['id'])}

def display_name(cfg):
 suffix='-'+{'argo':'CF','reality':'Vless','hysteria2':'hy2'}[cfg['protocol']]
 name=cfg['name']
 if cfg['protocol']=='argo' and name.endswith('-cf'):name=name[:-3]+'-CF'
 return name if name.endswith(suffix) else name+suffix

def link(uri,cfg):
 u=urlsplit(uri);address=cfg['address'];address='['+address+']' if ':' in address else address
 return urlunsplit((u.scheme,u.netloc.rsplit('@',1)[0]+'@'+address+':'+str(cfg['client_port']),u.path,u.query,quote(display_name(cfg),safe='')))

def edit(ident,b):
 from . import main
 with main.conn() as c:
  n=get(c,ident);old=config(n)
  if pending(c,ident):raise HTTPException(409,'已有更新命令，请先执行完成或取消该更新')
 if old['port'] is None:
  if n['protocol']!='argo' or not cloudflare.binding(ident):raise HTTPException(409,'缺少原监听端口资料，暂不能编辑此旧节点')
  old['port']=cloudflare.read_port(ident)
 cfg=old|b.model_dump();cfg['id']=ident;cfg['protocol']=n['protocol']
 if cfg['protocol']=='argo':
  cfg['address']=cfg['address'].strip() or main.DEFAULT_ARGO_ADDRESS
  if cfg['client_port'] not in (443,2053,2083,2087,2096,8443):raise HTTPException(422,'Argo 需使用 Cloudflare HTTPS 端口')
  cfg['sni']=old['sni']
 elif not cfg['address']:raise HTTPException(422,'直连节点必须填写公网地址')
 remote=cfg['port']!=old['port'] or cfg['sni']!=old['sni']
 if remote:
  if cfg['protocol']=='argo':
   if cloudflare.binding(ident):cfg['tunnel_token']=cloudflare.connector_token(ident)
   elif not cfg.get('tunnel_token'):raise HTTPException(409,'手动隧道缺少原 Token，无法生成端口更新命令')
  job=secrets.token_hex(8);token=secrets.token_urlsafe(32);cfg['_update_node']=ident
  with main.conn() as c:
   current=get(c,ident)
   if current['uri']!=n['uri'] or pending(c,ident):raise HTTPException(409,'节点已变化，请重新打开编辑')
   c.execute('INSERT INTO installs(id,token_hash,config,status,created,target_node) VALUES (?,?,?,?,?,?)',(job,hashlib.sha256(token.encode()).hexdigest(),json.dumps(cfg),'ready',time.time(),ident))
   c.execute('INSERT INTO node_updates VALUES (?,?,?,?)',(ident,job,n['uri'],json.dumps(cfg)))
  return {'pending':True,'id':job,'command':main.download_command(f'{main.PANEL}/install/{job}/{token}.sh'),'manual_route':cfg['protocol']=='argo' and not cloudflare.binding(ident)}
 with main.conn() as c:
  if get(c,ident)['uri']!=n['uri'] or pending(c,ident):raise HTTPException(409,'节点已变化，请重新打开编辑')
  uri=link(n['uri'],cfg);c.execute('UPDATE nodes SET name=?,uri=?,config=? WHERE id=?',(display_name(cfg),uri,json.dumps(cfg),ident))
  return {'pending':False,'node':describe(c,get(c,ident))}

def cancel(ident):
 from . import main
 with main.conn() as c:
  get(c,ident);row=c.execute('SELECT job_id FROM node_updates WHERE node_id=?',(ident,)).fetchone()
  if row:
   c.execute("UPDATE installs SET status='cancelled',config='{}',error='已取消更新；如远端命令已执行，请核对实际端口' WHERE id=?",(row['job_id'],));c.execute('DELETE FROM node_updates WHERE node_id=?',(ident,))
 return {'ok':True}

def delete(ident):
 from . import main
 with main.conn() as c:
  get(c,ident)
  # Remove source jobs too: startup migrates completed installs into nodes.
  # Deleting jobs also revokes their script and callback tokens.
  c.execute('DELETE FROM installs WHERE id=? OR target_node=?',(ident,ident))
  c.execute('DELETE FROM nodes WHERE id=?',(ident,))
 return {'ok':True}

def details(ident):
 from . import main
 with main.conn() as c:n=get(c,ident)
 cfg=config(n)
 if cfg['port'] is None:
  if n['protocol']!='argo' or not cloudflare.binding(ident):raise HTTPException(409,'缺少原监听端口资料，暂不能编辑此旧节点')
  cfg['port']=cloudflare.read_port(ident)
  with main.conn() as c:
   if get(c,ident)['uri']!=n['uri']:raise HTTPException(409,'节点已变化，请重试')
   c.execute('UPDATE nodes SET config=? WHERE id=?',(json.dumps(cfg),ident));n=get(c,ident)
 with main.conn() as c:return describe(c,n)


def argo_uri(uri):
 u=urlsplit(uri);name=unquote(u.fragment)
 if name.endswith('-cf'):return uri.rsplit('#',1)[0]+'#'+quote(name[:-3]+'-CF',safe='')
 return uri

def migrate_argo_suffix(c):
 for n in c.execute("SELECT id,name,uri,config FROM nodes WHERE protocol='argo'").fetchall():
  name=n['name'][:-3]+'-CF' if n['name'].endswith('-cf') else n['name']
  uri=argo_uri(n['uri']);cfg=json.loads(n['config'])
  if cfg.get('name','').endswith('-cf'):cfg['name']=cfg['name'][:-3]+'-CF'
  c.execute('UPDATE nodes SET name=?,uri=?,config=? WHERE id=?',(name,uri,json.dumps(cfg),n['id']))
  c.execute('UPDATE node_updates SET base_uri=? WHERE node_id=? AND base_uri=?',(uri,n['id'],n['uri']))
 for r in c.execute("SELECT id,result FROM installs WHERE result!=''").fetchall():
  try:
   if formats.parse(r['result'])['kind']=='argo':c.execute('UPDATE installs SET result=? WHERE id=?',(argo_uri(r['result']),r['id']))
  except (ValueError,TypeError):pass
