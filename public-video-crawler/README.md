# 公开列表视频索引器

该工具抓取 `https://91porn.com/v.php` 或 `index.php` 的公开列表，默认读取第 1、2 页，先按 `viewkey` 去重，并输出 JSON 与 CSV。每日任务还会维护跨日历史索引、榜单资源编号和 SHA-256 内容指纹，已见且已处理的视频不会再次访问详情页或进入下载队列，换了 `viewkey` 的同一内容也不会二次入库。

`npm run daily` / `npm run daily:download` 的来源统一配置在 `config/daily-sources.json`；同一 `viewkey` 即使同时出现在多个来源也只保留一条。日常任务使用串行详情解析、较长请求间隔，并只处理历史中从未见过的新条目；已见条目的重试交由修复任务显式触发。经用户确认写入 `data/ignored-media.json` 的条目只保留在索引中，永久跳过详情解析和下载；移除对应 key 后才会恢复到队列。

如需对已人工确认的条目永久停用详情页请求，可在项目根目录的 `data/ignored-media.json` 中维护 `items` 编号。该文件只读使用，程序不会把新的解析或下载失败自动加入；移除某个编号后，下一次显式修复即可重新处理。

它只处理公开列表和公开详情页，不尝试访问 VIP、付费、管理或其他用户私有内容。遇到 Cloudflare 验证时，可从项目根目录的本地 Cookie 任务栏配置普通浏览器会话；凭证只从忽略跟踪的 `data/auth-cookie.txt` 读取，不进入输出、日志或命令行。默认只收集列表元数据；加 `--resolve-media` 时会访问公开详情页并记录页面已经公开暴露的媒体 URL，但不会下载媒体文件。

运行：

```bash
cd '/Users/niutopia/文件/Codex WorkSpace/Transfer station/public-video-crawler'
python3 crawler.py \
  --url 'https://91porn.com/v.php?category=hot&viewtype=basic' \
  --pages 2 \
  --json videos.json \
  --csv videos.csv
```

如需解析公开详情页中的媒体 URL：

```bash
python3 crawler.py \
  --url 'https://91porn.com/v.php?category=hot&viewtype=basic' \
  --pages 2 \
  --resolve-media \
  --resolve-concurrency 1 \
  --media-rechecks 1 \
  --new-only \
  --ignored-history ../data/ignored-media.json \
  --delay 2 \
  --json videos-with-media.json \
  --csv videos-with-media.csv
```

串行下载（默认支持 `.part` 断点续传、拒绝覆盖已完成文件、每个文件最多 4 GiB）：

```bash
python3 download.py videos-with-media.json --output-dir downloads --delay 2
```

下载遇到过期签名链接或代理 Fake-IP 时，会自动重新解析媒体地址并重试；默认最多额外 3 次，可用 `--refresh-retries 0` 禁用，`--refresh-retries N` 调整上限。磁盘不足、媒体身份不匹配、无效 URL 和真实私网地址不会自动绕过。

首次建议只验证一个：

```bash
python3 download.py videos-with-media.json --output-dir downloads --limit 1
```

媒体 URL 带短期签名；遇到 401/403/410 时下载器会重新解析公开详情页。媒体文件请求本身不发送登录 Cookie；只有重新解析详情页时使用本地 Cookie 及与其匹配的浏览器 User-Agent，并把对应公开详情页作为 `Referer`。

下载任务启用 `MEDIA_HTTP_PROXY` 后，会在真正写入文件前分别用新签名链接短测直连和 HTTP 代理，自动选择更快路线；代理不可用或不占优时回退直连。代理只用于媒体详情刷新和媒体字节请求，不会通过全局 `HTTP_PROXY` 改变榜单/API 请求。当前 Docker 配置使用 `http://host.docker.internal:7897`；标准库不支持 SOCKS，不能填写 `socks5://`。

测试：

```bash
python3 -m unittest discover -s tests -v
```

去重规则：

- 主键是详情页的 `viewkey`，忽略 `page`、`c`、`category` 等跟踪/列表参数。
- 同一页出现两套重复卡片时，优先采用当前可见列表使用的 `c=llzvq` 卡片元数据。
- 同一 `viewkey` 跨页再次出现时合并 `source_pages`，不会重复输出。
- `data/video-history.json` 保存跨日 `viewkey` 历史；日常任务只解析历史中没有的新条目。
- 历史中榜单缩略图的稳定资源编号用于识别站点更换 `viewkey` 的同一条目，命中后直接记为已处理。
- `download-success.txt` 是永久成功历史；已成功的视频即使之后被移走或手动删除，也不会重新解析或下载。
- `data/download-content-history.json` 保存文件内容指纹；下载完成后、正式入库前检查，命中历史内容时删除临时副本并将新 `viewkey` 记为已处理。

边界：站点结构变化或 Cloudflare Challenge 可能导致解析为空；工具会保留每页原始链接数和最终唯一数，便于发现异常。请遵守书面授权范围、站点规则和适用法律。
