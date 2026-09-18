# ChatGPT in the Loop 02：从零搭建自己的 Self-hosted CI 执行层

> 上一篇解决的是“为什么”。这一篇只做一件事：把一台普通 Debian 机器变成 GitHub Actions 的真实执行环境。

如果你已经有 NAS、家用服务器、mini PC、旧电脑或者 VPS，都可以使用类似思路。我的实际环境是 Unraid 上的一台 Debian VM，但关键并不在 Unraid，而在于：**准备一台与正式业务隔离的 Linux 机器，专门执行 CI。**

---

## 1. 先把几个角色说清楚：Debian、Docker、Runner 各自做什么

在开始敲命令之前，先把这套系统里的几个角色弄明白，否则安装过程很容易变成“照着复制但不知道自己在做什么”。

最终结构大致是：

```text
GitHub Repository
       ↓
GitHub Actions
       ↓
Self-hosted Runner
       ↓
Debian
       ↓
Docker / Python / Database / Tests
```

这里：

**Debian** 是真正运行程序的 Linux 系统。它可以是虚拟机，也可以是一台独立电脑。

**Docker** 不是 GitHub Runner 必须依赖的，但对于现在很多项目非常实用。数据库、Web 服务、应用容器都可以在 CI 中临时启动，测试完成以后再销毁。

**GitHub self-hosted runner** 是装在 Debian 上的一个程序。它长期连接 GitHub，等 GitHub 有任务时领取任务并执行。

可以把 runner 想成一个“值班员”：

```text
GitHub：
“cryptoresearch 有一个测试任务。”

Runner：
“收到，我在这台 Debian 上执行。”
```

所以真正干活的是 Debian，Runner 负责接单。

---

## 2. 准备一台隔离的 Debian，并安装 Docker

我的环境是：

```text
Unraid
└── Debian VM
```

资源不需要很夸张。个人项目可以先从类似下面的配置开始：

```text
4 vCPU
6 GB RAM
50–60 GB disk
```

如果以后同时跑多个 Docker-heavy CI，再根据实际情况增加内存和 CPU。

安装 Debian 时不需要桌面环境。保留 SSH server 和 standard system utilities 即可，并创建一个普通用户，例如：

```text
actions
```

日常通过这个用户管理 runner，不要直接用 root 跑 GitHub Runner。

系统安装好以后：

```bash
sudo apt update
sudo apt full-upgrade -y

sudo apt install -y \
  ca-certificates \
  curl \
  gnupg \
  git \
  jq \
  build-essential \
  python3 \
  python3-pip \
  python3-venv \
  unzip \
  tar
```

如果项目需要 Docker，建议安装 Docker 官方 Engine，而不是把 runner 直接放到正式宿主机的 Docker daemon 上。

安装完成后至少检查：

```bash
docker version
docker compose version
docker run --rm hello-world
```

当前登录用户应该能够直接运行 Docker，而不是每次都写 `sudo docker`。如果使用 `actions` 用户，可以把它加入 docker group：

```bash
sudo usermod -aG docker actions
```

然后彻底退出 SSH，再重新登录，让新的 group 生效。

这里要特别注意安全边界：CI VM 可以有自己 VM 内部的 Docker 权限，但不要为了方便把 Unraid host 的 `/var/run/docker.sock` 直接暴露给它。否则 CI job 等于拿到了宿主机 Docker 的控制权。

---

## 3. 为什么我又写了 github-runner-tools

如果只有一个仓库，按照 GitHub 页面提供的命令手工注册 runner，其实并不困难。

麻烦出现在项目越来越多以后。

例如：

```text
ernestyu/CycleEdge
ernestyu/alphapulse
ernestyu/cryptoresearch
ernestyu/ScholarPulse
```

对于个人 GitHub 账号下的 repository-level runner，每个仓库都要分别注册。手工操作时，每多一个项目都要重复：

```text
下载 runner
→ 解压
→ 获取 registration token
→ config.sh
→ 设置名字和 label
→ 安装 systemd service
→ 启动
→ 检查状态
```

所以我把这些重复操作整理成了一个小工具：

```text
github.com/ernestyu/github-runner-tools
```

它本身只是一个管理脚本仓库，真正的 runner 不安装在工具目录里面。

例如：

```text
/home/actions/
├── github-runner-tools/
├── actions-runner-cycleedge/
├── actions-runner-alphapulse/
└── actions-runner-cryptoresearch/
```

先把工具仓库 clone 到 Debian：

```bash
cd ~
git clone git@github.com:ernestyu/github-runner-tools.git
cd github-runner-tools
```

如果仓库是 private，Debian 本身需要先具备访问 GitHub 私有仓库的认证方式，例如 SSH key。

以后更新工具：

```bash
cd ~/github-runner-tools
git pull
```

---

## 4. 给一个私有仓库注册 Runner，并让 GitHub Actions 使用它

假设我们要给：

```text
ernestyu/cryptoresearch
```

增加 runner。

先在 GitHub 打开目标仓库：

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

GitHub 会显示一段配置命令，其中包含一个临时 registration token。这个 token 是短时有效的，而且属于当前仓库，不要提交进 Git。

然后在 Debian 上：

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh ernestyu/cryptoresearch
```

脚本会要求：

```text
Paste GitHub registration token:
```

粘贴刚才的 token。

脚本会自动完成：

```text
下载官方 runner
→ 校验和解压
→ 安装依赖
→ 注册 repository runner
→ 设置 runner name 和 labels
→ 安装 systemd service
→ 启动
→ 输出状态
```

默认结果类似：

```text
Runner directory:
/home/actions/actions-runner-cryptoresearch

Runner name:
unraid-ci-cryptoresearch

Labels:
self-hosted
Linux
X64
unraid-ci
cryptoresearch
```

检查所有 runner：

```bash
cd ~/github-runner-tools
bash scripts/status-runners.sh
```

也可以：

```bash
systemctl --type=service | grep actions.runner
```

完成以后回 GitHub：

```text
Settings → Actions → Runners
```

应该能看到新 runner 处于 `Idle`。

接下来，原来 workflow 里如果写的是：

```yaml
runs-on: ubuntu-latest
```

可以改成：

```yaml
runs-on: [self-hosted, Linux, X64, cryptoresearch]
```

这意味着：

> GitHub 仍然负责启动 workflow，但这个 job 不再去 GitHub 的临时 Linux 机器，而是等待自己的 cryptoresearch runner 来领取。

---

## 5. 第一次真正跑通以后，应该检查什么

第一次成功并不只是看到 GitHub 页面变绿。

一个完整的检查应该包括几层。

先看 GitHub：

```text
push / PR
→ workflow 被触发
→ job 被 self-hosted runner 领取
→ test / build / integration steps 执行
→ PASS / FAIL 和 logs 回到 GitHub
```

再看 Debian：

```bash
bash ~/github-runner-tools/scripts/status-runners.sh
```

如果 CI 中会启动 Docker：

```bash
docker ps
docker system df
df -h
```

长期运行 self-hosted runner 与 GitHub-hosted runner 有一个重要区别：这台 Debian 不会在每次 job 结束后自动销毁。

所以你需要自己注意：

- stale container；
- Docker build cache；
- 磁盘空间；
- runner 更新；
- Debian 系统更新；
- 多个 runner 同时工作时的 CPU / RAM 压力。

还有一个实际问题：registration 失败时，可能已经留下一个下载并解压后的 runner 目录，但 GitHub 注册并没有成功。工具脚本应该尽量检测这种情况；如果出现异常，要先确认是否存在 `.runner` 配置文件，再决定是重试还是清理目录，不要盲目删除已经正常工作的 runner。

做到这里，第二篇的目标就完成了：

```text
ChatGPT 修改代码
      ↓
GitHub
      ↓
GitHub Actions
      ↓
自己的 Debian
      ↓
真实 test / build / Docker
      ↓
GitHub 返回结果
```

下一篇继续解决一个很实际的问题：**CI 已经会自己跑了，但我不想每次都打开 GitHub 页面看它有没有失败。**

我们会把执行结果主动推送到 Slack。

---

项目地址：

- github-runner-tools: https://github.com/ernestyu/github-runner-tools
- GitHub Self-hosted Runner 文档: https://docs.github.com/actions/hosting-your-own-runners
