"""Build a manifest for binaries supplied by the deployment administrator."""
import hashlib,json
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'assets'
manifest={}
for name in ('sing-box-amd64','sing-box-arm64','cloudflared-amd64','cloudflared-arm64'):
 path=root/name
 if not path.is_file():raise SystemExit('Missing binary: '+str(path))
 manifest[name]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
(root/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Generated assets/manifest.json for four binaries')
