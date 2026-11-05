"""Hourly discovery of new VPSNAT IDs and automatic enrollment in a chosen subscription."""
import json
import secrets
import sqlite3
import threading
import time
from fastapi import HTTPException
from . import platform_automation,vpsnat

INTERVAL=3600
MAX_ATTEMPTS=3
CHECK_LOCK=threading.Lock()
STOP=threading.Event()
WORKER=None


def init(c):
    c.executescript('''CREATE TABLE IF NOT EXISTS platform_watch(
        id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 0,
        base_port INTEGER NOT NULL DEFAULT 38001,subscription_id TEXT NOT NULL DEFAULT '',
        last_check REAL NOT NULL DEFAULT 0,next_check REAL NOT NULL DEFAULT 0,
        last_message TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '',
        last_batch_id TEXT NOT NULL DEFAULT '',last_new_count INTEGER NOT NULL DEFAULT 0,
        last_seen_count INTEGER NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
        scan_id TEXT NOT NULL DEFAULT '');
    INSERT OR IGNORE INTO platform_watch(id) VALUES (1);
    CREATE TABLE IF NOT EXISTS platform_watch_seen(
        vm_id TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',first_seen REAL NOT NULL,last_seen REAL NOT NULL,
        state TEXT NOT NULL DEFAULT 'baseline',attempts INTEGER NOT NULL DEFAULT 0,
        batch_id TEXT NOT NULL DEFAULT '',node_id TEXT NOT NULL DEFAULT '',last_error TEXT NOT NULL DEFAULT '');''')


def settings(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_watch'").fetchone():return None
    row=c.execute('SELECT * FROM platform_watch WHERE id=1').fetchone()
    return dict(row) if row else None


def target_subscription(c,cfg):
    row=c.execute('SELECT id,name,enabled FROM subscriptions WHERE id=?',(cfg['subscription_id'],)).fetchone()
    if not row or not row['enabled']:raise HTTPException(409,'目标订阅不存在或已停用，请恢复订阅后继续巡检')
    return dict(row)


def managed_nodes(c):
    nodes=[]
    for row in c.execute("SELECT id,config FROM nodes WHERE protocol='argo' ORDER BY created,id"):
        try:cfg=json.loads(row['config'])
        except (ValueError,TypeError):continue
        if cfg.get('platform_vm_id'):nodes.append((str(cfg['platform_vm_id']),row['id']))
    return nodes


def append_node(c,subscription,node):
    if c.execute('SELECT 1 FROM subscription_nodes WHERE subscription_id=? AND node_id=?',(subscription,node)).fetchone():return False
    position=c.execute('SELECT COALESCE(max(position),-1)+1 FROM subscription_nodes WHERE subscription_id=?',(subscription,)).fetchone()[0]
    c.execute('INSERT OR IGNORE INTO subscription_nodes(subscription_id,node_id,position) VALUES (?,?,?)',(subscription,node,position))
    return True


def sync_nodes(c,cfg):
    target_subscription(c,cfg);added=0
    for vm,node in managed_nodes(c):
        seen=c.execute('SELECT state FROM platform_watch_seen WHERE vm_id=?',(vm,)).fetchone()
        if not seen:continue
        added+=int(append_node(c,cfg['subscription_id'],node))
        c.execute("UPDATE platform_watch_seen SET state='managed',node_id=?,last_error='' WHERE vm_id=?",(node,vm))
    return added


def enroll_node(ident):
    """Best-effort post-callback enrollment, without making node callbacks depend on scheduling."""
    from . import main
    try:
        with main.conn() as c:
            cfg=settings(c)
            if not cfg or not cfg['subscription_id']:return False
            row=c.execute('SELECT protocol,config FROM nodes WHERE id=?',(ident,)).fetchone()
            if not row or row['protocol']!='argo':return False
            vm=str(json.loads(row['config']).get('platform_vm_id') or '')
            if not vm:return False
            seen=c.execute('SELECT state FROM platform_watch_seen WHERE vm_id=?',(vm,)).fetchone()
            if not seen or (not cfg['enabled'] and seen['state']!='installing'):return False
            target_subscription(c,cfg)
            append_node(c,cfg['subscription_id'],ident)
            c.execute("UPDATE platform_watch_seen SET state='managed',node_id=?,last_error='' WHERE vm_id=?",(ident,vm))
            return True
    except (sqlite3.Error,ValueError,TypeError,HTTPException):
        # Hourly reconciliation repairs a missing membership after a transient failure.
        return False


def reconcile_batch(batch):
    from . import main
    with main.conn() as c:
        cfg=settings(c)
        if not cfg or not cfg['subscription_id']:return
        if not cfg['enabled'] and not c.execute("SELECT 1 FROM platform_watch_seen WHERE state='installing' AND batch_id=?",(batch,)).fetchone():return
        try:sync_nodes(c,cfg)
        except HTTPException:return
        rows=c.execute("SELECT s.vm_id,s.attempts,a.state,a.message FROM platform_watch_seen s JOIN automation_items a ON a.batch_id=s.batch_id AND a.vm_id=s.vm_id WHERE s.batch_id=? AND s.state='installing'",(batch,)).fetchall()
        for row in rows:
            if row['state'] in ('failed','unknown'):
                state='blocked' if row['state']=='unknown' or row['attempts']>=MAX_ATTEMPTS else 'retry'
                c.execute('UPDATE platform_watch_seen SET state=?,last_error=? WHERE vm_id=?',(state,row['message'],row['vm_id']))


def configure(enabled,base_port=None,subscription_id=None):
    from . import main
    with main.conn() as c:
        old=settings(c)
        initial=enabled and not old['last_check'] and not c.execute('SELECT 1 FROM platform_watch_seen').fetchone()
    machines=vpsnat.vms() if initial else []
    now=time.time()
    with main.conn() as c:
        c.execute('BEGIN IMMEDIATE');cfg=settings(c)
        base=base_port if base_port is not None else cfg['base_port']
        target=subscription_id if subscription_id is not None else cfg['subscription_id']
        if enabled:
            if not target:
                candidates=c.execute("SELECT id FROM subscriptions WHERE name='CF'").fetchall()
                if len(candidates)>1:raise HTTPException(409,'存在多个 CF 订阅，请指定目标订阅')
                if candidates:target=candidates[0]['id']
                else:
                    target=secrets.token_hex(8)
                    c.execute('INSERT INTO subscriptions(id,name,token,enabled) VALUES (?,?,?,1)',(target,'CF',secrets.token_urlsafe(32)))
            target_subscription(c,{'subscription_id':target})
            if initial:
                for m in machines:c.execute("INSERT OR IGNORE INTO platform_watch_seen(vm_id,name,first_seen,last_seen,state) VALUES (?,?,?,?,'baseline')",(m['id'],m['name'],now,now))
            next_check=(now+INTERVAL) if initial else (now if not cfg['enabled'] else cfg['next_check'])
        else:next_check=cfg['next_check']
        c.execute('UPDATE platform_watch SET enabled=?,base_port=?,subscription_id=?,next_check=? WHERE id=1',(int(enabled),base,target,next_check))
        if initial:
            c.execute("UPDATE platform_watch SET last_check=?,last_seen_count=?,last_message='已建立现有实例基线；每小时发现新实例后自动安装并加入订阅',last_error='' WHERE id=1",(now,len(machines)))
        cfg=settings(c)
        if enabled:sync_nodes(c,cfg)
    return status()


def status():
    from . import main
    with main.conn() as c:
        cfg=settings(c)
        sub=c.execute('SELECT name,enabled FROM subscriptions WHERE id=?',(cfg['subscription_id'],)).fetchone()
        return {k:cfg[k] for k in ('enabled','base_port','subscription_id','last_check','next_check','last_message','last_error','last_batch_id','last_new_count','last_seen_count')}|{
            'interval_seconds':INTERVAL,'checking':cfg['lease_until']>time.time(),
            'subscription_name':sub['name'] if sub else '',
            'subscription_enabled':bool(sub and sub['enabled']),
            'counts':{r['state']:r['n'] for r in c.execute('SELECT state,count(*) AS n FROM platform_watch_seen GROUP BY state')},
            'instances':[dict(r) for r in c.execute("SELECT vm_id,name,state,attempts,batch_id,node_id,last_error,first_seen,last_seen FROM platform_watch_seen ORDER BY CASE WHEN state IN ('blocked','retry','pending','installing') THEN 0 ELSE 1 END,first_seen DESC LIMIT 100")]}


def tick(force=False,now=None):
    from . import main
    if not CHECK_LOCK.acquire(blocking=False):return {'busy':True}
    scan='';current=time.time() if now is None else now
    try:
        with main.conn() as c:
            c.execute('BEGIN IMMEDIATE');cfg=settings(c)
            if not cfg or not cfg['enabled']:return {'disabled':True}
            if cfg['lease_until']>current:return {'busy':True}
            if not force and cfg['next_check']>current:return {'not_due':True}
            scan=secrets.token_hex(8)
            c.execute("UPDATE platform_watch SET last_check=?,next_check=?,lease_until=?,scan_id=?,last_message='正在检查平台新实例',last_error='' WHERE id=1",(current,current+INTERVAL,current+300,scan))
        with main.conn() as c:target_subscription(c,cfg)
        machines=vpsnat.vms();new_count=0
        with main.conn() as c:
            for m in machines:
                old=c.execute('SELECT vm_id FROM platform_watch_seen WHERE vm_id=?',(m['id'],)).fetchone()
                if not old:
                    new_count+=1;c.execute("INSERT INTO platform_watch_seen(vm_id,name,first_seen,last_seen,state) VALUES (?,?,?,?,'pending')",(m['id'],m['name'],current,current))
                else:c.execute('UPDATE platform_watch_seen SET name=?,last_seen=? WHERE vm_id=?',(m['name'],current,m['id']))
            cfg=settings(c)
            added=sync_nodes(c,cfg)
            eligible={r['vm_id'] for r in c.execute("SELECT vm_id FROM platform_watch_seen WHERE state IN ('pending','retry') AND attempts<?",(MAX_ATTEMPTS,))}
            candidates=[m['id'] for m in machines if m['id'] in eligible and m['status']=='running']
        with main.conn() as c:
            old_batches=[r[0] for r in c.execute("SELECT DISTINCT batch_id FROM platform_watch_seen WHERE state='installing' AND batch_id!=''")]
        for batch in old_batches:reconcile_batch(batch)
        batch_id=''
        if candidates:
            with main.conn() as c:
                cfg=settings(c)
                if not cfg['enabled']:return {'disabled':True}
                busy=c.execute("SELECT 1 FROM automation_batches WHERE status='running'").fetchone()
            if not busy:
                result=platform_automation.start(candidates,cfg['base_port'],'hourly-'+scan,None);batch_id=result['id']
                with main.conn() as c:
                    for vm in candidates:c.execute("UPDATE platform_watch_seen SET state='installing',attempts=attempts+1,batch_id=?,last_error='' WHERE vm_id=? AND state IN ('pending','retry')",(batch_id,vm))
            else:
                with main.conn() as c:c.execute("UPDATE platform_watch SET last_message='发现待安装实例；已有批次运行，将在下次巡检处理' WHERE id=1")
        with main.conn() as c:
            message=f'发现 {new_count} 个新实例，当前共 {len(machines)} 台；已补齐 {added} 个订阅节点'
            if batch_id:message+='；自动安装批次已启动'
            elif candidates:message+='；待安装实例等待下一次巡检'
            c.execute('UPDATE platform_watch SET last_seen_count=?,last_new_count=?,last_batch_id=CASE WHEN ?!=\'\' THEN ? ELSE last_batch_id END,last_message=? WHERE id=1',(len(machines),new_count,batch_id,batch_id,message))
        if batch_id:reconcile_batch(batch_id)
        return {'new_count':new_count,'batch_id':batch_id,'known_count':len(machines)}
    except Exception as exc:
        message=str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,vpsnat.APIError) else '平台巡检失败（'+type(exc).__name__+'），下小时重试'
        if scan:
            with main.conn() as c:c.execute('UPDATE platform_watch SET last_error=?,last_message=? WHERE id=1',(message,'本次巡检未完成，下小时重试'))
        return {'error':message}
    finally:
        if scan:
            with main.conn() as c:c.execute('UPDATE platform_watch SET lease_until=0 WHERE id=1 AND scan_id=?',(scan,))
        CHECK_LOCK.release()


def trigger():
    from . import main
    with main.conn() as c:
        cfg=settings(c)
        if not cfg['enabled']:raise HTTPException(409,'请先启用每小时自动发现')
        if cfg['lease_until']>time.time():raise HTTPException(409,'巡检正在执行，请稍后查看结果')
    threading.Thread(target=tick,kwargs={'force':True},name='platform-watch-manual',daemon=True).start()
    return {'ok':True}


def worker():
    from . import main
    while not STOP.is_set():
        try:
            tick()
            with main.conn() as c:cfg=settings(c)
            delay=max(1,min(60,cfg['next_check']-time.time())) if cfg and cfg['enabled'] else 60
        except Exception:
            import logging
            logging.getLogger(__name__).warning('Hourly platform discovery cycle unavailable')
            delay=60
        STOP.wait(delay)


def start():
    global WORKER
    if WORKER and WORKER.is_alive():return
    STOP.clear();WORKER=threading.Thread(target=worker,name='platform-hourly-discovery',daemon=True);WORKER.start()


def stop():STOP.set()
