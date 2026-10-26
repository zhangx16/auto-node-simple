#!/bin/sh
# One-time upgrade of the complete node installation; reuse local node credentials.
set -eu
umask 077
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
[ "$(id -u)" = 0 ] || { echo '请以 root 执行';exit 1; }
BASE=/usr/local/etc/auto-node-simple
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for settings in "$BASE"/repair/*/settings.json;do
 [ -f "$settings" ] || continue
 DIR=${settings%/settings.json};NODE_ID=${DIR##*/}
 case "$NODE_ID" in ''|*[!a-f0-9]*) continue;; esac
 [ -s "$DIR/curl.conf" ] && [ -s "$BASE/configs/$NODE_ID.json" ] || continue
 PANEL=$(jq -r .panel "$settings")
 case "$PANEL" in https://*|http://127.0.0.1:*|http://localhost:*) ;; *) continue;; esac
 if curl --noproxy '*' --config "$DIR/curl.conf" -fsS --connect-timeout 5 --max-time 30 "$PANEL/repair/$NODE_ID/bootstrap" -o "$TMP/bootstrap.sh";then
  sh -n "$TMP/bootstrap.sh"
  sh "$TMP/bootstrap.sh"
  echo '完整节点安装脚本自动升级已启用。同机其他节点将由共享任务陆续升级，面板会显示版本与结果。'
  exit 0
 fi
done
echo '没有可用的本地节点凭据，或面板已停用自动升级；请检查节点管理中的配置。' >&2
exit 1
