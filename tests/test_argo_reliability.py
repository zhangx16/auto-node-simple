"""Transport recovery, real health-client wiring, and managed route transactions."""
import base64,hashlib,json,os,subprocess,time,unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from app import main,node_upgrade
import test_watchdog as watchdog
import test_node_upgrade as upgrade

class RecoveryTests(unittest.TestCase):
 setUp=watchdog.WatchdogShellTests.setUp
 fake=watchdog.WatchdogShellTests.fake
 rewrite=watchdog.WatchdogShellTests.rewrite
 run_watchdog=watchdog.WatchdogShellTests.run_watchdog
 def test_failed_public_tunnel_switches_transport_and_verifies(self):
  self.control.write_text('{"action":"tunnel"}')
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os
from pathlib import Path
a=sys.argv[1:]
if '--data-binary' in a:Path(a[a.index('-o')+1]).write_text('{"action":"tunnel"}')
elif '-w' in a:print('530' if a[-1].startswith('https:') else '101',end='')
''')
  self.run_watchdog()
  override=self.root/'units'/('auto-node-'+self.node+'.service.d')/'transport.conf'
  self.assertIn('TUNNEL_TRANSPORT_PROTOCOL=http2',override.read_text())
  self.assertEqual(self.log.read_text().splitlines(),['daemon-reload','restart auto-node-abc123'])
  self.assertEqual((self.state/'result').read_text().strip(),'tunnel_restarted_verified')
 def test_restart_success_with_forwarding_failure_is_not_recovery(self):
  self.control.write_text('{"action":"tunnel"}')
  (self.root/'lib'/'argo-health.sh').write_text('exit 1\n')
  self.run_watchdog()
  self.assertEqual((self.state/'result').read_text().strip(),'forwarding_failed')

class HealthClientTests(unittest.TestCase):
 setUp=watchdog.WatchdogShellTests.setUp
 fake=watchdog.WatchdogShellTests.fake
 rewrite=watchdog.WatchdogShellTests.rewrite
 def prepare(self):
  cfg={'inbounds':[{'users':[{'uuid':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1'}],'transport':{'path':'/original-ws'}}]}
  (self.base/'configs'/f'{self.node}.json').write_text(json.dumps(cfg))
  self.fake('health-core', '''#!/usr/bin/env python3
import sys,os,time
from pathlib import Path
Path(os.environ['MOCK_LOG']).write_bytes(Path(sys.argv[-1]).read_bytes())
Path(os.environ['MOCK_LOG']+'.pid').write_text(str(os.getpid()))
time.sleep(30)
''')
  self.fake('ss', '''#!/usr/bin/env python3
import os
from pathlib import Path
p=Path(os.environ['MOCK_LOG']+'.pid')
if p.exists():print('LISTEN 0 100 127.0.0.1:45678 0.0.0.0:* users:(("core",pid='+p.read_text()+',fd=7))')
''')
  self.fake('sleep','#!/bin/sh\n/bin/sleep .05\n')
  source=(watchdog.APP/'argo-health.sh').read_text()
  path=self.root/'health';path.write_text(self.rewrite(source).replace(str(self.core),str(self.bin/'health-core')))
  return path
 def test_authenticated_client_uses_advertised_endpoint_and_cleans_up(self):
  path=self.prepare()
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os
from pathlib import Path
a=sys.argv[1:]
assert a[a.index('--proxy')+1]=='http://127.0.0.1:45678'
assert a[a.index('--noproxy')+1]==''
Path(a[a.index('-o')+1]).write_text('');print('204 0.05',end='')
''')
  r=subprocess.run(['sh',str(path),self.node,'--stable'],env=self.env,capture_output=True,timeout=10)
  self.assertEqual(r.returncode,0,r.stderr.decode())
  cfg=json.loads(self.log.read_text());out=cfg['outbounds'][0]
  self.assertEqual(out['server'],'edge.example.com');self.assertEqual(out['server_port'],443)
  self.assertEqual(out['tls']['server_name'],'fixed.example.com')
  self.assertEqual(out['transport']['path'],'/original-ws');self.assertEqual(out['uuid'],'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1')
  pid=int(Path(str(self.log)+'.pid').read_text())
  with self.assertRaises(ProcessLookupError):os.kill(pid,0)
 def test_web_error_and_invalid_trace_do_not_pass(self):
  path=self.prepare()
  self.fake('curl', '''#!/usr/bin/env python3
import sys
from pathlib import Path
a=sys.argv[1:];Path(a[a.index('-o')+1]).write_text('<html>error page</html>');print('200',end='')
''')
  r=subprocess.run(['sh',str(path),self.node],env=self.env,capture_output=True,timeout=10)
  self.assertNotEqual(r.returncode,0)

class UpgradeRecoveryTests(unittest.TestCase):
 setUp=upgrade.UpgradePolicyTests.setUp
 setUpBase=upgrade.UpgradePolicyTests.setUpBase
 update=upgrade.UpgradePolicyTests.update
 row=upgrade.UpgradePolicyTests.row
 def test_failed_public_tunnel_can_receive_release_when_local_network_work(self):
  from app import auto_repair
  report=auto_repair.Report(local_ok=True,public_ok=False,network_ok=True,script_version=3,release='0'*64)
  self.assertEqual(auto_repair.report('abc123','fixture',report)['upgrade']['release'],self.target)
 def test_network_outage_cannot_trigger_upgrade(self):
  from app import auto_repair
  report=auto_repair.Report(local_ok=True,public_ok=False,network_ok=False,script_version=3)
  self.assertNotIn('upgrade',auto_repair.report('abc123','fixture',report))
 def test_route_preparation_and_rollback_are_bound_to_pending_install(self):
  from app import node_management,cloudflare
  ident='edit-job';token='fixture-install-token'
  with main.conn() as c:
   c.execute('INSERT INTO installs(id,status,created,error,token_hash,config,target_node) VALUES (?,?,?,?,?,?,?)',(ident,'ready',time.time(),'',hashlib.sha256(token.encode()).hexdigest(),json.dumps({'protocol':'argo','port':18081}),'abc123'))
   c.execute('INSERT INTO node_updates VALUES (?,?,?,?)',('abc123',ident,upgrade.fixtures.URI,'{}'))
  with patch.object(cloudflare,'binding',return_value={'id':'owned'}),patch.object(cloudflare,'update_port') as update_port:
   main.prepare_argo_route(ident,token,'prepare');update_port.assert_called_with('abc123',18081)
   main.prepare_argo_route(ident,token,'rollback');update_port.assert_called_with('abc123',18080)
   with self.assertRaises(HTTPException):main.prepare_argo_route(ident,'wrong','prepare')
   with main.conn() as c:c.execute('DELETE FROM node_updates')
   with self.assertRaises(HTTPException):main.prepare_argo_route(ident,token,'prepare')

class InstallerSelectionTests(unittest.TestCase):
 setUp=watchdog.WatchdogShellTests.setUp
 fake=watchdog.WatchdogShellTests.fake
 rewrite=watchdog.WatchdogShellTests.rewrite
 def install(self,quic='fail',http2='80',requested='auto'):
  (self.base/'configs'/f'{self.node}.json').unlink()
  cloud=self.root/'cloud';cloud.write_text('#!/bin/sh\nexit 0\n');cloud.chmod(0o755)
  self.fake('sleep','#!/bin/sh\nexit 0\n')
  self.fake('ss','#!/bin/sh\nexit 0\n')
  health=self.root/'lib'/'argo-health.sh'
  # Read the actual generated systemd override: a stale HTTP/2 override must
  # not make the QUIC trial accidentally measure HTTP/2.
  override=self.root/'units'/f'auto-node-{self.node}.service.d'/'transport.conf'
  health.write_text(f'''#!/bin/sh
protocol=$(sed -n 's/^Environment=TUNNEL_TRANSPORT_PROTOCOL=//p' '{override}')
case "$protocol" in
quic) value='{quic}' ;;
http2) value='{http2}' ;;
*) exit 1;;
esac
[ "$value" != fail ] || exit 1
printf '%s\\n' "$value"
''')
  override.parent.mkdir();override.write_text('[Service]\nEnvironment=TUNNEL_TRANSPORT_PROTOCOL=http2\n')
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os,json
from pathlib import Path
a=sys.argv[1:]
if '--data-binary' in a:
 body=Path(a[a.index('--data-binary')+1][1:]).read_text()
 with open(os.environ['MOCK_CONTROL'],'a') as f:f.write(body+'\\n')
''')
  self.control.write_text('')
  options={'protocol':'argo','port':33001,'sni':'fixed.example.com','credential':'341cad6d-9bf5-4b09-9c96-a7879ebc8fe1','path':'/ws','tunnel':'fixed','tunnel_token':'fixture-token','tunnel_hostname':'fixed.example.com','address':'edge.example.com','client_port':443,'name':'Fixture','argo_protocol':requested}
  manifest={name:{'sha256':hashlib.sha256(path.read_bytes()).hexdigest()} for name,path in [('sing-box-amd64',self.core),('cloudflared-amd64',cloud)]}
  env=self.env|{'PANEL':'https://panel.example.com','NODE_ID':self.node,'INSTALL_ID':'job','CALLBACK_TOKEN':'fixture','PAYLOAD':base64.b64encode(json.dumps(options).encode()).decode(),'MANIFEST':base64.b64encode(json.dumps(manifest).encode()).decode(),'REPAIR_KEEP_TOKEN':'1','REPORT_TOKEN':'fixture','REPAIR_INSTALLER':base64.b64encode(b'exit 0\n').decode(),'NODE_RELEASE':'a'*64,'NODE_DEFER_REPORT':'1'}
  source=self.rewrite((watchdog.APP/'install.sh').read_text()).replace('/usr/local/bin/auto-node-cloudflared',str(cloud)).replace('/etc/conf.d',str(self.root/'conf.d'))
  path=self.root/'install';path.write_text('set -eu\numask 077\n'+source)
  r=subprocess.run(['sh',str(path)],env=env,capture_output=True,timeout=15)
  reports=[]
  # jq produces multiline JSON; each callback writes a complete JSON document.
  decoder=json.JSONDecoder();text=self.control.read_text().lstrip()
  while text:
   obj,end=decoder.raw_decode(text);reports.append(obj);text=text[end:].lstrip()
  return r,reports,override
 def test_false_positive_quic_precheck_cannot_mark_broken_install_successful(self):
  r,reports,override=self.install(quic='fail',http2='fail')
  self.assertNotEqual(r.returncode,0,r.stdout.decode()+r.stderr.decode())
  self.assertFalse(any('link' in x for x in reports));self.assertTrue(any('error' in x for x in reports))
  self.assertFalse((self.base/'configs'/f'{self.node}.json').exists())
 def test_quic_failure_selects_http2_and_reports_actual_transport(self):
  r,reports,override=self.install()
  self.assertEqual(r.returncode,0,(r.stdout+r.stderr).decode())
  self.assertEqual(reports[-1]['argo_transport'],'http2')
  self.assertEqual((self.base/'state'/f'{self.node}.transport').read_text().strip(),'http2')
  self.assertIn('TUNNEL_TRANSPORT_PROTOCOL=http2',override.read_text())
 def test_significantly_faster_stable_quic_is_selected(self):
  r,reports,override=self.install(quic='40',http2='100')
  self.assertEqual(r.returncode,0,(r.stdout+r.stderr).decode())
  self.assertEqual(reports[-1]['argo_transport'],'quic');self.assertIn('TUNNEL_TRANSPORT_PROTOCOL=quic',override.read_text())
 def test_small_timing_difference_prefers_http2(self):
  r,reports,_=self.install(quic='95',http2='100')
  self.assertEqual(r.returncode,0,(r.stdout+r.stderr).decode());self.assertEqual(reports[-1]['argo_transport'],'http2')
 def test_explicit_quic_failure_falls_back_to_http2(self):
  r,reports,_=self.install(requested='quic')
  self.assertEqual(r.returncode,0,(r.stdout+r.stderr).decode());self.assertEqual(reports[-1]['argo_transport'],'http2')
