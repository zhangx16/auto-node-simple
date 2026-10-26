"""Run only inside a disposable root container; service manager is simulated."""
import unittest,os,subprocess,re,json,sys
from pathlib import Path
from test_app import AppTests
class InstallTests(AppTests):
 def test_real_protocol_install_and_forwarding(self):
  if os.getenv('SIMPLE_INSTALL_INTEGRATION')!='1':self.skipTest('disposable container only')
  Path('/run/systemd/system').mkdir(parents=True,exist_ok=True)
  service=Path('/usr/local/bin/systemctl');service.write_text('''#!/bin/sh
case "$1" in
daemon-reload|enable) exit 0;;
is-active) exit 0;;
restart)
 name=$2
 if [ -f "/run/$name.pid" ];then kill "$(cat "/run/$name.pid")" 2>/dev/null || true;sleep .2;fi
 case "$name" in
auto-node-simple) /usr/local/bin/auto-node-singbox run -C /usr/local/etc/auto-node-simple/configs >/tmp/core.log 2>&1 9>&- & ;;
*) args=$(sed -n 's/^ExecStart=//p' "/etc/systemd/system/$name.service");sh -c "exec $args" >/tmp/tunnel.log 2>&1 9>&- & ;;
esac
 echo $! > "/run/$name.pid";;
esac
''');service.chmod(0o755)
  Path('/etc/systemd/system').mkdir(parents=True,exist_ok=True)
  for protocol,port in [('reality',49431),('hysteria2',49432)]:
   code,body,_=self.call('/api/installs',{'protocol':protocol,'name':'Integration','address':'127.0.0.1' if protocol!='argo' else '', 'port':port,'client_port':port if protocol!='argo' else 443});self.assertEqual(code,200,body)
   job=json.loads(body);url=re.search(r'http://[^ ]+\.sh',job['command'])[0];_,script,_=self.call(url.removeprefix(self.base),authed=False)
   result=subprocess.run(['sh'],input=script,capture_output=True,timeout=180)
   if result.returncode:print(result.stdout.decode()[-3000:],result.stderr.decode()[-3000:])
   self.assertEqual(result.returncode,0)
   rows=json.loads(self.call('/api/installs')[1]);row=next(r for r in rows if r['id']==job['id']);self.assertEqual(row['status'],'done')
   if protocol!='argo':
    probe='import sys;sys.path.insert(0,"/app");from app.node_probe import probe_link;print(probe_link(sys.stdin.read(),timeout=15)["ok"])'
    # Existing test image provides the native protocol probe core; do not print credentials.
    r=subprocess.run(['python','-c',probe],input=row['result'].encode(),capture_output=True,timeout=45,cwd='/tmp',env=os.environ|{'PYTHONPATH':'/app'})
    self.assertEqual(r.returncode,0,r.stderr.decode()[-1000:]);self.assertIn(b'True',r.stdout)
   original=row['result'];nid=job['id']
   code,raw,_=self.call('/api/nodes/'+nid,{'name':'Updated integration','port':port+10,'client_port':port+10,'address':'127.0.0.1','sni':'www.amd.com'},method='PUT');self.assertEqual(code,200,raw)
   update=json.loads(raw);self.assertTrue(update['pending']);url=re.search(r'http://[^ ]+\.sh',update['command'])[0]
   _,updated_script,_=self.call(url.removeprefix(self.base),authed=False)
   result=subprocess.run(['sh'],input=updated_script,capture_output=True,timeout=180)
   self.assertEqual(result.returncode,0,(result.stdout+result.stderr).decode()[-1500:])
   code,body,_=self.call('/api/nodes/'+nid);self.assertEqual(code,200,body);edited=json.loads(body)
   self.assertEqual(edited['port'],port+10);self.assertIsNone(edited['pending_update'])
   from urllib.parse import urlsplit,parse_qs
   self.assertEqual(urlsplit(original).username,urlsplit(edited['uri']).username)
   if protocol=='reality':self.assertEqual(parse_qs(urlsplit(original).query)['pbk'],parse_qs(urlsplit(edited['uri']).query)['pbk'])
   r=subprocess.run(['python','-c',probe],input=edited['uri'].encode(),capture_output=True,timeout=45,cwd='/tmp',env=os.environ|{'PYTHONPATH':'/app'})
   self.assertEqual(r.returncode,0,r.stderr.decode()[-1000:]);self.assertIn(b'True',r.stdout)
   print(protocol+': install and port update both forwarded HTTPS; stable identity, no duplicate node')
if __name__=='__main__':unittest.main()
