"""Executable bootstrap fallback and bounded lock exclusion."""
import http.server,os,shlex,shutil,socketserver,subprocess,tempfile,threading,time,unittest
from pathlib import Path
from app.main import download_command

class InstallerPortabilityTests(unittest.TestCase):
 def test_bootstrap_runs_with_wget_and_no_curl(self):
  class Handler(http.server.BaseHTTPRequestHandler):
   def do_GET(self):
    self.send_response(200);self.end_headers();self.wfile.write(b"#!/bin/sh\nprintf 'WGET_BOOTSTRAP_OK\\n'\n")
   def log_message(self,*args):pass
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with tempfile.TemporaryDirectory() as temp:
    bin=Path(temp)/'bin';bin.mkdir()
    for cmd in ['sh','mktemp','rm','wget']:
     binary=shutil.which(cmd);self.assertIsNotNone(binary,cmd+' required by test runtime');(bin/cmd).symlink_to(binary)
    env=os.environ|{'PATH':str(bin),'TMPDIR':temp}
    result=subprocess.run(['/bin/sh','-c',download_command('http://127.0.0.1:'+str(server.server_port)+'/fixture.sh')],env=env,capture_output=True,timeout=10)
    self.assertEqual(result.returncode,0,result.stderr.decode());self.assertIn(b'WGET_BOOTSTRAP_OK',result.stdout)
    self.assertEqual(list(Path(temp).glob('tmp.*')),[],'downloaded script is removed')
  finally:server.shutdown();server.server_close();thread.join(timeout=2)
 def test_flock_child_keeps_parent_lock_until_parent_closes_fd(self):
  with tempfile.TemporaryDirectory() as temp:
   lock=Path(temp)/'lock';marker=Path(temp)/'ready'
   parent=subprocess.Popen(['sh','-c','exec 9>"$1"; timeout 2 flock 9; echo ready > "$2"; sleep 2','fixture',str(lock),str(marker)])
   try:
    for _ in range(100):
     if marker.exists():break
     time.sleep(.01)
    self.assertTrue(marker.exists())
    r=subprocess.run(['flock','-n',str(lock),'true']);self.assertNotEqual(r.returncode,0)
    r=subprocess.run(['timeout','.1','flock',str(lock),'true']);self.assertIn(r.returncode,(124,137,143))
   finally:parent.wait(timeout=3)
   self.assertEqual(subprocess.run(['flock','-n',str(lock),'true']).returncode,0)
