# Auto Node Simple

轻量节点安装与订阅面板，支持 VLESS-WS + Cloudflare 固定隧道、VLESS TCP Reality Vision、Hysteria2。通过一次性命令在目标服务器安装，成功回传后持久保存节点；无需 SSH 接入、心跳或常驻管理代理。

## 部署

需要 Docker Compose、HTTPS 反向代理，以及自行准备的 Linux amd64 / arm64 sing-box 与 cloudflared 二进制。

```sh
git clone https://github.com/zhangx16/auto-node-simple.git
cd auto-node-simple
cp .env.example .env
mkdir -p assets data
chmod 700 data
```

编辑 `.env`，将 `PANEL_URL` 设置为自己的完整 HTTPS 面板地址，例如 `https://panel.example.com`。此地址用于安装脚本、结果回调、订阅与同源校验，必须能从目标服务器访问。

将可信来源的二进制放入 `assets/`，文件名为：

- `sing-box-amd64`
- `sing-box-arm64`
- `cloudflared-amd64`
- `cloudflared-arm64`

仓库中的 `assets/manifest.json` 记录参考构建 sing-box 1.14.3、cloudflared 2026.10.4 的 SHA-256。使用其他构建时，先核实来源与版本、验证兼容性，再运行以下命令生成对应清单。生成校验值只用于传输完整性检查，不代替来源验证。

```sh
python3 scripts/prepare_assets.py
docker compose up -d --build
curl -fsS http://127.0.0.1:18901/health
```

默认仅绑定 `127.0.0.1:18901`。通过 Nginx 等代理提供 HTTPS；首次登录使用 `data/credentials.txt` 中自动生成的管理员账号与密码。会话 Cookie 使用 Secure 属性，浏览器登录应使用 HTTPS。

反向代理及应用访问日志可能包含安装、回调、订阅路径中的访问令牌。部署时应关闭这些路径的访问日志，或进行路径脱敏。可在 Compose 中设置应用启动命令：

```yaml
command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18901", "--no-access-log"]
```

## 安装节点

1. 登录面板，选择协议，填写名称、监听端口与客户端公网端口。
2. Reality / Hysteria2 必须填写服务器公网地址；分别放行或映射 TCP / UDP。监听端口与公网端口可不同。
3. Argo 仅支持固定隧道。可手动提供隧道 Token 与公开主机名，或按下一节配置自动创建。客户端地址留空使用 `www.cloudflare.com`；需要其他优选域名或 IP 时自行填写。固定隧道域名仍用于 TLS SNI 与 WebSocket Host。
4. 生成安装命令，在目标服务器以 root 执行。命令 24 小时有效，成功后不可重新下载。

支持 Alpine / Debian / Ubuntu，amd64 / arm64，systemd / OpenRC。安装成功表示本地配置与服务启动完成，不表示公网连通性已验证。Hysteria2 默认使用自签证书，导出链接启用 `insecure=1`。

同机节点共享专用 sing-box 核心，增加节点或修改监听参数会重启该核心，造成短暂中断。Argo 每个节点使用独立 cloudflared 服务。

## Cloudflare 自动创建

可选配置文件 `data/cloudflare.json` 由管理员自行创建，以下均为占位符：

```json
{
  "account_id": "YOUR_ACCOUNT_ID",
  "zone_id": "YOUR_ZONE_ID",
  "zone_name": "example.com",
  "api_token": "YOUR_CLOUDFLARE_API_TOKEN"
}
```

```sh
chmod 600 data/cloudflare.json
```

API Token 需要读取对应 Zone、管理该 Zone 的 DNS，以及管理对应账户的 Cloudflare Tunnel。面板只返回配置状态和 Zone 域名，不回显 API Token；节点只接收其自身的隧道连接 Token。

自动模式创建独立隧道、监听路由和 CNAME，域名留空时生成随机子域名；自定义域名须为该 Zone 下未被占用的一级子域名。已有 DNS 拒绝覆盖。失败时尝试清理本次资源，清理失败则保留所有权记录，供管理员处理。

安装命令过期、取消更新或删除节点均不会自动删除隧道与 DNS；退役云资源需另行处理。

## 节点管理

展开节点可复制链接、编辑和删除。Argo 默认名称后缀为 `-CF`，已有 `-cf` 后缀在启动时同步为大写；Reality 为 `-Vless`，Hysteria2 为 `-hy2`。

名称、客户端地址和公网端口保存后即时同步订阅。修改服务器监听端口或直连 SNI 时生成更新命令，需在原服务器执行；成功回传前保留原节点。更新复用节点 ID、认证信息及 Reality 身份密钥。自动隧道路由随成功回传更新，手动隧道需自行同步 Cloudflare 路由。

每个节点只允许一个未完成更新。取消更新使旧命令失效，不回滚已执行的远端更改。缺少可靠原监听资料的旧节点可能无法编辑。

删除节点需确认，会从面板及所有订阅移除，同时删除相关安装与更新记录，撤销对应命令和回调。不会卸载服务器服务，也不会删除 Cloudflare 资源及其所有权记录。

## 订阅

- 支持创建多份订阅，分别选择已安装节点；新节点需手动勾选加入。
- 提供通用 Base64、原始链接、Clash / Mihomo、sing-box 完整配置。Clash 输出 JSON，亦为合法 YAML。
- 无显式格式时根据客户端 User-Agent 识别，支持 HEAD；显式 `format` 参数优先。
- 支持编辑成员、启用、停用、删除订阅；删除订阅保留节点。
- 安装任务清理不影响已保存节点与订阅。空订阅拉取返回 409。

订阅地址自带访问凭据，无需登录即可拉取，应妥善保管。

## 数据与运维

`data/` 包含账号密码哈希、初始密码文件、会话密钥、数据库、节点连接凭据和可选的 Cloudflare 配置。数据库保存安装任务、节点、订阅及隧道所有权记录；节点配置可能包含运行凭据。请限制目录权限，按需备份，禁止上传源码仓库。

`data/`、`.env`、备份、日志、密钥文件与资产二进制均已加入忽略规则。仓库不附带账号、节点、订阅或真实 Cloudflare 配置。

节点端文件与服务：

- `/usr/local/bin/auto-node-singbox`、`/usr/local/bin/auto-node-cloudflared`
- `/usr/local/etc/auto-node-simple/configs/`：共享核心配置
- `/usr/local/etc/auto-node-simple/state/`：节点链接、证书、隧道 Token 和日志
- `auto-node-simple` 核心服务、`auto-node-<节点ID>` 隧道服务

没有远程卸载、心跳、测活、SSH 运维、自动修复或流量统计。退役节点需在服务器上清理对应服务和配置，注意共享核心仍可能供其他节点使用。

## 测试

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
DATA_DIR="$(mktemp -d)" python -m unittest discover -s tests -p 'test_*.py' -v
```

测试需要可绑定本地端口，以及 wget、flock、timeout。使用隔离数据库，覆盖登录、权限、固定隧道配置、安装回调、节点编辑与删除、订阅导出和安装器兼容性。`tests/integration_install.py` 会修改系统服务，仅供有完整测试依赖的一次性容器使用，不应在生产主机运行。
