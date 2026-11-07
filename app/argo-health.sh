#!/bin/sh
# Verify authenticated VLESS forwarding through the advertised TLS/WS endpoint.
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE=/usr/local/etc/auto-node-simple
NODE_ID=${1:-}
case "$NODE_ID" in ''|*[!a-f0-9]*) exit 1;; esac
TMP=$(mktemp -d);CLIENT=''
cleanup() {
 if [ -n "$CLIENT" ];then kill "$CLIENT" 2>/dev/null || true;wait "$CLIENT" 2>/dev/null || true;fi
 rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
CFG="$BASE/configs/$NODE_ID.json"
LINK=$(cat "$BASE/state/$NODE_ID.link")
HOST=$(jq -er .hostname "$BASE/repair/$NODE_ID/settings.json")
# The private client uses an ephemeral kernel-assigned port, never a fixed port.
printf '%s' "$LINK" | jq -R 'capture("^vless://[^@]+@(?<server>\\[[^]]+\\]|[^:]+):(?<port>[0-9]+)")' > "$TMP/endpoint"
jq -n --slurpfile cfg "$CFG" --slurpfile ep "$TMP/endpoint" --arg host "$HOST" '
 {log:{level:"error"},dns:{servers:[{type:"local",tag:"dns"}]},
 inbounds:[{type:"mixed",listen:"127.0.0.1",listen_port:0}],
 outbounds:[{type:"vless",tag:"node",server:($ep[0].server|ltrimstr("[")|rtrimstr("]")),server_port:($ep[0].port|tonumber),
 uuid:$cfg[0].inbounds[0].users[0].uuid,tls:{enabled:true,server_name:$host},
 transport:{type:"ws",path:$cfg[0].inbounds[0].transport.path,headers:{Host:$host}}}],
 route:{final:"node",default_domain_resolver:{server:"dns",strategy:"prefer_ipv4"}}}' > "$TMP/client.json"
GOMAXPROCS=1 /usr/local/bin/auto-node-singbox run -c "$TMP/client.json" > "$TMP/client.log" 2>&1 &
CLIENT=$!
PORT='';ATTEMPT=0
while [ "$ATTEMPT" -lt 10 ];do
 kill -0 "$CLIENT" 2>/dev/null || exit 1
 PORT=$(ss -H -lntp | awk -v p="pid=$CLIENT," 'index($0,p) {n=split($4,a,":");print a[n];exit}')
 [ -z "$PORT" ] || break
 ATTEMPT=$((ATTEMPT+1));sleep 1
done
case "$PORT" in ''|*[!0-9]*) exit 1;; esac
forward_check() {
 for url in https://www.gstatic.com/generate_204 https://www.cloudflare.com/cdn-cgi/trace;do
  sample=$(curl --noproxy '' --proxy "http://127.0.0.1:$PORT" --connect-timeout 5 --max-time 12 --max-filesize 4096 -fsS -o "$TMP/body" -w '%{http_code} %{time_total}' "$url" 2>/dev/null) || continue
  code=${sample%% *};duration=${sample#* }
  if [ "$code" = 204 ] || { [ "$code" = 200 ] && grep -q '^ip=' "$TMP/body"; };then
   printf '%s\n' "$duration" >> "$TMP/timings";return 0
  fi
 done
 return 1
}
forward_check || exit 1
if [ "${2:-}" = --stable ];then sleep 12;forward_check || exit 1;fi
awk '{total+=$1;count++} END {if(count) printf "%.0f\n",total*1000/count;else exit 1}' "$TMP/timings"
