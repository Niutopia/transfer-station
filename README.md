# Transfer station

本地 Web 监控器，用于汇总公开列表抓取、去重、媒体解析、视频下载和“中转站”目录的每日入库情况。

## 启动

```bash
cd '/Users/niutopia/文件/Codex WorkSpace/Transfer station'
npm run local
```

打开 `http://localhost:3000/`。任务运行时后台每 1 秒检查状态，只在数据变化时写入快照并通过 SSE 实时推送下载字节数、速度和完成比例；连接中断时页面自动降级为 10 秒轮询。

进度面板同时显示文件完成比例、已知字节比例、平滑下载速度、预计剩余时间和每个文件的当前状态。由 Web 启动的任务可以暂停、继续或安全取消；已经写入的可验证分片会保留，后续任务可继续传输。

## Docker 启动

Docker 会把网页、任务服务和状态刷新器一起启动，浏览器只需要访问一个端口：

```bash
docker compose up -d --build
```

打开 `http://localhost:3000/`。`data/`、`history-backups/`、`中转站/`、`public/`、抓取结果和配置目录通过卷挂载保留在项目中，删除或升级容器不会删除这些数据。

查看状态或停止：

```bash
docker compose ps
docker compose down
```

## 手动抓取

项目不会定时抓取，也不会因为 Docker 启动或页面刷新而自动运行任务。只有点击 Web 页面的抓取按钮，或手动执行下面的命令，才会开始抓取。

抓取任务读取 `config/daily-sources.json`，抓取当前配置来源的前 2 页。页面底部的“抓取链接任务栏”可以添加或删除 `/v.php` 榜单及 `/index.php` 首页链接，也可以删除全部来暂停抓取；无来源时 Web 不会启动空任务。修改会保存到该配置并在下一次手动任务中生效。任务运行期间配置会被锁定；删除链接不会删除已下载文件或历史记录。不同榜单及不同页之间先按 `viewkey` 去重，再与跨日历史索引去重；如果站点给同一条目更换了 `viewkey`，还会使用榜单缩略图中的稳定资源编号识别。文件进入“中转站”前最终校验 SHA-256 内容指纹，因此标题、地址或编号变化都不会生成第二份文件。

视频一旦成功下载，`data/download-success.txt` 就会永久记录它的 `viewkey`。之后即使视频被移出“中转站”或手动删除，后续任务也会直接跳过，不会再次解析或下载。
内容指纹保存在 `data/download-content-history.json`，首次使用会自动从旧下载日志补齐历史指纹。换编号的重复内容会被记为已处理，但不会落入“中转站”。
入库趋势使用 `data/download-history.json` 的首次成功下载账本统计；同一 `viewkey` 只记录一次。手动删除视频只会改变当前文件数和磁盘占用，不会减少历史入库数量、累计下载量或对应日期的趋势数据。

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

详情页解析只接受实际播放器 `<video>` / `<source>` 中的媒体地址；HTML 注释里的旧示例地址和脚本中的预播广告不会进入下载队列。播放器资源编号还必须与榜单缩略图一致，目标站点偶发返回其他视频时会拒绝下载。页面编号 `viewkey`、榜单资源编号和文件 SHA-256 依次构成三层去重。

断点续传只会在远端返回匹配的 `Content-Range`，且 ETag 或 Last-Modified 验证通过时追加数据。验证信息缺失、远端文件发生变化或区间不匹配时，会安全地重新下载，避免拼接出损坏文件。

## 数据来源

- `public-video-crawler/videos-with-media.json`：最近一次抓取、去重和媒体解析结果。
- `中转站/`：只存放已完成的视频文件。
- `data/download-manifest.json`：最近一次下载结果与校验信息。
- `data/download-history.json`：首次成功下载账本，用于生成不会因文件删除而回退的入库趋势。
- `data/video-history.json`：跨日视频历史索引，用于避免重复解析和下载。
- `data/pending-videos.json`：本次新增或需要重试的下载队列。
- `data/partials/`：下载中的临时 `.part` 分片。
- `data/logs/`：由每日任务生成的本地运行日志。
- `public/status.json`：供 Web 页面读取的只读状态快照。

监控器只在本地读取这些文件，不上传视频、Cookie、签名媒体地址或运行日志。

## 历史备份

每次抓取、修复和来源配置变更后，项目都会把去重账本与来源配置备份到 `history-backups/history-latest.zip`，并保留最近 7 个有实际内容变化的版本。备份目录不在 `data/` 内，重建容器或清理运行数据时不会一起丢失。

如需恢复，先停止任务服务，再执行：

```bash
docker compose down
python3 scripts/history_backup.py --restore-latest
docker compose up -d
```
