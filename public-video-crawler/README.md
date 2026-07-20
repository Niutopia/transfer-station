# 公开列表视频索引器

该工具抓取 `https://91porn.com/v.php` 或 `index.php` 的公开列表，默认读取第 1、2 页，按 `viewkey` 去重，并输出 JSON 与 CSV。每日任务还会维护跨日历史索引，已见且已下载的视频不会再次访问详情页或进入下载队列。

`npm run daily` / `npm run daily:download` 的来源统一配置在 `config/daily-sources.json`。当前包含 `hot`、`top`、`tf`、`top&m=-1`、`mf` 五个榜单；同一 `viewkey` 即使同时出现在多个榜单也只保留一条。

它只使用匿名会话，不接收账号、Cookie 或 Token，不尝试访问 VIP、付费、管理或其他用户私有内容。默认只收集列表元数据；加 `--resolve-media` 时会依次访问公开详情页并记录页面已经公开暴露的媒体 URL，但不会下载媒体文件。

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
  --delay 2 \
  --json videos-with-media.json \
  --csv videos-with-media.csv
```

串行下载（默认支持 `.part` 断点续传、拒绝覆盖已完成文件、每个文件最多 4 GiB）：

```bash
python3 download.py videos-with-media.json --output-dir downloads --delay 2
```

首次建议只验证一个：

```bash
python3 download.py videos-with-media.json --output-dir downloads --limit 1
```

媒体 URL 带短期签名；遇到 401/403/410 时重新运行带 `--resolve-media` 的抓取命令。下载器不读取或发送登录 Cookie，只把对应公开详情页作为 `Referer`。

测试：

```bash
python3 -m unittest discover -s tests -v
```

去重规则：

- 主键是详情页的 `viewkey`，忽略 `page`、`c`、`category` 等跟踪/列表参数。
- 同一页出现两套重复卡片时，优先采用当前可见列表使用的 `c=llzvq` 卡片元数据。
- 同一 `viewkey` 跨页再次出现时合并 `source_pages`，不会重复输出。
- `data/video-history.json` 保存跨日 `viewkey` 历史；日常任务只解析历史中没有的新条目。
- 下载任务会同时检查“中转站”内现有文件；历史条目对应文件缺失时才重新解析并重试。

边界：站点结构变化或 Cloudflare Challenge 可能导致解析为空；工具会保留每页原始链接数和最终唯一数，便于发现异常。请遵守书面授权范围、站点规则和适用法律。
