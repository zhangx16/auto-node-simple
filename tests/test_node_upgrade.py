import base64,hashlib,json,sqlite3,subprocess,time,unittest
from unittest.mock import patch
from fastapi import HTTPException
from app import main,auto_repair,node_upgrade
import test_auto_repair as fixtures
import test_watchdog as shell_fixtures

class UpgradePolicyTests(unittest.TestCase):
 setUpBase=fixtures.RepairPolicyTests.setUp
 update=fixtures.RepairPolicyTests.update
 def setUp(self):
  self.setUpBase()
  assets=self.root/'assets';assets.mkdir();(assets/'manifest.json').write_text('{}')
  p=patch.object(main,'ASSETS',assets);p.start();self.addCleanup(p.stop)
  with main.conn() as c:
   for col in ['token_hash','config','target_node']:c.execute("ALTER TABLE installs ADD COLUMN "+col+" TEXT NOT NULL DEFAULT ''")
   cfg={'name':'Node','protocol':'argo','port':18080,'client_port':443,'address':'edge.example.com','sni':'fixed.example.com','credential':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','path':'/ws','tunnel':'fixed','cf_mode':'manual','tunnel_hostname':'fixed.example.com','tunnel_token':''}
   c.execute('UPDATE nodes SET config=?',(json.dumps(cfg),))
  self.target=node_upgrade.release()
 def row(self):
  with main.conn() as c:return dict(c.execute("SELECT * FROM node_repair WHERE node_id='abc123'").fetchone())
 def offer(self,version=3,installed='',healthy=True):
  with main.conn() as c:return node_upgrade.allowed(c,'abc123',self.row(),version,installed,healthy)
 def test_legacy_bootstrap_permissions_and_same_release(self):
  self.assertEqual(self.offer(version=1),'');self.assertEqual(self.offer(),self.target)
  self.assertEqual(self.offer(installed=self.target),'');self.assertEqual(self.offer(healthy=False),'')
  self.update(auto_upgrade=0);self.assertEqual(self.offer(),'')
  self.update(auto_upgrade=1,paused=1);self.assertEqual(self.offer(),'')
 def test_bundle_auth_checksum_scope_and_failure_cleanup(self):
  with self.assertRaises(HTTPException):node_upgrade.bundle('abc123','wrong')
  bundle=node_upgrade.bundle('abc123','fixture');script=base64.b64decode(bundle['script'])
  self.assertEqual(hashlib.sha256(script).hexdigest(),bundle['sha256']);self.assertEqual(bundle['release'],self.target)
  self.assertIn(b'NODE_RELEASE='+self.target.encode(),script);subprocess.run(['sh','-n'],input=script,check=True)
  row=self.row();self.assertEqual(row['upgrade_attempts'],1);self.assertEqual(row['upgrade_status'],'running')
  with self.assertRaises(HTTPException):node_upgrade.bundle('abc123','fixture')
  node_upgrade.finished('abc123','fixture',self.target,False)
  with main.conn() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM node_updates').fetchone()[0],0)
   self.assertEqual(c.execute('SELECT status FROM installs').fetchone()[0],'cancelled')
  self.assertEqual(self.row()['upgrade_status'],'failed');self.assertEqual(self.offer(),'')
 def test_retry_cap_new_release_and_manual_configuration_guard(self):
  self.update(upgrade_target=self.target,upgrade_attempts=3,upgrade_at=time.time()-7200)
  self.assertEqual(self.offer(),'');node_upgrade.control('abc123','retry');self.assertEqual(self.offer(),self.target)
  self.update(upgrade_target='f'*64,upgrade_attempts=3,upgrade_at=time.time());self.assertEqual(self.offer(),self.target)
  with main.conn() as c:
   c.execute("INSERT INTO installs(id,status,created,error,config) VALUES ('manual','ready',?,'','{}')",(time.time()-7200,))
   c.execute("INSERT INTO node_updates VALUES ('abc123','manual','old','{}')")
  self.assertEqual(self.offer(),'')
 def test_lost_bundle_expires_without_cancelling_manual_jobs(self):
  node_upgrade.bundle('abc123','fixture')
  self.update(upgrade_at=time.time()-7200)
  with main.conn() as c:c.execute('UPDATE installs SET created=?',(time.time()-7200,))
  self.assertEqual(self.offer(),self.target)
  with main.conn() as c:self.assertEqual(c.execute('SELECT count(*) FROM node_updates').fetchone()[0],0)
 def test_success_requires_complete_install_callback(self):
  node_upgrade.bundle('abc123','fixture')
  with self.assertRaises(HTTPException):node_upgrade.finished('abc123','fixture',self.target,True)
  self.update(installed_release=self.target)
  node_upgrade.finished('abc123','fixture',self.target,True)
  self.assertEqual(self.row()['upgrade_status'],'ok')

class UpgradeShellTests(unittest.TestCase):
 setUp=shell_fixtures.WatchdogShellTests.setUp
 fake=shell_fixtures.WatchdogShellTests.fake
 rewrite=shell_fixtures.WatchdogShellTests.rewrite
 def prepare(self,body,bad_digest=False):
  (self.state/'curl.conf').write_text('fixture-header')
  (self.state/'release').write_text('b'*64)
  (self.root/'lib'/'watchdog.sh').write_text('exit 0\n');(self.root/'lib'/'update.sh').write_text('exit 0\n')
  package=body.encode();target='a'*64
  self.control.write_text(json.dumps({'release':target,'sha256':'0'*64 if bad_digest else hashlib.sha256(package).hexdigest(),'script':base64.b64encode(package).decode()}))
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os
from pathlib import Path
a=sys.argv[1:]
if '--data-binary' in a:
 body=a[a.index('--data-binary')+1][1:]
 Path(os.environ['MOCK_LOG']).write_bytes(Path(body).read_bytes())
else:Path(a[a.index('-o')+1]).write_bytes(Path(os.environ['MOCK_CONTROL']).read_bytes())
''')
  source=(shell_fixtures.APP/'node-update.sh').read_text().replace('/run/auto-node-upgrade.lock',str(self.root/'upgrade-lock'))
  path=self.root/'client';path.write_text(self.rewrite(source))
  return path
 def run_client(self,path):return subprocess.run(['sh',str(path),self.node],env=self.env,capture_output=True,timeout=15)
 def test_bad_checksum_never_executes(self):
  marker=self.root/'executed';path=self.prepare('touch '+str(marker)+'\n',bad_digest=True)
  self.assertNotEqual(self.run_client(path).returncode,0);self.assertFalse(marker.exists())
 def test_full_install_failure_restores_configuration_and_binary(self):
  original=(self.base/'configs'/f'{self.node}.json').read_bytes();old_core=self.core.read_bytes()
  body=f'''printf changed > {self.base}/configs/{self.node}.json
printf changed > {self.state}/settings.json
printf new > {self.core}.next
mv {self.core}.next {self.core}
exit 1
'''
  path=self.prepare(body);self.assertNotEqual(self.run_client(path).returncode,0)
  self.assertEqual((self.base/'configs'/f'{self.node}.json').read_bytes(),original);self.assertEqual(self.core.read_bytes(),old_core)
  self.assertEqual((self.state/'release').read_text(),'b'*64)
 def test_success_keeps_previous_snapshot(self):
  body=f"printf '%s' {'a'*64} > {self.state}/release\n"
  path=self.prepare(body);result=self.run_client(path);self.assertEqual(result.returncode,0,result.stderr.decode())
  self.assertTrue((self.base/'upgrade-previous').is_dir());self.assertEqual((self.state/'release').read_text(),'a'*64)
  self.assertTrue(json.loads(self.log.read_text())['ok'])
