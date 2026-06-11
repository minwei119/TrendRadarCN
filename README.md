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
| `TRENDRADAR_LLM_API_KEY` | DeepSeek key，用于 ai-cn / ai-frontier 自动打标签 | 否（不配会降级到规则匹配） |
| `SEC_USER_AGENT` | SEC EDGAR 联系人，格式 `Project name@domain.com` | 用 my-portfolio 板时需要 |

### 命令行模式（不启动 web）

```powershell
python run.py --crawl                   # 抓所有"热点榜"源
python run.py --board all               # 跑所有 4 个主题板
python run.py --board my-portfolio      # 只跑某一个主题板
python run.py --backfill-boards --force # 重新打标签 / 重新聚类所有已存在文章
python run.py --test-llm                # 测 LLM API 通不通
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
