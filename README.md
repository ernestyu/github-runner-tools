# github-runner-tools

用于在一台 Debian CI 主机上快速注册和管理多个 GitHub repository-level self-hosted runner。

当前设计适合这种结构：

```text
/home/actions/
├── github-runner-tools/
├── actions-runner-cycleedge/
├── actions-runner-alphapulse/
└── actions-runner-其他仓库/
```

`github-runner-tools` 只是管理脚本仓库。真正的 GitHub Actions runner **不会安装在这个仓库里面**，而是默认安装到它的同级目录。

因此即使你在：

```text
/home/actions/github-runner-tools
```

中运行：

```bash
bash scripts/register-runner.sh ernestyu/CycleEdge
```

脚本创建的 runner 目录仍然是：

```text
/home/actions/actions-runner-cycleedge
```

脚本不依赖你运行命令时的当前目录，而是根据脚本自己的实际路径计算安装位置。

## 适用环境

当前主要用于：

- Debian 13 / Debian 12
- Linux x64
- GitHub repository-level self-hosted runner
- Docker Engine / Docker Compose 已在主机安装
- 一个 Debian VM 为多个私有 GitHub 仓库提供独立 runner

建议使用普通用户运行，例如：

```text
actions
```

不要直接以 `root` 用户运行 runner 注册脚本。

## 主机前置条件

至少需要：

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  jq \
  tar \
  coreutils
```

如果 CI 需要 Docker，还应提前确认：

```bash
docker version
docker compose version
docker run --rm hello-world
```

当前登录用户应能够直接使用 Docker，而不需要 `sudo docker ...`。

## 获取本工具仓库

这是私有仓库，因此 Debian 主机需要已经具备访问 GitHub 私有仓库的认证方式。

如果已经配置 GitHub SSH key：

```bash
cd ~
git clone git@github.com:ernestyu/github-runner-tools.git
cd github-runner-tools
```

以后更新工具：

```bash
cd ~/github-runner-tools
git pull
```

脚本可以直接用 `bash` 执行，因此不依赖 Git 是否保留 executable bit：

```bash
bash scripts/status-runners.sh
```

## 注册一个 repository runner

### 1. 在 GitHub 生成临时 registration token

进入目标仓库：

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

选择：

```text
Linux
x64
```

GitHub 页面会显示类似：

```bash
./config.sh --url https://github.com/OWNER/REPO --token XXXXX
```

你只需要复制其中的临时 `token`。

**不要把 token 写进本仓库，也不要提交到 Git。**

### 2. 注册 CycleEdge

在 Debian 上：

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh ernestyu/CycleEdge
```

脚本会提示：

```text
Paste GitHub registration token:
```

粘贴刚才从 GitHub 页面取得的 token，然后回车。输入不会显示在终端。

默认结果：

```text
Runner directory: /home/actions/actions-runner-cycleedge
Runner name:      unraid-ci-cycleedge
Custom labels:    unraid-ci,cycleedge
```

### 3. 注册 AlphaPulse

先进入：

```text
https://github.com/ernestyu/alphapulse
→ Settings
→ Actions
→ Runners
→ New self-hosted runner
→ Linux / x64
```

生成新的、属于 AlphaPulse 的临时 token，然后：

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh ernestyu/alphapulse
```

默认结果：

```text
Runner directory: /home/actions/actions-runner-alphapulse
Runner name:      unraid-ci-alphapulse
Custom labels:    unraid-ci,alphapulse
```

每一个 GitHub repository 都需要自己的 registration token；token 不能跨 repository 共用。

## register-runner.sh 会做什么

脚本自动完成：

```text
1. 检查运行用户和必要命令
2. 根据脚本位置确定 runner 基础目录
3. 从 GitHub actions/runner 官方 release 获取最新版 Linux x64 runner
4. 下载 runner
5. 如果 GitHub release metadata 提供 SHA-256 digest，则自动校验
6. 解压 runner
7. 执行官方 installdependencies.sh
8. 注册到指定 repository
9. 创建独立 systemd service
10. 启动 service
11. 输出 runner 状态和后续 workflow 标签
```

脚本不会把 runner 安装进 `github-runner-tools` 子目录。

## 自定义 runner 基础目录

默认 runner 基础目录是 `github-runner-tools` 所在目录的父目录。

例如：

```text
工具仓库：/home/actions/github-runner-tools
默认基础目录：/home/actions
```

如果以后希望改到其他位置，可以显式指定：

```bash
RUNNER_BASE_DIR=/srv/github-runners \
  bash scripts/register-runner.sh ernestyu/CycleEdge
```

此时目录会变成：

```text
/srv/github-runners/actions-runner-cycleedge
```

## 自定义 runner 名字或 labels

通常不需要修改默认值。

如确有需要：

```bash
RUNNER_NAME=my-cycleedge-runner \
RUNNER_LABELS=unraid-ci,cycleedge,docker \
  bash scripts/register-runner.sh ernestyu/CycleEdge
```

GitHub 自带的标签（例如 `self-hosted`、`Linux`、`X64`）由 GitHub runner 自动加入。

## Workflow 使用方法

CycleEdge 推荐：

```yaml
runs-on: [self-hosted, Linux, X64, cycleedge]
```

AlphaPulse 推荐：

```yaml
runs-on: [self-hosted, Linux, X64, alphapulse]
```

这样每个 repository 只会领取属于自己的 runner。

也可以使用共同标签 `unraid-ci`，但当前一个 repository 一个 runner，没有必要只依赖共同标签。

## 查看所有 runner 状态

运行：

```bash
cd ~/github-runner-tools
bash scripts/status-runners.sh
```

也可以直接查看 systemd：

```bash
systemctl --type=service | grep actions.runner
```

查看 runner listener：

```bash
ps aux | grep Runner.Listener | grep -v grep
```

## 查看日志

先查看 service 名：

```bash
systemctl --type=service | grep actions.runner
```

然后例如：

```bash
sudo journalctl -u 'actions.runner*' --since today
```

实时查看：

```bash
sudo journalctl -u 'actions.runner*' -f
```

## 删除一个 runner

先在 GitHub 目标仓库中进入：

```text
Settings
→ Actions
→ Runners
→ 选择对应 runner
→ Remove
```

GitHub 会提供用于删除配置的临时 token。

然后在 Debian：

```bash
cd ~/github-runner-tools
bash scripts/remove-runner.sh ernestyu/CycleEdge
```

脚本会要求粘贴 removal token，并在确认后：

```text
停止 systemd service
卸载 systemd service
从 GitHub 注销 runner
删除本地 actions-runner-* 目录
```

删除操作会再次要求确认。

## Debian 重启后的检查

runner 通过 systemd 运行，所以 Debian 重启后应自动恢复。

```bash
sudo reboot
```

重新登录后：

```bash
cd ~/github-runner-tools
bash scripts/status-runners.sh
```

GitHub 对应仓库的：

```text
Settings → Actions → Runners
```

也应该显示 runner 为 `Idle` 或 `Active`。

## 并发注意事项

一个 repository runner 一次执行一个 job。

如果同一台 Debian VM 注册了：

```text
CycleEdge runner
AlphaPulse runner
ScholarPulse runner
```

GitHub 可以让它们同时执行 job。

对于当前约 4 vCPU / 6 GB RAM 的 CI VM，如果两个项目同时进行 Docker build、PostgreSQL 和 Python integration tests，可能出现明显资源压力。

先保持现有配置即可。如果实际发生内存不足或严重变慢，再增加 VM 内存、CPU，或进一步限制并发。

## 安全说明

不要向本仓库提交：

```text
GitHub registration token
GitHub removal token
GitHub personal access token
SSH private key
生产环境 API key
数据库密码
Cloudflare token
```

GitHub self-hosted workflow 可以在 Debian CI VM 内执行代码，因此应继续保持 CI VM 与 Unraid host 隔离。

不要把以下内容暴露给 runner：

```text
/mnt/user/appdata
/mnt/user/system
/mnt/user/domains
Unraid host /var/run/docker.sock
生产环境 secrets
```

本工具仓库只负责管理 Debian VM 内的 GitHub runner，不负责把 Unraid host 权限交给 CI。
