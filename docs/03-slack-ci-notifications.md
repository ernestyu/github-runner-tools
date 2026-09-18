# ChatGPT in the Loop 03：代码跑完以后，让 GitHub CI 主动在 Slack 找你

> 前两篇已经完成了“代码可以被真实执行”。这一篇解决的是反馈：执行结束以后，怎样让结果主动来到你面前，而不是让你不停刷新 GitHub。

Self-hosted runner 解决了执行问题，但它不会自动改变人的注意力成本。

如果每次让 ChatGPT 修改完代码以后，你都需要：

```text
打开 GitHub
→ 找 Actions
→ 找最新 workflow
→ 看它还在不在跑
→ 看是成功还是失败
→ 点进去找日志
```

这仍然是一种不断人工检查的工作方式。

更自然的做法是：**程序跑完以后，让结果主动告诉你。**

---

## 1. 为什么还需要 Slack：CI 有结果，不等于人能及时看到

GitHub Actions 本身会保存非常完整的执行记录。

它知道：

- 哪个 commit 触发了任务；
- 哪一个 job 成功或失败；
- 一共运行了多久；
- 哪一步报错；
- 完整日志在哪里。

这些信息对排错非常有用，但 GitHub Actions 页面更像“档案室”，不是所有人都会一直开着它。

我们真正需要的是两层信息：

```text
GitHub
→ 保存完整事实和日志

Slack
→ 告诉我现在有没有值得注意的事情
```

例如成功时只需要：

```text
✅ cryptoresearch CI passed
Branch: main
Commit: abc123
```

失败时才需要更醒目：

```text
❌ cryptoresearch CI failed
Branch: main
Commit: abc123
Workflow: tests
View run: ...
```

Slack 不替代 GitHub。它只是把“你需要注意的变化”送到人的工作流里。

---

## 2. 最简单的连接方式：Slack Incoming Webhook + GitHub Secret

Slack 提供 Incoming Webhook。可以把它理解成一个专门收消息的地址：

```text
GitHub Actions
      ↓
HTTP POST
      ↓
Slack Incoming Webhook
      ↓
某个 Slack channel
```

大致步骤是：

1. 在 Slack 创建一个 App；
2. 为 App 开启 Incoming Webhooks；
3. 选择要接收通知的 channel；
4. Slack 生成一个 webhook URL；
5. 把这个 URL 存到 GitHub repository secret；
6. workflow 结束时向它发送消息。

Slack webhook URL 本身就是凭证，不应该直接写进 workflow 文件或提交到 Git。

在 GitHub 仓库中进入：

```text
Settings
→ Secrets and variables
→ Actions
→ New repository secret
```

创建：

```text
SLACK_WEBHOOK_URL
```

值就是 Slack 生成的 webhook URL。

这样 workflow 只引用：

```text
${{ secrets.SLACK_WEBHOOK_URL }}
```

真正的 URL 不会出现在仓库源码中。

GitHub 官方支持 repository-level Actions secrets；Slack 官方也把 Incoming Webhook 作为从外部系统向 channel 发送消息的标准方式之一。

---

## 3. 在 GitHub Actions 结束时发送成功或失败通知

最容易理解的方式，是在 workflow 中增加一个最后执行的通知 job。

假设前面的测试 job 叫：

```yaml
jobs:
  test:
    runs-on: [self-hosted, Linux, X64, cryptoresearch]
```

可以再增加一个通知 job：

```yaml
  notify:
    needs: [test]
    if: always()
    runs-on: ubuntu-latest

    steps:
      - name: Notify Slack
        env:
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
          TEST_RESULT: ${{ needs.test.result }}
          REPO: ${{ github.repository }}
          BRANCH: ${{ github.ref_name }}
          SHA: ${{ github.sha }}
          RUN_URL: https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: |
          if [ "$TEST_RESULT" = "success" ]; then
            ICON="✅"
          else
            ICON="❌"
          fi

          curl -X POST \
            -H 'Content-type: application/json' \
            --data "$(jq -n \
              --arg text "$ICON $REPO CI: $TEST_RESULT\nBranch: $BRANCH\nCommit: ${SHA:0:7}\n$RUN_URL" \
              '{text: $text}')" \
            "$SLACK_WEBHOOK_URL"
```

这里有几个普通读者值得理解的点。

`needs: [test]` 表示：先等测试结束。

`if: always()` 表示：无论测试成功还是失败，通知步骤都要运行。

`needs.test.result` 是 GitHub 已经知道的测试结果。

而 `RUN_URL` 让 Slack 消息里可以直接回到 GitHub 看完整日志。

需要注意：上面的 `notify` 示例用了 `ubuntu-latest`，它本身也会消耗 GitHub-hosted runner 资源。对于只发送一个 HTTP 请求的通知 job，这个成本通常很小；如果希望完全不再使用 GitHub-hosted runner，也可以让通知步骤直接作为 self-hosted job 的最后一步执行，但 workflow 结构要根据现有测试流程调整。

---

## 4. 不要把 Slack 变成新的“噪声制造器”

把所有 CI 结果都推到 Slack，很快就会产生另一个问题：消息太多。

真正有价值的不是“能发通知”，而是**什么事情值得打断人**。

可以逐渐形成一个简单规则：

```text
main 成功
→ 简短记录

main 失败
→ 明显提醒

feature branch 成功
→ 可以不通知

feature branch 失败
→ 开发期间按需通知

连续失败
→ 需要更高优先级

长时间没有恢复
→ 再提醒
```

对于多个项目，也可以决定是共用一个 channel，还是分开。

例如：

```text
#ci
  CycleEdge
  AlphaPulse
  cryptoresearch

或者：

#cycleedge-ci
#alphapulse-ci
#cryptoresearch-ci
```

开始时没有必要做得过度复杂。一个 `#ci` channel 加上清楚的 repository 名称，通常已经足够。

Slack 消息也不要塞完整日志。真正的完整日志已经在 GitHub。

Slack 最适合提供：

```text
发生了什么
哪个项目
哪个分支
成功还是失败
点哪里看详情
```

也就是“提醒”，而不是“复制 GitHub”。

---

## 5. 从“通知我”再往前一步，就是 AI 开发闭环

做到 Slack 以后，整个链条已经比较完整：

```text
ChatGPT
   ↓
修改 GitHub 代码
   ↓
GitHub Actions
   ↓
Self-hosted runner
   ↓
真实执行
   ↓
PASS / FAIL
   ↓
Slack
   ↓
人看到结果
```

这仍然是一个 **human-supervised loop**。

也就是说，人仍然是中间非常重要的一环：

```text
失败
→ 人看到 Slack
→ 打开 GitHub 日志
→ 把问题继续交给 ChatGPT
→ 再修改
```

但到这里已经可以看到下一步会是什么：

```text
CI failed
   ↓
自动取得失败日志
   ↓
交给 AI 分析
   ↓
AI 判断是否可以安全修复
   ↓
重新提交
   ↓
再次运行 CI
```

这才会逐渐走向半自动甚至更自动的 debugging loop。

我并不认为第一步就应该把人完全移出去。

一个更稳妥的演进顺序是：

```text
第一阶段：AI 写代码，人手工验证
第二阶段：AI 写代码，CI 自动验证
第三阶段：CI 自动验证，Slack 主动通知
第四阶段：AI 自动读取部分失败结果并提出修复
第五阶段：在明确边界内自动修复和重试
```

前面三步已经能显著改变开发体验，而且风险相对容易控制。

整个系列到这里也形成了一条完整主线：

```text
ChatGPT 提供 reasoning
GitHub 提供 versioning 和 orchestration
Self-hosted runner 提供 execution
GitHub logs 提供 evidence
Slack 提供 notification
人继续掌握 decision
```

我没有让 ChatGPT 直接控制服务器。

我只是给它接了一间实验室，并让实验做完以后主动告诉我结果。

---

### 延伸阅读

- Slack Incoming Webhooks: https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/
- Slack GitHub Action + Incoming Webhook: https://docs.slack.dev/tools/slack-github-action/sending-data-slack-incoming-webhook/
- GitHub Actions Secrets: https://docs.github.com/actions/security-guides/using-secrets-in-github-actions
