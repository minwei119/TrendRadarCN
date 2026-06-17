# TrendRadarCN · 中文互联网热点雷达

一个本地运行的爬虫 + 统计系统，定期/手动抓取中文互联网上各大平台的热门话题，
将每次抓取结果保存为快照，并提供一个仪表盘可视化排行榜与跨平台聚合趋势
（类似谷歌趋势的思路，本地版本）。

## 已接入的数据源

| key        | 平台         | 接口                                                                 |
| ---------- | ------------ | -------------------------------------------------------------------- |
| `weibo`    | 微博热搜     | `weibo.com/ajax/side/hotSearch`                                      |
| `zhihu`    | 知乎热榜     | `zhihu.com/api/v3/feed/topstory/hot-lists/total`                     |
| `baidu`    | 百度热搜     | `top.baidu.com/api/board`                                            |
| `bilibili` | B 站热门视频 | `api.bilibili.com/x/web-interface/popular`                           |
| `toutiao`  | 今日头条     | `toutiao.com/hot-event/hot-board`                                    |
| `v2ex`     | V2EX 热门    | `v2ex.com/api/topics/hot.json`                                       |

> 这些接口大部分是平台前端自己用的公开 JSON 接口，不需要登录；
> 如果某个平台改了接口/加了限频/上了验证码，对应爬虫会失败但不影响其他源。

## 快速开始

### 推荐：一键部署（新机器/全新克隆）

```powershell
git clone https://github.com/minwei119/TrendRadarCN.git
cd TrendRadarCN
.\scripts\setup.ps1
```

`setup.ps1` 会自动完成：
1. 找到合适的 Python (>= 3.11)，找不到给你下载链接
2. 创建虚拟环境 `.venv`
3. 安装 `requirements.txt`
4. 交互式生成 `.env`（按提示填代理 / 知乎 cookie / DeepSeek key / SEC 邮箱，每项都可跳过）
5. 初始化 SQLite 数据库
6. 询问是否安装每日 7:30 的计划任务

脚本是**幂等**的，可以反复跑。要从头来过加 `-Force`：

```powershell
.\scripts\setup.ps1 -Force            # 删 .venv + .env 重建
.\scripts\setup.ps1 -SkipTask         # 不问计划任务
.\scripts\setup.ps1 -PythonVersion 3.13   # 强制用 3.13
```

如果 PowerShell 报 `running scripts is disabled`，开头加 `powershell -ExecutionPolicy Bypass -File`。

### 手动安装（不想用一键脚本）

```powershell
cd TrendRadarCN

py -3.13 -m venv .venv                  # 用 Python 3.13 建 venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env                  # 然后手动编辑 .env 填值
notepad .env

python run.py                           # 启动 web: http://127.0.0.1:8001
```

### 跑起来后

打开 <http://127.0.0.1:8001>，页面有两个 tab：

- **热点榜**：跨平台聚合 + 各平台独立榜单 + 历史趋势曲线（点话题查看）
- **主题板块**：4 个独立板（股市要闻 / 我的持仓 / AI 前沿中文 / AI Frontier 英文），
  支持标签筛选 + 事件聚类（同一事件多源合并显示）

右上角"立即抓取"触发综合榜抓取；主题板需要用 `python run.py --board <key>`
触发，或装好计划任务后每天自动跑。

### 关于 `.env`

所有秘密 / 私人配置都在项目根目录的 `.env` 里（已 gitignored，不会进仓库）。
模板见 `.env.example`。涉及的变量：

| 变量 | 用途 | 必需 |
|---|---|---|
| `TRENDRADAR_PROXIES` | HTTP 代理（访问 Google News / arxiv / HuggingFace 用） | 否（国内直连源不需要） |
| `TRENDRADAR_ZHIHU_COOKIE` | 知乎登录 cookie（绕过反爬） | 否（不配 zhihu 源会 401） |
| `TRENDRADAR_LLM_API_KEY` | DeepSeek key，用于 ai-cn / ai-frontier / robotics 自动打标签 + 一句话摘要 + 邮件去重 | 否（不配会降级到规则匹配，且不生成摘要） |
| `SEC_USER_AGENT` | SEC EDGAR 联系人，格式 `Project name@domain.com` | 用 my-portfolio 板时需要 |
| `TRENDRADAR_DASHBOARD_URL` | 邮件页脚仪表盘链接，例如 `http://192.168.1.100:8001`（手机能点） | 否（不配会自动探测 LAN IP） |
| `TRENDRADAR_HOST` / `TRENDRADAR_PORT` | FastAPI 监听地址 / 端口（默认 `0.0.0.0:8001`，LAN 内可访问） | 否 |

### 从手机/平板访问仪表盘（同 WiFi）

默认 `python run.py` 监听 `0.0.0.0:8001`，意味着同一 WiFi 下的任何设备
都能访问。三步搞定：

```powershell
# 1. 查本机 LAN IP（输出形如 192.168.1.100 或 10.x.x.x）
powershell -ExecutionPolicy Bypass -File scripts\show_lan_ip.ps1
# 或者直接:  ipconfig | findstr IPv4

# 2. 放行 8001 端口（管理员 PowerShell，一次性）
New-NetFirewallRule -DisplayName 'TrendRadarCN 8001' -Direction Inbound `
    -Protocol TCP -LocalPort 8001 -Action Allow -Profile Private

# 3. 把 IP 写到 .env，邮件页脚链接就自动用它
#   TRENDRADAR_DASHBOARD_URL="http://192.168.1.100:8001"
```

手机连同一 WiFi 后，浏览器打开 `http://<LAN-IP>:8001` 即可访问。

**注意**：

- 公司 WiFi 经常打开 client isolation（同网段设备互不可见），可能访问不通。
  这种情况下只能用 [Tailscale](https://tailscale.com) 之类 VPN（PC + 手机各装一次，免费）。
- 家里 WiFi 的 LAN IP 一般是 `192.168.x.x`；公司网络则常见 `10.x.x.x`。
  在不同网络间切换时 IP 会变，需要重新查并改 `.env`。

### 公网访问（推荐）：GitHub Pages 静态快照

LAN 方案只在同一 WiFi 下生效。如果你想从公司、4G、出差任意网络打开仪表盘
（包括在 126/Gmail 网页邮箱里点邮件链接），推荐用 GitHub Pages 托管每日快照。

**一次性配置**：

1. 生成第一份快照并推到 GitHub:

   ```powershell
   python run.py --snapshot-publish
   ```

2. 打开 GitHub 仓库 → **Settings → Pages**:
   - Source: `Deploy from a branch`
   - Branch: `main` · Folder: `/docs`
   - Save
3. 等 1-2 分钟, GitHub 会给你 `https://minwei119.github.io/TrendRadarCN/`
4. 把上一步的 URL 写到 `.env`:

   ```dotenv
   TRENDRADAR_PUBLIC_URL="https://minwei119.github.io/TrendRadarCN/"
   ```

之后每天 7:30 计划任务会自动跑 `--snapshot-publish`, 把当日数据 push 到 GitHub,
GitHub Pages 自动重新发布。邮件页脚的“仪表盘”链接会自动指向这个公网 URL。

**只生成不推送**（本地预览）：

```powershell
python run.py --snapshot
start docs\index.html      # 用默认浏览器打开看看
```

**隐私提示**: 数据会公开在 GitHub Pages 上（新闻本身已经公开, 但你的“关注列表”
即 tag 配置会暴露给任何看到 URL 的人）。如不接受, 改用方案 A (LAN) 或 Tailscale。

### 命令行模式（不启动 web）

#### 抓取数据

```powershell
python run.py --crawl                       # 抓所有"热点榜"源
python run.py --board all                   # 跑所有 5 个主题板
python run.py --board my-portfolio          # 只跑某一个主题板
python run.py --board all --email           # 跑完主题板顺便发邮件（定时任务用法）
python run.py --board all --email --snapshot-publish  # 全套：抓 + 发 + push Pages
```

#### 邮件

```powershell
python run.py --digest-preview              # 不发, 只看 LLM 去重统计 + 前 3 条样例
python run.py --digest-send                 # 用现有数据立即发一封 (不重新抓, ~10 秒)
python run.py --test-email                  # 发一封最小测试邮件 (验 SMTP 是否通)
```

#### GitHub Pages 静态快照

```powershell
python run.py --snapshot                    # 只生成 docs/index.html, 不 push
python run.py --snapshot-publish            # 生成 + git add/commit/push origin HEAD
start docs\index.html                       # 浏览器打开本地预览
```

#### 主题板维护（改 tag / 切 LLM / 修数据时用）

```powershell
python run.py --backfill-boards             # 给历史文章补 tag (只补 NULL 行)
python run.py --backfill-boards --force     # 重新打标签 / 重新聚类所有已存在文章
python run.py --reset-tags ai-cn            # 清空某板的 tag 列, 准备从头打标
python run.py --reset-tags all              # 清空所有板的 tag

python run.py --backfill-summaries          # 给没摘要的文章生成 LLM 一句话摘要
python run.py --backfill-summaries --force  # 重新生成所有文章的摘要
python run.py --reset-summaries robotics    # 清空某板的 llm_summary

python run.py --apply-llm-cluster my-portfolio          # 跑 LLM 语义聚类, 持久化到 llm_cluster_id (邮件 + 仪表盘共享)
python run.py --apply-llm-cluster my-portfolio --force  # 先清空旧分组再跑 (改了 prompt 后用)
python run.py --apply-llm-cluster all                   # 所有板块都重跑
```

#### 调试

```powershell
python run.py --test-llm                    # 发 3 条样例给 LLM, 看通不通 (用于排查 key/网络/模型名)

# 按关键词查 DB 里相关文章, 看 llm_cluster_id 分组情况
# (排查"邮件里同一新闻为啥还有 2 条"这类问题)
python scripts\diag_keyword.py 腾讯 回购
python scripts\diag_keyword.py 周靖人 --hours 48
```

#### 启动 web 仪表盘

```powershell
python run.py                               # 默认 0.0.0.0:8001, LAN 内任意设备可访问
python run.py --host 127.0.0.1              # 锁回本机 only
python run.py --port 9000                   # 换端口
```

## 代理与重试（环境变量配置，无需改代码）

每个爬虫的网络请求都走 `BaseCrawler.request()`，内置 **失败自动重试 + 指数退避 +
代理轮换**。通过环境变量控制（在启动 `python run.py` 的同一个终端里设置）：

| 变量 | 作用 | 默认 |
| ---- | ---- | ---- |
| `TRENDRADAR_PROXIES` | 逗号分隔的代理列表，按尝试轮换 | 空 |
| `TRENDRADAR_PROXY` | 单个代理（会并入上面的列表） | 空 |
| `TRENDRADAR_USE_DIRECT` | 是否在轮换中也尝试一次直连 | `true` |
| `TRENDRADAR_MAX_RETRIES` | 首次之外的额外重试次数 | `2`（共 3 次） |
| `TRENDRADAR_BACKOFF` | 退避基准秒数（指数增长，带抖动，上限 8s） | `0.8` |
| `TRENDRADAR_MIN_INTERVAL` | 同一站点两次请求的最小间隔秒数（礼貌限速） | `0`（关闭） |
| `TRENDRADAR_RESPECT_ROBOTS` | 是否遵守各站点的 robots.txt | `false` |
| `TRENDRADAR_LOG` | 结构化请求日志文件路径 | `logs/crawl.jsonl` |
| `TRENDRADAR_LOG_CONSOLE` | 是否同时把日志打到 stderr | `false` |

CN 的源基本都能直连，一般无需配代理。若某个源被限频（429）或偶发超时，
重试机制会自动多试几次。示例（PowerShell）：

```powershell
$env:TRENDRADAR_PROXIES = "http://127.0.0.1:7890,http://127.0.0.1:1080"
python run.py
```

> 没设 `TRENDRADAR_*` 时，标准的 `HTTPS_PROXY` / `HTTP_PROXY` 也会被自动识别。
> 会重试的状态码：408 / 425 / 429 / 500 / 502 / 503 / 504；其余 4xx 立即放弃（重试也没用）。

**礼貌限速**：开启后，对同一站点的请求会被排队、按 `MIN_INTERVAL` 间隔放行，
不同站点仍并行。适合避免把目标服务器打太频：

```powershell
$env:TRENDRADAR_MIN_INTERVAL = "1.0"   # 同站请求至少间隔 1 秒
python run.py
```

**robots.txt**：默认 **关闭**。注意微博/知乎/百度等的热榜其实是前端自用的
JSON 接口，它们的 robots.txt 往往会 `Disallow: /ajax/` 或 `/api/`——一旦开启，
这些源会被判定为"禁止抓取"并在抓取记录里报 `RobotsDisallowed`。所以仅在你
明确要做"守规矩的通用爬虫"时再开：

```powershell
$env:TRENDRADAR_RESPECT_ROBOTS = "1"
python run.py
```

## 请求日志（排查哪个源不稳定）

每一次 HTTP 请求尝试都会写一行 JSON 到 `logs/crawl.jsonl`（自动滚动，单文件上限
约 2MB，保留 3 个备份）。字段包括：源、host、方法、第几次尝试/共几次、用没用代理、
状态码、是否重试、耗时(ms)、错误。例如：

```json
{"ts":"2026-06-02T05:40:01+00:00","source":"weibo","host":"https://weibo.com","method":"GET","attempt":1,"attempts":3,"proxy":"direct","status":200,"ok":true,"retried":false,"elapsed_ms":312}
{"ts":"2026-06-02T05:40:02+00:00","source":"nodeseek","host":"https://www.nodeseek.com","method":"GET","attempt":1,"attempts":3,"proxy":"direct","status":403,"ok":false,"retried":false,"elapsed_ms":540,"error":"HTTPStatusError: ..."}
```

也可以直接调 `GET /api/logs?limit=100` 拿最近的记录（最新在前）。

快速看哪个源在报错（PowerShell）：

```powershell
Get-Content logs\crawl.jsonl -Tail 50 | Select-String '"ok":false'
```

## 架构

```
app/
├── main.py            FastAPI 应用、HTTP 路由
├── db.py              SQLAlchemy engine / session / 初始化
├── models.py          Source / Snapshot / Topic 三张表
├── service.py         抓取调度 + 查询 / 聚合 / 趋势
├── crawlers/          每个平台一个文件，全部继承 BaseCrawler
│   ├── base.py
│   ├── weibo.py
│   ├── zhihu.py
│   ├── baidu.py
│   ├── bilibili.py
│   ├── toutiao.py
│   └── v2ex.py
└── static/
    └── index.html     单页仪表盘（Tailwind + Chart.js，CDN 引入）
```

### 数据模型

- `sources` —— 数据源清单（key、平台名、地区、链接）
- `snapshots` —— 每次抓取的元数据（时间、状态、条数、耗时、错误）
- `topics` —— 每个快照里的一条热点（rank、title、url、score、extra）

`Topic.title` 加了索引，方便按标题查询历史趋势 (`/api/trend`)。

## REST API

| Method | Path             | 说明                                          |
| ------ | ---------------- | --------------------------------------------- |
| GET    | `/api/sources`   | 所有数据源 + 最近抓取时间                     |
| GET    | `/api/hot`       | 所有源最新快照的 topic 列表（可加 `?source=`）|
| GET    | `/api/aggregate` | 跨平台加权聚合榜 (`?hours=24&limit=30`)       |
| GET    | `/api/trend`     | 指定 title 的历史排名/分数 (`?title=&hours=`) |
| GET    | `/api/snapshots` | 最近的抓取记录                                |
| GET    | `/api/logs`      | 最近的结构化请求日志 (`?limit=100`)           |
| POST   | `/api/crawl`     | 触发抓取（带 `?source=` 抓单个，否则抓全部）  |

打开 `http://127.0.0.1:8001/docs` 可以看 FastAPI 自动生成的 OpenAPI 文档。

## 添加新数据源

1. 在 `app/crawlers/` 下新建 `mysite.py`，继承 `BaseCrawler` 并实现 `async fetch()`。
2. 在 `app/crawlers/__init__.py` 的 `_REGISTRY` 里加上这个类的实例。
3. 重启服务即可。下一次启动会自动把新源插入 `sources` 表。

## 定时抓取（Windows 计划任务）

仓库带了 3 个 PowerShell 脚本，一行命令把"每天早上 7:30 自动跑全部 4 个板块"装上：

```powershell
cd C:\Users\minwei\TrendRadarCN
.\scripts\install_scheduled_task.ps1
```

如果 PowerShell 报 `running scripts is disabled`（默认执行策略），改用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_scheduled_task.ps1
```

### 改时间 / 重新安装

```powershell
.\scripts\install_scheduled_task.ps1 -Time 08:00     # 改到 8:00
```

脚本是幂等的：先 unregister 旧任务，再注册新任务，可以反复跑。

### 立刻触发一次（不用等明天 7:30）

```powershell
Start-ScheduledTask -TaskName TrendRadarCN-DailyBoards
Get-Content logs\scheduled.log -Tail 80              # 看运行结果
```

### 卸载

```powershell
.\scripts\uninstall_scheduled_task.ps1
```

### 行为说明

| 场景 | 表现 |
|---|---|
| 7:30 电脑开着 + 已登录 | 后台无窗口跑（1-3 分钟） |
| 7:30 电脑开着但未登录 | 不跑，登录后立刻补跑 |
| 7:30 电脑关机 / 睡眠 | 不跑，下次开机登录后立刻补跑 |
| 网络中途断了 | 失败 → 10 分钟后重试，最多 2 次 |
| 跑超过 1 小时 | 强杀（防卡死） |

任务以**你自己的身份**运行，无需 admin，无需保存密码。日志统一写到
`logs\scheduled.log`，每次运行带时间戳分隔。

### 调试

| 命令 | 用途 |
|---|---|
| `Get-ScheduledTaskInfo -TaskName TrendRadarCN-DailyBoards` | 上次运行时间 + 结果 |
| `Get-Content logs\scheduled.log -Tail 80` | 最近一次输出 |
| `.\scripts\run_boards.ps1 -Board ai-cn` | 手动只跑一个板（绕过计划任务测试 wrapper） |

### 在进程内调度（不推荐）

如果硬要做进程内调度，给 `requirements.txt` 加 `apscheduler`，在
`app/main.py` 的 `_startup()` 里启动 `AsyncIOScheduler` 即可。
但用 Windows 任务计划程序更简单：进程崩了不影响下一次，
还能跨重启自动恢复。

## 邮件推送

定时任务跑完 `--board all` 后，可以把今天新增的文章按板块汇总成一封 HTML 邮件
发到你邮箱。零依赖（纯 stdlib `smtplib`），SMTP 没配就 silent skip，不影响抓取。

`scripts/run_boards.ps1` 已经默认带 `--email` 跑，所以你只要在 `.env` 里把
SMTP 4 个必填字段填好，第二天 7:30 就能收到早报；想关掉就把 `SMTP_HOST`
留空。

### Provider 配置速查

| Provider | `SMTP_HOST` | `SMTP_PORT` | `SMTP_USE_TLS` | `SMTP_PASS` 填什么 |
|---|---|---|---|---|
| QQ 邮箱 | `smtp.qq.com` | `465` | `false` | 授权码（不是登录密码） |
| 163 邮箱 | `smtp.163.com` | `465` | `false` | 客户端授权密码 |
| Gmail | `smtp.gmail.com` | `587` | `true` | App Password |
| Outlook / O365 | `smtp.office365.com` | `587` | `true` | 账号密码 |

> **国内强烈推荐 QQ 邮箱**——直连不需要代理，授权码 5 分钟搞定。Gmail
> 在国内基本要走代理，而 Python 的 `smtplib` **不读** `HTTPS_PROXY` 环境变量，
> 所以即便你给爬虫配了代理，发 Gmail 还是会超时。

### 推荐：QQ 邮箱（一分钟开通）

1. 浏览器登录 <https://mail.qq.com>
2. 顶部 **设置 → 账户**
3. 滚到 **POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务** 那一段
4. 点 **生成授权码**（会让你发条短信验证）
5. 复制那串 16 位授权码，填到 `.env` 里 `SMTP_PASS`

完整 `.env` 配置：

```dotenv
SMTP_HOST="smtp.qq.com"
SMTP_PORT="465"
SMTP_USER="你的QQ号@qq.com"
SMTP_PASS="刚才生成的16位授权码"
SMTP_FROM=""               # 留空 = 用 SMTP_USER
SMTP_TO="自己@qq.com"      # 多个收件人用逗号分隔
SMTP_USE_TLS="false"       # 465 端口必须填 false (走 SSL 而不是 STARTTLS)
```

### 测试 SMTP 通不通

```powershell
python run.py --test-email
```

发送成功打印 `OK: test email sent to ...`；失败立刻退出非零并打印
`ERROR: ...` 原因（鉴权失败、连不上、TLS 不匹配等），方便定位。

### 看真正的早报长啥样

```powershell
python run.py --board all --email
```

会跑完所有板，再把今天新增文章打包发邮件。同样的命令计划任务每天 7:30
自动跑一次。

### 关掉邮件

把 `SMTP_HOST` 清空就行（其它字段不用动），`run.py --email` 会打印
`[email] SMTP not configured ... Skipping send.` 后正常退出 0。

## 测试

**离线单元测试**（不联网，用 `httpx.MockTransport` 喂假数据，验证解析/重试/配置逻辑）：

```powershell
.venv\Scripts\activate
pip install -r requirements-dev.txt
pytest
```

覆盖：代理候选解析、429 自动重试、404 立即放弃、微博/V2EX 解析等。

**在线冒烟测试**（真实抓一遍所有源，打印每个源的成功/失败与条数）：

```powershell
python run.py --crawl
```

## 数据库存在哪

SQLite 文件位于项目根目录 `trendradar_cn.db`。删掉它就会重新初始化。
要导出数据可以直接用 `sqlite3 trendradar_cn.db` 操作。
