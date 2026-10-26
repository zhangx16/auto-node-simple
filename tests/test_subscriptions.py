import unittest,json,sqlite3,time,base64,urllib.request,urllib.error,subprocess,tempfile
from pathlib import Path
from test_app import AppTests
ARGO='vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe1@edge.example.com:443?security=tls&type=ws&sni=tunnel.example.com&host=tunnel.example.com&path=%2Fws#Argo'
REALITY='vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe2@1.1.1.1:443?security=reality&type=tcp&flow=xtls-rprx-vision&sni=www.amd.com&pbk=E34sHY3s3Z3w0MnSV7ybTXaPQR5gKU4rxUcd1Rac2hI&sid=0123456789abcdef&fp=chrome#Reality'
HY='hysteria2://fixture-password@1.1.1.1:443?sni=www.amd.com&insecure=1&alpn=h3#Hysteria2'
class SubscriptionTests(unittest.TestCase):
 setUpClass=classmethod(AppTests.setUpClass.__func__)
 tearDownClass=classmethod(AppTests.tearDownClass.__func__)
 call=AppTests.call
 def setUp(self):
  with sqlite3.connect(self.data/'simple.db') as c:
   for ident,uri,kind in [('argo',ARGO,'argo'),('reality',REALITY,'reality'),('hy',HY,'hysteria2')]:c.execute('INSERT OR IGNORE INTO nodes(id,name,protocol,uri,created) VALUES (?,?,?,?,?)',(ident,'同名节点',kind,uri,time.time()))
 def create(self,ids):
  code,raw,_=self.call('/api/subscriptions',{'name':'我的订阅','node_ids':ids});self.assertEqual(code,200,raw);return json.loads(raw)
 def fetch(self,url,ua='Python-urllib/3.12',method='GET'):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':ua},method=method),timeout=10) as r:return r.status,r.read(),r.headers
  except urllib.error.HTTPError as e:return e.code,e.read(),e.headers
 def test_formats_preserve_protocols_names_and_tls(self):
  s=self.create(['argo','reality','hy'])
  _,raw,_=self.fetch(s['urls']['raw']);self.assertEqual(len(raw.splitlines()),3)
  _,encoded,_=self.fetch(s['urls']['base64']);self.assertEqual(base64.b64decode(encoded),raw)
  code,body,headers=self.fetch(s['urls']['clash']);self.assertEqual(code,200)
  d=json.loads(body);self.assertEqual(len(d['proxies']),3);self.assertEqual(len({p['name'] for p in d['proxies']}),3)
  self.assertEqual(d['proxies'][0]['ws-opts']['headers']['Host'],'tunnel.example.com');self.assertEqual(d['proxies'][1]['flow'],'xtls-rprx-vision');self.assertTrue(d['proxies'][2]['skip-cert-verify'])
  _,body,_=self.fetch(s['urls']['singbox']);d=json.loads(body);self.assertEqual(d['outbounds'][1]['transport']['path'],'/ws');self.assertEqual(d['outbounds'][2]['tls']['reality']['short_id'],'0123456789abcdef')
  if Path('/usr/local/bin/sing-box-probe').exists():
   with tempfile.TemporaryDirectory() as temp:
    f=Path(temp)/'config.json';f.write_bytes(body);r=subprocess.run(['/usr/local/bin/sing-box-probe','check','-c',str(f)],capture_output=True)
    self.assertEqual(r.returncode,0,r.stderr.decode())
 def test_selection_update_disabled_deleted_and_public_auth(self):
  s=self.create(['argo']);self.assertEqual(self.call('/api/subscriptions',authed=False)[0],401)
  self.assertEqual(self.call('/api/subscriptions',{'name':'bad','node_ids':['unknown']})[0],422)
  self.assertEqual(self.call('/api/subscriptions',{'name':'bad','node_ids':['argo','argo']})[0],422)
  path='/api/subscriptions/'+s['id']
  self.assertEqual(self.call(path,{'name':'edited','node_ids':['hy']},method='PUT')[0],200)
  self.assertTrue(self.fetch(s['urls']['raw'])[1].startswith(b'hysteria2://'))
  self.assertEqual(self.call(path,{'name':'disabled','node_ids':['hy'],'enabled':False},method='PUT')[0],200);self.assertEqual(self.fetch(s['urls']['raw'])[0],404)
  self.assertEqual(self.call(path,method='DELETE')[0],200)
  self.assertEqual(self.fetch(s['urls']['raw'])[0],404);self.assertEqual(len(json.loads(self.call('/api/nodes')[1])),3)
 def test_client_autodetect_head_and_explicit_format(self):
  s=self.create(['argo']);url=s['urls']['base64'].split('?')[0]
  self.assertIn('proxies',json.loads(self.fetch(url,'clash.meta/v1.19')[1]));self.assertIn('outbounds',json.loads(self.fetch(url,'sing-box/1.14')[1]))
  self.assertTrue(base64.b64decode(self.fetch(s['urls']['base64'],'clash.meta')[1]).startswith(b'vless://'))
  code,body,h=self.fetch(url,'clash.meta',method='HEAD');self.assertEqual(code,200);self.assertEqual(body,b'');self.assertEqual(h['X-Subscription-Node-Count'],'1')
  bad=url[:-1]+('x' if url[-1]!='x' else 'y');self.assertEqual(self.fetch(bad)[0],404)
 def test_installed_nodes_persist_after_old_task_cleanup(self):
  s=self.create(['argo'])
  with sqlite3.connect(self.data/'simple.db') as c:c.execute("INSERT INTO installs(id,token_hash,config,status,created,result) VALUES ('oldtask','hash','{}','done',?,'')",(time.time()-172800,))
  self.assertEqual(self.call('/api/installs',{'protocol':'reality','address':'1.1.1.1'})[0],200)
  self.assertEqual(self.fetch(s['urls']['raw'])[0],200)

class EarlyDataTests(unittest.TestCase):
 def test_migrated_argo_preserves_early_data_without_sending_it_as_ws_path(self):
  from app.formats import parse,clash,singbox
  uri=ARGO.replace('path=%2Fws','path=%2Fws%3Fed%3D2560')
  p=parse(uri);self.assertEqual(p['path'],'/ws');self.assertEqual(p['early_data'],2560)
  c=clash(p);s=singbox(p)
  self.assertEqual(c['ws-opts']['max-early-data'],2560);self.assertEqual(c['ws-opts']['path'],'/ws')
  self.assertEqual(s['transport']['max_early_data'],2560);self.assertEqual(s['transport']['early_data_header_name'],'Sec-WebSocket-Protocol')
  self.assertEqual(s['transport']['headers']['Host'],'tunnel.example.com')
