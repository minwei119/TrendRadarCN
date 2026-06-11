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

```bash
cd C:\Users\minwei\TrendRadarCN

# 1. 创建虚拟环境 (推荐)
python -m venv .venv
.venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 跑起来
python run.py             # 启动 web 服务: http://127.0.0.1:8001
```

打开浏览器访问 <http://127.0.0.1:8001>，点击右上角 **立即抓取** 触发首次抓取，
之后页面会显示：

- **综合热度榜**：跨平台加权聚合，出现在多个平台的话题排名靠前
- **各平台热榜**：每个平台独立的 top 列表，点击话题可看其历史趋势曲线
- **最近抓取记录**：每次任务的成功率、耗时、错误

### 命令行一次性抓取（不启动 web）

```bash
python run.py --crawl
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

## 可选：定时抓取

当前是**手动触发**模式。要做定时抓取，最简单的方式是用 Windows 任务计划程序
或者 cron 调用：

```bash
python run.py --crawl
```

如果想常驻进程内调度，给 `requirements.txt` 加 `apscheduler`，在
`app/main.py` 的 `_startup()` 里启动 `AsyncIOScheduler` 即可。

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
