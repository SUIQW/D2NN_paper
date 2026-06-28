# Daily D2NN Paper Monitor

这个项目每天自动检索“衍射神经网络 / D2NN / diffractive neural network / optical diffractive neural network / all-optical neural network / photonic neural network”等方向的新论文，筛选高水平期刊和 arXiv 预印本，生成中文摘要后通过邮件推送。

## 项目结构

```text
.
├── .github/
│   └── workflows/
│       └── daily_paper_monitor.yml
├── README.md
├── config.yaml
├── monitor_papers.py
├── requirements.txt
└── seen_papers.json
```

## 功能

- 每天北京时间 / 新加坡时间约 09:07 运行一次，对应 GitHub Actions cron：`7 1 * * *`。避开整点可降低 GitHub Actions 高峰期延迟或漏触发的概率。
- 检索最近 `lookback_days` 天内新发布或新上线的论文，默认 7 天。
- 使用公开 API：arXiv API、Crossref REST API、Semantic Scholar Graph API。
- 对 title、abstract、keywords 做关键词匹配，并用光学 / 衍射上下文约束减少误报。
- 顶刊 / 高水平期刊加分，arXiv 结果标注为 `preprint`。
- 通过 `seen_papers.json` 记录已推送 DOI、arXiv ID 或标题哈希，避免重复推送。
- 当天没有符合条件的新论文时，也会发送“今日暂无新的顶刊论文”邮件。
- OpenAI 只用于可选的中文摘要增强；没有 `OPENAI_API_KEY` 时会自动使用本地规则摘要。

## GitHub Secrets

在 GitHub 仓库中进入 `Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`，至少配置：

| Secret | 必需 | 说明 |
| --- | --- | --- |
| `SMTP_HOST` | 是 | SMTP 服务器。163 邮箱通常是 `smtp.163.com` |
| `SMTP_PORT` | 是 | SMTP SSL 端口。163 邮箱通常是 `465` |
| `SMTP_USER` | 是 | 发件邮箱账号，例如 `your_name@163.com` |
| `SMTP_PASSWORD` | 是 | SMTP 授权码，不是邮箱登录密码 |
| `MAIL_FROM` | 否 | 发件人地址；不填则使用 `SMTP_USER` |
| `MAIL_TO` | 否 | 收件人地址；不填则使用 `config.yaml` 中的 `sui2324129420@163.com` |
| `OPENAI_API_KEY` | 否 | 用于生成更自然的中文摘要 |
| `OPENAI_MODEL` | 否 | 默认使用 `config.yaml` 中的 `gpt-4.1-mini` |
| `SEMANTIC_SCHOLAR_API_KEY` | 否 | 建议配置，可提高 Semantic Scholar 限速 |
| `CROSSREF_MAILTO` | 否 | 给 Crossref 的礼貌联系邮箱，可填你的邮箱 |

## 本地运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python monitor_papers.py --dry-run
```

`--dry-run` 会打印邮件内容，不发送邮件，也不会更新 `seen_papers.json`。

## 本地测试真实邮件

PowerShell 示例：

```powershell
$env:SMTP_HOST="smtp.163.com"
$env:SMTP_PORT="465"
$env:SMTP_USER="你的163邮箱"
$env:SMTP_PASSWORD="你的SMTP授权码"
$env:MAIL_TO="sui2324129420@163.com"
$env:OPENAI_API_KEY="可选"
$env:SEMANTIC_SCHOLAR_API_KEY="可选"
python monitor_papers.py
```

## GitHub Actions 手动测试

推送到 GitHub 后，进入仓库的 `Actions` 页面，选择 `Daily D2NN Paper Monitor`，点击 `Run workflow`。运行成功后：

1. 你会收到邮件。
2. 如果有新论文，`seen_papers.json` 会被 GitHub Actions 自动提交回仓库。
3. 如果没有新论文，会收到“今日暂无新的顶刊论文”的邮件，`seen_papers.json` 不会变化。

## 修改关键词或期刊

编辑 `config.yaml`：

- `keywords`：检索关键词。
- `strict_keywords`：精确强相关词，例如 D2NN 和 diffractive neural network。
- `optical_anchor_terms` / `ml_anchor_terms`：用于控制误报。
- `high_impact_venues`：顶刊和高水平期刊白名单。
- `score_threshold`：推送阈值，越高越严格。

## 切换推送渠道

当前默认通道是邮件。`monitor_papers.py` 中已经把推送封装为 notifier，后续可以新增 Feishu、企业微信或 Telegram notifier，并复用同一份论文摘要 payload。相关 webhook secret 名称已经预留在 `config.yaml` 的 `notification.webhooks` 中。
