"""Discard completed cleanup history while preserving unfinished resource cleanup."""
FINISHED_DELETIONS="DELETE FROM node_deletions WHERE status='done' AND cf_state='completed'"

def purge_completed(connection):
 if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='node_deletions'").fetchone():return 0
 return connection.execute(FINISHED_DELETIONS).rowcount
