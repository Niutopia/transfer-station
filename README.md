# Transfer station

本地 Web 监控器，用于汇总公开列表抓取、去重、媒体解析、视频下载和“中转站”目录的每日入库情况。

## 启动

```bash
cd '/Users/niutopia/文件/Codex WorkSpace/Transfer station'
npm run local
```

打开 `http://localhost:3000/`。任务运行时后台每 1 秒生成状态快照，并通过 SSE 实时推送下载字节数、速度和完成比例；连接中断时页面自动降级为 10 秒轮询。

进度面板同时显示文件完成比例、已知字节比例、平滑下载速度、预计剩余时间和每个文件的当前状态。由 Web 启动的任务可以暂停、继续或安全取消；已经写入的可验证分片会保留，后续任务可继续传输。

## Docker 启动

Docker 会把网页、任务服务和状态刷新器一起启动，浏览器只需要访问一个端口：

```bash
docker compose up -d --build
```

打开 `http://localhost:3000/`。`data/`、`中转站/`、`public/`、抓取结果和配置目录通过卷挂载保留在项目中，删除或升级容器不会删除这些数据。

查看状态或停止：

```bash
docker compose ps
docker compose down
```

## 每日任务

每日任务读取 `config/daily-sources.json`，当前抓取 5 个榜单的前 2 页。不同榜单及不同页之间统一按 `viewkey` 去重，再与跨日历史索引去重。

只抓取和解析公开页面，不下载：

```bash
npm run daily
```

抓取后串行下载到 `/Users/niutopia/文件/Codex WorkSpace/Transfer station/中转站`：

```bash
npm run daily:download
```

首次建议限量验证：

```bash
python3 scripts/run-daily-crawl.py --download --limit 1
```

抓取器会对 TLS 中断、超时、限流和临时服务错误执行指数退避重试。列表页采用有限并发，单页失败不会丢失其他榜单的结果；媒体链接超过 3 分钟或返回失效状态时，会在下载前自动重新解析并写回本地历史。

断点续传只会在远端返回匹配的 `Content-Range`，且 ETag 或 Last-Modified 验证通过时追加数据。验证信息缺失、远端文件发生变化或区间不匹配时，会安全地重新下载，避免拼接出损坏文件。

## 数据来源

- `public-video-crawler/videos-with-media.json`：最近一次抓取、去重和媒体解析结果。
- `中转站/`：只存放已完成的视频文件。
- `data/download-manifest.json`：最近一次下载结果与校验信息。
- `data/video-history.json`：跨日视频历史索引，用于避免重复解析和下载。
- `data/pending-videos.json`：本次新增或需要重试的下载队列。
- `data/partials/`：下载中的临时 `.part` 分片。
- `data/logs/`：由每日任务生成的本地运行日志。
- `public/status.json`：供 Web 页面读取的只读状态快照。

监控器只在本地读取这些文件，不上传视频、Cookie、签名媒体地址或运行日志。
