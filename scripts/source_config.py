"""Safe read/write helpers for the manually managed crawl sources."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit


class SourceConfigError(ValueError):
    pass


def load_source_config(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceConfigError("无法读取抓取配置") from exc
    if not isinstance(payload, dict):
        raise SourceConfigError("抓取配置格式无效")
    sources = payload.get("sources")
    if not isinstance(sources, list):
        raise SourceConfigError("抓取链接列表格式无效")
    try:
        pages_per_source = int(payload.get("pagesPerSource") or 2)
    except (TypeError, ValueError) as exc:
        raise SourceConfigError("抓取页数配置无效") from exc
    return {"pagesPerSource": pages_per_source, "sources": sources}


def normalize_source(name: str, raw_url: str) -> dict[str, str]:
    try:
        parsed = urlsplit(raw_url.strip())
    except ValueError as exc:
        raise SourceConfigError("链接格式无效") from exc
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or hostname not in {"91porn.com", "www.91porn.com"}:
        raise SourceConfigError("只支持 91porn.com 的 HTTPS 榜单链接")
    if parsed.username or parsed.password or parsed.path != "/v.php":
        raise SourceConfigError("请输入不含账号信息的 /v.php 榜单链接")
    query = parse_qs(parsed.query)
    category = str((query.get("category") or [""])[0]).strip()
    if not category:
        raise SourceConfigError("榜单链接必须包含 category 参数")
    normalized_query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    normalized_url = urlunsplit(("https", hostname, "/v.php", normalized_query, ""))
    source_name = name.strip() or category
    if len(source_name) > 40:
        raise SourceConfigError("名称不能超过 40 个字符")
    return {"name": source_name, "url": normalized_url}


def _write_source_config(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise SourceConfigError("无法保存抓取配置") from exc


def add_source(path: Path, name: str, url: str) -> list[dict[str, str]]:
    config = load_source_config(path)
    sources = [item for item in config["sources"] if isinstance(item, dict) and item.get("url")]
    if len(sources) >= 20:
        raise SourceConfigError("最多配置 20 个抓取链接")
    source = normalize_source(name, url)
    normalized_existing = set()
    for item in sources:
        try:
            normalized_existing.add(normalize_source(str(item.get("name") or ""), str(item.get("url") or ""))["url"])
        except SourceConfigError:
            continue
    if source["url"] in normalized_existing:
        raise SourceConfigError("该抓取链接已经存在")
    sources.append(source)
    _write_source_config(path, {"pagesPerSource": config["pagesPerSource"], "sources": sources})
    return sources


def remove_source(path: Path, url: str) -> list[dict[str, str]]:
    config = load_source_config(path)
    sources = [item for item in config["sources"] if isinstance(item, dict) and item.get("url")]
    if len(sources) <= 1:
        raise SourceConfigError("至少需要保留一个抓取链接")
    remaining = [item for item in sources if str(item.get("url")) != url]
    if len(remaining) == len(sources):
        raise SourceConfigError("未找到要删除的抓取链接")
    _write_source_config(path, {"pagesPerSource": config["pagesPerSource"], "sources": remaining})
    return remaining
