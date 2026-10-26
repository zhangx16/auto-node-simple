export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ "$(id -u)" = 0 ] || { echo '请以 root 执行';exit 1; }
BASE=/usr/local/etc/auto-node-simple
[ -s "$BASE/configs/$NODE_ID.json" ] && [ -x /usr/local/bin/auto-node-singbox ] || { echo '未找到此 Simple 节点的配置。旧项目迁入的节点需先迁移协议服务，不能直接接管旧管理脚本。';exit 1; }
for tool in curl jq flock ss timeout;do command -v "$tool" >/dev/null || { echo "缺少依赖：$tool";exit 1; };done
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null;then INIT=systemd
elif command -v rc-service >/dev/null;then INIT=openrc
else echo '需要 systemd 或 OpenRC';exit 1;fi
# Reject unexpected filenames even when a setup URL is copied to another host.
case "$NODE_ID" in ''|*[!a-f0-9]*) echo '无效节点 ID';exit 1;; esac
exec 9>/run/auto-node-simple.lock
flock -n 9 || { echo '安装或巡检执行中，请稍后重试';exit 1; }
DIR="$BASE/repair/$NODE_ID"
mkdir -p "$DIR" /usr/local/lib/auto-node-simple
chmod 700 "$BASE/repair" "$DIR"
KIND=$(jq -r '.inbounds[0].type' "$BASE/configs/$NODE_ID.json")
HOST=''
if [ "$KIND" = vless ];then
 if jq -e '.inbounds[0].transport.type=="ws"' "$BASE/configs/$NODE_ID.json" >/dev/null;then
  KIND=argo
  # The fixed tunnel hostname is obtained from the original node link.
  LINK=$(cat "$BASE/state/$NODE_ID.link")
  HOST=$(printf '%s' "$LINK" | sed -n 's/.*[?&]sni=\([^&#]*\).*/\1/p')
  [ -n "$HOST" ] || { echo '缺少固定隧道 SNI';exit 1; }
 else KIND=reality;fi
fi
case "$KIND" in argo|reality|hysteria2) ;; *) echo '不支持此节点协议';exit 1;; esac
jq -n --arg panel "$PANEL" --arg protocol "$KIND" --arg hostname "$HOST" '{panel:$panel,protocol:$protocol,hostname:$hostname}' > "$DIR/settings.json"
printf 'header = "Authorization: Bearer %s"\n' "$REPORT_TOKEN" > "$DIR/curl.conf"
chmod 600 "$DIR/settings.json" "$DIR/curl.conf"
printf '%s' '__WATCHDOG_B64__' | base64 -d > /usr/local/lib/auto-node-simple/watchdog.sh.new
chmod 700 /usr/local/lib/auto-node-simple/watchdog.sh.new
mv /usr/local/lib/auto-node-simple/watchdog.sh.new /usr/local/lib/auto-node-simple/watchdog.sh
if [ "$INIT" = systemd ];then
 cat > "/etc/systemd/system/auto-node-repair-$NODE_ID.service" <<EOF
[Unit]
Description=Auto Node Simple health check
After=network-online.target
[Service]
Type=oneshot
ExecStart=/bin/sh /usr/local/lib/auto-node-simple/watchdog.sh $NODE_ID
TimeoutStartSec=150
EOF
 cat > "/etc/systemd/system/auto-node-repair-$NODE_ID.timer" <<EOF
[Unit]
Description=Auto Node Simple minute check
[Timer]
OnBootSec=60
OnUnitInactiveSec=60
[Install]
WantedBy=timers.target
EOF
 systemctl daemon-reload
 systemctl enable --now "auto-node-repair-$NODE_ID.timer"
else
 command -v crontab >/dev/null || { echo '需要 crontab（Alpine: apk add busybox）';exit 1; }
 TMP_CRON=$(mktemp)
 (crontab -l 2>/dev/null || true) | sed "/# simple-repair-$NODE_ID\$/d" > "$TMP_CRON"
 printf '* * * * * /bin/sh /usr/local/lib/auto-node-simple/watchdog.sh %s >/dev/null 2>&1 # simple-repair-%s\n' "$NODE_ID" "$NODE_ID" >> "$TMP_CRON"
 crontab "$TMP_CRON";rm -f "$TMP_CRON"
 rc-update add crond default >/dev/null;rc-service crond start >/dev/null
fi
flock -u 9
/bin/sh /usr/local/lib/auto-node-simple/watchdog.sh "$NODE_ID" 9>&- || true
echo '自动修复已安装：每分钟巡检，连续失败后申请修复；5 分钟冷却，1 小时最多 3 次，达到上限暂停。'
