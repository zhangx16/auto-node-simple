"""Exercise the real shell with isolated paths and simulated service commands."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path
APP=Path(__file__).resolve().parents[1]/'app'
class WatchdogShellTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.base=self.root/'base';self.bin=self.root/'bin';self.bin.mkdir();self.node='abc123'
  self.state=self.base/'repair'/self.node;self.state.mkdir(parents=True)
  (self.base/'configs').mkdir();(self.base/'state').mkdir();(self.root/'systemd').mkdir();(self.root/'units').mkdir();(self.root/'lib').mkdir()
  self.log=self.root/'calls';self.control=self.root/'control';self.control.write_text('{"action":"none"}')
  self.env=os.environ|{'PATH':str(self.bin)+':'+os.environ['PATH'],'MOCK_LOG':str(self.log),'MOCK_CONTROL':str(self.control)}
  (self.state/'settings.json').write_text(json.dumps({'protocol':'argo','hostname':'fixed.example.com','panel':'https://panel.example.com'}));(self.state/'curl.conf').write_text('')
  (self.base/'configs'/f'{self.node}.json').write_text(json.dumps({'inbounds':[{'type':'vless','listen_port':18080,'transport':{'type':'ws','path':'/ws'}}]}))
  (self.base/'state'/f'{self.node}.link').write_text('vless://fixture@edge.example.com:443?security=tls&type=ws&sni=fixed.example.com&path=%2Fws#Node-CF')
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os
from pathlib import Path
a=sys.argv[1:]
if '--data-binary' in a:
 Path(a[a.index('-o')+1]).write_text(Path(os.environ['MOCK_CONTROL']).read_text())
elif '-w' in a:print('101',end='')
''')
  self.fake('ss','#!/bin/sh\necho "LISTEN 0 100 127.0.0.1:18080 0.0.0.0:*"\n')
  self.fake('systemctl','#!/bin/sh\necho "$*" >> "$MOCK_LOG"\n')
  self.core=self.root/'core';self.core.write_text('#!/bin/sh\nexit 0\n');self.core.chmod(0o755)
 def fake(self,name,text):
  p=self.bin/name;p.write_text(text);p.chmod(0o755)
 def rewrite(self,text):
  return text.replace('/usr/local/etc/auto-node-simple',str(self.base)).replace('/usr/local/lib/auto-node-simple',str(self.root/'lib')).replace('/usr/local/bin/auto-node-singbox',str(self.core)).replace('/run/auto-node-simple.lock',str(self.root/'lock')).replace('/run/auto-node-repair.lock',str(self.root/'runner-lock')).replace('/run/systemd/system',str(self.root/'systemd')).replace('/etc/systemd/system',str(self.root/'units')).replace('export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin','export PATH='+str(self.bin)+':'+os.environ['PATH'])
 def run_watchdog(self):
  script=self.root/'watchdog';script.write_text(self.rewrite((APP/'node-watchdog.sh').read_text()))
  p=subprocess.run(['sh',str(script),self.node],env=self.env,capture_output=True,timeout=15)
  self.assertEqual(p.returncode,0,p.stderr.decode())
 def test_no_permit_no_restart_and_only_tunnel_permit_restarts_tunnel(self):
  self.run_watchdog();self.assertFalse(self.log.exists())
  self.control.write_text('{"action":"tunnel"}');self.run_watchdog()
  self.assertEqual(self.log.read_text().splitlines(),['restart auto-node-abc123'])
  self.assertEqual((self.state/'result').read_text().strip(),'tunnel_restarted')
 def test_shared_core_cooldown_and_config_check_failure(self):
  self.control.write_text('{"action":"core"}');self.run_watchdog();self.run_watchdog()
  self.assertEqual(self.log.read_text().splitlines(),['restart auto-node-simple'])
  self.assertEqual((self.state/'result').read_text().strip(),'shared_core_cooldown')
  (self.base/'repair/last-core-repair').unlink();self.core.write_text('#!/bin/sh\nexit 1\n');self.run_watchdog()
  self.assertEqual(self.log.read_text().splitlines(),['restart auto-node-simple'])
 def test_installer_creates_timer_and_does_not_modify_protocol(self):
  import base64
  original=(self.base/'configs'/f'{self.node}.json').read_bytes()
  text=(APP/'repair-install.sh').read_text().replace('__WATCHDOG_B64__',base64.b64encode(self.rewrite((APP/'node-watchdog.sh').read_text()).encode()).decode())
  script=self.root/'setup';script.write_text('set -eu\numask 077\nPANEL=https://panel.example.com\nNODE_ID=abc123\nREPORT_TOKEN=fixture-report-token\n'+self.rewrite(text))
  p=subprocess.run(['sh',str(script)],env=self.env,capture_output=True,timeout=15)
  self.assertEqual(p.returncode,0,p.stderr.decode());self.assertEqual((self.base/'configs'/f'{self.node}.json').read_bytes(),original)
  self.assertIn('OnCalendar=*-*-* *:*:00',(self.root/'units'/'auto-node-repair.timer').read_text())
  self.assertIn('enable --now auto-node-repair.timer',self.log.read_text())
  self.assertEqual((self.state/'curl.conf').stat().st_mode&0o777,0o600)
 def test_openrc_installer_preserves_other_cron_entries(self):
  import base64
  self.fake('rc-service','#!/bin/sh\necho "$*" >> "$MOCK_LOG"\n');self.fake('rc-update','#!/bin/sh\nexit 0\n')
  self.fake('crontab','#!/bin/sh\nif [ "$1" = -l ];then cat "$MOCK_CONTROL";else cp "$1" "$MOCK_CONTROL";fi\n')
  # curl report needs JSON; leave watchdog body inert for this installation-path test.
  self.control.write_text('0 0 * * * unrelated-job\n* * * * * old-node-job # simple-repair-abc123\n')
  text=(APP/'repair-install.sh').read_text().replace('__WATCHDOG_B64__',base64.b64encode(b'exit 0\n').decode())
  text=self.rewrite(text).replace(str(self.root/'systemd'),str(self.root/'no-systemd'))
  script=self.root/'setup';script.write_text('set -eu\numask 077\nPANEL=https://panel.example.com\nNODE_ID=abc123\nREPORT_TOKEN=fixture-report-token\n'+text)
  for _ in range(2):
   p=subprocess.run(['sh',str(script)],env=self.env,capture_output=True,timeout=15);self.assertEqual(p.returncode,0,p.stderr.decode())
  cron=self.control.read_text();self.assertIn('unrelated-job',cron);self.assertEqual(cron.count('# simple-repair-all'),1);self.assertNotIn('old-node-job',cron)
 def test_shared_runner_checks_all_nodes_and_skips_deleted_configs(self):
  import shutil
  second='def456';shutil.copytree(self.state,self.base/'repair'/second)
  (self.base/'configs'/f'{second}.json').write_bytes((self.base/'configs'/f'{self.node}.json').read_bytes())
  deleted=self.base/'repair'/'fed123';deleted.mkdir();(deleted/'settings.json').write_bytes((self.state/'settings.json').read_bytes())
  self.fake('curl', '''#!/usr/bin/env python3
import sys,os
from pathlib import Path
a=sys.argv[1:]
if '--data-binary' in a:
 with open(os.environ['MOCK_LOG'],'a') as f:f.write(a[-3]+'\\n' if a[-2]=='-o' else 'report\\n')
 Path(a[a.index('-o')+1]).write_text('{"action":"none"}')
elif '-w' in a:print('101',end='')
''')
  script=self.root/'watchdog';script.write_text(self.rewrite((APP/'node-watchdog.sh').read_text()))
  p=subprocess.run(['sh',str(script),'--all'],env=self.env,capture_output=True,timeout=15)
  self.assertEqual(p.returncode,0,p.stderr.decode())
  lines=self.log.read_text().splitlines();self.assertEqual(len(lines),2)
  self.assertTrue(any('/abc123/report' in line for line in lines));self.assertTrue(any('/def456/report' in line for line in lines))
 def test_setup_failure_restores_existing_credentials(self):
  import base64
  (self.state/'curl.conf').write_text('original-credential')
  original=(self.state/'settings.json').read_bytes()
  self.fake('systemctl','#!/bin/sh\n[ "$1" != enable ]\n')
  script=self.root/'setup';text=(APP/'repair-install.sh').read_text().replace('__WATCHDOG_B64__',base64.b64encode(b'exit 0\n').decode())
  script.write_text('set -eu\numask 077\nPANEL=https://new.example.com\nNODE_ID=abc123\nREPORT_TOKEN=new-token\n'+self.rewrite(text))
  p=subprocess.run(['sh',str(script)],env=self.env,capture_output=True,timeout=15)
  self.assertNotEqual(p.returncode,0);self.assertEqual((self.state/'curl.conf').read_text(),'original-credential');self.assertEqual((self.state/'settings.json').read_bytes(),original)
 def test_upgrade_consolidates_only_owned_old_timers_and_cron(self):
  import base64
  for name in ['auto-node-repair-abc123.timer','auto-node-repair-abc123.service','unrelated.timer']:(self.root/'units'/name).write_text('fixture')
  text=(APP/'repair-install.sh').read_text().replace('__WATCHDOG_B64__',base64.b64encode(b'exit 0\n').decode())
  script=self.root/'setup';script.write_text('set -eu\numask 077\nPANEL=https://panel.example.com\nNODE_ID=abc123\nREPORT_TOKEN=fixture\n'+self.rewrite(text))
  p=subprocess.run(['sh',str(script)],env=self.env,capture_output=True,timeout=15)
  self.assertEqual(p.returncode,0,p.stderr.decode());self.assertFalse((self.root/'units'/'auto-node-repair-abc123.timer').exists());self.assertTrue((self.root/'units'/'unrelated.timer').exists())
  self.assertIn('disable --now auto-node-repair-abc123.timer',self.log.read_text())
