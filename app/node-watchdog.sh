#!/bin/sh
# Minute job; never reconfigure nodes or rotate identities.
set -eu
umask 077
export MALLOC_ARENA_MAX=1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE=/usr/local/etc/auto-node-simple
# A host owns one timer/cron entry; keep peak process count independent of node count.
if [ "${1:-}" = --all ];then
 exec 8>/run/auto-node-repair.lock
 flock -n 8 || exit 0
 for settings in "$BASE"/repair/*/settings.json;do
  [ -f "$settings" ] || continue
  node_dir=${settings%/settings.json};node_id=${node_dir##*/}
  case "$node_id" in ''|*[!a-f0-9]*) continue;; esac
  /bin/sh "$0" "$node_id" 8>&- || true
 done
 exit 0
fi
NODE_ID=${1:-}
case "$NODE_ID" in ''|*[!a-f0-9]*) exit 1;; esac
DIR="$BASE/repair/$NODE_ID"
[ -f "$DIR/settings.json" ] && [ -f "$BASE/configs/$NODE_ID.json" ] || exit 0
exec 9>/run/auto-node-simple.lock
flock -n 9 || exit 0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
CFG="$BASE/configs/$NODE_ID.json"
IFS='|' read -r PORT WS_PATH <<EOF
$(jq -er '.inbounds[0] | [.listen_port,(.transport.path // "/")] | join("|")' "$CFG")
EOF
IFS='|' read -r KIND PANEL HOST <<EOF
$(jq -r '[.protocol,.panel,.hostname] | join("|")' "$DIR/settings.json")
EOF
LOCAL=true;PUBLIC=true;NETWORK=false;RESULT=''
# ss examines the actual node port, including updates applied after setup.
NET=t;[ "$KIND" != hysteria2 ] || NET=u
ss -H -ln"$NET" | awk -v p=":$PORT" '$4 ~ (p "$") {found=1} END {exit !found}' || LOCAL=false
ws_check() {
 url=$1;host=$2;path=$3
 code=$(curl --noproxy '*' -sS --http1.1 --connect-timeout 4 --max-time 6 -o /dev/null -w '%{http_code}' -H "Host: $host" -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$url$path" 2>/dev/null) || true
 [ "$code" = 101 ]
}
if [ "$KIND" = argo ];then
 ws_check "http://127.0.0.1:$PORT" "$HOST" "$WS_PATH" || LOCAL=false
 ws_check "https://$HOST" "$HOST" "$WS_PATH" || PUBLIC=false
fi
# A successful public TLS/WS handshake already demonstrates outbound connectivity.
# Otherwise reuse a host-wide network result for 30 seconds across serial node checks.
NOW=$(date +%s)
if [ "$KIND" = argo ] && [ "$PUBLIC" = true ];then NETWORK=true
else
 CACHE_AT=0;CACHE_OK=false
 if [ -f "$BASE/repair/network-cache" ];then read -r CACHE_AT CACHE_OK < "$BASE/repair/network-cache" || true;fi
 if [ "$((NOW-CACHE_AT))" -lt 30 ];then NETWORK=$CACHE_OK
 else
  for url in https://www.cloudflare.com/cdn-cgi/trace https://www.gstatic.com/generate_204;do
   if curl --noproxy '*' -fsS --connect-timeout 3 --max-time 5 -o /dev/null "$url" 2>/dev/null;then NETWORK=true;break;fi
  done
  printf '%s %s\n' "$NOW" "$NETWORK" > "$BASE/repair/network-cache"
 fi
fi
[ ! -f "$DIR/result" ] || RESULT=$(cat "$DIR/result")
jq -n --argjson local "$LOCAL" --argjson public "$PUBLIC" --argjson network "$NETWORK" --arg result "$RESULT" '{local_ok:$local,public_ok:$public,network_ok:$network,result:$result,script_version:2}' > "$TMP/report"
if ! curl --noproxy '*' -fsS --config "$DIR/curl.conf" --connect-timeout 5 --max-time 10 -H 'Content-Type: application/json' --data-binary @"$TMP/report" "$PANEL/repair/$NODE_ID/report" -o "$TMP/control" 2>/dev/null;then exit 0;fi
ACTION=$(jq -r '.action // "none"' "$TMP/control")
[ "$ACTION" != none ] || exit 0
restart_service() {
 if [ -d /run/systemd/system ] && command -v systemctl >/dev/null;then timeout 35 systemctl restart "$1" 9>&-
 elif command -v rc-service >/dev/null;then timeout 35 rc-service "$1" restart 9>&-
 else return 1;fi
}
RESULT=restart_failed
case "$ACTION" in
 core|protocol)
  # Shared core: multiple nodes must not restart it repeatedly in the same window.
  LAST=0;[ ! -f "$BASE/repair/last-core-repair" ] || LAST=$(cat "$BASE/repair/last-core-repair")
  NOW=$(date +%s)
  if [ "$((NOW-LAST))" -ge 300 ];then
   printf '%s\n' "$NOW" > "$BASE/repair/last-core-repair"
   if /usr/local/bin/auto-node-singbox check -C "$BASE/configs" >/dev/null 2>&1 && restart_service auto-node-simple >/dev/null 2>&1;then RESULT=core_restarted;fi
  else RESULT=shared_core_cooldown;fi
  if [ "$ACTION" = protocol ] && [ "$KIND" = argo ];then
   if restart_service "auto-node-$NODE_ID" >/dev/null 2>&1;then
    [ "$RESULT" != core_restarted ] || RESULT=core_and_tunnel_restarted
   else RESULT=restart_failed;fi
  fi ;;
 tunnel) if [ "$KIND" = argo ] && restart_service "auto-node-$NODE_ID" >/dev/null 2>&1;then RESULT=tunnel_restarted;fi ;;
 *) exit 0 ;;
esac
printf '%s\n' "$RESULT" > "$DIR/result"
