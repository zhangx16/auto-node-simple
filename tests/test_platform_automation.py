import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from fastapi import HTTPException
from app import main,platform_automation as auto,vpsnat

class AutomationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=main.DATA;main.DATA=Path(self.tmp.name)
  with main.conn() as c:
   c.execute('CREATE TABLE installs(id TEXT PRIMARY KEY,status TEXT,created REAL)');c.execute('CREATE TABLE nodes(id TEXT PRIMARY KEY)');auto.init(c)
 def tearDown(self):main.DATA=self.old;self.tmp.cleanup()
 def test_port_selection_checks_both_sides_and_live_listeners(self):
  rows=[{'internal_port':38001,'public_port':40001},{'internal_port':10001,'public_port':38002}]
  self.assertEqual(auto.select_port(38001,rows,{38003}),38004)
  with self.assertRaises(HTTPException):auto.select_port(65535,[],{65535})
 def test_password_is_durable_before_reset_and_not_rotated_on_retry(self):
  path=auto.profile_path('123',38001);profile={}
  def request(*args):
   saved=json.loads(path.read_text());self.assertEqual(saved['reset_state'],'pending');self.assertEqual(saved['password'],args[2]['password'])
   return {'root_password':saved['password']}
  with patch.object(vpsnat,'request',side_effect=request) as call:
   auto.reset_password('123',profile,path);auto.reset_password('123',profile,path)
   self.assertEqual(call.call_count,1)
  self.assertEqual(path.stat().st_mode&0o777,0o600)
  self.assertEqual(path.parent.stat().st_mode&0o777,0o700)
  profile={};path=auto.profile_path('124',38001)
  with patch.object(vpsnat,'request',side_effect=vpsnat.APIError('timeout')) as call:
   with self.assertRaises(vpsnat.APIError):auto.reset_password('124',profile,path)
   self.assertTrue(json.loads(path.read_text())['password']);auto.reset_password('124',profile,path);self.assertEqual(call.call_count,1)
 def test_mapping_conflict_increments_and_reuses_saved_mapping(self):
  path=auto.profile_path('123',38001);profile={}
  with patch.object(vpsnat,'request',return_value={'port_forwards':[]}),patch.object(vpsnat,'mutate',side_effect=[vpsnat.APIError('occupied',422,'PORT_IN_USE'),{'id':7,'guest_port':38002,'host_port':38002}]) as call:
   result=auto.managed_mapping('123',38001,profile,path,[],set());self.assertEqual(result['internal_port'],38002);self.assertEqual(call.call_args.args[2]['host_port'],38002)
  with patch.object(vpsnat,'request',return_value={'port_forwards':[]}),patch.object(vpsnat,'mutate') as call:
   self.assertEqual(auto.managed_mapping('123',38001,profile,path,[result],set()),result);call.assert_not_called()
 def test_provider_port_range_skips_to_nearest_legal_free_port(self):
  profile={};path=auto.profile_path('123',38001)
  with patch.object(vpsnat,'request',return_value={'port_forwards':[]}),patch.object(vpsnat,'mutate',side_effect=[vpsnat.APIError('range',502,'PORT_OUT_OF_RANGE',(50000,60000)),{'id':7,'guest_port':50001,'host_port':50001}]) as call:
   result=auto.managed_mapping('123',38001,profile,path,[],{50000});self.assertEqual(result['internal_port'],50001);self.assertEqual(call.call_args.args[2]['host_port'],50001)
  self.assertEqual(json.loads(path.read_text())['allowed_port_range'],[50000,60000])
 def test_unknown_password_reset_replay_keeps_original_password_and_key(self):
  profile={};path=auto.profile_path('123',38001)
  with patch.object(vpsnat,'request',side_effect=vpsnat.APIError('timeout')) as call:
   with self.assertRaises(vpsnat.APIError):auto.reset_password('123',profile,path)
   original=call.call_args.args
  with patch.object(vpsnat,'request',return_value={'message':'ok'}) as call:
   auto.reset_password('123',profile,path,replay=True);self.assertEqual(call.call_args.args,original)
  self.assertEqual(profile['reset_state'],'confirmed')
 def test_lost_create_response_recovers_by_description_without_duplicate(self):
  row={'id':7,'guest_port':38001,'host_port':38001,'protocol':'tcp','description':'auto-node-simple:argo:123:38001'}
  with patch.object(vpsnat,'request',return_value={'port_forwards':[row]}),patch.object(vpsnat,'mutate') as mutate:
   result=auto.managed_mapping('123',38001,{},auto.profile_path('123',38001),[],set());self.assertEqual(result['id'],'7');mutate.assert_not_called()
 def test_start_is_authenticated_by_route_and_deduplicates_batch(self):
  machines=[{'id':'123','name':'Fixture'}]
  with patch.object(auto.cloudflare,'status',return_value={'configured':True}),patch.object(vpsnat,'vms',return_value=machines),patch.object(auto.threading,'Thread') as thread:
   result=auto.start([],38001,'fixture-batch-key',None);self.assertEqual(result['count'],1)
   self.assertEqual(auto.start([],38001,'fixture-batch-key',None)['id'],result['id']);self.assertEqual(thread.call_count,1)
   with self.assertRaises(HTTPException):auto.start([],38002,'fixture-batch-key',None)
   with self.assertRaises(HTTPException):auto.start([],38001,'fixture-second-key',None)
  payload=json.dumps(auto.listing());self.assertNotIn('password',payload);self.assertNotIn('request_key',payload)
 def test_completed_node_is_reused_and_reprobed_without_password_reset(self):
  from app import remote_install,node_probe
  with main.conn() as c:
   remote_install.init(c);c.execute('ALTER TABLE nodes ADD COLUMN uri TEXT');c.execute("INSERT INTO nodes VALUES ('node','fixture-uri')")
   c.execute("INSERT INTO automation_batches(id,status) VALUES ('batch','running')")
   c.execute("INSERT INTO automation_items(batch_id,vm_id,state) VALUES ('batch','123','queued')")
  auto.save_profile(auto.profile_path('123',38001),{'install_id':'node','completed':True,'port':38001,'public_port':38001,'verified':False})
  with patch.object(node_probe,'probe',return_value=12) as probe,patch.object(vpsnat,'request') as request:
   auto.install_one('batch',{'id':'123'},38001,None);probe.assert_called_once_with('fixture-uri');request.assert_not_called()
  with main.conn() as c:
   row=c.execute('SELECT state,verified FROM automation_items').fetchone();self.assertEqual(tuple(row),('done',1))
 def test_restart_marks_unknown_without_repeating_mutations(self):
  with main.conn() as c:
   c.execute("INSERT INTO automation_batches(id,status) VALUES ('batch','running')")
   c.execute("INSERT INTO automation_items(batch_id,vm_id,state) VALUES ('batch','123','running')")
  with patch.object(vpsnat,'request') as request:auto.recover();request.assert_not_called()
  with main.conn() as c:self.assertEqual(c.execute('SELECT state FROM automation_items').fetchone()[0],'unknown')

if __name__=='__main__':unittest.main()
