#!/usr/bin/env python3
"""Run the public crawl, optionally download into 中转站, then refresh the monitor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from history_backup import create_backup
from progress_history import archive_completed_progress
from source_config import load_source_config
from task_lock import TaskLock, update_lock


PROJECT = Path(__file__).resolve().parents[1]
CRAWLER = PROJECT / "public-video-crawler"
STAGING = PROJECT / "中转站"
DATA = PROJECT / "data"
DAILY_CONFIG = PROJECT / "config" / "daily-sources.json"
RUN_HISTORY = DATA / "run-history.jsonl"
REFRESH_MONITOR = PROJECT / "scripts" / "refresh-monitor.py"
SAFE_RESOLVE_FAILURE_KINDS = {"media_mismatch", "media_unavailable"}
AUTH_DETECTOR_VERSION = 3


def run_logged(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(command, cwd=CRAWLER, text=True, stdout=log_file, stderr=subprocess.STDOUT)
        return process.wait()


def load_json(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return default


def is_safe_resolve_failure(item: object) -> bool:
    return bool(
        isinstance(item, dict)
        and (
            item.get("kind") in SAFE_RESOLVE_FAILURE_KINDS
            or "详情页媒体与榜单不一致" in str(item.get("error") or "")
        )
    )


def event_manifest_rows(payload: object, *, download_requested: bool, pending_videos: list[object]) -> list[object]:
    if not download_requested or not pending_videos or not isinstance(payload, list):
        return []
    return payload


def build_crawl_command(pages: int, delay: float, source_urls: list[str]) -> list[str]:
    command = [
        sys.executable, "crawler.py", "--pages", str(pages), "--delay", str(delay),
        "--listing-concurrency", "2", "--resolve-concurrency", "1", "--media-rechecks", "1",
        "--retries", "2", "--new-only",
        "--resolve-media", "--history", str(DATA / "video-history.json"),
        "--success-history", str(DATA / "download-success.txt"),
        "--blocked-history", str(DATA / "blocked-media.json"),
        "--ignored-history", str(DATA / "ignored-media.json"),
        "--new-json", str(DATA / "pending-videos.json"), "--new-csv", str(DATA / "pending-videos.csv"),
        "--json", "videos-with-media.json", "--csv", "videos-with-media.csv",
        "--progress", str(DATA / "crawl-progress.json"),
    ]
    for source_url in source_urls:
        command += ["--url", source_url]
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int)
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--trigger", choices=("manual", "cli"), default="cli")
    args = parser.parse_args()
    config = load_source_config(DAILY_CONFIG)
    configured_sources = config["sources"]
    pages = args.pages if args.pages is not None else int(config["pagesPerSource"])
    if not 1 <= pages <= 20:
        parser.error("--pages 必须在 1 到 20 之间")
    source_urls = [
        str(item.get("url"))
        for item in configured_sources
        if isinstance(item, dict) and item.get("url")
    ]
    if not source_urls:
        raise SystemExit("daily source configuration is empty")
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    log_path = DATA / "logs" / f"crawl-{stamp}.log"
    crawl_command = build_crawl_command(pages, args.delay, source_urls)
    if args.download:
        crawl_command += ["--existing-dir", str(STAGING)]

    lock_file = DATA / ".crawling.lock"
    task_lock = TaskLock(lock_file, "daily-download" if args.download else "daily-crawl")
    if not task_lock.acquire():
        print("another daily task is already running", file=sys.stderr)
        return 75

    crawl_code = 1
    download_code = None
    pending_payload = {}
    task_started_at = datetime.now().astimezone()
    try:
        archive_completed_progress(
            DATA / "download-progress.json",
            DATA / "last-completed-progress.json",
        )
        update_lock(lock_file, phase="crawling")
        crawl_code = run_logged(crawl_command, log_path)
        if crawl_code == 0:
            pending_path = DATA / "pending-videos.json"
            pending_payload = load_json(pending_path, None)
            if (
                not isinstance(pending_payload, dict)
                or not isinstance(pending_payload.get("metadata"), dict)
                or not isinstance(pending_payload.get("videos"), list)
            ):
                print("crawler completed without a valid pending-videos.json", file=sys.stderr)
                pending_payload = {}
                crawl_code = 1
            pending_videos = pending_payload.get("videos") if isinstance(pending_payload, dict) else []
        if crawl_code == 0 and args.download:
            STAGING.mkdir(parents=True, exist_ok=True)
            download_log = DATA / "logs" / f"download-{stamp}.log"
            if isinstance(pending_videos, list) and pending_videos:
                update_lock(lock_file, phase="downloading")
                download_command = [
                    sys.executable, "download.py", str(pending_path), "--output-dir", str(STAGING),
                    "--manifest", str(DATA / "download-manifest.json"),
                    "--work-dir", str(DATA / "partials"),
                    "--success-history", str(DATA / "download-success.txt"),
                    "--content-history", str(DATA / "download-content-history.json"),
                    "--media-cache", str(DATA / "video-history.json"),
                    "--blocked-history", str(DATA / "blocked-media.json"),
                    "--delay", str(args.delay),
                    "--concurrency", "4",
                    "--retries", "5",
                    "--refresh-retries", "3",
                    "--link-max-age", "180",
                    "--progress", str(DATA / "download-progress.json"),
                ]
                if args.limit:
                    download_command += ["--limit", str(args.limit)]
                download_code = run_logged(download_command, download_log)
            else:
                update_lock(lock_file, phase="finalizing")
                download_log.write_text("no new or retry videos\n", encoding="utf-8")
                os.chmod(download_log, 0o600)
                download_code = 0
    finally:
        if crawl_code != 0:
            progress_path = DATA / "crawl-progress.json"
            progress = load_json(progress_path, {})
            if not isinstance(progress, dict):
                progress = {}
            progress.update({"stage": "failed", "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds")})
            temporary_progress = progress_path.with_suffix(progress_path.suffix + ".tmp")
            temporary_progress.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
            os.chmod(temporary_progress, 0o600)
            os.replace(temporary_progress, progress_path)
        task_lock.release()

    task_finished_at = datetime.now().astimezone()
    pending_metadata = pending_payload.get("metadata", {}) if isinstance(pending_payload, dict) else {}
    pending_videos = pending_payload.get("videos", []) if isinstance(pending_payload, dict) else []
    if not isinstance(pending_metadata, dict):
        pending_metadata = {}
    if not isinstance(pending_videos, list):
        pending_videos = []
    crawl_progress_payload = load_json(DATA / "crawl-progress.json", {})
    if not isinstance(crawl_progress_payload, dict):
        crawl_progress_payload = {}
    manifest_payload = load_json(DATA / "download-manifest.json", [])
    manifest_results = event_manifest_rows(
        manifest_payload,
        download_requested=args.download,
        pending_videos=pending_videos,
    )
    downloaded_videos = sum(1 for item in manifest_results if isinstance(item, dict) and item.get("status") == "downloaded")
    duplicate_videos = sum(1 for item in manifest_results if isinstance(item, dict) and item.get("status") == "duplicate")
    failed_videos = sum(1 for item in manifest_results if isinstance(item, dict) and item.get("status") == "failed")
    # download.py reports "skipped" when the target file is already in place.
    # Those items are handled, so they need their own bucket; leaving them out of
    # every bucket made a fully successful re-run look like it ingested nothing.
    already_present_videos = sum(1 for item in manifest_results if isinstance(item, dict) and item.get("status") == "skipped")
    auto_retry_attempts = sum(
        int(item.get("autoRetries") or 0)
        for item in manifest_results
        if isinstance(item, dict)
    )
    auto_retried_videos = sum(
        1 for item in manifest_results
        if isinstance(item, dict) and int(item.get("autoRetries") or 0) > 0
    )
    auto_recovered_videos = sum(
        1 for item in manifest_results
        if isinstance(item, dict)
        and int(item.get("autoRetries") or 0) > 0
        and item.get("status") in {"downloaded", "skipped", "duplicate"}
    )
    resolve_failures = pending_metadata.get("resolve_failures") if isinstance(pending_metadata, dict) else []
    if not isinstance(resolve_failures, list):
        resolve_failures = []
    blocked_keys = {
        str(item.get("viewkey") or "")
        for item in resolve_failures
        if is_safe_resolve_failure(item)
    }
    blocked_keys.update(
        str(item.get("viewkey") or "")
        for item in manifest_results
        if isinstance(item, dict) and item.get("status") == "blocked"
    )
    blocked_keys.discard("")
    blocked_videos = len(blocked_keys)
    resolver_failed_videos = sum(1 for item in resolve_failures if not is_safe_resolve_failure(item))
    failed_videos += resolver_failed_videos
    listing_failures = pending_metadata.get("listing_failures") if isinstance(pending_metadata, dict) else []
    listing_failure_count = (
        len(listing_failures)
        if isinstance(listing_failures, list)
        else 0
    )
    if crawl_code != 0:
        listing_failure_count = int(crawl_progress_payload.get("listingFailures") or listing_failure_count)
    downloaded_bytes = sum(int(item.get("bytes") or 0) for item in manifest_results if isinstance(item, dict) and item.get("status") == "downloaded")
    # A resolver error does not undo the run: the listing crawl still produced a
    # usable snapshot and the download stage still ran, so it belongs in
    # "attention" rather than turning the whole run into a failure.
    task_succeeded = crawl_code == 0 and download_code in {0, None}
    auth_challenges = sum(
        1 for item in resolve_failures
        if isinstance(item, dict) and item.get("kind") == "auth_challenge"
    )
    # A partly challenged listing never reaches the crawler's own authFailure
    # flag, because that flag is only written when *every* listing page fails.
    if isinstance(listing_failures, list):
        auth_challenges += sum(
            1 for item in listing_failures
            if isinstance(item, dict) and item.get("kind") == "auth_challenge"
        )
    # One challenged page already means the stored clearance is not working.
    # Requiring *every* failure to be a challenge hid the common mixed case
    # (some pages challenged, one page timing out) that this signal exists for.
    auth_failure = bool(crawl_progress_payload.get("authFailure")) or auth_challenges > 0
    event = {
        "timestamp": task_finished_at.isoformat(timespec="seconds"),
        "startedAt": task_started_at.isoformat(timespec="seconds"),
        "durationSeconds": round((task_finished_at - task_started_at).total_seconds(), 1),
        "taskType": "crawl",
        "trigger": args.trigger,
        "authFailure": auth_failure,
        "authChallenges": auth_challenges,
        "authDetectorVersion": AUTH_DETECTOR_VERSION,
        "resultStatus": (
            "failed" if not task_succeeded
            else "attention" if (listing_failure_count or resolver_failed_videos or auth_failure)
            else "success"
        ),
        "crawlExitCode": crawl_code,
        "downloadExitCode": download_code,
        "downloadRequested": args.download,
        "pages": pages,
        "sources": len(source_urls),
        "listingFailures": listing_failure_count,
        "rawLinks": int(crawl_progress_payload.get("rawLinks") or 0) if crawl_code != 0 else int(pending_metadata.get("raw_detail_links") or 0),
        "uniqueVideos": int(crawl_progress_payload.get("uniqueVideos") or 0) if crawl_code != 0 else int(pending_metadata.get("unique_videos") or 0),
        "skippedVideos": int(crawl_progress_payload.get("skippedVideos") or 0) if crawl_code != 0 else int(pending_metadata.get("known_videos_skipped") or 0),
        "newVideos": int(pending_metadata.get("new_videos") or 0),
        "retryVideos": int(pending_metadata.get("retry_videos") or 0),
        "downloadedVideos": downloaded_videos,
        "alreadyPresentVideos": already_present_videos,
        "detailPagesRequested": int(pending_metadata.get("detail_pages_requested") or 0),
        "duplicateVideos": duplicate_videos,
        "blockedVideos": blocked_videos,
        "ignoredVideos": int(pending_metadata.get("ignored_videos") or 0),
        "failedVideos": failed_videos,
        "downloadedBytes": downloaded_bytes,
        "autoRetryAttempts": auto_retry_attempts,
        "autoRetriedVideos": auto_retried_videos,
        "autoRecoveredVideos": auto_recovered_videos,
    }
    with RUN_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        create_backup(PROJECT, PROJECT / "history-backups")
    except (OSError, ValueError):
        pass

    subprocess.run([sys.executable, str(REFRESH_MONITOR)], cwd=PROJECT, check=False)
    if crawl_code != 0:
        return crawl_code
    return download_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
