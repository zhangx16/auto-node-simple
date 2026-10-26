import hashlib,json,os,re,shlex,sqlite3,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from app import main,auto_repair as repair,node_management
from test_app import AppTests
from test_node_management import NodeManagementTests
URI='vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe1@edge.example.com:443?security=tls&type=ws&sni=fixed.example.com&host=fixed.example.com&path=%2Fws#Node-CF'

class RepairPolicyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.p=patch.object(main,'DATA',self.root);self.p.start();self.addCleanup(self.p.stop);self.addCleanup(self.tmp.cleanup)
  with main.conn() as c:
   c.executescript("CREATE TABLE nodes(id TEXT PRIMARY KEY,name TEXT,protocol TEXT,uri TEXT,created REAL,config TEXT);CREATE TABLE installs(id TEXT PRIMARY KEY,status TEXT,created REAL,error TEXT);")
   node_management.init(c);repair.init(c)
   c.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?)',('abc123','Node-CF','argo',URI,0,'{}'))
   c.execute('INSERT INTO node_repair(node_id,token_hash) VALUES (?,?)',('abc123',hashlib.sha256(b'fixture').hexdigest()))
  self.now=10000
 def report(self,local=False,public=False,network=True):
  self.now+=60
  with patch.object(repair.time,'time',return_value=self.now):return repair.report('abc123','fixture',repair.Report(local_ok=local,public_ok=public,network_ok=network))['action']
 def update(self,**values):
  with main.conn() as c:c.execute('UPDATE node_repair SET '+','.join(k+'=?' for k in values)+' WHERE node_id=?',(*values.values(),'abc123'))
 def status(self):
  with main.conn() as c:return repair.status(c,'abc123')
 def test_three_failures_cooldown_and_persistent_pause(self):
  self.assertEqual([self.report() for _ in range(3)],['none','none','core'])
  self.assertEqual(self.status()['repair_count'],1)
  self.assertEqual(self.report(),'none')
  self.now+=300;self.assertEqual(self.report(),'none');self.assertEqual(self.report(),'core')
  self.now+=300
  self.assertEqual([self.report() for _ in range(3)],['none','none','core'])
  self.assertTrue(self.status()['paused']);self.now+=7200
  self.assertEqual(self.report(),'none')
  repair.control('abc123','resume');self.assertFalse(self.status()['paused'])
  self.assertEqual(self.status()['repair_count'],0)
 def test_recovery_network_outage_and_disabled_node(self):
  self.report();self.report();self.assertEqual(self.report(local=True,public=True),'none')
  self.assertEqual(self.status()['local_failures'],0)
  for _ in range(5):self.assertEqual(self.report(network=False),'none')
  repair.control('abc123','disable');self.assertEqual(self.report(),'none')
 def test_public_tunnel_repair_and_no_repeat_for_consumed_probe(self):
  self.assertEqual([self.report(local=True) for _ in range(3)],['none','none','tunnel'])
  self.now+=300
  self.update(probe_status='failed',probe_failures=2,probe_at=self.now)
  self.assertEqual(self.report(local=True,public=True),'protocol')
  self.now+=300;self.assertEqual(self.report(local=True,public=True),'none')
 def test_stale_probe_pending_update_and_bad_token_never_permit(self):
  self.update(probe_status='failed',probe_failures=3,probe_at=1)
  self.assertEqual(self.report(local=True,public=True),'none')
  with main.conn() as c:
   c.execute("INSERT INTO installs VALUES ('job','ready',?,'')",(time.time(),))
   c.execute("INSERT INTO node_updates VALUES ('abc123','job',?,'{\"port\":18081}')",(URI,))
  for _ in range(4):self.assertEqual(self.report(),'none')
  with self.assertRaises(HTTPException):repair.report('abc123','wrong',repair.Report(local_ok=False))
 def test_duplicate_report_is_not_an_extra_failure(self):
  self.report()
  with patch.object(repair.time,'time',return_value=self.now+1):repair.report('abc123','fixture',repair.Report(local_ok=False))
  self.assertEqual(self.status()['local_failures'],1)
 def test_old_link_probe_result_and_unavailable_component_not_failure(self):
  self.update(last_report=time.time())
  with patch('app.node_probe.probe',side_effect=RuntimeError('private error')):repair.check('abc123')
  self.assertEqual(self.status()['probe_status'],'unavailable')
  self.assertNotIn('private error',self.status()['probe_error'])
  def edit(uri):
   with main.conn() as c:c.execute("UPDATE nodes SET uri=? WHERE id='abc123'",(uri+'-changed',))
   return 8
  with patch('app.node_probe.probe',side_effect=edit):repair.check('abc123')
  self.assertEqual(self.status()['probe_status'],'unavailable')

class RepairAPITests(unittest.TestCase):
 setUpClass=classmethod(AppTests.setUpClass.__func__)
 tearDownClass=classmethod(AppTests.tearDownClass.__func__)
 call=AppTests.call
 prepare=NodeManagementTests.prepare
 command=NodeManagementTests.command
 uri=NodeManagementTests.uri
 installed=NodeManagementTests.installed
 def test_setup_auth_script_report_disable_and_delete(self):
  ident,_=self.installed();path='/api/nodes/'+ident+'/repair/setup'
  self.assertEqual(self.call(path,{},authed=False)[0],401)
  self.assertEqual(self.call(path,{},origin='https://evil.example')[0],403)
  code,body,_=self.call(path,{});self.assertEqual(code,200,body)
  url=re.search(r'http://[^ ]+\.sh',json.loads(body)['command'])[0];script_path=url.removeprefix(self.base)
  code,script,_=self.call(script_path,authed=False);self.assertEqual(code,200)
  subprocess.run(['sh','-n'],input=script,check=True)
  self.assertIn(b'OnCalendar=*-*-* *:*:00',script);self.assertNotIn(b'fixture-token',script)
  token=script_path.rsplit('/',1)[1].removesuffix('.sh')
  import urllib.request
  req=urllib.request.Request(self.base+'/repair/'+ident+'/report',method='POST',data=b'{"local_ok":true}',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  with urllib.request.urlopen(req) as r:self.assertEqual(json.load(r)['action'],'none')
  n=json.loads(self.call('/api/nodes')[1]);n=next(n for n in n if n['id']==ident)
  self.assertGreater(n['repair']['last_report'],0);self.assertNotIn(token,json.dumps(n))
  self.assertEqual(self.call('/api/nodes/'+ident+'/repair/disable',{})[0],200)
  self.assertEqual(self.call('/api/nodes/'+ident,method='DELETE')[0],200)
  self.assertEqual(self.call(script_path,authed=False)[0],404)
 def test_setup_rotates_token_and_missing_node_rejected(self):
  ident,_=self.installed('reality');path='/api/nodes/'+ident+'/repair/setup'
  first=json.loads(self.call(path,{})[1]);url=re.search(r'http://[^ ]+\.sh',first['command'])[0].removeprefix(self.base)
  self.assertEqual(self.call(path,{})[0],200)
  self.assertEqual(self.call(url,authed=False)[0],404)
  self.assertEqual(self.call('/api/nodes/missing/repair/setup',{})[0],404)

 def assignment(self,script,name):
  return shlex.split(next(line for line in script.decode().splitlines() if line.startswith(name+'=')).split('=',1)[1])[0]
 def test_new_install_embeds_watchdog_and_enrolls_only_after_success(self):
  ident,cfg,result,script=self.prepare('reality')
  report_token=self.assignment(script,'REPORT_TOKEN')
  self.assertEqual(self.assignment(script,'REPAIR_KEEP_TOKEN'),'0')
  self.assertIn(b'REPAIR_INSTALLER=',script)
  with sqlite3.connect(self.data/'simple.db') as c:self.assertEqual(c.execute('SELECT count(*) FROM node_repair WHERE node_id=?',(ident,)).fetchone()[0],0)
  self.assertEqual(self.call(result,{'link':self.uri(cfg),'repair_installed':True},authed=False)[0],200)
  with sqlite3.connect(self.data/'simple.db') as c:
   row=c.execute('SELECT token_hash,enabled,last_report FROM node_repair WHERE node_id=?',(ident,)).fetchone()
   self.assertEqual(row,(hashlib.sha256(report_token.encode()).hexdigest(),1,0))
   c.execute('UPDATE node_repair SET repair_count=2,last_report=123 WHERE node_id=?',(ident,))
  # Callback replay must not reset credentials or repair accounting.
  self.assertEqual(self.call(result,{'link':self.uri(cfg),'repair_installed':True},authed=False)[0],200)
  with sqlite3.connect(self.data/'simple.db') as c:self.assertEqual(c.execute('SELECT repair_count,last_report FROM node_repair WHERE node_id=?',(ident,)).fetchone(),(2,123))
 def test_update_keeps_existing_watchdog_credential_and_pause_state(self):
  ident,cfg=self.installed('reality')
  setup=json.loads(self.call('/api/nodes/'+ident+'/repair/setup',{})[1]);url=re.search(r'http://[^ ]+\.sh',setup['command'])[0].removeprefix(self.base)
  token=url.rsplit('/',1)[1].removesuffix('.sh')
  with sqlite3.connect(self.data/'simple.db') as c:c.execute('UPDATE node_repair SET enabled=0,paused=1,repair_count=2 WHERE node_id=?',(ident,))
  edit={k:cfg[k] for k in ['name','port','client_port','address','sni']};edit['port']+=1
  code,body,_=self.call('/api/nodes/'+ident,edit,method='PUT');self.assertEqual(code,200,body)
  _,new,result,script=self.command(json.loads(body));self.assertEqual(self.assignment(script,'REPAIR_KEEP_TOKEN'),'1')
  self.assertEqual(self.call(result,{'link':self.uri(new),'repair_installed':True,'repair_token_kept':True},authed=False)[0],200)
  with sqlite3.connect(self.data/'simple.db') as c:self.assertEqual(c.execute('SELECT token_hash,enabled,paused,repair_count FROM node_repair WHERE node_id=?',(ident,)).fetchone(),(hashlib.sha256(token.encode()).hexdigest(),0,1,2))
 def test_failed_install_never_registers_watchdog(self):
  ident,cfg,result,_=self.prepare('hysteria2')
  self.assertEqual(self.call(result,{'error':'fixture failure'},authed=False)[0],200)
  with sqlite3.connect(self.data/'simple.db') as c:self.assertEqual(c.execute('SELECT count(*) FROM node_repair WHERE node_id=?',(ident,)).fetchone()[0],0)


class ProtocolProbeTests(unittest.TestCase):
 def test_fallback_trace_validation_and_process_cleanup(self):
  from app import node_probe
  from unittest.mock import MagicMock
  with tempfile.TemporaryDirectory() as tmp:
   assets=Path(tmp);binary=assets/'sing-box-amd64';binary.write_bytes(b'fixture');binary.chmod(0o700)
   (assets/'manifest.json').write_text(json.dumps({binary.name:{'sha256':hashlib.sha256(b'fixture').hexdigest()}}))
   for trace,valid in [(b'ip=192.0.2.1\n\n200',True),(b'error page\n200',False)]:
    process=MagicMock();process.poll.return_value=None
    replies=[subprocess.CompletedProcess([],28,b'\n000'),subprocess.CompletedProcess([],0,trace),subprocess.CompletedProcess([],0)]
    with patch.object(main,'ASSETS',assets),patch.object(node_probe.platform,'machine',return_value='x86_64'),patch.object(node_probe.subprocess,'Popen',return_value=process),patch.object(node_probe.subprocess,'run',side_effect=replies),patch.object(node_probe.socket,'create_connection',return_value=MagicMock()):
     if valid:self.assertGreaterEqual(node_probe.probe(URI),0)
     else:
      with self.assertRaises(ValueError):node_probe.probe(URI)
    process.terminate.assert_called_once();process.wait.assert_called_once()
 def test_panel_network_failure_never_becomes_node_failure(self):
  from app import node_probe
  from unittest.mock import MagicMock
  with tempfile.TemporaryDirectory() as tmp:
   assets=Path(tmp);binary=assets/'sing-box-amd64';binary.write_bytes(b'fixture');binary.chmod(0o700)
   (assets/'manifest.json').write_text(json.dumps({binary.name:{'sha256':hashlib.sha256(b'fixture').hexdigest()}}))
   process=MagicMock();process.poll.return_value=None
   with patch.object(main,'ASSETS',assets),patch.object(node_probe.platform,'machine',return_value='x86_64'),patch.object(node_probe.subprocess,'Popen',return_value=process),patch.object(node_probe.subprocess,'run',return_value=subprocess.CompletedProcess([],28,b'\n000')),patch.object(node_probe.socket,'create_connection',return_value=MagicMock()):
    with self.assertRaises(RuntimeError):node_probe.probe(URI)
   process.terminate.assert_called_once()
