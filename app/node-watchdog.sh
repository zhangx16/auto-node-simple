#!/bin/sh
# Minute job; never reconfigure nodes or rotate identities.
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE=/usr/local/etc/auto-node-simple
NODE_ID=$1
case "$NODE_ID" in ''|*[!a-f0-9]*) exit 1;; esac
DIR="$BASE/repair/$NODE_ID"
[ -f "$DIR/settings.json" ] && [ -f "$BASE/configs/$NODE_ID.json" ] || exit 0
exec 9>/run/auto-node-simple.lock
flock -n 9 || exit 0
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
CFG="$BASE/configs/$NODE_ID.json"
PORT=$(jq -er '.inbounds[0].listen_port' "$CFG")
KIND=$(jq -r .protocol "$DIR/settings.json")
PANEL=$(jq -r .panel "$DIR/settings.json")
LOCAL=true;PUBLIC=true;NETWORK=false;RESULT=''
# ss examines the actual node port, including updates applied after setup.
NET=t;[ "$KIND" != hysteria2 ] || NET=u
ss -H -ln"$NET" | awk '{print $4}' | grep -Eq ":${PORT}$" || LOCAL=false
ws_check() {
 url=$1;host=$2;path=$3
 code=$(curl --noproxy '*' -sS --http1.1 --connect-timeout 4 --max-time 6 -o /dev/null -w '%{http_code}' -H "Host: $host" -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' "$url$path" 2>/dev/null) || true
 [ "$code" = 101 ]
}
if [ "$KIND" = argo ];then
 HOST=$(jq -r .hostname "$DIR/settings.json")
 WS_PATH=$(jq -r '.inbounds[0].transport.path' "$CFG")
 ws_check "http://127.0.0.1:$PORT" "$HOST" "$WS_PATH" || LOCAL=false
 ws_check "https://$HOST" "$HOST" "$WS_PATH" || PUBLIC=false
fi
for url in https://www.cloudflare.com/cdn-cgi/trace https://www.gstatic.com/generate_204;do
 if curl --noproxy '*' -fsS --connect-timeout 3 --max-time 5 -o /dev/null "$url" 2>/dev/null;then NETWORK=true;break;fi
done
[ ! -f "$DIR/result" ] || RESULT=$(cat "$DIR/result")
jq -n --argjson local "$LOCAL" --argjson public "$PUBLIC" --argjson network "$NETWORK" --arg result "$RESULT" '{local_ok:$local,public_ok:$public,network_ok:$network,result:$result}' > "$TMP/report"
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
