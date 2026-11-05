"""Offline transport contract and authenticated ASGI integration tests (no sockets)."""
import asyncio
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from app import vpsnat, main

class Reply(io.BytesIO):
    status = 200

class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'VPSNAT_API_KEY': 'vk_live_fixture_private'})
        self.env.start()
    def tearDown(self):
        self.env.stop()
    def reply(self, data):
        return Reply(json.dumps(data).encode())
    def test_transport_auth_idempotency_and_timeout(self):
        with patch('urllib.request.OpenerDirector.open', return_value=self.reply({'success': True})) as call:
            vpsnat.request('POST', '/vms/123/port-forwards', {'internal_port': 18080, 'protocol': 'tcp'}, 'fixture-key-0001')
        req = call.call_args.args[0]
        self.assertEqual(req.full_url, vpsnat.BASE+'/vms/123/port-forwards')
        self.assertEqual(req.get_header('Authorization'), 'Bearer vk_live_fixture_private')
        self.assertEqual(req.get_header('Idempotency-key'), 'fixture-key-0001')
        self.assertEqual(call.call_args.kwargs['timeout'], 20)
        self.assertEqual(json.loads(req.data)['internal_port'], 18080)
        for key in (None, 'short', 'bad\nheader'):
            with self.assertRaises(vpsnat.APIError):vpsnat.request('POST', '/vms/123/start', key=key)
    def test_redirects_and_errors_never_disclose_secrets(self):
        self.assertIsNone(vpsnat.NoRedirect().redirect_request(None,None,302,'',None,'https://other.example'))
        for code in (302,401,403,404,422,429,500):
            err=urllib.error.HTTPError(vpsnat.BASE,code,'vk_live_fixture_private',{},io.BytesIO(b'{"password":"secret"}'))
            with patch('urllib.request.OpenerDirector.open',side_effect=err):
                with self.assertRaises(vpsnat.APIError) as caught:vpsnat.vms()
            self.assertNotIn('secret',str(caught.exception));self.assertNotIn('vk_live',str(caught.exception))
    def test_wrapped_lists_and_private_field_filter(self):
        row={'id':123,'remark':'fixture','public_ip':'203.0.113.10','password':'must-not-leak','api_key':'must-not-leak'}
        for envelope in ([row],{'vms':[row]},{'success':True,'data':[row]},{'data':{'items':[row]}}):
            with patch.object(vpsnat,'request',return_value=envelope):result=vpsnat.vms()
            self.assertEqual(result[0]['id'],'123');self.assertNotIn('must-not-leak',json.dumps(result))
        with patch.object(vpsnat,'request',side_effect=[{'vms':[row],'total':2},{'vms':[row|{'id':124}],'total':2}]) as request:
            self.assertEqual(len(vpsnat.vms()),2)
            self.assertIn('page=2',request.call_args.args[1])
        for ident in ('../user/me','0','12?foo','abc'):
            with self.assertRaises(vpsnat.APIError):vpsnat.identifier(ident)
    def test_port_range_problem_is_safe_and_confirmed_rejections_are_cached(self):
        raw=json.dumps({'code':'PORT_OUT_OF_RANGE','detail':'Host port must be within 50000-60000'}).encode()
        error=urllib.error.HTTPError(vpsnat.BASE,400,'bad',{},io.BytesIO(raw))
        with patch('urllib.request.OpenerDirector.open',side_effect=error):
            with self.assertRaises(vpsnat.APIError) as caught:vpsnat.request('POST','/vms/123/port-forwards',{},'fixture-range-key')
        self.assertEqual(caught.exception.port_range,(50000,60000));self.assertEqual(caught.exception.http_status,400)
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'DATA_DIR':tmp}),patch.object(vpsnat,'request',side_effect=caught.exception) as request:
            for _ in range(2):
                with self.assertRaises(vpsnat.APIError) as replay:vpsnat.mutate('POST','/vms/123/port-forwards',{},'fixture-range-key')
                self.assertEqual(list(replay.exception.port_range),[50000,60000])
            self.assertEqual(request.call_count,1)
    def test_mapping_validation_and_no_content_delete(self):
        row={'id':7,'guest_port':18080,'host_port':28080,'protocol':'TCP'}
        with patch.object(vpsnat,'request',return_value={'data':{'port_forwards':[row]}}):
            self.assertEqual(vpsnat.forwards('123')[0]['protocol'],'tcp')
        with patch.object(vpsnat,'request',return_value={'data':[row|{'host_port':70000}]}):
            with self.assertRaises(vpsnat.APIError):vpsnat.forwards('123')
        reply=Reply(b'');reply.status=204
        with patch('urllib.request.OpenerDirector.open',return_value=reply):
            self.assertTrue(vpsnat.request('DELETE','/vms/123/port-forwards/7',key='fixture-delete-key')['success'])
    def test_bad_response_and_network_failure(self):
        for raw in (b'not json',b'null',b'{"success":false}',b'x'*(vpsnat.LIMIT+1)):
            with patch('urllib.request.OpenerDirector.open',return_value=Reply(raw)):
                with self.assertRaises(vpsnat.APIError):vpsnat.vms()
        with patch('urllib.request.OpenerDirector.open',side_effect=OSError('secret')):
            with self.assertRaises(vpsnat.APIError) as caught:vpsnat.vms()
        self.assertNotIn('secret',str(caught.exception))

    def test_local_mutations_deduplicate_and_block_unknown_replays(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'DATA_DIR':tmp}):
            with patch.object(vpsnat,'request',return_value={'id':7}) as request:
                first=vpsnat.mutate('POST','/vms/123/port-forwards',{'guest_port':18080},'fixture-dedup-key')
                self.assertEqual(vpsnat.mutate('POST','/vms/123/port-forwards',{'guest_port':18080},'fixture-dedup-key'),first)
                self.assertEqual(request.call_count,1)
                with self.assertRaises(vpsnat.APIError):vpsnat.mutate('POST','/vms/123/start',None,'fixture-dedup-key')
            with patch.object(vpsnat,'request',side_effect=vpsnat.APIError('timeout')) as request:
                with self.assertRaises(vpsnat.APIError):vpsnat.mutate('POST','/vms/123/restart',key='fixture-unknown-key')
                with self.assertRaises(vpsnat.APIError) as caught:vpsnat.mutate('POST','/vms/123/restart',key='fixture-unknown-key')
                self.assertEqual(caught.exception.status,409);self.assertEqual(request.call_count,1)

    def test_private_ip_uses_only_unambiguous_public_entry(self):
        row={'id':123,'public_ip':'10.1.2.3','region':'fixture-region'}
        with patch.object(vpsnat,'request',side_effect=[row,{'machines':[{'region':'fixture-region','entry_ip':'1.1.1.1'}]}]):
            self.assertEqual(vpsnat.vm('123')['public_ip'],'1.1.1.1')
        with patch.object(vpsnat,'request',side_effect=[row,{'machines':[{'region':'fixture-region','entry_ip':'1.1.1.1'},{'region':'fixture-region','entry_ip':'8.8.8.8'}]}]):
            result=vpsnat.vm('123');self.assertEqual(result['public_ip'],'');self.assertEqual(len(result['entry_candidates']),2)

class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=Path(self.tmp.name)
        # Copy only the schema, never production node data.
        self.old=main.DATA;main.DATA=self.data
        with main.conn() as c:
            c.executescript("CREATE TABLE installs(id TEXT PRIMARY KEY,token_hash TEXT,config TEXT,status TEXT,created REAL,result TEXT DEFAULT '',error TEXT DEFAULT '',target_node TEXT DEFAULT '');")
        self.cookie='simple_session='+main.signer.dumps({'user':'fixture'})
    def tearDown(self):
        main.DATA=self.old;self.tmp.cleanup()
    def call(self,path,body=None,method='GET',authed=True,origin=None,key=None):
        async def run():
            headers=[(b'content-type',b'application/json')]
            if authed:headers.append((b'cookie',self.cookie.encode()))
            if origin:headers.append((b'origin',origin.encode()))
            if key:headers.append((b'idempotency-key',key.encode()))
            scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,'scheme':'https',
                   'path':path,'raw_path':path.encode(),'query_string':b'','headers':headers,'client':('127.0.0.1',1),'server':('test',443)}
            received=False;messages=[]
            async def receive():
                nonlocal received
                if received:return {'type':'http.disconnect'}
                received=True;return {'type':'http.request','body':json.dumps(body).encode() if body is not None else b'','more_body':False}
            async def send(message):messages.append(message)
            async def inline(func,*args,**kwargs):return func(*args,**kwargs)
            with patch('fastapi.routing.run_in_threadpool',inline):
                await main.app(scope,receive,send)
            status=next(m['status'] for m in messages if m['type']=='http.response.start')
            raw=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
            return status,json.loads(raw)
        return asyncio.run(run())
    def test_all_platform_routes_require_owner_and_csrf(self):
        paths=[('/api/vpsnat/status','GET'),('/api/vpsnat/vms','GET'),('/api/vpsnat/vms/123','GET'),
               ('/api/vpsnat/vms/123/port-forwards','GET'),('/api/vpsnat/vms/123/port-forwards','POST'),
               ('/api/vpsnat/vms/123/port-forwards/7','DELETE'),('/api/vpsnat/vms/123/port-forwards/7/change','POST'),
               ('/api/vpsnat/vms/123/actions/start','POST')]
        for path,method in paths:
            body={'internal_port':18080} if path.endswith('port-forwards') and method=='POST' else None
            self.assertEqual(self.call(path,body,method,authed=False)[0],401,path)
            if method!='GET':self.assertEqual(self.call(path,body,method,origin='https://evil.example')[0],403,path)
    def test_write_route_forwards_key_and_rejects_unsupported_actions(self):
        with patch.object(vpsnat,'mutate',return_value={'success':True}) as request:
            code,_=self.call('/api/vpsnat/vms/123/port-forwards',{'internal_port':18080,'protocol':'tcp'},'POST',key='fixture-create-key')
            self.assertEqual(code,200);self.assertEqual(request.call_args.args,('POST','/vms/123/port-forwards',{'guest_port':18080,'protocol':'tcp'},'fixture-create-key'))
            code,_=self.call('/api/vpsnat/vms/123/port-forwards/7/change',method='POST',key='fixture-change-key')
            self.assertEqual(code,200);self.assertEqual(request.call_args.args[1],'/vms/123/port-forwards/7/change')
        self.assertEqual(self.call('/api/vpsnat/vms/123/actions/renew',method='POST')[0],422)
        self.assertEqual(self.call('/api/vpsnat/vms/123/port-forwards',{'internal_port':70000},'POST')[0],422)
    def test_platform_direct_install_uses_current_mapping(self):
        with patch.object(vpsnat,'vm',return_value={'id':'123','public_ip':'203.0.113.10'}),patch.object(vpsnat,'forwards',return_value=[{'id':'7','internal_port':18080,'public_port':28080,'protocol':'tcp','public_ip':''}]):
            body={'protocol':'reality','platform_vm_id':'123','platform_forward_id':'7','port':18080,'client_port':1,'address':'old.example'}
            code,result=self.call('/api/installs',body,'POST');self.assertEqual(code,200,result)
            with main.conn() as c:cfg=json.loads(c.execute('SELECT config FROM installs WHERE id=?',(result['id'],)).fetchone()[0])
            self.assertEqual(cfg['client_port'],28080);self.assertEqual(cfg['address'],'203.0.113.10');self.assertEqual(cfg['platform_vm_id'],'123')
            self.assertEqual(self.call('/api/installs',body|{'protocol':'hysteria2'},'POST')[0],422)
            self.assertEqual(self.call('/api/installs',body|{'port':18081},'POST')[0],422)
            self.assertEqual(self.call('/api/installs',body|{'platform_vm_id':''},'POST')[0],422)
    def test_platform_argo_reuses_installer_without_port_mapping(self):
        with patch.object(vpsnat,'vm',return_value={'id':'123'}),patch.object(vpsnat,'forwards') as forwards:
            code,result=self.call('/api/installs',{'protocol':'argo','platform_vm_id':'123','cf_mode':'manual','tunnel_token':'fixture-token','tunnel_hostname':'fixed.example.com'},'POST')
            self.assertEqual(code,200,result);self.assertIn('command',result);forwards.assert_not_called()
    def test_remote_endpoints_enforce_auth_and_origin_before_connecting(self):
        from app import remote_install
        probe={'host':'example.com','forward_id':'7'}
        deploy={'install':{'platform_vm_id':'123'},'ssh':probe}
        with patch.object(remote_install,'probe') as ssh_probe,patch.object(remote_install,'start') as start:
            for path,body in [('/api/vpsnat/vms/123/ssh-probe',probe),('/api/vpsnat/vms/123/deploy',deploy)]:
                self.assertEqual(self.call(path,body,'POST',authed=False)[0],401)
                self.assertEqual(self.call(path,body,'POST',origin='https://evil.example')[0],403)
            ssh_probe.assert_not_called();start.assert_not_called()

    def test_automation_requires_owner_origin_and_valid_parameters(self):
        from app import platform_automation
        path='/api/vpsnat/automation/argo'
        with patch.object(platform_automation,'start') as start:
            self.assertEqual(self.call(path,{'base_port':38001},'POST',authed=False)[0],401)
            self.assertEqual(self.call(path,{'base_port':38001},'POST',origin='https://evil.example')[0],403)
            self.assertEqual(self.call(path,{'base_port':70000},'POST')[0],422)
            start.assert_not_called()
        self.assertEqual(self.call(path,authed=False)[0],401)

    def test_upstream_failure_becomes_safe_api_error(self):
        with patch.object(vpsnat,'vms',side_effect=vpsnat.APIError('平台密钥缺少所需权限')):
            self.assertEqual(self.call('/api/vpsnat/vms'),(502,{'detail':'平台密钥缺少所需权限'}))

if __name__=='__main__':unittest.main()
