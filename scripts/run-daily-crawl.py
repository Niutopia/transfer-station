#!/usr/bin/env python3
"""Run the public crawl, optionally download into 中转站, then refresh the monitor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from progress_history import archive_completed_progress
from task_lock import TaskLock, update_lock


PROJECT = Path(__file__).resolve().parents[1]
CRAWLER = PROJECT / "public-video-crawler"
STAGING = PROJECT / "中转站"
DATA = PROJECT / "data"
DAILY_CONFIG = PROJECT / "config" / "daily-sources.json"
RUN_HISTORY = DATA / "run-history.jsonl"
REFRESH_MONITOR = PROJECT / "scripts" / "refresh-monitor.py"


def run_logged(command: list[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", buffering=1) as log_file:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(command, cwd=CRAWLER, text=True, stdout=log_file, stderr=subprocess.STDOUT)
        return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    config = json.loads(DAILY_CONFIG.read_text(encoding="utf-8"))
    configured_sources = config.get("sources") if isinstance(config, dict) else []
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
    crawl_command = [
        sys.executable, "crawler.py", "--pages", str(args.pages), "--delay", str(args.delay),
        "--listing-concurrency", "3", "--resolve-concurrency", "6", "--retries", "4",
        "--resolve-media", "--history", str(DATA / "video-history.json"),
        "--success-history", str(DATA / "download-success.txt"),
        "--new-json", str(DATA / "pending-videos.json"), "--new-csv", str(DATA / "pending-videos.csv"),
        "--json", "videos-with-media.json", "--csv", "videos-with-media.csv",
    ]
    crawl_command += ["--progress", str(DATA / "crawl-progress.json")]
    for source_url in source_urls:
        crawl_command += ["--url", source_url]
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
        if crawl_code == 0 and args.download:
            STAGING.mkdir(parents=True, exist_ok=True)
            pending_path = DATA / "pending-videos.json"
            pending_payload = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
            pending_videos = pending_payload.get("videos") if isinstance(pending_payload, dict) else []
            download_log = DATA / "logs" / f"download-{stamp}.log"
            if isinstance(pending_videos, list) and pending_videos:
                update_lock(lock_file, phase="downloading")
                download_command = [
                    sys.executable, "download.py", str(pending_path), "--output-dir", str(STAGING),
                    "--manifest", str(DATA / "download-manifest.json"),
                    "--work-dir", str(DATA / "partials"),
                    "--success-history", str(DATA / "download-success.txt"),
                    "--media-cache", str(DATA / "video-history.json"),
                    "--delay", str(args.delay),
                    "--concurrency", "4",
                    "--retries", "5",
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
            progress = {}
            try:
                progress = json.loads(progress_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
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
    progress_payload = {}
    if pending_videos:
        try:
            progress_payload = json.loads((DATA / "download-progress.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress_payload = {}
    processed_videos = int(progress_payload.get("done") or 0) if isinstance(progress_payload, dict) else 0
    failed_videos = int(progress_payload.get("failed") or 0) if isinstance(progress_payload, dict) else 0
    event = {
        "timestamp": task_finished_at.isoformat(timespec="seconds"),
        "startedAt": task_started_at.isoformat(timespec="seconds"),
        "durationSeconds": round((task_finished_at - task_started_at).total_seconds(), 1),
        "crawlExitCode": crawl_code,
        "downloadExitCode": download_code,
        "downloadRequested": args.download,
        "pages": args.pages,
        "sources": len(source_urls),
        "rawLinks": int(pending_metadata.get("raw_detail_links") or 0),
        "uniqueVideos": int(pending_metadata.get("unique_videos") or 0),
        "skippedVideos": int(pending_metadata.get("known_videos_skipped") or 0),
        "newVideos": int(pending_metadata.get("new_videos") or 0) if args.download else None,
        "retryVideos": int(pending_metadata.get("retry_videos") or 0) if args.download else None,
        "downloadedVideos": max(0, processed_videos - failed_videos),
        "failedVideos": failed_videos,
        "downloadedBytes": int(progress_payload.get("bytesDone") or 0) if isinstance(progress_payload, dict) else 0,
    }
    with RUN_HISTORY.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    subprocess.run([sys.executable, str(REFRESH_MONITOR)], cwd=PROJECT, check=False)
    if crawl_code != 0:
        return crawl_code
    return download_code or 0


if __name__ == "__main__":
    raise SystemExit(main())
