"""Actual protocol forwarding checks via a temporary sing-box client, one at a time."""
import hashlib,json,os,platform,shutil,socket,subprocess,tempfile,threading,time
from . import formats
SLOT=threading.Lock()

def binary_digest(binary):
 digest=hashlib.sha256()
 with binary.open('rb') as stream:
  for chunk in iter(lambda:stream.read(65536),b''):digest.update(chunk)
 return digest.hexdigest()

def probe(uri):
 from . import main
 arch={'x86_64':'amd64','aarch64':'arm64','amd64':'amd64','arm64':'arm64'}.get(platform.machine())
 binary=main.ASSETS/('sing-box-'+str(arch))
 try:manifest=json.loads((main.ASSETS/'manifest.json').read_text())
 except (OSError,ValueError,TypeError) as exc:raise RuntimeError('Probe manifest unavailable') from exc
 if not binary.is_file() or not os.access(binary,os.X_OK) or not shutil.which('curl'):raise RuntimeError('Missing probe component')
 expected=manifest.get(binary.name,{}).get('sha256')
 try:actual=binary_digest(binary)
 except OSError as exc:raise RuntimeError('Probe binary unreadable') from exc
 if actual!=expected:raise RuntimeError('Probe binary checksum mismatch')
 with SLOT,tempfile.TemporaryDirectory(prefix='simple-probe-') as temp:
  with socket.socket() as reservation:reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
  outbound=formats.singbox(formats.parse(uri));outbound['tag']='node'
  cfg={'log':{'level':'error'},'dns':{'servers':[{'type':'local','tag':'dns'}]},'inbounds':[{'type':'mixed','listen':'127.0.0.1','listen_port':port}],'outbounds':[outbound],'route':{'final':'node','default_domain_resolver':{'server':'dns','strategy':'prefer_ipv4'}}}
  from pathlib import Path
  path=Path(temp)/'config.json';path.write_text(json.dumps(cfg));path.chmod(0o600)
  start=time.monotonic()
  with open(os.devnull,'wb') as quiet:
   process=subprocess.Popen([str(binary),'run','-c',str(path)],stdout=quiet,stderr=quiet,env=os.environ|{'GOMAXPROCS':'1'})
   try:
    for _ in range(40):
     if process.poll() is not None:raise RuntimeError('Probe core startup failed')
     try:
      with socket.create_connection(('127.0.0.1',port),timeout=.1):break
     except OSError:time.sleep(.05)
    else:raise RuntimeError('Probe core did not listen')
    for url,code in [('https://www.gstatic.com/generate_204',b'204'),('https://www.cloudflare.com/cdn-cgi/trace',b'200')]:
     try:
      r=subprocess.run(['curl','--noproxy','','--proxy',f'http://127.0.0.1:{port}','--connect-timeout','5','--max-time','12','--max-filesize','4096','-fsS','-w','\n%{http_code}',url],stdout=subprocess.PIPE,stderr=quiet,timeout=15)
     except subprocess.TimeoutExpired:continue
     body,_,status=r.stdout.rpartition(b'\n')
     if r.returncode==0 and status==code and (code==b'204' or any(line.startswith(b'ip=') for line in body.splitlines())):return round((time.monotonic()-start)*1000)
    # A panel network outage must not restart healthy remote nodes.
    reachable=False
    for url in ['https://www.gstatic.com/generate_204','https://www.cloudflare.com/cdn-cgi/trace']:
     try:
      r=subprocess.run(['curl','--noproxy','*','--connect-timeout','3','--max-time','5','-fsS','-o',os.devnull,url],stdout=quiet,stderr=quiet,timeout=7)
      if r.returncode==0:reachable=True;break
     except subprocess.TimeoutExpired:pass
    if not reachable:raise RuntimeError('Panel outbound network unavailable')
    raise ValueError('Protocol forwarding failed')
   finally:
    process.terminate()
    try:process.wait(timeout=3)
    except subprocess.TimeoutExpired:process.kill();process.wait()
