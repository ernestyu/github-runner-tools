# github-runner-tools

`github-runner-tools` 是一组用于管理 GitHub repository-level self-hosted runner 的轻量脚本。它适合这样的场景：你有一台长期在线的 Linux 主机，希望让多个 GitHub 仓库使用自己的计算资源运行 GitHub Actions，而不是为每个仓库重复手工下载、注册和维护 runner。

这个项目不会替代 GitHub Actions，也不会修改你的应用代码或自动部署项目。GitHub 仍然负责任务触发、workflow 调度、状态和日志；本项目只负责把 GitHub 官方 self-hosted runner 更方便地安装、注册和管理在你自己的主机上。

当前主要面向 Debian 12 / 13、Linux x86_64 和 systemd 环境。实际使用中，一台主机可以运行多个彼此独立的 repository-level runner。

## 为什么需要它

GitHub Actions 本身已经可以使用 GitHub-hosted runner 执行测试、构建和其他自动任务。对于偶尔运行的小项目，这种方式非常方便。但当多个私有仓库需要频繁运行测试、Docker build 或 integration test 时，GitHub-hosted runner 会受到套餐额度、费用和运行环境控制等因素影响。

Self-hosted runner 可以把真正执行任务的计算资源换成自己的服务器、NAS、mini PC、虚拟机或 VPS，同时继续使用 GitHub Actions 原有的 workflow、日志和状态系统。

如果 GitHub 仓库都属于个人账号，而不是同一个 Organization，每个仓库通常需要分别注册 repository-level runner。官方注册流程并不复杂，但每增加一个仓库，都需要重复下载 runner、解压、执行 `config.sh`、设置名称和 labels、安装 systemd service、启动并检查状态。

`github-runner-tools` 把这些重复步骤整理成几个脚本，让“为一个仓库增加 runner”变成一次标准化操作。

## 快速开始

### 1. 准备主机

至少需要一台 Linux x86_64 主机，并安装以下基础工具：

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  jq \
  tar \
  coreutils
```

建议使用普通用户运行 runner，不要直接用 `root` 注册。例如可以创建一个专门的用户：

```text
actions
```

如果项目的 CI 需要 Docker，应先单独安装 Docker Engine 和 Docker Compose，并确认当前用户可以正常使用：

```bash
docker version
docker compose version
docker run --rm hello-world
```

如果把普通用户加入了 `docker` group，需要退出当前登录会话后重新登录，新的权限才会生效。

### 2. 获取本项目

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools
```

以后更新：

```bash
cd ~/github-runner-tools
git pull
```

脚本可以直接通过 `bash` 执行，不依赖 executable bit：

```bash
bash scripts/status-runners.sh
```

### 3. 在 GitHub 生成 registration token

进入你准备接入 self-hosted runner 的目标仓库：

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

GitHub 会显示一条包含临时 registration token 的 `config.sh` 命令。只需要复制其中的 token，不要把它写进脚本，也不要提交到 Git。

### 4. 注册 runner

回到 Linux 主机，在本项目目录运行：

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh OWNER/REPO
```

例如：

```bash
bash scripts/register-runner.sh yourname/project-a
```

脚本会提示：

```text
Paste GitHub registration token:
```

粘贴刚才生成的临时 token 并回车。输入内容不会显示在终端。

注册成功后，脚本会自动安装并启动对应的 systemd service，并输出 runner 状态以及建议使用的 workflow label。

## 它会创建什么

`github-runner-tools` 只是管理脚本仓库。真正的 GitHub Actions runner 不会安装在这个项目目录里面。

假设工具仓库位于：

```text
/home/actions/github-runner-tools
```

那么默认的 runner 基础目录就是：

```text
/home/actions
```

如果依次为两个仓库注册 runner，目录可能类似：

```text
/home/actions/
├── github-runner-tools/
├── actions-runner-project-a/
└── actions-runner-project-b/
```

脚本根据自身实际路径计算安装位置，所以无论你从哪个当前工作目录调用：

```bash
bash ~/github-runner-tools/scripts/register-runner.sh yourname/project-a
```

runner 仍然会安装到 `github-runner-tools` 的同级目录，而不是放进工具仓库内部。

默认命名规则是：

```text
repository:   yourname/project-a
runner dir:   actions-runner-project-a
runner name:  unraid-ci-project-a
labels:       unraid-ci,project-a
```

GitHub 还会自动增加 `self-hosted`、`Linux` 和 `X64` 等系统 labels。

注册脚本会自动完成以下工作：检查必要命令、读取最新版 GitHub Actions Runner release、下载并解压 runner、在可用时校验 SHA-256 digest、安装官方依赖、注册 repository runner、创建 systemd service、启动服务并输出最终状态。

## Workflow、状态和日常管理

注册完成以后，目标仓库原本如果使用：

```yaml
runs-on: ubuntu-latest
```

可以改成使用仓库自己的 label，例如：

```yaml
runs-on: [self-hosted, Linux, X64, project-a]
```

这样 GitHub Actions 会等待带有对应 label 的 self-hosted runner 来领取 job。

查看当前主机上的所有 runner：

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

查看日志：

```bash
sudo journalctl -u 'actions.runner*' --since today
```

实时跟踪日志：

```bash
sudo journalctl -u 'actions.runner*' -f
```

runner 安装为 systemd service 后，主机重启时应自动恢复。重启后可以再次运行：

```bash
bash ~/github-runner-tools/scripts/status-runners.sh
```

确认各 runner 处于正常状态。

### 删除一个 runner

先进入目标 GitHub 仓库的：

```text
Settings
→ Actions
→ Runners
→ 选择对应 runner
→ Remove
```

GitHub 会提供 removal token。然后在主机运行：

```bash
cd ~/github-runner-tools
bash scripts/remove-runner.sh OWNER/REPO
```

脚本会要求粘贴 removal token，并在再次确认后停止和卸载 systemd service、从 GitHub 注销 runner，并删除对应的本地 runner 目录。

### 自定义安装目录、名称和 labels

默认情况下，runner 会安装在 `github-runner-tools` 所在目录的父目录。如果需要改到其他位置，可以设置：

```bash
RUNNER_BASE_DIR=/srv/github-runners \
  bash scripts/register-runner.sh yourname/project-a
```

也可以覆盖 runner name 和 labels：

```bash
RUNNER_NAME=my-runner \
RUNNER_LABELS=self-ci,project-a,docker \
  bash scripts/register-runner.sh yourname/project-a
```

一个 repository runner 一次只能执行一个 job，但同一台主机上的多个 runner 可以同时执行不同 job。因此，如果多个项目同时进行 Docker build、数据库启动或大型测试，它们会竞争同一台主机的 CPU、内存和磁盘。是否需要限制并发，应根据实际负载决定。

## 安全与限制

Self-hosted runner 会执行 GitHub workflow 中定义的命令，因此应该把 runner 主机当成真正的代码执行环境，而不是普通的只读客户端。建议把 CI 主机与生产环境隔离，并只给它完成测试所需要的权限。

不要把以下内容提交到本项目或普通 CI 配置中：

```text
GitHub registration token
GitHub removal token
Personal Access Token
SSH private key
生产 API key
生产数据库密码
其他长期 secrets
```

如果 CI 主机运行在 NAS、家庭服务器或其他正式宿主机旁边，不建议把宿主机的 Docker socket、生产数据目录或其他高权限接口直接暴露给 runner。尤其不要仅为了方便而把宿主机的 `/var/run/docker.sock` 挂进 CI 环境。

这个项目当前只负责 repository-level self-hosted runner 的安装和管理。它不会自动创建或修改 GitHub Actions workflow，不会自动运行应用部署，也不会把 CI 测试环境转换成生产环境。

当前主要在 Debian 13、Linux x86_64、systemd 环境下使用；设计上也兼容 Debian 12。其他 Linux 发行版、ARM 架构、非 systemd 环境以及 organization-level runner 尚未作为当前版本的主要目标。
