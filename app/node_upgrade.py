"""Node-authorized upgrades of the complete installer; no arbitrary remote commands."""
import hashlib,json,secrets,time
from fastapi import HTTPException
SCRIPT_VERSION=3
RETRY_SECONDS=3600

def init(c):
 columns={r[1] for r in c.execute('PRAGMA table_info(node_repair)')}
 for name,kind in [('auto_upgrade','INTEGER NOT NULL DEFAULT 1'),('installed_release',"TEXT NOT NULL DEFAULT ''"),('upgrade_target',"TEXT NOT NULL DEFAULT ''"),('upgrade_status',"TEXT NOT NULL DEFAULT 'bootstrap'"),('upgrade_error',"TEXT NOT NULL DEFAULT ''"),('upgrade_attempts','INTEGER NOT NULL DEFAULT 0'),('upgrade_at','REAL NOT NULL DEFAULT 0')]:
  if name not in columns:c.execute('ALTER TABLE node_repair ADD COLUMN '+name+' '+kind)

def release():
 from . import main
 if not (main.ASSETS/'manifest.json').is_file():return ''
 h=hashlib.sha256()
 for name in ('install.sh','repair-install.sh','node-watchdog.sh','node-update.sh'):
  h.update(name.encode()+b'\0'+(main.ROOT/name).read_bytes()+b'\0')
 h.update((main.ASSETS/'manifest.json').read_bytes())
 return h.hexdigest()

def allowed(c,ident,n,version,installed,healthy=True):
 from . import node_management
 if version<SCRIPT_VERSION or not n['auto_upgrade'] or n['paused'] or not healthy:return ''
 target=release()
 if installed==target:return ''
 if n['upgrade_target']==target and (n['upgrade_attempts']>=3 or time.time()-n['upgrade_at']<RETRY_SECONDS):return ''
 pending=c.execute('SELECT u.job_id,j.config,j.created FROM node_updates u LEFT JOIN installs j ON j.id=u.job_id WHERE u.node_id=?',(ident,)).fetchone()
 if pending:
  cfg=json.loads(pending['config'] or '{}')
  if not cfg.get('_upgrade_release') or time.time()-(pending['created'] or 0)<RETRY_SECONDS:return ''
  c.execute("UPDATE installs SET status='cancelled',config='{}',error='升级命令超时，等待重试' WHERE id=?",(pending['job_id'],))
  c.execute('DELETE FROM node_updates WHERE node_id=?',(ident,))
 return target

def bundle(ident,token):
 from . import main,node_management,auto_repair
 with node_management.LOCK,main.conn() as c:
  n=auto_repair.authorize(c,ident,token)
  target=allowed(c,ident,n,SCRIPT_VERSION,n['installed_release'])
  if not target:raise HTTPException(409,'暂无可执行升级，或升级已停用、冷却、暂停、配置任务未完成')
  node=node_management.get(c,ident);cfg=node_management.config(node)
  if not cfg.get('port'):raise HTTPException(409,'缺少监听端口资料，请先手动升级一次完整安装脚本')
  cfg.update(id=ident,_update_node=ident,_upgrade_release=target,_upgrade_uri=node['uri'],tunnel_token='')
  # Existing Argo token files are reused locally; no Cloudflare resource changes.
  job=secrets.token_hex(8);callback=secrets.token_urlsafe(32)
  c.execute('INSERT INTO installs(id,token_hash,config,status,created,target_node) VALUES (?,?,?,?,?,?)',(job,hashlib.sha256(callback.encode()).hexdigest(),json.dumps(cfg),'ready',time.time(),ident))
  c.execute('INSERT INTO node_updates VALUES (?,?,?,?)',(ident,job,node['uri'],json.dumps(cfg)))
  attempts=n['upgrade_attempts']+1 if n['upgrade_target']==target else 1
  c.execute("UPDATE node_repair SET upgrade_target=?,upgrade_attempts=?,upgrade_at=?,upgrade_status='running',upgrade_error='' WHERE node_id=?",(target,attempts,time.time(),ident))
 # Existing installer token authentication and callback validation remain in force.
 script=main.script(job,callback).body
 return {'release':target,'sha256':hashlib.sha256(script).hexdigest(),'script':__import__('base64').b64encode(script).decode()}

def finished(ident,token,target,ok):
 from . import main,node_management,auto_repair
 with node_management.LOCK,main.conn() as c:
  n=auto_repair.authorize(c,ident,token)
  if n['upgrade_target']!=target:raise HTTPException(409,'升级版本已变化')
  if ok:
   if n['installed_release']!=target:raise HTTPException(409,'尚未收到完整安装成功回传')
   c.execute("UPDATE node_repair SET upgrade_status='ok',upgrade_error='' WHERE node_id=?",(ident,))
  else:
   row=c.execute('SELECT u.job_id,j.config FROM node_updates u JOIN installs j ON j.id=u.job_id WHERE u.node_id=?',(ident,)).fetchone()
   if row and json.loads(row['config']).get('_upgrade_release')==target:
    c.execute("UPDATE installs SET status='cancelled',config='{}',error='自动升级失败，远端已尝试恢复原版本' WHERE id=?",(row['job_id'],));c.execute('DELETE FROM node_updates WHERE node_id=?',(ident,))
   c.execute("UPDATE node_repair SET upgrade_status='failed',upgrade_error='升级未完成，保留或恢复原版本；一小时后重试，同版最多三次' WHERE node_id=?",(ident,))
 return {'ok':True}

def control(ident,action):
 from . import main,node_management
 with node_management.LOCK,main.conn() as c:
  node_management.get(c,ident)
  if not c.execute('SELECT 1 FROM node_repair WHERE node_id=?',(ident,)).fetchone():raise HTTPException(409,'尚未安装巡检')
  if action=='disable':c.execute('UPDATE node_repair SET auto_upgrade=0 WHERE node_id=?',(ident,))
  elif action=='enable':c.execute('UPDATE node_repair SET auto_upgrade=1 WHERE node_id=?',(ident,))
  else:c.execute("UPDATE node_repair SET upgrade_attempts=0,upgrade_at=0,upgrade_error='' WHERE node_id=?",(ident,))
 return {'ok':True}


def bootstrap(ident,token):
 import shlex
 from . import main,auto_repair
 with main.conn() as c:
  n=auto_repair.authorize(c,ident,token)
  if not n['auto_upgrade']:raise HTTPException(409,'自动升级已停用')
 values={'PANEL':main.PANEL,'NODE_ID':ident,'REPORT_TOKEN':'','REPAIR_KEEP_TOKEN':'1'}
 return '#!/bin/sh\nset -eu\numask 077\n'+'\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n'+auto_repair.installer()
