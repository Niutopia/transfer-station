#!/usr/bin/env python3
"""Build the local dashboard snapshot from crawler artifacts and 中转站."""

from __future__ import annotations

import json
import os
import shutil
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from task_lock import lock_is_active, read_lock


PROJECT = Path(__file__).resolve().parents[1]
STAGING = PROJECT / "中转站"
CRAWLER = PROJECT / "public-video-crawler"
CRAWL_JSON = CRAWLER / "videos-with-media.json"
DAILY_CONFIG = PROJECT / "config" / "daily-sources.json"
DATA = PROJECT / "data"
DOWNLOAD_MANIFEST = DATA / "download-manifest.json"
PARTIAL_DIR = DATA / "partials"
OUTPUT = PROJECT / "public" / "status.json"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ts", ".mkv", ".mov", ".avi"}
STAGING_CACHE = DATA / "staging-index-cache.json"


def iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def scan_files():
    files = []
    if not STAGING.exists():
        STAGING.mkdir(parents=True, exist_ok=True)
    try:
        marker = STAGING.stat().st_mtime_ns
    except OSError:
        marker = 0
    cached = load_json(STAGING_CACHE, {})
    cached_rows = cached.get("files") if isinstance(cached, dict) and cached.get("marker") == marker else None
    if isinstance(cached_rows, list):
        for row in cached_rows:
            if not isinstance(row, dict) or not row.get("relativePath"):
                continue
            path = STAGING / str(row["relativePath"])
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                files.append({**row, "path": path})
        return files
    for path in STAGING.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        item = {
            "path": path,
            "relativePath": path.relative_to(STAGING).as_posix(),
            "sizeBytes": stat.st_size,
            "modifiedAt": iso_from_timestamp(stat.st_mtime),
            "timestamp": stat.st_mtime,
            "extension": path.suffix.lower(),
        }
        if path.suffix.lower() in VIDEO_EXTENSIONS:
            files.append(item)
    cache_rows = [{key: value for key, value in item.items() if key != "path"} for item in files]
    try:
        STAGING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        temporary = STAGING_CACHE.with_suffix(STAGING_CACHE.suffix + ".tmp")
        temporary.write_text(json.dumps({"marker": marker, "files": cache_rows}, ensure_ascii=False), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, STAGING_CACHE)
    except OSError:
        pass
    return files


def scan_partials():
    PARTIAL_DIR.mkdir(parents=True, exist_ok=True)
    return [path for path in PARTIAL_DIR.rglob("*.part") if path.is_file()]


def main() -> int:
    now = datetime.now().astimezone()
    LOCK_FILE = DATA / ".crawling.lock"
    is_crawling = lock_is_active(LOCK_FILE)
    lock_payload = read_lock(LOCK_FILE) if is_crawling else {}
    crawl = load_json(CRAWL_JSON, {"metadata": {}, "videos": []})
    metadata = crawl.get("metadata") if isinstance(crawl, dict) else {}
    videos = crawl.get("videos") if isinstance(crawl, dict) else []
    if not isinstance(metadata, dict):
        metadata = {}
    if not isinstance(videos, list):
        videos = []
    daily_config = load_json(DAILY_CONFIG, {"pagesPerSource": 2, "sources": []})
    configured_sources = daily_config.get("sources") if isinstance(daily_config, dict) else []
    if not isinstance(configured_sources, list):
        configured_sources = []

    title_by_key = {
        str(video.get("viewkey")): str(video.get("title") or "未命名视频")
        for video in videos
        if isinstance(video, dict) and video.get("viewkey")
    }
    listed_keys = set(title_by_key)

    success_keys = set()
    success_file = DATA / "download-success.txt"
    if success_file.exists():
        success_keys = {line.strip() for line in success_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    files = scan_files()
    partials = scan_partials()
    downloaded_keys = {item["path"].stem for item in files if item["path"].stem in listed_keys}

    resolved_keys = {
        str(video.get("viewkey"))
        for video in videos
        if isinstance(video, dict) and video.get("viewkey") and (video.get("media_url") or str(video.get("viewkey")) in downloaded_keys)
    }
    total_bytes = sum(item["sizeBytes"] for item in files)
    today = now.date()
    today_files = [item for item in files if datetime.fromtimestamp(item["timestamp"]).astimezone().date() == today]
    today_bytes = sum(item["sizeBytes"] for item in today_files)

    day_rows = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        matched = [item for item in files if datetime.fromtimestamp(item["timestamp"]).astimezone().date() == day]
        day_rows.append({
            "date": day.isoformat(),
            "label": day.strftime("%m/%d"),
            "files": len(matched),
            "bytes": sum(item["sizeBytes"] for item in matched),
        })

    types = Counter(item["extension"].lstrip(".").upper() or "OTHER" for item in files)
    disk = shutil.disk_usage(STAGING)
    latest_crawl_at = iso_from_timestamp(CRAWL_JSON.stat().st_mtime) if CRAWL_JSON.exists() else None
    crawl_age_hours = ((now.timestamp() - CRAWL_JSON.stat().st_mtime) / 3600) if CRAWL_JSON.exists() else None

    manifest = load_json(DOWNLOAD_MANIFEST, [])
    manifest_failures = []
    if isinstance(manifest, list):
        manifest_failures = [item for item in manifest if isinstance(item, dict) and item.get("status") == "failed"]

    alerts = []
    if not CRAWL_JSON.exists():
        alerts.append({"level": "error", "title": "缺少抓取快照", "detail": "尚未找到 videos-with-media.json。"})
    elif crawl_age_hours is not None and crawl_age_hours > 26:
        alerts.append({"level": "warning", "title": "抓取数据已过期", "detail": f"最近抓取距今 {crawl_age_hours:.1f} 小时。"})
    if len(resolved_keys) < len(listed_keys):
        alerts.append({"level": "warning", "title": "存在未解析媒体", "detail": f"{len(listed_keys) - len(resolved_keys)} 个条目缺少媒体地址。"})
    if partials and not is_crawling:
        alerts.append({"level": "warning", "title": "发现未完成下载", "detail": f"临时目录有 {len(partials)} 个 .part 文件。"})
    unresolved_manifest_failures = [item for item in manifest_failures if str(item.get("viewkey") or "") not in downloaded_keys]
    if unresolved_manifest_failures:
        alerts.append({"level": "error", "title": "最近下载有失败项", "detail": f"下载清单记录 {len(unresolved_manifest_failures)} 个尚未恢复的失败。"})
    missing_success_files = {key for key in success_keys if key in listed_keys and key not in downloaded_keys}
    if missing_success_files:
        alerts.append({"level": "warning", "title": "历史记录与文件不一致", "detail": f"成功历史中有 {len(missing_success_files)} 个条目缺少实际文件，将在后续任务中重试。"})
    if not alerts:
        alerts.append({"level": "success", "title": "数据链路正常", "detail": "未发现过期快照、未解析媒体或残留分片。"})

    recent = []
    for item in sorted(files, key=lambda value: value["timestamp"], reverse=True)[:12]:
        key = item["path"].stem
        recent.append({
            "name": title_by_key.get(key, item["path"].name),
            "fileName": item["path"].name,
            "relativePath": item["relativePath"],
            "sizeBytes": item["sizeBytes"],
            "modifiedAt": item["modifiedAt"],
            "status": "complete",
        })

    download_progress = load_json(DATA / "download-progress.json", None)
    progress_active = download_progress.get("active", []) if isinstance(download_progress, dict) else []
    progress_by_key = {
        str(item.get("viewkey")): item
        for item in progress_active
        if isinstance(item, dict) and item.get("viewkey")
    }

    partial_by_key = {Path(path.name.removesuffix(".part")).stem: path for path in partials}
    active_downloads = []
    active_keys = set(partial_by_key) | set(progress_by_key)
    for key in sorted(active_keys if is_crawling else set()):
        p = partial_by_key.get(key)
        active_keys.add(key)
        file_size = p.stat().st_size if p else 0
        mtime = p.stat().st_mtime if p else now.timestamp()
        mtime_dt = datetime.fromtimestamp(mtime).astimezone()
        age_seconds = max(0.0, (now - mtime_dt).total_seconds())
        estimated_speed = file_size / age_seconds if age_seconds > 0 else 0
        live_progress = progress_by_key.get(key, {})
        bytes_done = int(live_progress.get("bytesDone") or file_size)
        bytes_total = int(live_progress.get("bytesTotal") or 0)
        speed_bytes_s = float(live_progress.get("speedBytesS") or estimated_speed)
        active_downloads.append({
            "name": title_by_key.get(key, key),
            "fileName": str(live_progress.get("fileName") or (p.name if p else f"{key}.mp4")),
            "sizeBytes": bytes_done,
            "totalBytes": bytes_total,
            "progressPercent": round(bytes_done / bytes_total * 100, 1) if bytes_total else None,
            "modifiedAt": iso_from_timestamp(mtime),
            "status": str(live_progress.get("state") or "downloading"),
            "speedBytesS": round(speed_bytes_s, 1),
            "etaSeconds": live_progress.get("etaSeconds"),
            "attempt": int(live_progress.get("attempt") or 0),
            "retries": int(live_progress.get("retries") or 0),
            "message": str(live_progress.get("message") or ""),
            "resumed": bool(live_progress.get("resumed")),
        })

    pending_downloads = []
    if not is_crawling:
        for key, partial in sorted(partial_by_key.items()):
            if key in resolved_keys and key not in downloaded_keys:
                pending_downloads.append({
                    "name": title_by_key.get(key, key),
                    "fileName": partial.name,
                    "sizeBytes": partial.stat().st_size,
                    "status": "resumable",
                })
    for key, title in title_by_key.items():
        if key in resolved_keys and key not in downloaded_keys and key not in active_keys:
            pending_downloads.append({
                "name": title,
                "fileName": f"{key}.mp4",
                "status": "waiting"
            })

    unique_videos = int(metadata.get("unique_videos") or len(listed_keys))
    resolved_count = len(resolved_keys)
    downloaded_count = len(downloaded_keys)
    pending_count = max(0, unique_videos - downloaded_count)
    raw_links = int(metadata.get("raw_detail_links") or unique_videos)
    duplicates = int(metadata.get("duplicates_removed") or max(0, raw_links - unique_videos))
    has_attention = any(alert.get("level") in {"warning", "error"} for alert in alerts)
    run_status = "active" if is_crawling else ("ready" if unique_videos and downloaded_count == unique_videos and not has_attention else "attention")

    payload = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
        "source": {
            "stagingPath": str(STAGING),
            "crawlerPath": str(CRAWLER),
            "latestCrawlAt": latest_crawl_at,
            "listingCount": len(configured_sources),
            "pagesPerListing": int(daily_config.get("pagesPerSource") or 2) if isinstance(daily_config, dict) else 2,
        },
        "overview": {
            "rawLinks": raw_links,
            "uniqueVideos": unique_videos,
            "duplicatesRemoved": duplicates,
            "resolvedVideos": resolved_count,
            "downloadedVideos": downloaded_count,
            "pendingVideos": pending_count,
            "partialDownloads": len(partials),
            "todayFiles": len(today_files),
            "todayBytes": today_bytes,
            "totalFiles": len(files),
            "totalBytes": total_bytes,
            "downloadRate": round((downloaded_count / unique_videos * 100), 1) if unique_videos else 0,
        },
        "storage": {
            "usedBytes": total_bytes,
            "diskFreeBytes": disk.free,
            "diskTotalBytes": disk.total,
            "diskUsedPercent": round((disk.used / disk.total * 100), 1) if disk.total else 0,
        },
        "daily": day_rows,
        "types": [{"name": name, "count": count} for name, count in types.most_common()],
        "latestRun": {
            "status": run_status,
            "startedAt": str(lock_payload.get("startedAt") or latest_crawl_at) if is_crawling else latest_crawl_at,
            "taskState": lock_payload.get("state") if lock_payload else None,
            "taskControllable": bool(lock_payload.get("controllable")) if lock_payload else False,
            "stages": [
                {"name": "列表抓取", "status": "active" if is_crawling else ("done" if raw_links else "waiting"), "value": raw_links, "note": "原始详情链接"},
                {"name": "去重", "status": "active" if is_crawling else ("done" if unique_videos else "waiting"), "value": unique_videos, "note": f"移除 {duplicates} 个重复"},
                {"name": "媒体解析", "status": "active" if is_crawling else ("done" if resolved_count == unique_videos and unique_videos else "attention"), "value": resolved_count, "note": f"共 {unique_videos} 个唯一视频"},
                {"name": "下载入库", "status": "done" if downloaded_count == unique_videos and unique_videos else "active" if is_crawling else "attention" if partials else "waiting", "value": downloaded_count, "note": f"目标目录：{STAGING.name}"},
            ],
        },
        "alerts": alerts,
        "recentFiles": recent,
        "activeDownloads": active_downloads,
        "pendingDownloads": pending_downloads[:50],
    }
    # Read crawl / download progress files
    crawl_progress = load_json(DATA / "crawl-progress.json", None)
    progress = None
    if crawl_progress and isinstance(crawl_progress, dict):
        progress = {"stage": crawl_progress.get("stage", "resolving"), "done": int(crawl_progress.get("done", 0)), "total": int(crawl_progress.get("total", 0))}
    elif download_progress and isinstance(download_progress, dict):
        progress = {
            "stage": download_progress.get("stage", "downloading"),
            "done": int(download_progress.get("done", 0)),
            "total": int(download_progress.get("total", 0)),
            "active": download_progress.get("active", []),
            "bytesDone": int(download_progress.get("bytesDone", 0)),
            "bytesTotalKnown": int(download_progress.get("bytesTotalKnown", 0)),
            "knownItems": int(download_progress.get("knownItems", 0)),
            "failed": int(download_progress.get("failed", 0)),
            "speedBytesS": float(download_progress.get("speedBytesS", 0)),
            "etaSeconds": download_progress.get("etaSeconds"),
            "startedAt": download_progress.get("startedAt"),
            "updatedAt": download_progress.get("updatedAt"),
        }
    payload["progress"] = progress
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, OUTPUT)
    print(f"updated {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
