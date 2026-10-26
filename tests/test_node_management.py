import base64,json,re,shlex,unittest,sqlite3,subprocess,sys,os
from urllib.parse import quote,urlsplit,parse_qs
from test_app import AppTests
PUB='E34sHY3s3Z3w0MnSV7ybTXaPQR5gKU4rxUcd1Rac2hI'
class NodeManagementTests(unittest.TestCase):
 setUpClass=classmethod(AppTests.setUpClass.__func__)
 tearDownClass=classmethod(AppTests.tearDownClass.__func__)
 call=AppTests.call
 def prepare(self,kind='argo'):
  code,raw,_=self.call('/api/installs',{'name':'Node','protocol':kind,'address':'1.1.1.1','port':18080,'client_port':443,'cf_mode':'manual','tunnel_token':'fixture-token','tunnel_hostname':'fixed.example.com'});self.assertEqual(code,200,raw)
  return self.command(json.loads(raw))
 def command(self,r):
  path=re.search(r'http://[^ ]+\.sh',r['command'])[0].removeprefix(self.base)
  code,script,_=self.call(path,authed=False);self.assertEqual(code,200,script)
  line=next(s for s in script.decode().splitlines() if s.startswith('PAYLOAD='));cfg=json.loads(base64.b64decode(shlex.split(line.split('=',1)[1])[0]))
  return r['id'],cfg,path.replace('/install/','/result/').removesuffix('.sh'),script
 def uri(self,cfg):
  auth=cfg['credential'];addr=cfg['address'];port=cfg['client_port'];name=quote(cfg['name'])
  if cfg['protocol']=='argo':return f'vless://{auth}@{addr}:{port}?security=tls&type=ws&sni=fixed.example.com&host=fixed.example.com&path={quote(cfg["path"],safe="")}#{name}-CF'
  if cfg['protocol']=='reality':return f'vless://{auth}@{addr}:{port}?security=reality&type=tcp&flow=xtls-rprx-vision&sni={cfg["sni"]}&pbk={PUB}&sid=0123456789abcdef#{name}-Vless'
  return f'hysteria2://{auth}@{addr}:{port}?sni={cfg["sni"]}&insecure=1&alpn=h3#{name}-hy2'
 def installed(self,kind='argo'):
  ident,cfg,result,_=self.prepare(kind);self.assertEqual(self.call(result,{'link':self.uri(cfg)},authed=False)[0],200)
  return ident,cfg
 def editbody(self,cfg,**changes):return {k:changes.get(k,cfg[k]) for k in ['name','port','client_port','address','sni']}
 def node(self,ident):
  code,body,_=self.call('/api/nodes/'+ident);self.assertEqual(code,200,body);return json.loads(body)
 def test_link_changes_preserve_auth_query_and_membership_and_hide_private_config(self):
  ident,cfg=self.installed();before=self.node(ident)
  code,raw,_=self.call('/api/subscriptions',{'name':'Node edits','node_ids':[ident]});self.assertEqual(code,200)
  sub=json.loads(raw)
  code,raw,_=self.call('/api/nodes/'+ident,self.editbody(cfg,name='Renamed',address='edge.example.com',client_port=8443,sni='fixed.example.com'),method='PUT');self.assertEqual(code,200,raw);self.assertFalse(json.loads(raw)['pending'])
  n=self.node(ident);self.assertEqual(n['name'],'Renamed-CF');self.assertEqual(urlsplit(n['uri']).query,urlsplit(before['uri']).query);self.assertEqual(urlsplit(n['uri']).username,urlsplit(before['uri']).username);self.assertEqual(n['client_port'],8443)
  self.assertNotIn('config',n);self.assertNotIn('fixture-token',json.dumps(n))
  import urllib.request
  with urllib.request.urlopen(sub['urls']['raw']) as r:self.assertIn(b'edge.example.com:8443',r.read())
 def test_remote_update_keeps_old_record_until_callback_and_reuses_node_id(self):
  ident,cfg=self.installed('reality');before=self.node(ident)
  code,raw,_=self.call('/api/nodes/'+ident,self.editbody(cfg,port=18081,client_port=18081,name='Updated'),method='PUT');self.assertEqual(code,200,raw);r=json.loads(raw);self.assertTrue(r['pending'])
  job,new,result,script=self.command(r);self.assertNotEqual(job,ident);self.assertEqual(new['id'],ident);self.assertIn(('NODE_ID='+ident).encode(),script)
  self.assertEqual(self.node(ident)['uri'],before['uri']);self.assertEqual(self.node(ident)['port'],18080)
  self.assertEqual(self.call('/api/nodes/'+ident,self.editbody(cfg),method='PUT')[0],409)
  self.assertEqual(self.call(result,{'link':self.uri(new)},authed=False)[0],200)
  after=self.node(ident);self.assertEqual(after['port'],18081);self.assertEqual(after['client_port'],18081);self.assertIsNone(after['pending_update'])
  self.assertEqual(urlsplit(after['uri']).username,urlsplit(before['uri']).username)
  rows=json.loads(self.call('/api/nodes')[1]);self.assertNotIn(job,[n['id'] for n in rows])
 def test_failed_or_cancelled_update_does_not_replace_live_node(self):
  ident,cfg=self.installed('hysteria2');before=self.node(ident)
  code,raw,_=self.call('/api/nodes/'+ident,self.editbody(cfg,port=18082),method='PUT');self.assertEqual(code,200,raw);_,new,result,_=self.command(json.loads(raw))
  self.assertEqual(self.call(result,{'error':'fixture failed'},authed=False)[0],200);self.assertEqual(self.node(ident)['uri'],before['uri']);self.assertEqual(self.node(ident)['pending_update']['status'],'failed')
  self.assertEqual(self.call('/api/nodes/'+ident+'/cancel-update',{})[0],200);self.assertIsNone(self.node(ident)['pending_update'])
  self.assertEqual(self.call(result,{'link':self.uri(new)},authed=False)[0],409)
 def test_invalid_auth_or_reality_key_cannot_overwrite_old_node(self):
  ident,cfg=self.installed('reality');before=self.node(ident)
  code,raw,_=self.call('/api/nodes/'+ident,self.editbody(cfg,port=18081),method='PUT');self.assertEqual(code,200,raw);_,new,result,_=self.command(json.loads(raw))
  changed=self.uri(new).replace(PUB,'ChangedPublicKey');self.assertEqual(self.call(result,{'link':changed},authed=False)[0],409)
  self.assertEqual(self.node(ident)['uri'],before['uri'])
 def test_auth_and_missing_direct_address(self):
  ident,cfg=self.installed('reality')
  self.assertEqual(self.call('/api/nodes/'+ident,authed=False)[0],401)
  self.assertEqual(self.call('/api/nodes/'+ident,self.editbody(cfg,address=''),method='PUT')[0],422)
  self.assertEqual(self.call('/api/nodes/'+ident,self.editbody(cfg),method='PUT',origin='https://evil.example')[0],403)
 def test_delete_removes_memberships_and_jobs_without_restoring_on_startup(self):
  ident,cfg=self.installed();other,_=self.installed('reality')
  code,raw,_=self.call('/api/subscriptions',{'name':'Delete node','node_ids':[ident,other]});self.assertEqual(code,200)
  sub=json.loads(raw)
  code,raw,_=self.call('/api/subscriptions',{'name':'Only deleted node','node_ids':[ident]});self.assertEqual(code,200)
  empty=json.loads(raw)
  # Keep the ownership record for separately retiring Cloudflare resources.
  with sqlite3.connect(self.data/'simple.db') as c:
   c.execute('INSERT INTO fixed_tunnels(install_id,hostname) VALUES (?,?)',(ident,ident+'.example.com'))
  self.assertEqual(self.call('/api/nodes/'+ident,method='DELETE')[0],200)
  self.assertEqual(self.call('/api/nodes/'+ident)[0],404)
  subs=json.loads(self.call('/api/subscriptions')[1])
  self.assertEqual(next(s for s in subs if s['id']==sub['id'])['node_ids'],[other])
  self.assertEqual(next(s for s in subs if s['id']==empty['id'])['node_ids'],[])
  import urllib.request
  with urllib.request.urlopen(sub['urls']['raw']) as r:self.assertEqual(r.read().decode().strip(),self.node(other)['uri'])
  # Run the real startup migration against this isolated database.
  subprocess.run([sys.executable,'-c','import app.main'],cwd=self.root,env=os.environ|{'DATA_DIR':str(self.data)},check=True,capture_output=True)
  self.assertEqual(self.call('/api/nodes/'+ident)[0],404)
  with sqlite3.connect(self.data/'simple.db') as c:
   self.assertEqual(c.execute('SELECT count(*) FROM installs WHERE id=? OR target_node=?',(ident,ident)).fetchone()[0],0)
   self.assertEqual(c.execute('SELECT count(*) FROM fixed_tunnels WHERE install_id=?',(ident,)).fetchone()[0],1)
   self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])
 def test_delete_revokes_original_and_update_callbacks_and_scripts(self):
  ident,cfg,result,_=self.prepare('hysteria2')
  self.assertEqual(self.call(result,{'link':self.uri(cfg)},authed=False)[0],200)
  code,raw,_=self.call('/api/nodes/'+ident,self.editbody(cfg,port=18081),method='PUT');self.assertEqual(code,200,raw)
  job,new,update_result,_=self.command(json.loads(raw))
  self.assertEqual(self.call('/api/nodes/'+ident,method='DELETE')[0],200)
  for callback,link in [(result,self.uri(cfg)),(update_result,self.uri(new))]:
   self.assertEqual(self.call(callback,{'link':link},authed=False)[0],404)
   self.assertEqual(self.call(callback.replace('/result/','/install/')+'.sh',authed=False)[0],404)
  with sqlite3.connect(self.data/'simple.db') as c:
   self.assertEqual(c.execute('SELECT count(*) FROM node_updates WHERE node_id=?',(ident,)).fetchone()[0],0)
  self.assertNotIn(job,[r['id'] for r in json.loads(self.call('/api/installs')[1])])
 def test_delete_auth_origin_and_missing_node(self):
  ident,_=self.installed('reality');path='/api/nodes/'+ident
  self.assertEqual(self.call(path,method='DELETE',authed=False)[0],401)
  self.assertEqual(self.call(path,method='DELETE',origin='https://evil.example')[0],403)
  self.assertEqual(self.node(ident)['id'],ident)
  self.assertEqual(self.call(path,method='DELETE')[0],200)
  self.assertEqual(self.call(path,method='DELETE')[0],404)
