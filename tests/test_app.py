import unittest,tempfile,subprocess,os,socket,time,json,urllib.request,urllib.error,hashlib,re
from pathlib import Path
from itsdangerous import URLSafeTimedSerializer
class AppTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.data=Path(cls.tmp.name);salt='fixture-salt';cls.data.joinpath('auth.json').write_text(json.dumps({'username':'admin','salt':salt,'hash':hashlib.pbkdf2_hmac('sha256',b'test-password',salt.encode(),120000).hex()}))
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  cls.base='http://127.0.0.1:'+str(port);cls.root=Path(__file__).resolve().parents[1]
  cls.proc=subprocess.Popen(['python','-m','uvicorn','app.main:app','--host','127.0.0.1','--port',str(port)],cwd=cls.root,env=os.environ|{'DATA_DIR':str(cls.data),'ASSET_DIR':str(cls.root/'assets'),'PANEL_URL':cls.base},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for _ in range(100):
   try:urllib.request.urlopen(cls.base+'/health',timeout=.2);break
   except Exception:time.sleep(.05)
  else:raise RuntimeError('App failed to start')
  cls.cookie='simple_session='+URLSafeTimedSerializer((cls.data/'session.key').read_text(),salt='simple-session').dumps({'user':'admin'})
 @classmethod
 def tearDownClass(cls):cls.proc.terminate();cls.proc.wait(timeout=10);cls.tmp.cleanup()
 def call(self,path,body=None,authed=True,origin=None,method=None):
  headers={'Content-Type':'application/json'}
  if authed:headers['Cookie']=self.cookie
  if origin:headers['Origin']=origin
  req=urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
  try:
   with urllib.request.urlopen(req,timeout=10) as r:return r.status,r.read(),r.headers
  except urllib.error.HTTPError as e:return e.code,e.read(),e.headers
 def test_auth_and_validation(self):
  self.assertEqual(self.call('/api/installs',authed=False)[0],401)
  self.assertEqual(self.call('/api/installs',{'protocol':'reality'})[0],422)
  self.assertEqual(self.call('/api/installs',{'protocol':'argo','tunnel':'fixed'})[0],422)
  self.assertEqual(self.call('/api/installs',{'protocol':'argo','tunnel':'temporary'})[0],422)
  self.assertEqual(self.call('/api/installs',{'address':'bad;command'})[0],422)
  self.assertEqual(self.call('/api/installs',{},origin='https://evil.example')[0],403)
  code,body,headers=self.call('/api/login',{'username':'admin','password':'test-password'},authed=False)
  self.assertEqual(code,200);self.assertIn('Secure',headers['Set-Cookie']);self.assertIn('HttpOnly',headers['Set-Cookie'])
  self.assertEqual(self.call('/api/login',{'username':'admin','password':'wrong'},authed=False)[0],401)
 def test_three_scripts_callbacks_replay_and_no_management(self):
  for protocol in ['argo','reality','hysteria2']:
   code,body,_=self.call('/api/installs',{'protocol':protocol,'address':'1.1.1.1','port':18443,'tunnel':'fixed','tunnel_token':'fixture-token','tunnel_hostname':'fixed.example.com'});self.assertEqual(code,200,body);job=json.loads(body)
   subprocess.run(['sh','-n'],input=job['command'].encode(),check=True)
   url=re.search(r'http://[^ ]+\.sh',job['command'])[0];path=url.removeprefix(self.base)
   code,script,_=self.call(path,authed=False);self.assertEqual(code,200);subprocess.run(['sh','-n'],input=script,check=True)
   self.assertNotIn(b'crontab ',script);self.assertNotIn(b'/agent/',script)
   result=path.replace('/install/','/result/').removesuffix('.sh')
   link='vless://341cad6d-9bf5-4b09-9c96-a7879ebc8fe1@1.1.1.1:443?security=tls&type=ws&sni=edge.example.com&host=edge.example.com&path=%2Fws#result'
   self.assertEqual(self.call(result,{'link':link},authed=False)[0],200)
   self.assertEqual(self.call(path,authed=False)[0],410)
   self.assertEqual(self.call(result,{'link':'vless://changed'},authed=False)[0],200)
   rows=json.loads(self.call('/api/installs')[1]);self.assertEqual(next(r for r in rows if r['id']==job['id'])['result'],link)
 def test_assets_allowlist_and_secret_boundaries(self):
  self.assertEqual(self.call('/assets/session.key',authed=False)[0],404)
  self.assertEqual(self.call('/static/../data/auth.json',authed=False)[0],404)
  rows=json.loads(self.call('/api/installs')[1]);self.assertTrue(all('config' not in r and 'token_hash' not in r for r in rows))
