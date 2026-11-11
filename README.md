# 美国政治·市场每小时要闻

每 2 小时抓一次美国政治、金融和科技新闻，加上行情和日程，交给 **Claude Haiku 5.5** 写中文摘要。按量付费，每天 9 版，大约每月 1 美元。

## 每版内容

- 本期要点、美国政治、科技与 AI、宏观与公司、对预测市场的含义、接下来关注
- 每条带来源编号，点击可以打开原文
- 宏观数据表：标普500期货、纳指100期货、道指期货、WTI 原油、黄金、比特币，显示 1 小时和较前收涨跌，并附一句涨跌原因（原因必须来自新闻，找不到就写“无明确消息”）
- 标普500 龙头股（市值前 50）涨幅榜、跌幅榜各 5 只，附涨跌原因（依据 Yahoo Finance 个股新闻）
- 日程从 `../market_calendar/calendar.db` 读取（只读），包括 CPI、FOMC、官员讲话、大公司财报

## 数据来源

Politico、The Hill、NPR Politics、CNBC（含科技）、WSJ Markets、MarketWatch、美联储新闻稿、TechCrunch、The Verge、Google News（美国、商业、科技）的 RSS；行情和个股新闻来自 Yahoo Finance。

去重：已经处理过的新闻记在 `data/seen.sqlite`，下一版不会再出现。上一版的要点也会交给模型，避免重复报道。本期没有新新闻时，不调用模型，也不收费。

## 使用

```bash
cd /Users/shawntong/pm_bot_us/news_digest
../venv/bin/python digest.py             # 生成一版
../venv/bin/python digest.py --dry-run   # 只看发给模型的内容
```

输出：
- `data/latest.md`：最新一版
- `data/digests/YYYY-MM-DD/HH00.md`：历史版本
- 每版生成后会弹出 macOS 通知

## 云端定时运行（GitHub Actions，默认）

`.github/workflows/digest.yml` 在 GitHub 的服务器上运行，电脑关机、休眠都不影响：

- 每小时第 5 分钟唤醒一次，由 `digest.py --check` 按美东时间判断这次要不要生成；GitHub 的定时有时会晚几分钟到半小时，错过的版本下一次会补上
- 生成前先用 `events/sync_events.py`（从 `market_calendar` 复制）同步事件日历到 `events/calendar.db`
- 生成的摘要和去重记录（`data/`）会提交回仓库，在 GitHub 网页或手机 App 里打开 `data/latest.md` 就能看
- 需要在仓库的 Settings → Secrets and variables → Actions 里添加 `ANTHROPIC_API_KEY`（可选 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`）
- 手动生成一版：仓库 Actions 页面 → news-digest → Run workflow，勾选 force

本地拉取最新摘要：`git pull`

## 本地每小时运行（launchd，已停用）

```bash
cp com.pmbot.newsdigest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.pmbot.newsdigest.plist
```

launchd 每小时第 5 分钟唤醒一次，但只在美东时间 8、10、12、14、16、18、20、22、0 点生成（每天 9 版），其他时间直接跳过。某一版因为电脑休眠错过了，醒来后的下一个整点会补上；每版都会回看到上一版之后的全部新闻，夜间新闻不会漏。不在时段内也想生成，可以加 `--force`。日志写在 `data/run.log`。停止：

```bash
launchctl unload ~/Library/LaunchAgents/com.pmbot.newsdigest.plist
```

要求：电脑没有休眠，能联网。笔记本用电池合盖时会休眠，这期间不会生成；想准时生成，需要插电并在“系统设置 → 电池 → 选项”里打开“使用电源适配器时，防止在显示器关闭时自动进入睡眠”。

## 配置（`news_digest/.env`）

必须填 Claude API 密钥（在 console.anthropic.com 创建，和 Claude 订阅分开计费）：

```
ANTHROPIC_API_KEY=sk-ant-...
```

可选：

```
DIGEST_PROVIDER=ollama          # 改回本地 Qwen（免费，但运行时占约 24GB 内存）
DIGEST_MODEL=qwen3:14b          # 换模型（默认 claude-haiku-5-5；ollama 下默认 qwen3:32b）
TELEGRAM_BOT_TOKEN=...          # 填上后会同时推送到 Telegram
TELEGRAM_CHAT_ID=...
```
