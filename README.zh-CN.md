[English](README.md)

# github-runner-tools

`github-runner-tools` 是一组用于在 Linux 主机上注册和管理 GitHub repository-level self-hosted runner 的轻量脚本。GitHub Actions 仍然负责任务触发、调度、状态和日志，真正的计算则放到你自己的服务器、NAS、mini PC、虚拟机或 VPS 上完成。

这个项目不会替代 GitHub Actions，也不会修改应用代码或自动部署应用。它解决的是一个更窄的问题：让多个仓库使用官方 GitHub Actions Runner 时，安装、注册和日常管理更简单，也更保守安全。

当前主要面向 Debian 12/13 和 systemd。x86_64 路径，包括本地 artifact 归档主链，已经在真实 Debian self-hosted runner 主机上验证通过。脚本已经包含 ARM64 的下载与识别逻辑，但在真实 ARM64 主机完成验证之前，仍不把它作为已经验证的平台。

## 当前验证状态

当前实现已经通过完整仓库测试，并在真实 Debian self-hosted runner 上完成实机验证。completed-job hook、本地归档、manifest/hash 发布、GitHub Step Summary、已有 runner migration，以及真实 workflow 执行都已经实际跑通。

目前主要剩余的平台级限制是 ARM64：代码路径已经实现，但还没有在真实 ARM64 runner 上验证。

## 快速开始

请使用最终应该拥有 runner 的普通 Linux 用户执行脚本，不要直接用 root。默认安装位置是这个用户真实的 home 目录。

先安装基础工具：

```bash
sudo apt update
sudo apt install -y ca-certificates curl jq tar coreutils rsync util-linux
```

然后进入目标 GitHub 仓库：

```text
Settings
→ Actions
→ Runners
→ New self-hosted runner
```

选择 Linux 和与主机相符的架构，从 GitHub 显示的 `config.sh` 命令里复制临时 registration token。

### 一次性的本地归档平台配置

当前版本默认会把 self-hosted job 完成时的 workspace 归档到 Debian 本地。因此，在以后注册新 runner 之前，先在这台 runner 主机上做一次平台级配置。

这一步需要完整 clone 仓库：

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools

bash scripts/setup-local-archive.sh --dry-run
bash scripts/setup-local-archive.sh --apply
```

默认是 dry-run。先检查脚本解析出的路径、用户和权限，再使用 `--apply` 真正写入 host-level 配置。

请使用实际拥有 runner 的普通 Linux 用户执行，不要写成 `sudo bash scripts/setup-local-archive.sh`。脚本只有在写入 `/srv`、`/etc` 和 `/usr/local/lib` 这些 host-level 位置时才会在内部调用 sudo。

默认归档位置是：

```text
/srv/github-actions-archive
```

已有 runner 可以先检查，再一次性迁移：

```bash
bash scripts/enable-local-archive.sh --dry-run
bash scripts/enable-local-archive.sh --apply
```

`--apply` 只会对经过 metadata 验证的 runner 配置统一的 `ACTIONS_RUNNER_HOOK_JOB_COMPLETED`，然后重启对应 service。如果原来已经有不同的 completed hook，脚本会停止，不会直接覆盖。

### 一行注册 Runner

完成上面这一次平台配置以后，以后新仓库仍然可以继续使用一条命令注册 runner。

在第一个正式 tag 发布之前，可以使用开发版本：

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/main/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

脚本会从 `/dev/tty` 读取 registration token，而不是从标准输入读取，所以 `curl | bash` 不会和 token 输入冲突。token 输入时不会显示，也不会被脚本保存。

正式发布 tag 后，更建议固定版本：

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/VERSION/scripts/register-runner.sh \
  | bash -s -- OWNER/REPO
```

如果希望先检查脚本再执行：

```bash
curl -fsSL https://raw.githubusercontent.com/ernestyu/github-runner-tools/VERSION/scripts/register-runner.sh \
  -o register-runner.sh

less register-runner.sh
bash register-runner.sh OWNER/REPO
```

### 完整管理方式

如果还需要状态检查和删除工具，可以 clone 整个仓库：

```bash
cd ~
git clone https://github.com/ernestyu/github-runner-tools.git
cd github-runner-tools

bash scripts/register-runner.sh OWNER/REPO
bash scripts/status-runners.sh
```

clone 方式和一行 bootstrap 使用相同的默认规则。两种方式现在都要求这台主机已经完成一次本地归档平台配置。

## 本地 Artifact 归档

通过当前工具注册的 runner 会自动配置统一的 completed-job hook。普通 workflow steps 结束以后，hook 会把最终 workspace 保存到：

```text
/srv/github-actions-archive/OWNER/REPO/RUN_ID/attempt_N/JOB_KEY/
```

每个成功归档包含 `workspace/`、`manifest.json` 和 `manifest.sha256`。默认会排除 `.git/`、`node_modules/`、虚拟环境、Python bytecode cache 和 pytest cache 这类可重新生成的内容，但不会默认排除 `data/`、`results/`、`reports/`、`artifacts/`、`output/`、`checkpoints/` 等研究结果目录。

归档采用 fail-closed：如果本地归档失败，hook 会输出稳定的 `LOCAL_ARTIFACT_*` 错误并返回非零。默认 disk guard 是剩余空间低于 15% 时拒绝开始新的归档，默认 copy timeout 是 3600 秒。

completed hook 会在归档完成后写 GitHub Step Summary。这条“零仓库额外配置”的 Summary 路径已经在真实 Debian self-hosted runner 上完成实机验证，因此现在作为默认路径使用。项目仍然保留 reusable fallback action：

```text
.github/actions/local-artifact-summary
```

Retention 清理由独立脚本完成，不放进 job completion hook：

```bash
bash scripts/cleanup-local-artifacts.sh --dry-run
bash scripts/cleanup-local-artifacts.sh --apply
```

默认 retention 是 90 天。某个 Run 根目录存在 `.keep` 时，自动 cleanup 永远跳过这个 Run。

已有 GitHub Actions artifact 可以下载并迁移到本地：

```bash
bash scripts/migrate-github-artifacts.sh OWNER/REPO
```

默认只做 download + verify，不删除 GitHub 远端 artifact。只有显式使用 `--delete-after-verified`，而且本地验证已经 PASS，才会执行远端删除。

## 脚本会创建什么

对于 `example/project-a`，默认本地身份同时包含 owner 和 repo：

```text
example--project-a
```

runner 默认安装在当前用户 home 下：

```text
~/actions-runner-example--project-a
```

默认 runner name 和自定义 labels 是：

```text
runner name: local-ci-example--project-a
labels:      local-ci,project-a
```

本地身份包含 owner，是为了避免 `example/project-a` 与 `another/project-a` 这样的同名仓库发生目录冲突。本地身份最多 64 个字符；超长时会确定性截断，并附加 8 位 SHA-256 后缀。

正式下载之前，脚本会检查当前用户、sudo、systemd、必要命令、安装目录、TTY 和 CPU 架构。随后从官方 `actions/runner` release 下载匹配架构的 runner，在 GitHub 提供 digest 时校验 SHA-256，运行官方依赖安装脚本，完成注册、systemd service 安装和启动，并输出最终状态。

脚本默认**不会**传入 `--replace`。如果 GitHub 远端已经存在同名 runner，注册应该失败，而不是静默替换已有 runner。

## 固定 GitHub Runner 版本

默认会安装最新官方 GitHub Actions Runner。如果希望固定 runner binary 版本，可以使用：

```bash
RUNNER_VERSION=2.328.0 \
  bash scripts/register-runner.sh OWNER/REPO
```

或者：

```bash
bash scripts/register-runner.sh --runner-version v2.328.0 OWNER/REPO
```

`2.328.0` 和 `v2.328.0` 两种写法都可以，脚本会统一处理。

需要区分两种版本：固定 `github-runner-tools` 的 URL，只是固定 bootstrap 脚本逻辑；只有同时指定 `RUNNER_VERSION` 或 `--runner-version`，才会固定实际安装的 GitHub Actions Runner binary。

## Workflow 与日常管理

注册成功后，可以在目标仓库 workflow 中使用 repository-specific label：

```yaml
runs-on: [self-hosted, Linux, X64, project-a]
```

ARM64 对应的系统架构 label 是 `ARM64`。

检查当前用户 home 下的 runner：

```bash
bash scripts/status-runners.sh
```

删除 runner：

```bash
bash scripts/remove-runner.sh OWNER/REPO
```

删除工具同时兼容新版 owner+repo 目录和旧版 repo-only 目录。对于旧目录，脚本不会只根据目录名猜它属于哪个仓库，而是必须读取 `.runner` 元数据确认 OWNER/REPO。无法确认身份时会停止，不会删除。

systemd service 卸载失败时，本地 runner 目录也不会继续被删除。旧版 runner 不会被自动改名或迁移。

## 自定义设置

可以覆盖安装目录、runner name、labels 和 runner binary 版本：

```bash
RUNNER_BASE_DIR=/srv/github-runners \
RUNNER_NAME=my-runner \
RUNNER_LABELS=local-ci,project-a,docker \
RUNNER_VERSION=2.328.0 \
  bash scripts/register-runner.sh OWNER/REPO
```

自定义 `RUNNER_BASE_DIR` 必须已经存在，而且当前用户需要有进入、读取和写入权限。bootstrap 不会自动对任意目录执行 `sudo mkdir` 或 `sudo chown`。

v1 不支持跨用户安装。执行 `config.sh` 的用户、runner 文件所有者和 systemd service 用户必须是同一个普通 Linux 用户。

如果上一次失败注册留下了非空但尚未配置的目标目录，默认会停止。确认该目录确实只是失败残留后，可以显式清理：

```bash
bash scripts/register-runner.sh --clean-incomplete OWNER/REPO
```

如果目录中已经存在 `.runner`，这个选项不会删除它。

## 测试

目前包含原有 runner 管理测试，以及本地 artifact 的 unit / integration / failure-path 测试：

```bash
bash tests/run-all.sh
```

归档测试覆盖路径校验、hook 配置、workspace 归档、默认 exclusion、symlink 安全、JOB_KEY 碰撞、failure manifest、disk guard、timeout、retention cleanup、已有 runner migration 和 GitHub artifact migration。

完整自动测试已经在真实 Debian CI 主机上通过，本地 artifact 主链也已经用实际 self-hosted runner 跑通。实机验证覆盖 runner hook 安装、job 完成后的 workspace 归档、manifest/hash 发布、GitHub Step Summary、已有 runner 的迁移与 systemd 重启，以及真实 workflow 执行。

ARM64 路径已经实现，但仍未在真实 ARM64 主机上验证。

## 安全与限制

Self-hosted runner 会执行 repository workflow 中的命令，因此应该把 runner 主机看成真正的代码执行环境。条件允许时，应与生产环境隔离。

不要提交 registration/removal token、PAT、SSH private key、生产 API key、数据库密码或其他长期 secrets。也不要为了方便，把宿主机 Docker socket 或无关的生产数据直接暴露给 CI。

当前项目只处理 systemd Linux 上的 repository-level runner。Organization runner group、自动扩缩容、Kubernetes、Windows、macOS、跨用户安装、自动生成 token、自动修改 workflow 和生产部署都不属于 v1 范围。
