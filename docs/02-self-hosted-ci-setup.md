# ChatGPT in the Loop 02：把 GitHub Actions 接到自己的电脑上

上一篇讲的是思路：ChatGPT 负责理解问题和修改代码，GitHub 负责保存代码、触发任务和记录结果，真正的测试则交给一台自己的机器来完成。这样，即使普通 ChatGPT 本身不能像 coding agent 那样直接进入本地目录运行程序，代码修改以后仍然可以自动进入一个真实环境接受测试。

这一篇不再讨论为什么这么做，而是把这套东西真正搭起来。

听起来好像会很复杂，其实拆开以后只有几件事：先准备一台长期在线的 Linux 电脑；再让 GitHub 知道“这台电脑可以替我干活”；然后把某个代码仓库和这台电脑连起来。现在这套工具又多做了一件事：测试跑完以后，把当时的工作目录和结果自动留在这台机器上，不再依赖 GitHub 的 Artifact 空间保存完整结果。

我自己的环境是一台运行在 Unraid 服务器里的 Debian 虚拟机。不过这一点并不重要。它也可以是一台旧电脑、mini PC、NAS 里的虚拟机，甚至 VPS。只要这台机器能长期在线、能联网，而且你愿意让它执行 CI 测试，就可以。

## 1. 先准备一台专门做测试的 Linux 机器

第一篇里我把这台机器叫作“实验室”。这个比喻其实很合适。代码会在这里被反复下载、运行、报错、重新测试，所以最好不要直接拿正式业务服务器来做。

我用的是 Debian 13，没有安装桌面环境，只保留 SSH 和基本系统工具。Runner 本身不需要图形界面。

安装完 Debian 以后，先创建一个普通用户。我自己的用户名叫 `actions`，但这只是名字，完全可以叫 `debian`、`ubuntu`、`ci` 或别的。现在的脚本不会假定用户名必须是 `actions`，而是自动使用当前执行脚本的普通用户。

这里有一个很重要的原则：不要直接用 root 跑 runner。Root 几乎可以修改整台机器，而 CI 的本质是执行代码。测试代码一旦写错，权限越大，后果越难控制。普通用户足够完成大多数工作；只有安装系统依赖、写入系统目录或者管理 systemd service 这类确实需要管理员权限的步骤，脚本才会临时调用 `sudo`。

系统准备好以后，可以先安装这些基础工具：

```bash
sudo apt update
sudo apt install -y \
  ca-certificates \
  curl \
  jq \
  tar \
  coreutils \
  rsync \
  util-linux
```

如果你的项目要用 Docker，再另外安装 Docker Engine 和 Docker Compose。Docker 不是 self-hosted runner 的必需条件，只是很多项目的测试会用它启动数据库、Web 服务或其他临时环境。

比如可以用：

```bash
docker version
docker compose version
docker run --rm hello-world
```

确认 Docker 是否正常。

这里仍然要保持安全边界。Runner 所在的 Debian 可以拥有自己这台虚拟机里的 Docker 权限，但不要顺手把 Unraid 宿主机的 Docker socket、生产数据库、正式 API Key 或其他高权限资源暴露给它。我们的目标只是让代码有地方测试，不是让 CI 获得整个家庭服务器的控制权。

## 2. GitHub Runner 到底是在做什么

真正开始注册之前，最好先把 GitHub、Actions 和 runner 的关系再说清楚一次。

GitHub Actions 可以理解成一套自动任务系统。我们会在仓库里写一份 workflow，告诉 GitHub：

“代码发生变化以后，请做这些事情。”

比如：

```text
下载代码
安装依赖
运行测试
构建 Docker
检查程序能不能启动
```

但是 GitHub Actions 本身只是负责安排任务。真正执行这些命令的，还是一台电脑。

如果使用 GitHub 默认提供的机器，这台机器叫 GitHub-hosted runner。GitHub 临时给你一台 Linux 环境，任务做完以后回收。

Self-hosted runner 则正好相反：GitHub 仍然负责调度，但机器换成你自己的。

整个关系可以简单理解成：

```text
GitHub Actions：发任务
Runner：接任务
Debian：真正运行命令
```

Runner 本身只是一个长期运行的小程序。它连接 GitHub，等着有没有属于自己的任务。一旦 GitHub 有 job 要执行，它就把任务接回来，在这台 Debian 上运行。

所以所谓“安装 self-hosted runner”，其实就是让 GitHub 和自己的 Linux 主机建立这样一条任务通道。

现在这条通道还多了一层用途：job 跑完以后，runner 会执行一个统一的 completed hook，把最后的 workspace 保存到 Debian 本地。这样完整结果不再需要每次都上传回 GitHub。

## 3. 先做一次平台级配置，以后新项目就简单了

最早的版本里，每个仓库只需要注册 runner。但当我开始把越来越多测试结果留在自己机器上以后，又出现了一个很现实的问题：如果每个项目都自己决定结果放哪里、怎么命名、什么时候清理，过不了多久就会乱。

所以现在多了一次性的“平台配置”。

先把工具仓库 clone 到 Debian：

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools
```

先做 dry-run：

```bash
bash scripts/setup-local-archive.sh --dry-run
```

它会告诉你准备使用哪个 Linux 用户、归档目录在哪里、需要写哪些系统位置。确认没有问题以后，再真正应用：

```bash
bash scripts/setup-local-archive.sh --apply
```

这一步也要用普通 runner 用户执行，不要在命令前直接加 `sudo`。脚本只在确实需要写系统目录时，自己在内部调用 `sudo`。

默认情况下，所有项目的本地结果统一放到：

```text
/srv/github-actions-archive
```

后面的目录会按照 GitHub 自己的身份自动展开，例如：

```text
/srv/github-actions-archive/
└── owner/
    └── repo/
        └── run_id/
            └── attempt_1/
                └── job/
```

里面会保存最终 workspace、`manifest.json` 和 `manifest.sha256`。

这个设计有两个直接好处。

第一，我不需要再给每个项目手工设计结果目录。GitHub 的 repository、run ID、attempt 和 job 本身就已经可以把每次执行唯一地组织起来。

第二，完整结果留在自己机器上，不再受 GitHub Artifact 空间限制。GitHub 继续保存 Actions 日志、状态和运行记录，但几百 MB 甚至几 GB 的完整实验结果没有必要再传一遍回 GitHub。

默认归档会跳过 `.git`、`node_modules`、虚拟环境和 Python cache 这类容易重新生成的内容，但 `data`、`results`、`reports`、`output`、`checkpoints` 这些真正可能有价值的结果目录不会被默认排除。

如果这台机器上已经有以前注册好的 runner，也不用重装。可以先检查：

```bash
bash scripts/enable-local-archive.sh --dry-run
```

确认以后再：

```bash
bash scripts/enable-local-archive.sh --apply
```

它会给已有 runner 加上统一的 completed hook，然后重启对应的 systemd service。

## 4. 以后注册一个 Runner，还是只需要一条命令

平台配置做一次就够了。以后再增加新仓库，流程反而更简单。

GitHub 官方本身已经提供完整的 runner 安装方法。如果只注册一个项目，手工做一次没有什么问题。麻烦的是，个人账号下如果有很多 private repository，每个仓库都要分别做一遍。

现在可以直接这样：

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

这里的 `OWNER/REPO` 就是 GitHub 仓库名。比如一个仓库的网址是：

```text
https://github.com/example/project-a
```

那么就是：

```text
example/project-a
```

这条命令前半段的 `curl` 做的事情很简单：从 GitHub 下载注册脚本。后半段的 `bash` 则直接执行它。

不过在执行之前，还要先去目标仓库取得一个临时授权码。

打开 GitHub 上准备使用 self-hosted runner 的仓库：

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

选择 Linux，再选择与自己的机器相符的架构。普通 Intel/AMD PC 一般是 x64。

GitHub 会显示一段安装命令，其中包含一个临时 registration token。这个 token 可以理解成一次性的“准入证”：它允许一台 runner 注册到这个仓库。

这个 token 不应该写进脚本，也不要放进 Git。执行刚才那条 `curl` 命令以后，脚本会停下来问：

```text
Paste GitHub registration token:
```

这时把 token 粘贴进去即可。

这里其实有一个很容易忽略的技术细节。因为脚本本身就是通过：

```text
curl | bash
```

送进 Bash 的，所以标准输入已经被用来传脚本内容了。如果 token 也从标准输入读取，两者会打架。现在脚本专门从终端设备 `/dev/tty` 读取 token，因此一行命令和交互输入可以同时正常工作。

对普通使用者来说不用理解 `/dev/tty` 的细节，只需要知道：脚本内容从网络进来，token 则从你正在操作的终端进来，两条通道彼此分开。

这套方式已经在真实 Debian 主机上用 private repository 跑过。目标仓库保持 private 完全没有问题，因为公开的只是安装工具；真正把 runner 注册到 private repository 的权限来自 GitHub 临时生成的 registration token。

注册成功以后，脚本会自动识别当前 Linux 用户和 home 目录，判断 CPU 架构，下载 GitHub 官方 runner，向指定仓库注册，配置本地 artifact completed hook，创建 systemd service，启动服务，再输出最终状态。

比如：

```text
example/project-a
```

默认会得到类似：

```text
/home/your-user/actions-runner-example--project-a
```

Runner 名称大致是：

```text
local-ci-example--project-a
```

这里特意同时使用 owner 和 repo，是为了避免不同账号下存在同名仓库时发生冲突。

## 5. 注册以后，不只是看它在线，还要看结果有没有留下来

Runner 注册成功以后，可以直接回到 GitHub：

```text
Settings
→ Actions
→ Runners
```

正常情况下应该能看到刚才注册的 runner，状态显示：

```text
Idle
```

这表示 GitHub 已经知道这台机器在线，而且目前正在等任务。

Debian 上也可以运行：

```bash
bash scripts/status-runners.sh
```

现在它不只会列出 runner name、GitHub URL 和 systemd service 状态，还会同时告诉你本地 archive root、磁盘空间、剩余比例、retention 和 disk guard 状态。

要真正让某个 GitHub Actions job 跑到这台机器上，还需要在项目自己的 workflow 里指定 labels。

例如原来：

```yaml
runs-on: ubuntu-latest
```

表示“请 GitHub 给我一台临时 Linux 机器”。

改成：

```yaml
runs-on: [self-hosted, Linux, X64, project-a]
```

意思就变成：

“我要一台自己管理的 Linux x64 runner，而且它必须属于 project-a。”

GitHub 找到符合这些 labels 的 runner 后，就会把 job 发过来。

现在 job 跑完以后，事情也不会停在“GitHub 上显示绿色或者红色”。completed hook 会继续把最终 workspace 保存到 Debian，本地生成 manifest 和 hash，并把归档结果写进 GitHub Step Summary。这个流程已经在真实 Debian self-hosted runner 上跑通。

于是完整链路现在变成：

```text
ChatGPT 修改代码
→ push GitHub
→ GitHub Actions 触发
→ self-hosted runner 真正运行
→ completed hook 保存最终 workspace
→ GitHub 保留日志和状态
→ Debian 保留完整结果
```

这和最初“把代码复制到本地运行，再把错误贴回 ChatGPT”相比，已经完全是另一种工作方式。

人仍然控制整个开发过程，也仍然决定什么时候继续修改、什么时候接受结果、什么时候发布。区别只是原来那些重复的机械动作——下载、运行、等结果、保存实验输出、再回去找日志——已经可以交给 GitHub Actions 和自己的 runner 自动完成。

至于通知，现在最简单的方式其实已经不一定需要 Slack。GitHub App 自己就可以给手机推送 workflow 运行结果。下一篇真正值得讨论的，不再是“必须接 Slack”，而是怎么让 CI 结果主动找到你，以及什么时候 GitHub 原生通知已经够用，什么时候才需要 Slack 这样的额外通知层。
