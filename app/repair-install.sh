#!/bin/sh
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ "$(id -u)" = 0 ] || { echo '请以 root 执行';exit 1; }
BASE=/usr/local/etc/auto-node-simple
case "$NODE_ID" in ''|*[!a-f0-9]*) echo '无效节点 ID';exit 1;; esac
[ -s "$BASE/configs/$NODE_ID.json" ] && [ -x /usr/local/bin/auto-node-singbox ] || { echo '未找到此 Simple 节点的配置，不能直接接管旧管理脚本。';exit 1; }
for tool in curl jq flock ss timeout;do command -v "$tool" >/dev/null || { echo "缺少依赖：$tool";exit 1; };done
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null;then INIT=systemd
elif command -v rc-service >/dev/null;then
 INIT=openrc
 command -v crontab >/dev/null || { echo '需要 crontab（Alpine: apk add busybox）';exit 1; }
else echo '需要 systemd 或 OpenRC';exit 1;fi
# The combined installer already holds this lock; never unlock its shared FD.
if [ "${REPAIR_INSTALL_LOCKED:-0}" != 1 ];then
 exec 9>/run/auto-node-simple.lock
 flock -n 9 || { echo '安装或巡检执行中，请稍后重试';exit 1; }
fi
DIR="$BASE/repair/$NODE_ID"
mkdir -p "$DIR" /usr/local/lib/auto-node-simple
chmod 700 "$BASE/repair" "$DIR"
REPAIR_TMP=$(mktemp -d);REPAIR_OK=0
for file in settings.json curl.conf;do [ ! -f "$DIR/$file" ] || cp -p "$DIR/$file" "$REPAIR_TMP/$file";done
repair_cleanup() {
 code=$?
 if [ "$REPAIR_OK" != 1 ];then
  for file in settings.json curl.conf;do
   if [ -f "$REPAIR_TMP/$file" ];then cp -p "$REPAIR_TMP/$file" "$DIR/$file";else rm -f "$DIR/$file";fi
  done
  if [ -f "$REPAIR_TMP/cron-changed" ];then crontab "$REPAIR_TMP/cron-before" || true;fi
 fi
 rm -rf "$REPAIR_TMP";exit "$code"
}
trap repair_cleanup EXIT
# One jq process instead of multiple parses of the same inbound.
IFS='|' read -r KIND TRANSPORT <<EOF
$(jq -r '.inbounds[0] | [.type,(.transport.type // "")] | join("|")' "$BASE/configs/$NODE_ID.json")
EOF
HOST=''
if [ "$KIND" = vless ];then
 if [ "$TRANSPORT" = ws ];then
  KIND=argo
  LINK=$(cat "$BASE/state/$NODE_ID.link")
  HOST=$(printf '%s' "$LINK" | sed -n 's/.*[?&]sni=\([^&#]*\).*/\1/p')
  [ -n "$HOST" ] || { echo '缺少固定隧道 SNI';exit 1; }
 else KIND=reality;fi
fi
case "$KIND" in argo|reality|hysteria2) ;; *) echo '不支持此节点协议';exit 1;; esac
jq -n --arg panel "$PANEL" --arg protocol "$KIND" --arg hostname "$HOST" '{panel:$panel,protocol:$protocol,hostname:$hostname}' > "$REPAIR_TMP/settings-new"
chmod 600 "$REPAIR_TMP/settings-new";mv "$REPAIR_TMP/settings-new" "$DIR/settings.json"
if [ "${REPAIR_KEEP_TOKEN:-0}" != 1 ];then
 printf 'header = "Authorization: Bearer %s"\n' "$REPORT_TOKEN" > "$REPAIR_TMP/curl-new"
 chmod 600 "$REPAIR_TMP/curl-new";mv "$REPAIR_TMP/curl-new" "$DIR/curl.conf"
fi
[ -s "$DIR/curl.conf" ] || { echo '缺少巡检凭据';exit 1; }
printf '%s' '__WATCHDOG_B64__' | base64 -d > /usr/local/lib/auto-node-simple/watchdog.sh.new
chmod 700 /usr/local/lib/auto-node-simple/watchdog.sh.new
mv /usr/local/lib/auto-node-simple/watchdog.sh.new /usr/local/lib/auto-node-simple/watchdog.sh
if [ "$INIT" = systemd ];then
 cat > /etc/systemd/system/auto-node-repair.service <<EOF
[Unit]
Description=Auto Node Simple shared health check
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/sh /usr/local/lib/auto-node-simple/watchdog.sh --all
TimeoutStartSec=600
Nice=10
Environment=MALLOC_ARENA_MAX=1
EOF
 cat > /etc/systemd/system/auto-node-repair.timer <<EOF
[Unit]
Description=Auto Node Simple shared minute check
[Timer]
OnBootSec=30
OnCalendar=*-*-* *:*:00
AccuracySec=1s
[Install]
WantedBy=timers.target
EOF
 systemctl daemon-reload
 systemctl enable --now auto-node-repair.timer
 # Consolidate only this application's old per-node timers, after enabling the new one.
 REMOVED=0
 for old in "$BASE"/repair/*;do
  [ -d "$old" ] || continue
  old_id=${old##*/}
  case "$old_id" in ''|*[!a-f0-9]*) continue;; esac
  if [ -f "/etc/systemd/system/auto-node-repair-$old_id.timer" ];then
   systemctl disable --now "auto-node-repair-$old_id.timer"
   rm -f "/etc/systemd/system/auto-node-repair-$old_id.timer" "/etc/systemd/system/auto-node-repair-$old_id.service"
   REMOVED=1
  fi
 done
 [ "$REMOVED" = 0 ] || systemctl daemon-reload
else
 (crontab -l 2>/dev/null || true) > "$REPAIR_TMP/cron-before"
 # Preserve unrelated jobs. All node settings remain separate and private.
 sed '/# simple-repair-[a-f0-9][a-f0-9]*$/d; /# simple-repair-all$/d' "$REPAIR_TMP/cron-before" > "$REPAIR_TMP/cron-new"
 printf '* * * * * /bin/sh /usr/local/lib/auto-node-simple/watchdog.sh --all >/dev/null 2>&1 # simple-repair-all\n' >> "$REPAIR_TMP/cron-new"
 touch "$REPAIR_TMP/cron-changed";crontab "$REPAIR_TMP/cron-new"
 rc-update add crond default >/dev/null;rc-service crond start >/dev/null
fi
REPAIR_OK=1
if [ "${REPAIR_INSTALL_LOCKED:-0}" != 1 ];then flock -u 9;fi
if [ "${REPAIR_DEFER_REPORT:-0}" != 1 ];then /bin/sh /usr/local/lib/auto-node-simple/watchdog.sh "$NODE_ID" 9>&- || true;fi
echo '轻量巡检已安装：同机共用一个定时任务，节点串行检查，检查结束即退出。'
