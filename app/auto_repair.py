"""Bounded health checks and node-initiated, rate-limited repair permits."""
import base64,hashlib,hmac,json,secrets,threading,time
from fastapi import HTTPException
from pydantic import BaseModel,Field
from . import node_upgrade
LOCK=threading.RLock()
STOP=threading.Event()
WORKER=None

def init(c):
 c.execute('''CREATE TABLE IF NOT EXISTS node_repair(
 node_id TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,paused INTEGER NOT NULL DEFAULT 0,
 last_report REAL NOT NULL DEFAULT 0,local_failures INTEGER NOT NULL DEFAULT 0,
 public_failures INTEGER NOT NULL DEFAULT 0,probe_failures INTEGER NOT NULL DEFAULT 0,
 probe_at REAL NOT NULL DEFAULT 0,probe_status TEXT NOT NULL DEFAULT 'unknown',
 latency_ms INTEGER,probe_error TEXT NOT NULL DEFAULT '',last_repair REAL NOT NULL DEFAULT 0,
 repair_count INTEGER NOT NULL DEFAULT 0,history TEXT NOT NULL DEFAULT '[]',
 last_action TEXT NOT NULL DEFAULT '',last_result TEXT NOT NULL DEFAULT '')''')
 if 'script_version' not in {r[1] for r in c.execute('PRAGMA table_info(node_repair)')}:c.execute('ALTER TABLE node_repair ADD COLUMN script_version INTEGER NOT NULL DEFAULT 1')
 node_upgrade.init(c)

def status(c,ident):
 r=c.execute('SELECT * FROM node_repair WHERE node_id=?',(ident,)).fetchone()
 if not r:return {'configured':False}
 n=dict(r)
 return {'configured':True,**{k:n[k] for k in ('enabled','paused','last_report','probe_at','probe_status','latency_ms','probe_error','last_repair','repair_count','last_action','last_result','local_failures','public_failures','script_version','auto_upgrade','installed_release','upgrade_target','upgrade_status','upgrade_error','upgrade_attempts','upgrade_at')}}|{'target_release':node_upgrade.release()}

def setup(ident):
 from . import main,node_management
 with LOCK,node_management.LOCK,main.conn() as c:
  node_management.get(c,ident)
  if node_management.pending(c,ident):raise HTTPException(409,'请先完成或取消节点更新')
  token=secrets.token_urlsafe(32)
  c.execute('INSERT INTO node_repair(node_id,token_hash) VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET token_hash=excluded.token_hash,enabled=1,last_report=0,probe_status=\'unknown\',probe_at=0,probe_failures=0', (ident,hashlib.sha256(token.encode()).hexdigest()))
 return {'command':main.download_command(f'{main.PANEL}/repair/{ident}/{token}.sh')}

def authorize(c,ident,token):
 row=c.execute('SELECT * FROM node_repair WHERE node_id=?',(ident,)).fetchone()
 if not row or not hmac.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest()):raise HTTPException(404,'自动修复凭据已失效')
 return dict(row)

def installer():
 from . import main
 return (main.ROOT/'repair-install.sh').read_text().replace('__WATCHDOG_B64__',base64.b64encode((main.ROOT/'node-watchdog.sh').read_bytes()).decode()).replace('__UPDATER_B64__',base64.b64encode((main.ROOT/'node-update.sh').read_bytes()).decode())

def install_token(ident,callback_token):
 from . import main
 return hmac.new(main.KEY.read_bytes(),('repair-install:'+ident+':'+callback_token).encode(),hashlib.sha256).hexdigest()

def enroll(c,ident,callback_token,kept=False):
 if kept:
  if not c.execute('SELECT 1 FROM node_repair WHERE node_id=?',(ident,)).fetchone():raise HTTPException(409,'原巡检凭据已撤销，请重新下载安装命令')
  return
 token=install_token(ident,callback_token)
 # Updates preserve the user's disabled/paused state and repair history.
 c.execute("INSERT INTO node_repair(node_id,token_hash) VALUES (?,?) ON CONFLICT(node_id) DO UPDATE SET token_hash=excluded.token_hash,last_report=0",(ident,hashlib.sha256(token.encode()).hexdigest()))

def script(ident,token):
 import shlex
 from . import main
 with main.conn() as c:authorize(c,ident,token)
 values={'PANEL':main.PANEL,'NODE_ID':ident,'REPORT_TOKEN':token}
 return '#!/bin/sh\nset -eu\numask 077\n'+'\n'.join(k+'='+shlex.quote(v) for k,v in values.items())+'\n'+installer()

class Report(BaseModel):
 local_ok:bool
 public_ok:bool=True
 network_ok:bool=True
 result:str=Field(default='',max_length=80)
 script_version:int=Field(default=1,ge=1,le=1000)
 release:str=Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')

def report(ident,token,b):
 from . import main,node_management
 now=time.time()
 with LOCK,node_management.LOCK,main.conn() as c:
  n=authorize(c,ident,token)
  if now-n['last_report']<30:return {'action':'none'}
  local=0 if b.local_ok else n['local_failures']+1
  public=0 if b.public_ok else n['public_failures']+1
  c.execute('UPDATE node_repair SET last_report=?,local_failures=?,public_failures=?,last_result=?,script_version=? WHERE node_id=?',(now,local,public,b.result,b.script_version,ident))
  if b.release:c.execute('UPDATE node_repair SET installed_release=? WHERE node_id=?',(b.release,ident))
  target=node_upgrade.allowed(c,ident,n,b.script_version,b.release,b.local_ok and b.public_ok and b.network_ok)
  if target:return {'action':'none','upgrade':{'release':target}}
  # Configuration changes and disabled/paused nodes never receive restart permits.
  if not n['enabled'] or n['paused'] or node_management.pending(c,ident) or not b.network_ok:return {'action':'none'}
  protocol_failed=n['probe_status']=='failed' and n['probe_failures']>=2 and n['probe_at']>n['last_repair'] and now-n['probe_at']<660
  action='core' if local>=3 else 'tunnel' if public>=3 and node_management.get(c,ident)['protocol']=='argo' else 'protocol' if protocol_failed else 'none'
  if action=='none' or now-n['last_repair']<300:return {'action':'none'}
  history=[t for t in json.loads(n['history']) if now-t<3600]
  if len(history)>=3:
   c.execute('UPDATE node_repair SET paused=1 WHERE node_id=?',(ident,));return {'action':'none'}
  history.append(now)
  # Consume the permit before replying, even if the reply or restart is lost.
  c.execute('UPDATE node_repair SET last_repair=?,repair_count=repair_count+1,history=?,paused=?,last_action=?,local_failures=0,public_failures=0,probe_failures=0 WHERE node_id=?',(now,json.dumps(history),int(len(history)>=3),action,ident))
  return {'action':action}

def control(ident,action):
 from . import main,node_management
 with LOCK,node_management.LOCK,main.conn() as c:
  node_management.get(c,ident)
  if not c.execute('SELECT 1 FROM node_repair WHERE node_id=?',(ident,)).fetchone():raise HTTPException(409,'尚未安装自动修复')
  if action=='disable':c.execute('UPDATE node_repair SET enabled=0 WHERE node_id=?',(ident,))
  else:c.execute("UPDATE node_repair SET enabled=1,paused=0,history='[]',repair_count=0 WHERE node_id=?",(ident,))
 return {'ok':True}

def check(ident):
 from . import main
 from .node_probe import probe
 with main.conn() as c:
  n=c.execute('SELECT n.uri FROM nodes n JOIN node_repair r ON r.node_id=n.id WHERE n.id=? AND r.enabled=1 AND r.last_report>0',(ident,)).fetchone()
 if not n:return
 uri=n['uri']
 try:latency=probe(uri);state='ok';error=''
 except (OSError,ValueError,TimeoutError):latency=None;state='failed';error='两处 HTTPS 实际转发均未通过，请检查地址、映射及 TCP/UDP 放行'
 except Exception:latency=None;state='unavailable';error='面板探测组件不可用，未作为修复依据'
 with LOCK,main.conn() as c:
  # A result for an old link must not trigger repair of an edited node.
  current=c.execute('SELECT uri FROM nodes WHERE id=?',(ident,)).fetchone()
  if not current or current['uri']!=uri:return
  c.execute('UPDATE node_repair SET probe_at=?,probe_status=?,probe_error=?,latency_ms=?,probe_failures=CASE WHEN ?=\'failed\' THEN probe_failures+1 ELSE 0 END WHERE node_id=?',(time.time(),state,error,latency,state,ident))

def worker():
 from . import main
 while not STOP.wait(5):
  try:
   with main.conn() as c:ids=[r[0] for r in c.execute('SELECT node_id FROM node_repair WHERE enabled=1 AND last_report>? AND probe_at<? ORDER BY probe_at LIMIT 2',(time.time()-180,time.time()-300))]
   for ident in ids:
    if STOP.is_set():break
    check(ident)
  except Exception:
   # Do not log node links or credentials on transient failures.
   import logging;logging.getLogger(__name__).warning('Auto repair health cycle failed')

def start():
 global WORKER
 STOP.clear();WORKER=threading.Thread(target=worker,name='node-health',daemon=True);WORKER.start()

def stop():STOP.set()
