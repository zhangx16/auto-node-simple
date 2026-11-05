import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app import main,platform_watch as watch,platform_automation as auto,remote_install,vpsnat

class WatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.old=main.DATA;main.DATA=Path(self.tmp.name)
  with main.conn() as c:
   c.executescript("CREATE TABLE nodes(id TEXT PRIMARY KEY,name TEXT,protocol TEXT,uri TEXT,created REAL,config TEXT);CREATE TABLE installs(id TEXT PRIMARY KEY,status TEXT);CREATE TABLE subscriptions(id TEXT PRIMARY KEY,name TEXT,token TEXT,enabled INTEGER);CREATE TABLE subscription_nodes(subscription_id TEXT REFERENCES subscriptions(id),node_id TEXT REFERENCES nodes(id),position INTEGER,PRIMARY KEY(subscription_id,node_id));")
   c.execute("INSERT INTO subscriptions VALUES ('cf','CF','fixture-subscription-token',1)")
   self.node(c,'old','123')
   remote_install.init(c);auto.init(c);watch.init(c)
  self.existing={'id':'123','name':'Existing','status':'running'};self.new={'id':'456','name':'New','status':'running'}
 def tearDown(self):main.DATA=self.old;self.tmp.cleanup()
 def node(self,c,ident,vm,protocol='argo'):
  c.execute('INSERT INTO nodes VALUES (?,?,?,?,?,?)',(ident,ident,protocol,'fixture-uri',0,json.dumps({'platform_vm_id':vm})))
 def enable(self):
  with patch.object(vpsnat,'vms',return_value=[self.existing]):return watch.configure(True)
 def due(self):return watch.status()['next_check']+1
 def test_first_enable_baselines_existing_and_preserves_cf_token(self):
  with patch.object(auto,'start') as start:self.enable();start.assert_not_called()
  s=watch.status();self.assertEqual(s['interval_seconds'],3600);self.assertEqual(s['counts'],{'managed':1});self.assertEqual(s['subscription_id'],'cf')
  self.assertNotIn('fixture-subscription-token',json.dumps(s))
  with main.conn() as c:
   self.assertEqual(c.execute('SELECT token FROM subscriptions').fetchone()[0],'fixture-subscription-token')
   self.assertEqual(c.execute('SELECT count(*) FROM subscription_nodes').fetchone()[0],1)
 def test_due_scan_only_starts_new_instance_and_deduplicates_future_scans(self):
  self.enable()
  with patch.object(vpsnat,'vms') as vms:self.assertEqual(watch.tick(now=self.due()-2),{'not_due':True});vms.assert_not_called()
  with patch.object(vpsnat,'vms',return_value=[self.existing,self.new]),patch.object(auto,'start',return_value={'id':'batch'}) as start:
   result=watch.tick(now=self.due());self.assertEqual(result['new_count'],1)
   self.assertEqual(start.call_args.args[0],['456']);self.assertEqual(start.call_args.args[1],38001);self.assertIsNone(start.call_args.args[3])
   watch.tick(now=self.due());self.assertEqual(start.call_count,1)
  with main.conn() as c:self.assertEqual(c.execute("SELECT attempts FROM platform_watch_seen WHERE vm_id='456'").fetchone()[0],1)
 def test_success_callback_adds_cf_membership_once_and_never_rotates_subscription_token(self):
  self.enable()
  with main.conn() as c:
   c.execute("INSERT INTO platform_watch_seen(vm_id,first_seen,last_seen,state) VALUES ('456',0,0,'installing')");self.node(c,'new','456')
  self.assertTrue(watch.enroll_node('new'));self.assertTrue(watch.enroll_node('new'))
  with main.conn() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM subscription_nodes').fetchone()[0],2)
   self.assertEqual(c.execute("SELECT position FROM subscription_nodes WHERE node_id='new'").fetchone()[0],1)
   self.assertEqual(c.execute('SELECT token FROM subscriptions').fetchone()[0],'fixture-subscription-token')
   self.assertEqual(c.execute("SELECT state FROM platform_watch_seen WHERE vm_id='456'").fetchone()[0],'managed')
 def test_paused_scheduler_has_no_network_or_mutations(self):
  self.enable();watch.configure(False)
  with patch.object(vpsnat,'vms') as vms,patch.object(auto,'start') as start:
   self.assertEqual(watch.tick(force=True),{'disabled':True});vms.assert_not_called();start.assert_not_called()
 def test_disabled_subscription_prevents_install_and_keeps_hourly_backoff(self):
  self.enable();due=self.due()
  with main.conn() as c:c.execute("UPDATE subscriptions SET enabled=0 WHERE id='cf'")
  with patch.object(vpsnat,'vms') as vms,patch.object(auto,'start') as start:
   self.assertIn('error',watch.tick(now=due));vms.assert_not_called();start.assert_not_called()
  self.assertEqual(watch.status()['next_check'],due+3600)
 def test_failed_install_retries_hourly_then_blocks_after_three_attempts(self):
  self.enable()
  with patch.object(vpsnat,'vms',return_value=[self.existing,self.new]),patch.object(auto,'start') as start:
   for n in range(1,4):
    batch='batch'+str(n);start.return_value={'id':batch};watch.tick(now=self.due())
    with main.conn() as c:
     c.execute('INSERT INTO automation_batches(id,status) VALUES (?,?)',(batch,'finished'))
     c.execute('INSERT INTO automation_items(batch_id,vm_id,state,message) VALUES (?,?,?,?)',(batch,'456','failed','temporary failure'))
    watch.reconcile_batch(batch)
   watch.tick(now=self.due());self.assertEqual(start.call_count,3)
  with main.conn() as c:
   row=c.execute("SELECT state,attempts FROM platform_watch_seen WHERE vm_id='456'").fetchone();self.assertEqual(tuple(row),('blocked',3))
 def test_stopped_new_instance_waits_for_running_without_consuming_an_attempt(self):
  self.enable()
  with patch.object(vpsnat,'vms',return_value=[self.existing,self.new|{'status':'stopped'}]),patch.object(auto,'start') as start:
   watch.tick(now=self.due());start.assert_not_called()
  with main.conn() as c:
   row=c.execute("SELECT state,attempts FROM platform_watch_seen WHERE vm_id='456'").fetchone();self.assertEqual(tuple(row),('pending',0))
 def test_active_batch_defers_new_install_without_losing_instance(self):
  self.enable()
  with main.conn() as c:c.execute("INSERT INTO automation_batches(id,status) VALUES ('manual','running')")
  with patch.object(vpsnat,'vms',return_value=[self.existing,self.new]),patch.object(auto,'start') as start:
   watch.tick(now=self.due());start.assert_not_called()
  self.assertEqual(watch.status()['counts']['pending'],1)
 def test_database_lease_blocks_concurrent_process_scan_and_schedule_survives_reopen(self):
  self.enable();due=self.due()
  with main.conn() as c:c.execute('UPDATE platform_watch SET lease_until=?',(due+200,))
  with patch.object(vpsnat,'vms') as vms:self.assertEqual(watch.tick(force=True,now=due),{'busy':True});vms.assert_not_called()
  with main.conn() as c:self.assertEqual(c.execute('SELECT next_check FROM platform_watch').fetchone()[0],due-1)
 def test_no_cf_subscription_is_created_until_explicit_enable(self):
  with main.conn() as c:c.execute("DELETE FROM subscriptions WHERE id='cf'")
  self.assertFalse(watch.status()['enabled'])
  with patch.object(vpsnat,'vms',return_value=[self.existing]):s=watch.configure(True)
  self.assertTrue(s['enabled']);self.assertEqual(s['subscription_name'],'CF')

if __name__=='__main__':unittest.main()
