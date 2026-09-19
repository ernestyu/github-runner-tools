# ChatGPT in the Loop 03：代码跑完以后，让 CI 主动来找你

前两篇已经把最重要的一段接起来了：ChatGPT 修改代码以后，GitHub Actions 会触发任务，self-hosted runner 在自己的 Debian 上真正运行测试，最后把成功或失败的结果留在 GitHub。

这时候还有一个很小、但每天都会碰到的问题：测试虽然已经自动跑了，人还是要自己去看结果。

如果每次改完代码，都要重新打开 GitHub，进入 Actions，找到刚才那一次运行，再确认它到底成功还是失败，其实还是有一点“守着机器等结果”的感觉。尤其是测试时间比较长的时候，你很可能先去做别的事情，过一会儿才突然想起来：刚才那个 CI 跑完了吗？

所以我想做的第三步很简单：测试结束以后，不再让我去找结果，而是让结果主动来找我。

我用的是 Slack，但这里的思路并不依赖 Slack。它也可以换成其他能接收消息的工具。Slack 只是刚好很适合做这件事：GitHub 继续保存完整日志，Slack 只负责告诉我“这次测试成功了”或者“这次失败了，点这里回 GitHub 看原因”。

## 1. GitHub 负责保存事实，Slack 只负责提醒

GitHub Actions 本身其实已经保存了我们需要的全部信息。

它知道是哪一个仓库触发了任务，在哪个分支，哪个 commit，测试什么时候开始、什么时候结束，哪一步失败，也保留完整日志。

所以没有必要把这些内容全部复制到 Slack。

我更愿意把两者分成两个角色：

GitHub 是完整记录。真正排查问题的时候，还是回 GitHub 看日志。

Slack 是提醒。它只告诉我发生了什么，以及从哪里点回去。

例如成功的时候，一条消息可能只需要包含：

```text
project-a CI passed
main
a1b2c3d
```

如果失败，则告诉我：

```text
project-a CI failed
main
a1b2c3d
View run: ...
```

这样手机收到消息时，我马上就知道这次修改有没有通过。成功了，通常不需要做任何事情；失败了，再点链接回 GitHub 看完整日志。

这和前两篇的思路其实是一致的：不同系统只做自己最合适的事情。ChatGPT 不负责执行代码，GitHub 不负责给我发即时消息，Slack 也不负责保存 CI 的全部历史。

## 2. 让 Slack 给 GitHub 一个“收消息的地址”

要让 GitHub Actions 给 Slack 发消息，最简单的办法之一是使用 Slack 的 Incoming Webhook。

这个名字听起来有一点技术，其实可以把它理解成一个专门的“收件地址”。

Slack 会给你一个 URL。只要某个程序向这个 URL 发送一段符合格式的消息，Slack 就把它显示到指定的 channel 里。

大致过程是先在 Slack 创建一个 App，然后为这个 App 打开 Incoming Webhooks，再指定一个接收 CI 消息的 channel。完成以后，Slack 会生成一个 webhook URL。

这个 URL 很重要，因为拿到它的人理论上就可以往那个 Slack channel 发消息。所以它不应该直接写进 GitHub 仓库里的 workflow 文件。

更合适的做法，是把它放到 GitHub 的 Secret 里。

进入目标仓库：

```text
Settings
→ Secrets and variables
→ Actions
```

新建一个 repository secret，例如：

```text
SLACK_WEBHOOK_URL
```

然后把 Slack 生成的 webhook URL 放进去。

以后 workflow 里只写：

```text
${{ secrets.SLACK_WEBHOOK_URL }}
```

真正的地址不会出现在源码里。

如果有很多项目，一开始也没有必要给每个项目建立一个 Slack channel。我更倾向先建一个统一的 CI channel，例如 `#ci`，消息里写清楚 repository 名称。等以后真的觉得消息太多，再拆分。

## 3. 测试结束以后，再多做一步：发一条消息

有了 webhook 以后，剩下的事情其实很简单。

原来的 GitHub Actions workflow 是：

```text
代码变化
→ 运行测试
→ GitHub 保存结果
```

现在只是在后面再加一步：

```text
代码变化
→ 运行测试
→ GitHub 保存结果
→ 把结果发到 Slack
```

比如原来的测试 job 叫 `test`，可以再增加一个通知 job：

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

如果不熟悉 GitHub Actions，这段配置里真正值得理解的只有几件事。

`needs: [test]` 的意思是：先等测试结束，再发通知。

`if: always()` 很重要。因为如果测试失败，默认情况下后面的步骤可能不会继续执行，但我们恰恰最需要知道失败。所以这里明确告诉 GitHub：无论前面的测试成功还是失败，这个通知都要执行。

`needs.test.result` 则是 GitHub 已经知道的测试结果，我们不需要自己重新判断。

后面的 `curl` 只是把仓库名、分支、commit、测试结果和 GitHub Actions 链接整理成一条消息，发给 Slack。

上面的例子为了容易理解，让通知 job 使用 `ubuntu-latest`。它只发送一次很短的 HTTP 请求，消耗很少。如果特别在意 GitHub-hosted runner 的额度，也可以把通知放到 self-hosted runner 上，或者直接并入现有 job。这个可以等整个流程跑通以后再优化，没有必要一开始就把 workflow 写得很复杂。

## 4. 通知的目的不是让消息更多，而是少看一次 GitHub

把 CI 接进 Slack 以后，很容易走到另一个极端：什么都通知。

每个 branch、每一次成功、每一个中间步骤都发消息，过不了多久，这个 channel 就会变成新的噪声源。最后人的反应还是一样：不看。

所以我觉得通知规则应该尽量简单。

对我来说，Slack 不应该变成第二份 CI 日志。真正的日志已经在 GitHub 里保存得很好。Slack 只需要解决一个问题：

**我不用主动检查，也不会错过需要处理的结果。**

例如可以先采用这样的规则：main 分支失败一定通知；main 分支成功发一条很短的确认；其他开发分支如果太频繁，可以只在失败时提醒。

这样整个过程就变成：

```text
ChatGPT 修改代码
→ GitHub 触发 CI
→ 自己的 runner 运行测试
→ Slack 告诉我结果
→ 失败时再回 GitHub 看日志
```

这时候，人仍然在整个循环里。

Slack 收到失败消息以后，我决定要不要继续处理；需要处理时，再把 GitHub 的错误日志交给 ChatGPT 分析。它不是一个“AI 自动发现失败以后自己不断修改代码”的全自动系统。

我其实更喜欢先停在这里。

因为到了这一步，原来最麻烦的几个人工动作已经基本消失了：不用把代码复制到本地测试，不用一直占着 coding agent 等结果，也不用反复刷新 GitHub 看 CI 有没有结束。

但什么时候继续修改、什么时候接受结果、什么时候发布，仍然是人来决定。

这也是这个系列前三篇最终接起来的样子：第一篇解决“代码改完以后，谁来验证”；第二篇把验证真正放到自己的机器上；第三篇再把验证结果主动送回来。原来需要人不断在几个窗口之间来回检查的过程，到这里已经变成了一个比较顺畅的“修改—测试—反馈”循环。
