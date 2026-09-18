# ChatGPT in the Loop 01：不用 Codex，也能让 ChatGPT 真正跑代码

> 这一篇先不讲安装。它只回答一个问题：为什么 ChatGPT 已经会写代码了，我们还需要给它接一个真实的执行环境？

很多人第一次让 ChatGPT 写程序时，会有一种很强的感觉：它已经会开发软件了。你描述需求，它能写代码；贴出错误，它能分析；把一段旧代码给它，它还能重构。

但真正做过稍微复杂一点项目以后，会很快碰到一个问题：

**代码写出来了，不等于代码真的能运行。**

这篇文章想讨论的，就是这个看起来很小、实际上很关键的缺口。

---

## 1. ChatGPT 会写代码，但“会写”和“跑得起来”是两回事

普通 ChatGPT 很像一个会读代码、会分析问题、也会修改程序的人。它可以告诉你：

- 某个函数应该怎么改；
- 某个错误可能在哪里；
- Docker 配置是否合理；
- 数据库连接代码有没有明显问题；
- 一段程序在逻辑上是否成立。

但它给出的很多判断，本质上仍然是：

> “从代码上看，应该可以。”

而软件开发真正需要的是：

> “我已经在真实环境里跑过，它确实可以。”

这两句话中间，可能隔着很多问题。

例如，一个命令名写错了；某个 Python 包版本不兼容；Docker image 可以构建，但容器启动失败；数据库正常启动了，但应用连不上；几个模块单独测试都通过，放在一起却出错；JSON、Parquet 或其他数据格式在真实读写时才暴露问题。

这些错误并不说明 ChatGPT “不聪明”。原因很简单：**有些问题只有真正执行以后才会出现。**

可以把它想成工程图纸。ChatGPT 很会看图纸，也会改图纸，但图纸画完以后，机器还是要真的装起来、通电、运行，才能知道有没有问题。

所以如果想让 ChatGPT 真正进入软件开发流程，就必须补上一块能力：

**让它修改后的代码进入一个真实、可重复的运行环境。**

---

## 2. 为什么这里会出现 GitHub？它不只是一个“放代码的网站”

很多非开发者第一次接触 GitHub，会把它理解成“程序员上传源码的网站”。这没有错，但只说了一小部分。

可以把 GitHub 理解成一个项目的“中央代码仓库”。

代码可能来自不同地方：

```text
自己的电脑
     ↓
   GitHub
     ↓
家里的服务器
     ↓
另一台开发电脑
```

大家围绕同一份代码工作，而且每一次修改都有记录。谁改了什么、什么时候改的、能不能回到以前的版本，都可以追踪。

这件事对 ChatGPT 很重要。

如果 ChatGPT 只是把一段修改后的代码贴在聊天窗口里，那仍然是一份“建议”。但如果修改真正进入 GitHub 仓库，它就进入了项目正式的版本管理流程。

GitHub 还有另一项很重要的能力：**GitHub Actions**。

它可以理解成 GitHub 里的自动流水线。你可以提前告诉 GitHub：

```text
只要代码有变化：
1. 安装程序
2. 运行测试
3. 构建 Docker
4. 启动数据库
5. 启动应用
6. 检查应用是否真的可以工作
```

以后每次 push 或 pull request，GitHub 都可以自动执行这些步骤。

所以 GitHub 在这套系统里做的不只是“存代码”，它还负责：

```text
保存代码
→ 发现代码发生变化
→ 安排测试任务
→ 保存测试结果和日志
```

这已经离“让 ChatGPT 真正验证代码”很近了。

---

## 3. GitHub 已经能运行代码，为什么还需要 Self-hosted Runner？

GitHub Actions 负责安排任务，但任务总得在一台真实电脑上执行。

GitHub 默认可以临时提供这台电脑。比如 workflow 里常见的：

```yaml
runs-on: ubuntu-latest
```

意思大致就是：

> 请 GitHub 给我准备一台 Linux 机器，在上面执行下面这些任务。

这种机器通常叫 **GitHub-hosted runner**。

对于偶尔运行的小项目，它非常方便：不需要自己安装 Linux，不需要维护服务器，用完以后环境就被回收。

问题出现在项目越来越多、测试越来越重以后。

对于 private repository，高频运行 GitHub-hosted Actions 会受到套餐额度和计费规则影响；Docker build、数据库启动、integration test 这类任务又很容易把运行时间拉长。多个私有项目同时开发时，这些使用量会继续累积。

另外，GitHub-hosted runner 的机器是 GitHub 提供的。它很干净，但你不能像管理自己电脑一样完全控制它。很多环境、缓存、系统工具和长期状态都需要每次重新准备。

所以真正的问题并不是：

> GitHub 能不能运行代码？

当然能。

真正的问题是：

> **如果我长期、高频地开发多个私有项目，是否一定要一直租用 GitHub 提供的临时计算资源？**

如果家里本来就有一台长期在线的 NAS、服务器、mini PC，或者你已经有一台 VPS，就出现了另一种选择：

**GitHub 继续负责发任务，但把真正干活的电脑换成自己的。**

这就是 **self-hosted runner**。

可以把它理解成：

```text
以前：

GitHub Actions
      ↓
GitHub 提供的电脑
      ↓
运行程序


现在：

GitHub Actions
      ↓
自己的 Debian 电脑
      ↓
运行程序
```

GitHub 的调度、日志、PR 检查仍然都保留，只是计算资源换成自己的机器。

---

## 4. 我没有把服务器直接交给 ChatGPT，而是给它一间“实验室”

这里还有一个很重要的问题：安全。

最简单粗暴的方案当然是：

```text
ChatGPT
   ↓
 SSH
   ↓
正式服务器
```

这样 AI 确实可以直接执行命令，但问题也很明显。正式服务器上可能还有数据库、API Key、Docker、其他服务，甚至家庭文件。为了让 ChatGPT 调试一段代码，没有必要把这些权限一起交出去。

我的做法是单独准备一个隔离的 Debian VM：

```text
Unraid
├── 正式服务
│
└── Debian CI VM
      ├── Docker
      └── GitHub self-hosted runner
```

ChatGPT 不直接登录这台 VM。

它只和 GitHub 这一层协作：

```text
ChatGPT
   ↓
GitHub Repository
   ↓
GitHub Actions
   ↓
Debian CI VM
   ↓
真实运行
```

而且 CI VM 不需要接触生产环境的敏感内容。例如：

- 不挂载正式的 appdata；
- 不把 Unraid host 的 Docker socket 暴露进去；
- 不放真实交易 API Key；
- 不放生产数据库密码；
- 不让普通 CI 直接操作正式业务数据。

这样做的核心不是“绝对安全”，而是**把执行范围限制在一个专门用于测试的环境里**。

我很喜欢一个类比：

> 我没有让 ChatGPT 直接进入工厂生产线，而是给它接了一间实验室。

它可以做实验、失败、重试，但实验室和正式生产环境之间还有一层边界。

---

## 5. 真正改变的不是 CI，而是 ChatGPT 的反馈循环

做到这里以后，整个开发流程就发生了变化。

以前更像：

```text
人：
“帮我改这个程序。”

ChatGPT：
“改好了，从代码上看应该可以。”
```

现在可以变成：

```text
ChatGPT
   ↓
修改代码
   ↓
GitHub
   ↓
GitHub Actions
   ↓
Self-hosted runner 真正运行
   ↓
PASS / FAIL / logs
   ↓
ChatGPT 再分析
```

这就形成了一个很基本、但非常重要的循环：

```text
思考
 ↓
修改
 ↓
执行
 ↓
观察结果
 ↓
继续思考
```

它并不只是编程方法，其实和科学实验非常接近：

> 提出假设 → 做实验 → 看结果 → 修正假设。

所以这套东西并不是另一个 Codex，也不是另一个 coding agent。

Codex、Claude Code、Copilot Coding Agent 解决的是“AI 怎样更主动地写和改代码”。

这里解决的是另一个问题：

**无论代码是谁写的，怎么给 AI 一个真实、可控、可以反复验证的执行环境。**

可以把整个系统简单理解为：

```text
ChatGPT = 思考和分析
GitHub = 代码、版本和任务调度
Self-hosted runner = 真正动手执行
CI logs = 实验结果
```

下一篇，我会完全转到实操：怎样从零准备 Debian、Docker 和 GitHub self-hosted runner，并让第一个私有仓库真正跑起来。

---

### 延伸阅读

- GitHub Actions: https://docs.github.com/actions
- GitHub-hosted runners: https://docs.github.com/actions/using-github-hosted-runners
- Self-hosted runners: https://docs.github.com/actions/hosting-your-own-runners
