# ChatGPT in the Loop 02：从零搭建自己的 Self-hosted CI 执行层

## 1. 先把几个角色说清楚：Debian、Docker、Runner 各自做什么

真正开始安装之前，最好先把这套系统里的几个角色弄清楚。否则很容易变成照着命令一路复制，最后虽然跑起来了，却不知道每一层到底负责什么。

Debian 是真正执行测试的 Linux 系统。它可以是一台独立电脑，也可以像我这样，运行在 Unraid 里的虚拟机。Docker 并不是 GitHub Runner 的硬性要求，但很多现代项目会用它临时启动数据库、Web 服务或者应用容器，所以把 Docker 放进 CI 环境会方便很多。GitHub self-hosted runner 则是装在 Debian 上的一个长期运行程序，它负责连接 GitHub，等待任务，然后把 GitHub Actions 发来的命令交给这台 Debian 执行。

换句话说，GitHub Actions 负责决定“什么时候该做什么”，runner 负责接单，Debian 才是真正提供 CPU、内存、磁盘和操作系统环境的那台机器。如果项目还需要 Docker、Python、PostgreSQL 或其他工具，它们也都运行在这台 Debian 里。

这个区分很重要，因为 self-hosted runner 本身并不是一台新机器。它只是把 GitHub 的任务接到你已经准备好的 Linux 主机上。

## 2. 准备一台隔离的 Debian，并安装基础环境

我的实际环境是 Unraid 上的一台 Debian VM，但并不是必须使用 Unraid。NAS、mini PC、旧电脑或者 VPS 都可以，只要它能够长期在线，并且最好与正式业务环境隔离。

对于个人项目，一开始不需要准备很大的机器。我目前采用的是 4 vCPU、6 GB RAM 和大约 50–60 GB 磁盘。这个配置足够跑普通的 Python 测试、Docker build、数据库启动和 integration test。如果以后多个项目同时执行比较重的 CI，再根据真实负载增加 CPU 或内存。

安装 Debian 时没有必要安装桌面环境。保留 SSH server 和 standard system utilities 即可，并创建一个普通用户，例如 `actions`。Runner 日常应该由这个普通用户运行，而不是直接使用 root。

系统安装好以后，先更新系统并安装常用工具：

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

如果项目需要 Docker，建议安装 Docker 官方 Engine。安装完成后，可以用下面三条命令确认 Docker、Compose 和容器运行都正常：

```bash
docker version
docker compose version
docker run --rm hello-world
```

当前登录用户最好能够直接运行 Docker，而不是每次都写 `sudo docker`。如果使用 `actions` 用户，可以执行：

```bash
sudo usermod -aG docker actions
```

然后彻底退出 SSH，再重新登录，让新的用户组生效。

这里仍然要保持第一篇里讲过的安全边界。CI VM 可以拥有自己这台 VM 内部的 Docker 权限，但不要为了方便把 Unraid host 的 `/var/run/docker.sock` 暴露进来，也不要把正式业务数据、生产数据库密码或者 API Key 放进这个测试环境。我们要的是一台可以反复试错的测试机，而不是让 CI 顺手获得生产环境权限。

## 3. 多个私有仓库时，为什么需要 github-runner-tools

如果只有一个仓库，GitHub 页面已经会给出完整的 runner 注册命令，手工操作一次并不复杂。麻烦出现在项目越来越多以后。

我自己有 CycleEdge、AlphaPulse、cryptoresearch、ScholarPulse 等多个私有仓库。对于个人 GitHub 账号下的 repository-level runner，每个仓库都需要单独注册。如果完全手工操作，每增加一个项目，都要重新下载 runner、解压、取得 registration token、执行 `config.sh`、设置 runner 名称和 labels、安装 systemd service、启动服务，再检查状态。

这些步骤本身都不难，但非常重复，而且一旦项目多起来，很容易在目录、名称、labels 或 systemd service 上出错。

所以我把这部分整理成了一个小工具仓库：`github.com/ernestyu/github-runner-tools`。它的目的不是重新实现 GitHub Runner，而只是把 GitHub 官方的注册流程自动化。

工具仓库本身放在：

```text
/home/actions/github-runner-tools
```

真正的 runner 则安装在它的同级目录，例如：

```text
/home/actions/actions-runner-cycleedge
/home/actions/actions-runner-alphapulse
/home/actions/actions-runner-cryptoresearch
```

这样每个仓库都有独立 runner 配置，但 Debian、Docker 和系统工具只需要安装一套。

如果 Debian 已经配置好访问 GitHub 私有仓库的 SSH key，就可以把工具仓库拉下来：

```bash
cd ~
git clone git@github.com:ernestyu/github-runner-tools.git
cd github-runner-tools
```

以后工具有更新，只需要：

```bash
cd ~/github-runner-tools
git pull
```

## 4. 给一个私有仓库注册 Runner，并切换 GitHub Actions

假设现在要给 `ernestyu/cryptoresearch` 增加 self-hosted runner。先打开这个仓库，在 GitHub 页面进入 `Settings → Actions → Runners → New self-hosted runner`，选择 Linux 和 x64。

GitHub 会显示一段配置命令，其中包含一个临时 registration token。这个 token 只用于当前仓库，而且是短时有效的。不要把它写进脚本，也不要提交到 Git。

然后回到 Debian，在工具仓库目录运行：

```bash
cd ~/github-runner-tools
bash scripts/register-runner.sh ernestyu/cryptoresearch
```

脚本会提示粘贴 registration token。输入以后，它会自动完成官方 runner 的下载和解压、必要依赖安装、repository runner 注册、runner name 和 labels 设置、systemd service 安装、服务启动以及最终状态检查。

默认情况下，cryptoresearch 的 runner 会安装到：

```text
/home/actions/actions-runner-cryptoresearch
```

runner 名称是 `unraid-ci-cryptoresearch`，同时会带上 `self-hosted`、`Linux`、`X64`、`unraid-ci` 和 `cryptoresearch` 等 labels。

注册完成后，可以在 GitHub 的 `Settings → Actions → Runners` 页面确认它是否显示为 `Idle`。Debian 上也可以统一检查：

```bash
cd ~/github-runner-tools
bash scripts/status-runners.sh
```

或者直接查看 systemd：

```bash
systemctl --type=service | grep actions.runner
```

最后还要修改项目自己的 GitHub Actions workflow。原来如果使用的是：

```yaml
runs-on: ubuntu-latest
```

现在可以改成：

```yaml
runs-on: [self-hosted, Linux, X64, cryptoresearch]
```

这一步很关键。它告诉 GitHub：这个 job 不再使用 GitHub 临时提供的 Linux runner，而是等待带有 `cryptoresearch` label 的 self-hosted runner 来领取。

## 5. 第一次真正跑通以后，应该检查什么

第一次看到 GitHub Actions 变绿，并不代表后面就完全不用管了。Self-hosted runner 和 GitHub-hosted runner 最大的区别之一，是这台 Debian 会长期存在，不会在每次 job 结束后被自动销毁。

所以第一次跑通以后，最好确认三件事。第一，GitHub 上的 workflow 确实是被自己的 runner 领取，而不是仍然跑在 `ubuntu-latest`。第二，项目里的测试、Docker build、数据库启动和 integration test 能正常结束，并且成功或失败的日志都能回到 GitHub。第三，Debian 自己没有留下异常的容器、磁盘压力或者失控的后台进程。

日常可以通过下面这些命令检查 runner 和 Docker 状态：

```bash
bash ~/github-runner-tools/scripts/status-runners.sh
docker ps
docker system df
df -h
```

长期运行以后，还需要关注 Docker build cache、磁盘空间、runner 更新、Debian 系统更新，以及多个 runner 同时工作时的 CPU 和内存压力。这些都是 self-hosted 带来自由度以后需要自己承担的维护责任。

还有一个很现实的问题是注册失败。如果 registration token 过期或者复制错误，GitHub 可能拒绝注册，但 runner 文件已经下载并解压到本地。这时不要看到目录存在就直接删除，应该先确认这个目录里是否已经有 `.runner` 配置文件。如果没有，通常说明注册没有真正完成，可以清理失败残留后重新注册；如果已经存在，则要先判断 runner 是否已经处于有效状态，避免误删正在工作的实例。

做到这里，第二篇的目标就完成了：GitHub 仍然负责代码、Actions、日志和状态，而真正的测试计算已经迁移到自己的 Debian 主机。ChatGPT 做出的代码修改可以自动进入这套环境接受验证，但它仍然不是自动部署系统，也没有因此获得直接控制服务器的权限。

下一篇继续解决另一个很实际的问题：测试已经会自己跑了，但我不想每次都打开 GitHub 页面检查它有没有失败。我们会把 CI 结果主动推送到 Slack。
