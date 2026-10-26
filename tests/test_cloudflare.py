import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from starlette.requests import Request
from fastapi import HTTPException
from app import main,cloudflare as cf

class CloudflareTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
  self.patches=[patch.object(cf,'DATA',self.data),patch.object(main,'DATA',self.data)]
  for p in self.patches:p.start()
  self.cfg={'account_id':'a'*32,'zone_id':'b'*32,'zone_name':'example.com','api_token':'fixture-private-api-token'}
  (self.data/'cloudflare.json').write_text(json.dumps(self.cfg))
  with cf.db() as c:
   cf.init(c);c.execute("CREATE TABLE installs(id TEXT PRIMARY KEY,token_hash TEXT,config TEXT,status TEXT,created REAL,result TEXT DEFAULT '',error TEXT DEFAULT '')")
  cookie='simple_session='+main.signer.dumps({'user':'fixture'})
  self.request=Request({'type':'http','method':'POST','path':'/api/installs','headers':[(b'cookie',cookie.encode())]})
 def tearDown(self):
  for p in reversed(self.patches):p.stop()
  self.tmp.cleanup()
 def test_default_status_is_private_and_generated_host_is_restricted(self):
  self.assertEqual(cf.status(),{'configured':True,'zone_name':'example.com'})
  self.assertEqual(cf.hostname('', 'fixture'),'node-fixture.example.com')
  for host in ['other.org','example.com','a.b.example.com','-bad.example.com']:
   with self.assertRaises(cf.CFError):cf.hostname(host,'fixture')
  self.assertNotIn(self.cfg['api_token'],json.dumps(main.cf_status(self.request)))
 def test_provision_sets_route_and_creates_only_unoccupied_dns(self):
  calls=[]
  def api(method,path,body=None,missing_ok=False):
   calls.append((method,path,body))
   if method=='GET' and 'dns_records?' in path:return []
   if method=='POST' and 'cfd_tunnel' in path:return {'id':'tunnel-fixture'}
   if path.endswith('/token'):return 'fixture-connector-token-long-enough'
   if method=='POST' and 'dns_records' in path:return {'id':'dns-fixture'}
   return {}
  with patch.object(cf,'api',side_effect=api):result=cf.provision('job','custom.example.com',33001)
  self.assertEqual(result['hostname'],'custom.example.com');self.assertGreater(len(result['tunnel_token']),20)
  route=next(body for method,path,body in calls if method=='PUT')
  self.assertEqual(route['config']['ingress'][0]['service'],'http://127.0.0.1:33001');self.assertEqual(cf.binding('job')['status'],'ready')
  with patch.object(cf,'api',return_value=[{'id':'existing'}]) as api:
   with self.assertRaises(cf.CFError):cf.provision('other','occupied.example.com',18080)
   self.assertEqual(api.call_count,1);self.assertIsNone(cf.binding('other'))
 def test_failed_route_cleans_owned_tunnel_and_records_no_live_binding(self):
  deleted=[]
  def api(method,path,body=None,missing_ok=False):
   if method=='DELETE':deleted.append(path);return None
   if 'dns_records?' in path:return []
   if method=='POST':return {'id':'owned-tunnel'}
   if path.endswith('/token'):return 'fixture-connector-token-long-enough'
   if method=='PUT':raise cf.CFError('fixture route failure')
   return {}
  with patch.object(cf,'api',side_effect=api):
   with self.assertRaises(cf.CFError):cf.provision('job','custom.example.com',18080)
  self.assertEqual(len(deleted),1);self.assertTrue(deleted[0].endswith('/owned-tunnel'));self.assertIsNone(cf.binding('job'))
 def test_cleanup_refuses_changed_dns(self):
  with cf.db() as c:c.execute('INSERT INTO fixed_tunnels VALUES (?,?,?,?,?)',('job','custom.example.com','owned','dns-owned','ready'))
  with patch.object(cf,'api',return_value={'name':'custom.example.com','type':'CNAME','content':'another.cfargotunnel.com'}) as api:
   with self.assertRaises(cf.CFError):cf.cleanup('job')
   self.assertEqual(api.call_count,1);self.assertIsNotNone(cf.binding('job'))
 def test_create_command_uses_default_without_returning_any_cf_secret(self):
  with patch.object(cf,'provision',return_value={'hostname':'assigned.example.com','tunnel_token':'fixture-connector-token'}):r=main.create(main.Install(name='Default Argo'),self.request)
  self.assertEqual(r['hostname'],'assigned.example.com');self.assertNotIn('fixture-connector-token',json.dumps(r));self.assertNotIn(self.cfg['api_token'],json.dumps(r))
  with cf.db() as c:cfg=json.loads(c.execute('SELECT config FROM installs WHERE id=?',(r['id'],)).fetchone()[0])
  self.assertEqual(cfg['tunnel_hostname'],'assigned.example.com');self.assertEqual(cfg['tunnel_token'],'fixture-connector-token')
  self.assertEqual(cfg['address'],'www.cloudflare.com')
 def test_custom_address_wins_and_direct_address_is_not_defaulted(self):
  r=main.create(main.Install(cf_mode='manual',tunnel_hostname='manual.example.com',tunnel_token='fixture-token',address='custom.example.com'),self.request)
  with cf.db() as c:cfg=json.loads(c.execute('SELECT config FROM installs WHERE id=?',(r['id'],)).fetchone()[0])
  self.assertEqual(cfg['address'],'custom.example.com')
  with self.assertRaises(HTTPException) as e:main.create(main.Install(protocol='reality'),self.request)
  self.assertEqual(e.exception.status_code,422)
 def test_manual_and_direct_modes_do_not_call_default_api(self):
  with patch.object(cf,'provision') as provision:
   main.create(main.Install(protocol='reality',address='1.1.1.1'),self.request)
   main.create(main.Install(cf_mode='manual',tunnel_hostname='manual.example.com',tunnel_token='fixture-manual-token'),self.request)
   provision.assert_not_called()
 def test_default_failure_is_visible_and_not_returned_as_ready_command(self):
  with patch.object(cf,'provision',side_effect=cf.CFError('fixture rejected')):
   with self.assertRaises(HTTPException) as e:main.create(main.Install(),self.request)
   self.assertEqual(e.exception.status_code,502)
  with cf.db() as c:row=c.execute('SELECT status,error FROM installs').fetchone()
  self.assertEqual(row['status'],'failed');self.assertEqual(row['error'],'fixture rejected')
 def test_port_change_preserves_other_ingress_entries(self):
  with cf.db() as c:c.execute('INSERT INTO fixed_tunnels VALUES (?,?,?,?,?)',('job','custom.example.com','owned','dns-owned','ready'))
  calls=[]
  def api(method,path,body=None,missing_ok=False):
   calls.append((method,path,body))
   if method=='GET':return {'config':{'ingress':[{'hostname':'custom.example.com','service':'http://127.0.0.1:18080'},{'hostname':'other.example.com','service':'http://127.0.0.1:19000'},{'service':'http_status:404'}]}}
   return {}
  with patch.object(cf,'api',side_effect=api):
   self.assertEqual(cf.read_port('job'),18080);cf.update_port('job',18081)
  body=next(b for method,path,b in calls if method=='PUT')
  self.assertEqual(body['config']['ingress'][0]['service'],'http://127.0.0.1:18081');self.assertEqual(body['config']['ingress'][1]['service'],'http://127.0.0.1:19000')
