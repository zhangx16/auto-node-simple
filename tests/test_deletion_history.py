import sqlite3,unittest
from app.deletion_history import purge_completed

class DeletionHistoryTests(unittest.TestCase):
 def test_fresh_database_requires_no_cleanup_history_table(self):
  with sqlite3.connect(':memory:') as c:self.assertEqual(purge_completed(c),0)
 def test_only_completed_history_is_removed_and_repeat_is_idempotent(self):
  with sqlite3.connect(':memory:') as c:
   c.execute('CREATE TABLE node_deletions(id TEXT,status TEXT,cf_state TEXT)')
   c.executemany('INSERT INTO node_deletions VALUES(?,?,?)',[('old1','done','completed'),('old2','done','completed'),('old3','done','completed'),('retry','error','pending'),('active','running','running')])
   self.assertEqual(purge_completed(c),3)
   self.assertEqual(c.execute('SELECT id FROM node_deletions ORDER BY id').fetchall(),[('active',),('retry',)])
   self.assertEqual(purge_completed(c),0)
