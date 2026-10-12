# github-runner-tools

在 Debian 上管理 GitHub Actions Self-hosted Runner。终端 CLI 与 Web UI 共用同一套生命周期权威状态、mutation lock、systemd Unit 规则和结构化运维日志。

## 全新 Debian 安装

正式发布版本使用一条命令安装。必须以未来实际拥有 Runner 的**普通非 root 用户**执行：

```bash
curl -fsSL https://github.com/ernestyu/github-runner-tools/releases/latest/download/install.sh | bash
```

不要使用 `sudo bash install.sh`，也不要使用 `curl ... | sudo bash`。安装程序会在需要修改系统文件时自己调用受限的 `sudo`。

普通安装不需要 `git clone`。安装程序会校验固定 release payload 的 SHA-256，然后直接安装：

- CLI；
- 生命周期 Authority；
- Executor；
- Web UI；
- 本地 archive platform；
- systemd services；
- lifecycle state、lock 和 operational log。

首次安装会要求设置 Web 管理员密码。

## 默认 Web：可信局域网 HTTP

安装程序会选择一个具体的 RFC1918 私有 IPv4，并输出类似：

```text
http://192.168.66.3:8765/
```

不会默认监听 `0.0.0.0`，也不会自动选择公网地址。

这个地址安装后固定保存。重启、DHCP 变化、VPN 出现时，Web 不会偷偷改绑到另一个地址。如果原地址已经不属于本机，Web 应明确启动失败，由管理员显式修改 `WEB_BIND_ADDRESS`。

默认 HTTP 仅适合可信家庭网、实验室网或管理网。管理员密码和临时 GitHub token 在网络上没有 TLS 加密，因此不要直接暴露公网、公共 Wi-Fi 或不可信共享网络。

HTTP 模式仍然保留：

- 管理员密码登录；
- server-side session；
- `HttpOnly; SameSite=Strict` Cookie；
- 登录限速；
- CSRF；
- 一次性 confirmation nonce；
- 请求大小限制；
- Token 不落盘、不进入 operational log。

Tailscale、反向代理和 HTTPS 都是可选增强，不是安装依赖。

## CLI

Create：

```bash
/usr/local/lib/github-runner-tools/cli/register-runner.sh OWNER/REPO
```

Status：

```bash
/usr/local/lib/github-runner-tools/cli/status-runners.sh
```

Remove：

```bash
/usr/local/lib/github-runner-tools/cli/remove-runner.sh OWNER/REPO
```

## Web

安装完成后直接打开安装程序输出的 LAN URL。

Web 不自己实现 Runner 生命周期，而是：

```text
Web
 ↓
Executor
 ↓
已安装 CLI + lifecycle authority
```

因此 CLI 和 Web 使用同一套 Create/Status/Remove 安全语义。

## 状态与日志

生命周期状态：

```text
/var/lib/github-runner-tools
```

结构化运维日志：

```text
/var/log/github-runner-tools/operational.jsonl
```

共享 mutation lock：

```text
/run/lock/github-runner-tools/mutation.lock
```

安装事务状态：

```text
/var/lib/github-runner-tools/install-state.json
```

同一 release 的已证明 partial install 可以重新执行同一条安装命令继续；版本、SHA、用户身份或已安装对象不匹配时会 fail closed。

## 开发

Git checkout 只用于开发、测试和贡献，不是普通安装方式。

发布包构建：

```bash
bash scripts/build-release.sh vX.Y.Z
```
