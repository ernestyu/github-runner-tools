# ChatGPT in the Loop 03：代码跑完以后，让 GitHub CI 主动在 Slack 找你

## 1. CI 已经有结果，为什么还需要 Slack？

前两篇已经把自动测试这件事跑通了。ChatGPT 修改代码以后，GitHub Actions 会触发测试，self-hosted runner 负责真正执行，最后 GitHub 会保存成功或失败的结果和完整日志。

问题是，测试结果虽然已经存在，但人未必会及时看到。每次修改代码以后，如果都要自己打开 GitHub、进入 Actions、找到最新 workflow、确认它是不是跑完了，再点进去看成功还是失败，这仍然是一种需要不断主动检查的工作方式。

GitHub Actions 很适合保存完整记录。它知道哪个 commit 触发了任务、哪个 job 失败、运行了多久、具体哪一步报错，也保存完整日志。但它更像一个档案室，适合需要排错时进去查，而不是一个会主动提醒你的地方。

Slack 在这里承担的角色很简单：不是替代 GitHub，而是把真正需要注意的信息主动送到你面前。比如测试成功时，只需要告诉你哪个项目、哪个分支已经通过；测试失败时，再给出更明显的提醒，并附上 GitHub Actions 的链接，让你需要时直接跳回完整日志。

所以这一篇要解决的不是“怎样把 GitHub 搬到 Slack”，而是怎样把 CI 的结果从一个需要主动查看的页面，变成一个会主动通知你的开发反馈。

## 2. 最简单的连接方式：Slack Incoming Webhook 和 GitHub Secret

Slack 提供一种很简单的外部消息入口，叫 Incoming Webhook。可以把它理解成一个专门接收消息的地址。只要 GitHub Actions 在测试结束以后向这个地址发送一段 HTTP 请求，Slack 就能把消息显示到指定 channel。

实际配置并不复杂。先在 Slack 创建一个 App，为它开启 Incoming Webhooks，然后选择一个用来接收 CI 通知的 channel。Slack 会生成一个 webhook URL。

这个 URL 本身相当于一个凭证，所以不能直接写进 GitHub workflow，也不应该提交到代码仓库。更合适的做法是把它保存为 GitHub Actions secret。

进入目标 GitHub 仓库的 `Settings → Secrets and variables → Actions`，新建一个 repository secret，例如：

```text
SLACK_WEBHOOK_URL
```

它的值就是 Slack 生成的 webhook URL。以后 workflow 只需要引用：

```text
${{ secrets.SLACK_WEBHOOK_URL }}
```

这样真正的 webhook 地址不会出现在源码里。

如果有多个仓库，可以选择让它们都发到同一个 Slack channel，也可以给不同项目单独分 channel。个人项目刚开始没有必要设计得太复杂，一个统一的 `#ci` channel，加上清楚的 repository 名称，通常已经足够。

## 3. 让 GitHub Actions 在测试结束后发送通知

最容易理解的做法，是让 workflow 在测试结束以后执行一个额外的通知步骤。这个通知需要知道测试是成功还是失败，还需要知道仓库、分支、commit 和当前 Actions run 的链接。

假设前面的测试 job 叫 `test`，可以再增加一个通知 job：

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

这里有三个地方值得理解。`needs: [test]` 表示通知 job 要等测试完成以后再执行；`if: always()` 表示即使测试失败，通知也不能被跳过；`needs.test.result` 则是 GitHub 已经记录好的测试结果。

这样 Slack 收到的并不是一大段日志，而是一条摘要。成功时可能只是一条“cryptoresearch CI passed”，失败时则是一条更醒目的“cryptoresearch CI failed”，再附上 GitHub Actions 的链接。真正需要排错时，再回 GitHub 看完整日志。

上面的示例为了清楚，通知 job 使用了 `ubuntu-latest`。这意味着发送通知本身也会使用一点 GitHub-hosted runner 资源。因为它只执行一次很短的 HTTP 请求，消耗通常很小。如果希望整个流程完全不再依赖 GitHub-hosted runner，也可以把通知步骤放到 self-hosted runner 上执行，但需要根据现有 workflow 的结构调整。

## 4. 通知不是越多越好，关键是别把 Slack 变成噪声源

CI 一旦接入 Slack，很容易出现另一个问题：消息太多。

如果每个 feature branch、每一次成功、每一个中间步骤都发通知，很快就会让人开始忽略这个 channel。这样虽然技术上“通知成功了”，但实际效果反而变差。

更合理的做法是把 Slack 当成提醒层，而不是日志层。完整事实和执行细节已经保存在 GitHub，Slack 只需要告诉你发生了什么、哪个项目、哪个分支、成功还是失败，以及去哪里看详情。

对于个人项目，可以先采用很简单的规则。例如 main 分支失败一定通知，main 分支成功可以简短记录；feature branch 是否通知，则根据自己的开发习惯决定。如果某个项目非常频繁，也可以只在失败时发消息。

多个项目也不一定需要多个 channel。开始时可以让 CycleEdge、AlphaPulse、cryptoresearch 都进入一个 `#ci`，消息里明确写 repository 名称。如果以后真的出现信息太多，再拆成不同 channel。

通知系统的目的不是让你知道每一件小事，而是让你不用不停刷新 GitHub，同时又不会错过真正重要的失败。

## 5. 从“主动提醒”再往前，就是下一阶段的 AI 开发闭环

做到 Slack 这一步以后，整个流程已经比最开始完整很多。ChatGPT 负责分析和修改代码，GitHub 负责版本管理和触发 Actions，self-hosted runner 负责执行真实测试，GitHub 保存完整日志，而 Slack 把结果主动送到人面前。

这里仍然有一个很重要的边界：这还是一个 human-supervised loop。测试失败以后，是人先看到 Slack，再决定要不要打开 GitHub 日志、继续让 ChatGPT 分析和修改。Slack 只是减少了“我是不是应该去看看 CI”的人工检查，并没有把决策权拿走。

如果继续往前走，下一步才会进入更自动的方向。例如 CI 失败以后，系统自动取得失败日志，把错误交给 AI 分析，由 AI 提出修复，再由人确认是否提交；或者在更明确的安全边界内，让一部分简单错误自动修复并重新触发测试。

一个比较稳妥的演进顺序，是先让 AI 写代码、人手工测试；再让 CI 自动测试；然后让 Slack 主动通知；最后才考虑让 AI 自动读取失败结果，甚至在有限范围内自动修复和重试。

前面三步已经解决了大量实际问题，而且人仍然掌握整个开发过程。它们把“写代码”变成了一个更完整的“修改—验证—反馈”循环，同时没有把正式服务器、生产数据和部署权限直接交给 AI。

到这里，这个系列的前三篇就形成了一条很清楚的主线：第一篇解释为什么需要自动验证，第二篇搭好 self-hosted CI，第三篇再把验证结果主动送回人的工作流。下一步如果继续写，就可以进入“怎样让 AI 自动读取 CI 失败日志并继续分析”这一层。
