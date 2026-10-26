#!/bin/sh
# Only executes the authenticated panel's complete, checksummed installer.
set -eu
umask 077
export MALLOC_ARENA_MAX=1
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
BASE=/usr/local/etc/auto-node-simple
LIB=/usr/local/lib/auto-node-simple
NODE_ID=${1:-}
case "$NODE_ID" in ''|*[!a-f0-9]*) exit 1;; esac
DIR="$BASE/repair/$NODE_ID"
[ -s "$DIR/curl.conf" ] && [ -s "$DIR/settings.json" ] || exit 1
PANEL=$(jq -r .panel "$DIR/settings.json")
case "$PANEL" in https://*|http://127.0.0.1:*|http://localhost:*) ;; *) exit 1;; esac
exec 7>/run/auto-node-upgrade.lock
flock -n 7 || exit 0
exec 9>/run/auto-node-simple.lock
flock -n 9 || exit 0
TMP=$(mktemp -d)
TARGET='';COMMITTED=0;CHANGED=0;BACKUP="$BASE/upgrade-stage"
paths() {
 cat <<EOF
/usr/local/bin/auto-node-singbox
/usr/local/bin/auto-node-cloudflared
$LIB/watchdog.sh
$LIB/update.sh
$BASE/configs/$NODE_ID.json
$BASE/configs/base.json
$BASE/state/$NODE_ID.link
$BASE/state/$NODE_ID.token
$BASE/state/$NODE_ID.key
$BASE/state/$NODE_ID.pem
$DIR/settings.json
$DIR/curl.conf
$DIR/release
/etc/systemd/system/auto-node-simple.service
/etc/systemd/system/auto-node-$NODE_ID.service
/etc/systemd/system/auto-node-repair.service
/etc/systemd/system/auto-node-repair.timer
/etc/init.d/auto-node-simple
/etc/init.d/auto-node-$NODE_ID
EOF
}
restore() {
 index=0
 paths > "$TMP/paths"
 while IFS= read -r path;do
  index=$((index+1))
  if [ -f "$BACKUP/$index" ];then
   cp -p "$BACKUP/$index" "$path.restore" && mv -f "$path.restore" "$path" || return 1
  else rm -f "$path";fi
 done < "$TMP/paths"
 if [ -f "$BACKUP/cron" ];then crontab "$BACKUP/cron" || return 1;fi
 if [ -d /run/systemd/system ] && command -v systemctl >/dev/null;then
  systemctl daemon-reload
  timeout 35 systemctl restart auto-node-simple 7>&- 9>&- || return 1
  if [ -f "/etc/systemd/system/auto-node-$NODE_ID.service" ];then timeout 35 systemctl restart "auto-node-$NODE_ID" 7>&- 9>&- || return 1;fi
 elif command -v rc-service >/dev/null;then
  timeout 35 rc-service auto-node-simple restart 7>&- 9>&- || return 1
  if [ -f "/etc/init.d/auto-node-$NODE_ID" ];then timeout 35 rc-service "auto-node-$NODE_ID" restart 7>&- 9>&- || return 1;fi
 fi
}
finish() {
 code=$?
 if [ "$CHANGED" = 1 ] && [ "$COMMITTED" != 1 ];then restore >/dev/null 2>&1 || true;fi
 if [ -n "$TARGET" ];then
  OK=false;[ "$COMMITTED" != 1 ] || OK=true
  jq -n --arg release "$TARGET" --argjson ok "$OK" '{release:$release,ok:$ok}' > "$TMP/result"
  curl --noproxy '*' --config "$DIR/curl.conf" -fsS --connect-timeout 5 --max-time 10 -H 'Content-Type: application/json' --data-binary @"$TMP/result" "$PANEL/repair/$NODE_ID/upgrade-result" >/dev/null 2>&1 || true
 fi
 # A failed rollback keeps its private snapshot for manual recovery.
 if [ "$COMMITTED" = 1 ];then rm -rf "$BASE/upgrade-previous";mv "$BACKUP" "$BASE/upgrade-previous";fi
 rm -rf "$TMP";exit "$code"
}
trap finish EXIT
curl --noproxy '*' --config "$DIR/curl.conf" -fsS --connect-timeout 5 --max-time 30 --max-filesize 262144 "$PANEL/repair/$NODE_ID/upgrade" -o "$TMP/bundle" 2>/dev/null || exit 0
TARGET=$(jq -er '.release' "$TMP/bundle")
EXPECTED=$(jq -er '.sha256' "$TMP/bundle")
case "$TARGET$EXPECTED" in *[!a-f0-9]*) exit 1;; esac
[ "${#TARGET}" = 64 ] && [ "${#EXPECTED}" = 64 ] || exit 1
jq -er '.script' "$TMP/bundle" | base64 -d > "$TMP/install.sh"
printf '%s  %s\n' "$EXPECTED" "$TMP/install.sh" | sha256sum -c - >/dev/null
sh -n "$TMP/install.sh"
mkdir -p "$BASE";chmod 700 "$BASE"
# Store at most one pending and one previous snapshot. Large binaries use hard links.
rm -rf "$BACKUP";mkdir -m 700 "$BACKUP"
paths > "$TMP/paths";index=0
while IFS= read -r path;do
 index=$((index+1))
 if [ -f "$path" ];then
  case "$path" in /usr/local/bin/auto-node-*) ln "$path" "$BACKUP/$index" 2>/dev/null || cp -p "$path" "$BACKUP/$index";; *) cp -p "$path" "$BACKUP/$index";; esac
 fi
done < "$TMP/paths"
if command -v crontab >/dev/null && command -v rc-service >/dev/null;then (crontab -l 2>/dev/null || true) > "$BACKUP/cron";fi
CHANGED=1
# Keep both locks in this supervisor; service children must not inherit them.
NODE_INSTALL_LOCKED=1 NODE_DEFER_REPORT=1 sh "$TMP/install.sh" 7>&- 9>&- > "$TMP/install.log" 2>&1
sh -n "$LIB/watchdog.sh";sh -n "$LIB/update.sh"
/usr/local/bin/auto-node-singbox check -C "$BASE/configs" >/dev/null 2>&1
[ -s "$DIR/release" ] && [ "$(cat "$DIR/release")" = "$TARGET" ]
COMMITTED=1
