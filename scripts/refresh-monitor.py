#!/usr/bin/env python3
"""Build the local dashboard snapshot from crawler artifacts and 中转站."""

from __future__ import annotations

import html
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1] / "public-video-crawler"))
# Share the crawler's definition of a safe block instead of re-typing the set in
# every consumer; the copies had already drifted apart.
from crawler import BLOCKED_MEDIA_FAILURE_KINDS as SAFE_BLOCK_KINDS, load_ignored_media_keys

from download_history import sync_download_history
from task_lock import lock_is_active, read_lock
from task_history import event_counter, event_result_status, event_task_type, latest_run_event, latest_successful_daily_run, latest_task_event


PROJECT = Path(__file__).resolve().parents[1]
STAGING = PROJECT / "中转站"
CRAWLER = PROJECT / "public-video-crawler"
CRAWL_JSON = CRAWLER / "videos-with-media.json"
DAILY_CONFIG = PROJECT / "config" / "daily-sources.json"
DATA = PROJECT / "data"
DOWNLOAD_MANIFEST = DATA / "download-manifest.json"
REPAIR_MANIFEST = DATA / "repair-download-manifest.json"
IGNORED_MEDIA = DATA / "ignored-media.json"
PARTIAL_DIR = DATA / "partials"
OUTPUT = PROJECT / "public" / "status.json"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".ts", ".mkv", ".mov", ".avi"}
RUN_HISTORY = DATA / "run-history.jsonl"
LAST_COMPLETED_PROGRESS = DATA / "last-completed-progress.json"
DOWNLOAD_HISTORY = DATA / "download-history.json"


def iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def hours_since(value: object, now: datetime) -> float | None:
    try:
        timestamp = datetime.fromisoformat(str(value or ""))
        if timestamp.tzinfo is None:
            timestamp = timestamp.astimezone()
        return max(0.0, (now - timestamp.astimezone()).total_seconds() / 3600)
    except (TypeError, ValueError):
        return None


def snapshots_equal(previous: object, current: object) -> bool:
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    return (
        {key: value for key, value in previous.items() if key != "generatedAt"}
        == {key: value for key, value in current.items() if key != "generatedAt"}
    )


def serialize_download_progress(progress, *, blocked_failures: int = 0, true_failures: int | None = None):
    if not isinstance(progress, dict):
        return None
    stage = str(progress.get("stage") or "downloading")
    failed = int(progress.get("failed", 0))
    if stage == "failed" and failed and true_failures == 0 and failed <= blocked_failures:
        stage = "complete"
        failed = 0
    return {
        "stage": stage,
        "route": progress.get("route"),
        "routeProbe": progress.get("routeProbe"),
        "done": int(progress.get("done", 0)),
        "total": int(progress.get("total", 0)),
        "active": progress.get("active", []),
        "bytesDone": int(progress.get("bytesDone", 0)),
        "bytesTotalKnown": int(progress.get("bytesTotalKnown", 0)),
        "knownItems": int(progress.get("knownItems", 0)),
        "failed": failed,
        "blocked": blocked_failures,
        "speedBytesS": float(progress.get("speedBytesS", 0)),
        "etaSeconds": progress.get("etaSeconds"),
        "startedAt": progress.get("startedAt"),
        "updatedAt": progress.get("updatedAt"),
    }


def scan_files():
    files = []
    if not STAGING.exists():
        STAGING.mkdir(parents=True, exist_ok=True)
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
        str(video.get("viewkey")): html.unescape(str(video.get("title") or "未命名视频"))
        for video in videos
        if isinstance(video, dict) and video.get("viewkey")
    }
    listed_keys = set(title_by_key)
    configured_ignored_keys = load_ignored_media_keys(IGNORED_MEDIA)

    success_keys = set()
    success_file = DATA / "download-success.txt"
    if success_file.exists():
        success_keys = {line.strip() for line in success_file.read_text(encoding="utf-8").splitlines() if line.strip()}
    files = scan_files()
    partials = scan_partials()
    downloaded_keys = {item["path"].stem for item in files if item["path"].stem in listed_keys}
    completed_keys = listed_keys & (success_keys | downloaded_keys)
    # Keep the configured count visible even if an ignored item later appears
    # in the success ledger; ignored entries belong to the ignored bucket, not
    # to the eligible downloaded inventory.
    ignored_keys = listed_keys & configured_ignored_keys

    resolved_keys = {
        str(video.get("viewkey"))
        for video in videos
        if isinstance(video, dict) and video.get("viewkey") and (video.get("media_url") or str(video.get("viewkey")) in completed_keys)
    } - ignored_keys
    resolve_failures = metadata.get("resolve_failures") if isinstance(metadata, dict) else []
    if not isinstance(resolve_failures, list):
        resolve_failures = []
    blocked_failure_keys = {
        str(item.get("viewkey"))
        for item in resolve_failures
        if isinstance(item, dict)
        and item.get("viewkey")
        and (
            item.get("kind") in SAFE_BLOCK_KINDS
            or "详情页媒体与榜单不一致" in str(item.get("error") or "")
        )
    }
    total_bytes = sum(item["sizeBytes"] for item in files)
    today = now.date()
    current_today_files = [item for item in files if datetime.fromtimestamp(item["timestamp"]).astimezone().date() == today]
    current_today_bytes = sum(item["sizeBytes"] for item in current_today_files)

    download_history = sync_download_history(DOWNLOAD_HISTORY, DATA / "logs")
    history_by_day: dict[str, list[dict[str, object]]] = {}
    for item in download_history:
        history_by_day.setdefault(str(item.get("date") or ""), []).append(item)
    today_history = history_by_day.get(today.isoformat(), [])
    ingested_bytes = sum(int(item.get("bytes") or 0) for item in download_history)

    day_rows = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        matched = history_by_day.get(day.isoformat(), [])
        day_rows.append({
            "date": day.isoformat(),
            "label": day.strftime("%m/%d"),
            "files": len(matched),
            "bytes": sum(int(item.get("bytes") or 0) for item in matched),
        })

    types = Counter(item["extension"].lstrip(".").upper() or "OTHER" for item in files)
    disk = shutil.disk_usage(STAGING)
    snapshot_updated_at = iso_from_timestamp(CRAWL_JSON.stat().st_mtime) if CRAWL_JSON.exists() else None
    latest_event = latest_run_event(RUN_HISTORY)
    latest_crawl_event = latest_task_event(RUN_HISTORY, "crawl")
    latest_repair_event = latest_task_event(RUN_HISTORY, "repair")
    latest_event_type = event_task_type(latest_event)
    latest_event_status = event_result_status(latest_event)
    latest_event_listing_failures = event_counter(latest_event, "listingFailures")
    latest_crawl_at = str((latest_crawl_event or {}).get("timestamp") or snapshot_updated_at or "") or None
    latest_repair_at = str((latest_repair_event or {}).get("timestamp") or "") or None
    # Failed attempts do not refresh the crawl snapshot, but a repair rewrites
    # that same file, which reset its mtime and hid a stale inventory for as long
    # as the user kept clicking 复核.  Use the newest crawl that actually produced
    # data and fall back to the file only when no such event exists yet.
    latest_completed_crawl = latest_task_event(RUN_HISTORY, "crawl", statuses={"success", "attention"})
    crawl_age_hours = hours_since(
        str((latest_completed_crawl or {}).get("timestamp") or "") or snapshot_updated_at,
        now,
    )

    # The repair task writes its own manifest, so reading only the crawl one made
    # every repair-phase failure and safe block invisible to the overview.
    manifest_sources = []
    for path in (DOWNLOAD_MANIFEST, REPAIR_MANIFEST):
        rows = load_json(path, [])
        if isinstance(rows, list) and rows:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                stamp = 0.0
            manifest_sources.append((stamp, rows))
    manifest_rows = [row for _stamp, rows in manifest_sources for row in rows if isinstance(row, dict)]
    # "最近下载" means the newer of the two runs, not both concatenated.
    latest_manifest_rows = [
        row
        for row in (max(manifest_sources, key=lambda entry: entry[0])[1] if manifest_sources else [])
        if isinstance(row, dict)
    ]

    def manifest_block_kind(row: dict[str, object]) -> str:
        kind = str(row.get("kind") or "")
        if kind in SAFE_BLOCK_KINDS:
            return kind
        if "详情页媒体与榜单不一致" in str(row.get("error") or ""):
            return "media_mismatch"
        return ""

    manifest_blocked_kinds = {
        str(row.get("viewkey") or ""): kind
        for row in manifest_rows
        if row.get("status") in {"blocked", "failed"} and (kind := manifest_block_kind(row))
        and (row.get("status") == "blocked" or kind == "media_mismatch")
    }
    manifest_blocked_kinds.pop("", None)
    manifest_blocked_keys = set(manifest_blocked_kinds)
    manifest_failures = [
        row
        for row in latest_manifest_rows
        if row.get("status") == "failed"
        and str(row.get("viewkey") or "") not in manifest_blocked_keys
    ]
    unresolved_keys = listed_keys - resolved_keys - ignored_keys
    # A resolver block only counts while the item still has no media URL.  A
    # download-phase block is confirmed by the manifest even though the snapshot
    # still carries the URL that produced it, so intersecting it away left a
    # confirmed mismatch with no alert, no count, and a re-queue on every 复核.
    blocked_keys = (unresolved_keys & blocked_failure_keys) | ((listed_keys & manifest_blocked_keys) - ignored_keys)
    blocked_count = len(blocked_keys)

    # The snapshot is a long-lived inventory, while a crawl event only covers
    # the new/retry items selected by that run.  Do not turn every old item
    # without a media URL into a failure for the latest (often no-op) crawl.
    # A successful crawl writes the resolver failures for that run; when the
    # latest task is a repair, the snapshot may still contain failures from
    # earlier crawls, so those are deliberately treated as backlog here.
    current_failure_keys: set[str] = set()
    if latest_event_type == "crawl" and latest_event_status in {"success", "attention"}:
        current_failure_keys = {
            str(item.get("viewkey"))
            for item in resolve_failures
            if isinstance(item, dict) and item.get("viewkey")
        } & unresolved_keys
    current_unresolved_error_keys = current_failure_keys - blocked_keys
    historical_unresolved_keys = unresolved_keys - current_failure_keys
    latest_crawl_no_work = bool(
        latest_event_type == "crawl"
        and latest_event_status == "success"
        and isinstance(latest_event, dict)
        and "newVideos" in latest_event
        and "retryVideos" in latest_event
        and event_counter(latest_event, "newVideos") == 0
        and event_counter(latest_event, "retryVideos") == 0
    )

    alerts = []
    listing_failures = metadata.get("listing_failures") if isinstance(metadata, dict) else []
    listing_failure_count = len(listing_failures) if isinstance(listing_failures, list) else 0
    if latest_event_status == "failed":
        failed_scope = (
            f"，{latest_event_listing_failures} 个榜单页面未成功读取"
            if latest_event_listing_failures
            else ""
        )
        alerts.append({
            "level": "error",
            "title": "最近一次任务执行失败",
            "detail": f"任务结果已按本次实际数据归零{failed_scope}；上次成功快照仅用于历史总览。",
        })
    if not CRAWL_JSON.exists():
        alerts.append({"level": "error", "title": "缺少抓取快照", "detail": "尚未找到 videos-with-media.json。"})
    elif crawl_age_hours is not None and crawl_age_hours > 26:
        alerts.append({"level": "warning", "title": "抓取数据已过期", "detail": f"最近抓取距今 {crawl_age_hours:.1f} 小时。"})
    if listing_failure_count:
        alerts.append({
            "level": "warning",
            "title": "部分榜单页面抓取失败",
            "detail": f"本次有 {listing_failure_count} 个列表页未成功读取，其余成功页面已保留。",
        })
    if blocked_count:
        # Classify each blocked key by its own recorded kind.  Subtracting a count
        # of resolver rows from a count of blocked keys mixed two populations, so
        # download-phase mismatches were reported as "没有可下载媒体".
        blocked_kinds: dict[str, str] = {}
        for item in resolve_failures:
            if not isinstance(item, dict):
                continue
            key = str(item.get("viewkey") or "")
            if key not in blocked_keys or key in blocked_kinds:
                continue
            kind = str(item.get("kind") or "")
            if kind not in SAFE_BLOCK_KINDS and "详情页媒体与榜单不一致" in str(item.get("error") or ""):
                kind = "media_mismatch"
            if kind in SAFE_BLOCK_KINDS:
                blocked_kinds[key] = kind
        for key, kind in manifest_blocked_kinds.items():
            if key in blocked_keys:
                blocked_kinds.setdefault(key, kind)
        mismatch_count = sum(1 for kind in blocked_kinds.values() if kind == "media_mismatch")
        unavailable_count = sum(1 for kind in blocked_kinds.values() if kind == "media_unavailable")
        unclassified_count = max(0, blocked_count - mismatch_count - unavailable_count)
        reasons = []
        if mismatch_count:
            reasons.append(f"{mismatch_count} 个播放器资源与榜单不一致")
        if unavailable_count:
            reasons.append(f"{unavailable_count} 个详情页没有可下载媒体")
        if unclassified_count:
            reasons.append(f"{unclassified_count} 个此前已确认不可用")
        alerts.append({
            "level": "success",
            "title": f"已安全跳过 {blocked_count} 个不可用媒体",
            "detail": "，".join(reasons) + "；均不会写入中转站。",
        })
    if current_unresolved_error_keys:
        alerts.append({
            "level": "warning",
            "scope": "task",
            "title": "本次有未解析媒体",
            "detail": f"本次任务有 {len(current_unresolved_error_keys)} 个条目因解析错误缺少媒体地址。",
        })
    if historical_unresolved_keys:
        alerts.append({
            "level": "info",
            "scope": "backlog",
            "title": "存在历史待复核项",
            "detail": f"{len(historical_unresolved_keys)} 个条目未在本次任务中重试；点击“复核待处理项”时才会发起低频详情页请求。",
        })
    if ignored_keys:
        alerts.append({
            "level": "info",
            "scope": "backlog",
            "title": f"已忽略 {len(ignored_keys)} 个无媒体条目",
            "detail": "这些条目按用户确认永久跳过，不会进入日常抓取或复核队列；移除 ignored-media.json 中对应记录即可恢复。",
        })
    if partials and not is_crawling:
        alerts.append({"level": "warning", "title": "发现未完成下载", "detail": f"临时目录有 {len(partials)} 个 .part 文件。"})
    unresolved_manifest_failures = [
        item
        for item in manifest_failures
        if str(item.get("viewkey") or "") not in completed_keys
        and str(item.get("viewkey") or "") not in blocked_keys
    ]
    if unresolved_manifest_failures:
        alerts.append({"level": "error", "title": "最近下载有失败项", "detail": f"下载清单记录 {len(unresolved_manifest_failures)} 个尚未恢复的失败。"})
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
            if key in resolved_keys and key not in completed_keys and key not in ignored_keys:
                pending_downloads.append({
                    "name": title_by_key.get(key, key),
                    "fileName": partial.name,
                    "sizeBytes": partial.stat().st_size,
                    "status": "resumable",
                })
    for key, title in title_by_key.items():
        if key in resolved_keys and key not in completed_keys and key not in active_keys and key not in ignored_keys:
            pending_downloads.append({
                "name": title,
                "fileName": f"{key}.mp4",
                "status": "waiting"
            })

    unique_videos = int(metadata.get("unique_videos") or len(listed_keys))
    resolved_count = len(resolved_keys)
    downloaded_count = len(completed_keys - ignored_keys)
    handled_resolve_count = resolved_count + blocked_count + len(ignored_keys)
    handled_download_count = downloaded_count + blocked_count + len(ignored_keys)
    pending_count = max(0, unique_videos - handled_download_count)
    raw_links = int(metadata.get("raw_detail_links") or unique_videos)
    duplicates = int(metadata.get("duplicates_removed") or max(0, raw_links - unique_videos))
    latest_result_raw_links = event_counter(latest_event, "rawLinks", raw_links)
    latest_result_unique = event_counter(latest_event, "uniqueVideos", unique_videos)
    latest_result_skipped = event_counter(latest_event, "skippedVideos", int(metadata.get("known_videos_skipped") or 0))
    latest_result_ignored = event_counter(
        latest_event,
        "ignoredVideos",
        len(ignored_keys) if latest_crawl_no_work else 0,
    )
    has_attention = any(
        alert.get("scope") != "backlog" and alert.get("level") in {"warning", "error"}
        for alert in alerts
    )
    inventory_handled = not unique_videos or handled_download_count == unique_videos
    if is_crawling:
        run_status = "active"
    elif latest_event_type == "crawl" and latest_event_status == "success" and not has_attention:
        # A successful new-only crawl is ready even when the inventory still
        # has an explicit, separately actionable repair backlog.
        run_status = "ready"
    elif latest_event_status == "success" and not has_attention and inventory_handled:
        # A repair alone only means "ready" once the backlog it works on is
        # actually cleared; it says nothing about the listing being current.
        run_status = "ready"
    elif latest_event_status == "none" and unique_videos and inventory_handled and not has_attention:
        run_status = "ready"
    else:
        run_status = "attention"
    completed_run = latest_successful_daily_run(RUN_HISTORY, today=today)
    # Whether today's daily task finished is a property of that run, not of
    # whatever ran most recently: appending a repair afterwards used to clear the
    # flag even though the crawl had completed successfully.
    completed_today = bool(
        completed_run
        and not is_crawling
        and not partials
        and not current_unresolved_error_keys
        and event_counter(completed_run, "listingFailures") == 0
        and not unresolved_manifest_failures
    )
    failed_crawl = not is_crawling and latest_event_type == "crawl" and latest_event_status == "failed"
    if failed_crawl:
        # A failed run is not the same as a run that never got anywhere.  Blanking
        # these two stages contradicted the very same event's counters, which the
        # UI printed right next to them.
        failed_attempted = event_counter(latest_event, "newVideos") + event_counter(latest_event, "retryVideos")
        failed_blocked = event_counter(latest_event, "blockedVideos")
        failed_failed = event_counter(latest_event, "failedVideos")
        failed_downloaded = event_counter(latest_event, "downloadedVideos")
        failed_present = event_counter(latest_event, "alreadyPresentVideos")
        failed_ingested = failed_downloaded + failed_present
        stages = [
            {
                "name": "列表抓取",
                "status": "attention",
                "value": latest_result_raw_links,
                "note": f"{latest_event_listing_failures} 个页面失败" if latest_event_listing_failures else "任务未完成",
            },
            {
                "name": "去重",
                "status": "done" if latest_result_raw_links else "waiting",
                "value": latest_result_unique,
                "note": f"移除 {duplicates} 个重复" if latest_result_raw_links else "等待榜单抓取成功",
            },
            {
                "name": "媒体解析",
                "status": "attention" if failed_attempted else "waiting",
                "value": max(0, failed_attempted - failed_blocked - failed_failed),
                "note": f"本次处理 {failed_attempted} 个详情后中断" if failed_attempted else "本次未进入该阶段",
            },
            {
                "name": "下载入库",
                "status": "attention" if failed_ingested else "waiting",
                "value": failed_ingested,
                "note": f"中断前已入库 {failed_downloaded}，{failed_present} 个文件已存在" if failed_present else (
                    f"中断前已入库 {failed_downloaded}" if failed_ingested else "本次未进入该阶段"
                ),
            },
        ]
    elif latest_crawl_no_work:
        stages = [
            {"name": "列表抓取", "status": "done", "value": latest_result_raw_links, "note": "本次榜单读取成功"},
            {"name": "去重", "status": "done", "value": latest_result_unique, "note": "本次无新增详情"},
            {"name": "媒体解析", "status": "done", "value": 0, "note": "本次无新增条目，未请求详情页"},
            {"name": "下载入库", "status": "done", "value": 0, "note": "本次无新增条目，无需下载"},
        ]
    elif latest_event_type == "crawl" and latest_event_status in {"success", "attention"}:
        attempted = event_counter(latest_event, "newVideos") + event_counter(latest_event, "retryVideos")
        event_blocked = event_counter(latest_event, "blockedVideos")
        event_failed = event_counter(latest_event, "failedVideos")
        media_value = max(0, attempted - event_blocked - event_failed)
        stage_status = "attention" if latest_event_status == "attention" or event_failed else "done"
        stages = [
            {"name": "列表抓取", "status": "done" if not latest_event_listing_failures else "attention", "value": latest_result_raw_links, "note": "原始详情链接"},
            {"name": "去重", "status": "done", "value": latest_result_unique, "note": f"移除 {duplicates} 个重复"},
            {"name": "媒体解析", "status": stage_status, "value": media_value, "note": f"本次处理 {attempted} 个详情，安全拦截 {event_blocked} 个" if event_blocked else f"本次处理 {attempted} 个详情"},
            {
                "name": "下载入库",
                "status": stage_status if stage_status == "attention" else "done",
                # download.py reports "skipped" for a file that is already in
                # place.  Counting only fresh downloads made a successful re-run
                # look like it ingested nothing at all.
                "value": event_counter(latest_event, "downloadedVideos") + event_counter(latest_event, "alreadyPresentVideos"),
                "note": (
                    f"目标目录：{STAGING.name}；{event_counter(latest_event, 'alreadyPresentVideos')} 个文件已存在"
                    if event_counter(latest_event, "alreadyPresentVideos")
                    else f"目标目录：{STAGING.name}"
                ),
            },
        ]
    else:
        stages = [
            {"name": "列表抓取", "status": "active" if is_crawling else ("done" if raw_links else "waiting"), "value": raw_links, "note": "原始详情链接"},
            {"name": "去重", "status": "active" if is_crawling else ("done" if unique_videos else "waiting"), "value": unique_videos, "note": f"移除 {duplicates} 个重复"},
            {"name": "媒体解析", "status": "active" if is_crawling else ("done" if handled_resolve_count == unique_videos and unique_videos else "attention"), "value": resolved_count, "note": f"已解析 {resolved_count}，安全拦截 {blocked_count}" if blocked_count else f"共 {unique_videos} 个唯一视频"},
            {"name": "下载入库", "status": "done" if handled_download_count == unique_videos and unique_videos else "active" if is_crawling else "attention" if partials else "waiting", "value": downloaded_count, "note": f"目标目录：{STAGING.name}"},
        ]

    payload = {
        "generatedAt": now.isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
        "source": {
            "stagingPath": str(STAGING),
            "crawlerPath": str(CRAWLER),
            "latestCrawlAt": latest_crawl_at,
            "listingCount": len(configured_sources),
            "pagesPerListing": int(daily_config.get("pagesPerSource") or 2) if isinstance(daily_config, dict) else 2,
            "sources": [
                {"name": str(item.get("name") or "未命名"), "url": str(item.get("url"))}
                for item in configured_sources
                if isinstance(item, dict) and item.get("url")
            ],
        },
        "overview": {
            "rawLinks": raw_links,
            "uniqueVideos": unique_videos,
            "duplicatesRemoved": duplicates,
            "resolvedVideos": resolved_count,
            "downloadedVideos": downloaded_count,
            "blockedVideos": blocked_count,
            "ignoredVideos": len(ignored_keys),
            "pendingVideos": pending_count,
            "repairableVideos": pending_count,
            "partialDownloads": len(partials),
            "todayFiles": len(current_today_files),
            "todayBytes": current_today_bytes,
            "totalFiles": len(files),
            "totalBytes": total_bytes,
            "todayIngestedFiles": len(today_history),
            "todayIngestedBytes": sum(int(item.get("bytes") or 0) for item in today_history),
            "ingestedFiles": len(download_history),
            "ingestedBytes": ingested_bytes,
            "downloadRate": round(
                (downloaded_count / max(1, unique_videos - len(ignored_keys)) * 100),
                1,
            ) if unique_videos else 0,
        },
        "storage": {
            "usedBytes": total_bytes,
            "diskFreeBytes": disk.free // (100 * 1024 * 1024) * (100 * 1024 * 1024),
            "diskTotalBytes": disk.total,
            "diskUsedPercent": round((disk.used / disk.total * 100), 1) if disk.total else 0,
        },
        "daily": day_rows,
        "types": [{"name": name, "count": count} for name, count in types.most_common()],
        "latestRun": {
            "status": run_status,
            "startedAt": (
                str(lock_payload.get("startedAt") or latest_crawl_at)
                if is_crawling
                else str((latest_event or {}).get("startedAt") or (latest_event or {}).get("timestamp") or latest_crawl_at or "") or None
            ),
            "lastCrawlAt": latest_crawl_at,
            "lastRepairAt": latest_repair_at,
            "taskState": lock_payload.get("state") if lock_payload else None,
            "taskKind": (
                "repair"
                if str(lock_payload.get("task") or "").startswith("repair-")
                else "crawl" if lock_payload else event_task_type(latest_event)
            ),
            "taskControllable": bool(lock_payload.get("controllable")) if lock_payload else False,
            "completedToday": completed_today,
            "completedAt": completed_run.get("timestamp") if completed_today and completed_run else None,
            "result": {
                "status": "active" if is_crawling else (
                    event_result_status(latest_event) if latest_event else "none"
                ),
                "taskType": latest_event_type,
                "startedAt": latest_event.get("startedAt") if latest_event else None,
                "finishedAt": latest_event.get("timestamp") if latest_event else None,
                "durationSeconds": latest_event.get("durationSeconds") if latest_event else None,
                "rawLinks": latest_result_raw_links,
                "uniqueVideos": latest_result_unique,
                "skippedVideos": latest_result_skipped,
                "newVideos": event_counter(latest_event, "newVideos"),
                "retryVideos": event_counter(latest_event, "retryVideos"),
                "downloadedVideos": event_counter(latest_event, "downloadedVideos"),
                "alreadyPresentVideos": event_counter(latest_event, "alreadyPresentVideos"),
                "duplicateVideos": event_counter(latest_event, "duplicateVideos"),
                "blockedVideos": int(
                    latest_event.get("blockedVideos")
                    if latest_event and "blockedVideos" in latest_event
                    else blocked_count
                ),
                "ignoredVideos": latest_result_ignored,
                "failedVideos": event_counter(latest_event, "failedVideos"),
                "downloadedBytes": event_counter(latest_event, "downloadedBytes"),
                "autoRetryAttempts": event_counter(latest_event, "autoRetryAttempts"),
                "autoRetriedVideos": event_counter(latest_event, "autoRetriedVideos"),
                "autoRecoveredVideos": event_counter(latest_event, "autoRecoveredVideos"),
                "listingFailures": int(
                    latest_event.get("listingFailures")
                    if latest_event and "listingFailures" in latest_event
                    else listing_failure_count if latest_event_type == "crawl" else 0
                ),
            },
            "stages": stages,
        },
        "alerts": alerts,
        "recentFiles": recent,
        "activeDownloads": active_downloads,
        "pendingDownloads": pending_downloads[:50],
    }
    # Keep the current task separate from the most recent completed download.
    crawl_progress = load_json(DATA / "crawl-progress.json", None)
    current_progress = None
    if is_crawling:
        if isinstance(crawl_progress, dict) and crawl_progress.get("stage") not in {"failed", "cancelled"}:
            current_progress = {
                "stage": crawl_progress.get("stage", "resolving"),
                "done": int(crawl_progress.get("done", 0)),
                "total": int(crawl_progress.get("total", 0)),
                "mode": "repair" if str(lock_payload.get("task") or "").startswith("repair-") else "crawl",
            }
        elif isinstance(download_progress, dict) and download_progress.get("stage") not in {"complete", "failed", "cancelled"}:
            current_progress = serialize_download_progress(
                download_progress,
                blocked_failures=len(manifest_blocked_keys),
                true_failures=len(manifest_failures),
            )
            current_progress["mode"] = "repair" if str(lock_payload.get("task") or "").startswith("repair-") else "crawl"
        else:
            current_progress = {
                "stage": str(lock_payload.get("phase") or "crawling"),
                "done": 0,
                "total": 0,
                "startedAt": lock_payload.get("startedAt"),
                "mode": "repair" if str(lock_payload.get("task") or "").startswith("repair-") else "crawl",
            }

    last_progress_payload = None
    terminal_progress_stages = {"complete", "failed", "cancelled"}
    if isinstance(download_progress, dict) and download_progress.get("stage") in terminal_progress_stages and int(download_progress.get("total") or 0) > 0:
        last_progress_payload = download_progress
    else:
        archived_progress = load_json(LAST_COMPLETED_PROGRESS, None)
        if isinstance(archived_progress, dict) and archived_progress.get("stage") in terminal_progress_stages:
            last_progress_payload = archived_progress
    last_progress = serialize_download_progress(
        last_progress_payload,
        blocked_failures=len(manifest_blocked_keys),
        true_failures=len(manifest_failures),
    )

    payload["currentProgress"] = current_progress
    payload["lastProgress"] = last_progress
    payload["progress"] = current_progress if is_crawling else last_progress
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    previous_payload = load_json(OUTPUT, None)
    if snapshots_equal(previous_payload, payload):
        return 0
    temporary = OUTPUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, OUTPUT)
    print(f"updated {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
