from __future__ import annotations

import errno
import csv
import datetime
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

import crawler
import download

sys.path.append(str(Path(__file__).resolve().parents[2] / "scripts"))
import task_lock
import task_history
import history_backup
import progress_history
import source_config
import download_history
import repair_pending

MONITOR_SPEC = importlib.util.spec_from_file_location(
    "refresh_monitor_test_module",
    Path(__file__).resolve().parents[2] / "scripts" / "refresh-monitor.py",
)
assert MONITOR_SPEC and MONITOR_SPEC.loader
refresh_monitor = importlib.util.module_from_spec(MONITOR_SPEC)
MONITOR_SPEC.loader.exec_module(refresh_monitor)
RUN_DAILY_SPEC = importlib.util.spec_from_file_location(
    "run_daily_crawl_test_module",
    Path(__file__).resolve().parents[2] / "scripts" / "run-daily-crawl.py",
)
assert RUN_DAILY_SPEC and RUN_DAILY_SPEC.loader
run_daily_crawl = importlib.util.module_from_spec(RUN_DAILY_SPEC)
RUN_DAILY_SPEC.loader.exec_module(run_daily_crawl)


FIXTURE = """
<div class="col-lg-8">
  <a href="https://91porn.com/view_video.php?viewkey=abc12345&page=1&c=allzvq">
    <img src="https://cdn.example/fake.jpg">
    <span class="video-title">decoy title</span>
  </a>
</div>
<div class="col-lg-3">
  <a href="https://91porn.com/view_video.php?viewkey=abc12345&page=1&c=llzvq">
    <img src="https://cdn.example/real.jpg">
    <span class="duration">00:01:02</span>
    <span class="video-title"> real   title </span>
  </a>
</div>
<a href="/view_video.php?viewkey=xyz98765&c=llzvq">
  <span class="video-title">second</span>
</a>
"""


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None, url: str = "https://media.example/video.mp4") -> None:
        self.stream = io.BytesIO(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size)

    def geturl(self) -> str:
        return self.url

    def getcode(self) -> int:
        return self.status


class CrawlerTests(unittest.TestCase):
    def test_download_history_survives_file_and_log_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            first_log = logs / "download-20260720-120000.log"
            first_log.write_text("\n".join([
                json.dumps({"viewkey": "first", "status": "downloaded", "bytes": 100}),
                json.dumps({"viewkey": "failed", "status": "failed", "bytes": 200}),
                "not-json",
            ]), encoding="utf-8")
            second_log = logs / "download-20260721-130000.log"
            second_log.write_text("\n".join([
                json.dumps({"viewkey": "first", "status": "downloaded", "bytes": 999}),
                json.dumps({"viewkey": "duplicate", "status": "duplicate", "bytes": 100}),
                json.dumps({"viewkey": "second", "status": "downloaded", "bytes": 300}),
            ]), encoding="utf-8")

            index = root / "download-history.json"
            items = download_history.sync_download_history(index, logs)
            by_key = {str(item["viewkey"]): item for item in items}
            self.assertEqual(set(by_key), {"first", "second"})
            self.assertEqual(by_key["first"]["date"], "2026-07-20")
            self.assertEqual(by_key["first"]["bytes"], 100)
            self.assertEqual(by_key["second"]["date"], "2026-07-21")

            first_log.unlink()
            second_log.unlink()
            persisted = download_history.sync_download_history(index, logs)
            self.assertEqual({item["viewkey"] for item in persisted}, {"first", "second"})

    def test_download_history_includes_repair_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            (logs / "repair-download-20260721-140000.log").write_text(
                json.dumps({"viewkey": "repaired", "status": "downloaded", "bytes": 321}) + "\n",
                encoding="utf-8",
            )

            items = download_history.sync_download_history(root / "download-history.json", logs)

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["viewkey"], "repaired")
            self.assertEqual(items[0]["date"], "2026-07-21")
            self.assertEqual(items[0]["bytes"], 321)
            self.assertTrue(str(items[0]["downloadedAt"]).startswith("2026-07-21T14:00:00"))

    def test_source_config_adds_and_removes_validated_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily-sources.json"
            original = {
                "pagesPerSource": 2,
                "sources": [{"name": "hot", "url": "https://91porn.com/v.php?category=hot&viewtype=basic"}],
            }
            path.write_text(json.dumps(original), encoding="utf-8")

            sources = source_config.add_source(
                path,
                "top",
                "https://91porn.com/v.php?viewtype=basic&category=top",
            )
            self.assertEqual(len(sources), 2)
            self.assertEqual(sources[-1]["name"], "top")
            self.assertIn("category=top", sources[-1]["url"])

            remaining = source_config.remove_source(path, sources[-1]["url"])
            self.assertEqual(remaining, original["sources"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pagesPerSource"], 2)

            empty = source_config.remove_source(path, original["sources"][0]["url"])
            self.assertEqual(empty, [])

    def test_source_config_accepts_homepage_and_rejects_invalid_or_duplicate_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily-sources.json"
            path.write_text(json.dumps({
                "pagesPerSource": 2,
                "sources": [{"name": "hot", "url": "https://91porn.com/v.php?category=hot&viewtype=basic"}],
            }), encoding="utf-8")

            with self.assertRaises(source_config.SourceConfigError):
                source_config.add_source(path, "bad", "https://example.com/v.php?category=hot")
            with self.assertRaises(source_config.SourceConfigError):
                source_config.add_source(path, "duplicate", "https://91porn.com/v.php?viewtype=basic&category=hot")
            sources = source_config.add_source(path, "", "https://91porn.com/index.php")
            self.assertEqual(sources[-1], {"name": "首页", "url": "https://91porn.com/index.php"})
            sources = source_config.add_source(path, "", "https://91porn.com/v.php?next=watch")
            self.assertEqual(sources[-1], {"name": "watch", "url": "https://91porn.com/v.php?next=watch"})

    def test_source_config_rejects_out_of_range_page_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily-sources.json"
            path.write_text(json.dumps({"pagesPerSource": 21, "sources": []}), encoding="utf-8")
            with self.assertRaises(source_config.SourceConfigError):
                source_config.load_source_config(path)

    def test_source_config_rejects_invalid_or_duplicate_persisted_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "daily-sources.json"
            path.write_text(json.dumps({
                "pagesPerSource": 2,
                "sources": [{"name": "bad", "url": "https://example.com/v.php"}],
            }), encoding="utf-8")
            with self.assertRaises(source_config.SourceConfigError):
                source_config.load_source_config(path)

            path.write_text(json.dumps({
                "pagesPerSource": 2,
                "sources": [
                    {"name": "hot", "url": "https://91porn.com/v.php?category=hot&viewtype=basic"},
                    {"name": "same", "url": "https://91porn.com/v.php?viewtype=basic&category=hot"},
                ],
            }), encoding="utf-8")
            with self.assertRaises(source_config.SourceConfigError):
                source_config.load_source_config(path)

    def test_completed_progress_is_archived_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download-progress.json"
            destination = root / "last-completed-progress.json"
            source.write_text(json.dumps({"stage": "complete", "done": 3, "total": 3}), encoding="utf-8")
            self.assertTrue(progress_history.archive_completed_progress(source, destination))
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["done"], 3)

            source.write_text(json.dumps({"stage": "downloading", "done": 1, "total": 3}), encoding="utf-8")
            self.assertFalse(progress_history.archive_completed_progress(source, destination))
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["done"], 3)

    def test_latest_run_event_ignores_malformed_trailing_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text('{"timestamp":"2026-07-20T19:00:00+08:00","newVideos":2}\nnot-json\n', encoding="utf-8")
            event = task_history.latest_run_event(history)
            self.assertIsNotNone(event)
            self.assertEqual(event["newVideos"], 2)

    def test_task_history_separates_crawl_and_repair_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text("\n".join([
                json.dumps({"timestamp": "2026-07-20T09:00:00+08:00", "downloadRequested": True}),
                json.dumps({"timestamp": "2026-07-20T10:00:00+08:00", "taskType": "repair"}),
            ]), encoding="utf-8")

            crawl_event = task_history.latest_task_event(history, "crawl")
            repair_event = task_history.latest_task_event(history, "repair")

            self.assertEqual(task_history.event_task_type(crawl_event), "crawl")
            self.assertEqual(crawl_event["timestamp"], "2026-07-20T09:00:00+08:00")
            self.assertEqual(repair_event["timestamp"], "2026-07-20T10:00:00+08:00")

    def test_task_history_uses_only_dashboard_manual_crawls_for_cookie_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text("\n".join([
                json.dumps({"timestamp": "2026-07-20T09:00:00+08:00", "taskType": "crawl", "trigger": "manual", "crawlExitCode": 0}),
                json.dumps({"timestamp": "2026-07-20T10:00:00+08:00", "taskType": "crawl", "trigger": "cli", "crawlExitCode": 1}),
                json.dumps({"timestamp": "2026-07-20T11:00:00+08:00", "taskType": "repair", "trigger": "manual"}),
                json.dumps({"timestamp": "2026-07-20T12:00:00+08:00", "taskType": "crawl", "trigger": "manual", "crawlExitCode": 1, "resultStatus": "failed", "authFailure": False}),
            ]), encoding="utf-8")
            event = task_history.latest_manual_crawl_event(history)
            self.assertIsNotNone(event)
            self.assertEqual(event["timestamp"], "2026-07-20T09:00:00+08:00")

    def test_cookie_status_ignores_auth_failures_from_legacy_detector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text("\n".join([
                json.dumps({"timestamp": "2026-07-20T09:00:00+08:00", "taskType": "crawl", "trigger": "manual", "crawlExitCode": 0}),
                json.dumps({"timestamp": "2026-07-20T10:00:00+08:00", "taskType": "crawl", "trigger": "manual", "crawlExitCode": 1, "resultStatus": "failed", "authFailure": True}),
            ]), encoding="utf-8")
            event = task_history.latest_manual_crawl_event(history)
            self.assertIsNotNone(event)
            self.assertEqual(event["timestamp"], "2026-07-20T09:00:00+08:00")

    def test_cookie_status_accepts_auth_failure_from_current_detector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text(json.dumps({
                "timestamp": "2026-07-20T10:00:00+08:00",
                "taskType": "crawl",
                "trigger": "manual",
                "crawlExitCode": 1,
                "resultStatus": "failed",
                "authFailure": True,
                "authDetectorVersion": 2,
            }), encoding="utf-8")
            event = task_history.latest_manual_crawl_event(history)
            self.assertIsNotNone(event)
            self.assertEqual(event["timestamp"], "2026-07-20T10:00:00+08:00")

    def test_partial_listing_failure_is_an_attention_result(self) -> None:
        event = {
            "crawlExitCode": 0,
            "downloadExitCode": 0,
            "listingFailures": 1,
        }
        self.assertEqual(task_history.event_result_status(event), "attention")

    def test_daily_result_ignores_old_manifest_when_download_was_not_requested(self) -> None:
        old_manifest = [{"viewkey": "old", "status": "downloaded"}]
        self.assertEqual(
            run_daily_crawl.event_manifest_rows(
                old_manifest,
                download_requested=False,
                pending_videos=[{"viewkey": "new"}],
            ),
            [],
        )
        self.assertEqual(
            run_daily_crawl.event_manifest_rows(
                old_manifest,
                download_requested=True,
                pending_videos=[{"viewkey": "new"}],
            ),
            old_manifest,
        )

    def test_daily_command_uses_low_pressure_detail_options(self) -> None:
        command = run_daily_crawl.build_crawl_command(
            2,
            2.0,
            ["https://91porn.com/v.php?category=hot&viewtype=basic"],
        )

        def option(flag: str) -> str:
            return command[command.index(flag) + 1]

        self.assertEqual(option("--delay"), "2.0")
        self.assertEqual(option("--listing-concurrency"), "2")
        self.assertEqual(option("--resolve-concurrency"), "1")
        self.assertEqual(option("--media-rechecks"), "1")
        self.assertEqual(option("--retries"), "2")
        self.assertIn("--new-only", command)
        self.assertEqual(command.count("--url"), 1)

    def test_daily_result_separates_safe_blocks_from_true_resolver_failures(self) -> None:
        self.assertTrue(run_daily_crawl.is_safe_resolve_failure({"kind": "media_mismatch"}))
        self.assertTrue(run_daily_crawl.is_safe_resolve_failure({"kind": "media_unavailable"}))
        self.assertFalse(run_daily_crawl.is_safe_resolve_failure({"kind": "network_error"}))

    def test_event_counter_preserves_explicit_zero(self) -> None:
        event = {"rawLinks": 0, "uniqueVideos": "0", "invalid": "nope"}
        self.assertEqual(task_history.event_counter(event, "rawLinks", 746), 0)
        self.assertEqual(task_history.event_counter(event, "uniqueVideos", 310), 0)
        self.assertEqual(task_history.event_counter(event, "missing", 310), 310)
        self.assertEqual(task_history.event_counter(event, "invalid", 310), 0)

    def test_listing_failure_progress_is_private_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl-progress.json"
            crawler.write_progress_state(path, {
                "stage": "failed",
                "listingFailures": 18,
                "rawLinks": 0,
                "uniqueVideos": 0,
            })
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["listingFailures"], 18)
            self.assertEqual(payload["rawLinks"], 0)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_crawl_result_does_not_reuse_success_snapshot_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            crawler_dir = root / "crawler"
            data = root / "data"
            config = root / "config"
            for path in (staging, crawler_dir, data / "logs", data / "partials", config):
                path.mkdir(parents=True, exist_ok=True)
            crawl_json = crawler_dir / "videos-with-media.json"
            crawl_json.write_text(json.dumps({
                "metadata": {
                    "raw_detail_links": 4,
                    "unique_videos": 2,
                    "duplicates_removed": 2,
                    "known_videos_skipped": 2,
                    "listing_failures": [],
                    "resolve_failures": [],
                },
                "videos": [
                    {"viewkey": "old-one", "title": "Old one", "media_url": "https://media.example/1.mp4"},
                    {"viewkey": "old-two", "title": "Old two", "media_url": "https://media.example/2.mp4"},
                ],
            }), encoding="utf-8")
            run_history = data / "run-history.jsonl"
            run_history.write_text(json.dumps({
                "timestamp": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                "taskType": "crawl",
                "resultStatus": "failed",
                "crawlExitCode": 1,
                "downloadExitCode": None,
                "listingFailures": 18,
                "rawLinks": 0,
                "uniqueVideos": 0,
                "skippedVideos": 0,
            }) + "\n", encoding="utf-8")
            daily_config = config / "daily-sources.json"
            daily_config.write_text(json.dumps({"pagesPerSource": 2, "sources": []}), encoding="utf-8")

            overrides = {
                "PROJECT": root,
                "STAGING": staging,
                "CRAWLER": crawler_dir,
                "CRAWL_JSON": crawl_json,
                "DAILY_CONFIG": daily_config,
                "DATA": data,
                "DOWNLOAD_MANIFEST": data / "download-manifest.json",
                "IGNORED_MEDIA": data / "ignored-media.json",
                "PARTIAL_DIR": data / "partials",
                "OUTPUT": root / "status.json",
                "RUN_HISTORY": run_history,
                "LAST_COMPLETED_PROGRESS": data / "last-completed-progress.json",
                "DOWNLOAD_HISTORY": data / "download-history.json",
            }
            with mock.patch.multiple(refresh_monitor, **overrides):
                self.assertEqual(refresh_monitor.main(), 0)
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["overview"]["rawLinks"], 4)
            self.assertEqual(status["overview"]["uniqueVideos"], 2)
            self.assertEqual(status["latestRun"]["result"]["rawLinks"], 0)
            self.assertEqual(status["latestRun"]["result"]["uniqueVideos"], 0)
            self.assertEqual(status["latestRun"]["result"]["listingFailures"], 18)
            self.assertEqual(status["latestRun"]["stages"][0]["status"], "attention")
            self.assertTrue(any(alert["title"] == "最近一次任务执行失败" for alert in status["alerts"]))

    def test_staging_scan_reflects_in_place_file_size_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory) / "staging"
            staging.mkdir()
            video = staging / "same-name.mp4"
            video.write_bytes(b"old")
            with mock.patch.object(refresh_monitor, "STAGING", staging):
                first = refresh_monitor.scan_files()
                video.write_bytes(b"new-content")
                second = refresh_monitor.scan_files()
            self.assertEqual(first[0]["sizeBytes"], 3)
            self.assertEqual(second[0]["sizeBytes"], 11)

    def test_monitor_excludes_explicitly_ignored_media_from_pending_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = root / "staging"
            crawler_dir = root / "crawler"
            data = root / "data"
            config = root / "config"
            for path in (staging, crawler_dir, data / "logs", data / "partials", config):
                path.mkdir(parents=True, exist_ok=True)
            crawl_json = crawler_dir / "videos-with-media.json"
            crawl_json.write_text(json.dumps({
                "metadata": {
                    "raw_detail_links": 2,
                    "unique_videos": 2,
                    "duplicates_removed": 0,
                    "known_videos_skipped": 2,
                    "listing_failures": [],
                    "resolve_failures": [],
                },
                "videos": [
                    {"viewkey": "ignored12", "title": "Ignored", "media_url": ""},
                    {"viewkey": "ready123", "title": "Ready", "media_url": "https://media.example/ready.mp4"},
                ],
            }), encoding="utf-8")
            (data / "download-success.txt").write_text("ready123\n", encoding="utf-8")
            ignored_path = data / "ignored-media.json"
            ignored_path.write_text(json.dumps({"version": 1, "items": {"ignored12": {"reason": "user-approved"}}}), encoding="utf-8")
            run_history = data / "run-history.jsonl"
            timestamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            run_history.write_text(json.dumps({
                "timestamp": timestamp,
                "startedAt": timestamp,
                "taskType": "crawl",
                "resultStatus": "success",
                "crawlExitCode": 0,
                "downloadExitCode": 0,
                "downloadRequested": True,
                "rawLinks": 2,
                "uniqueVideos": 2,
                "skippedVideos": 2,
                "newVideos": 0,
                "retryVideos": 0,
                "failedVideos": 0,
                "listingFailures": 0,
            }) + "\n", encoding="utf-8")
            daily_config = config / "daily-sources.json"
            daily_config.write_text(json.dumps({"pagesPerSource": 2, "sources": []}), encoding="utf-8")
            overrides = {
                "PROJECT": root,
                "STAGING": staging,
                "CRAWLER": crawler_dir,
                "CRAWL_JSON": crawl_json,
                "DAILY_CONFIG": daily_config,
                "DATA": data,
                "DOWNLOAD_MANIFEST": data / "download-manifest.json",
                "IGNORED_MEDIA": ignored_path,
                "PARTIAL_DIR": data / "partials",
                "OUTPUT": root / "status.json",
                "RUN_HISTORY": run_history,
                "LAST_COMPLETED_PROGRESS": data / "last-completed-progress.json",
                "DOWNLOAD_HISTORY": data / "download-history.json",
            }
            with mock.patch.multiple(refresh_monitor, **overrides):
                self.assertEqual(refresh_monitor.main(), 0)
            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["overview"]["ignoredVideos"], 1)
            self.assertEqual(status["overview"]["pendingVideos"], 0)
            self.assertEqual(status["overview"]["repairableVideos"], 0)
            self.assertEqual(status["latestRun"]["status"], "ready")
            self.assertTrue(any(alert["title"] == "已忽略 1 个无媒体条目" for alert in status["alerts"]))

    def test_history_backup_round_trip_and_avoids_unchanged_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup_dir = root / "history-backups"
            (root / "config").mkdir()
            (root / "data").mkdir()
            source = root / "config" / "daily-sources.json"
            history = root / "data" / "download-success.txt"
            source.write_text('{"sources":[]}\n', encoding="utf-8")
            history.write_text("first\n", encoding="utf-8")

            first = history_backup.create_backup(root, backup_dir)
            self.assertIsNotNone(first)
            first_archives = sorted(
                path for path in backup_dir.glob("history-*.zip")
                if path.name != "history-latest.zip"
            )
            history_backup.create_backup(root, backup_dir)
            self.assertEqual(
                sorted(
                    path for path in backup_dir.glob("history-*.zip")
                    if path.name != "history-latest.zip"
                ),
                first_archives,
            )

            history.write_text("first\nsecond\n", encoding="utf-8")
            history_backup.create_backup(root, backup_dir)
            self.assertEqual(
                len([
                    path for path in backup_dir.glob("history-*.zip")
                    if path.name != "history-latest.zip"
                ]),
                2,
            )
            history.write_text("damaged\n", encoding="utf-8")
            restored = history_backup.restore_backup(root, backup_dir / "history-latest.zip")
            self.assertIn(history, restored)
            self.assertEqual(history.read_text(encoding="utf-8"), "first\nsecond\n")

    def test_parser_deduplicates_and_prefers_visible_card(self) -> None:
        videos = crawler.parse_listing(FIXTURE, crawler.DEFAULT_URL, 1)
        by_key = {video.viewkey: video for video in videos}
        self.assertEqual(len(by_key), 2)
        self.assertEqual(by_key["abc12345"].title, "real title")
        self.assertEqual(by_key["abc12345"].duration, "00:01:02")
        self.assertEqual(by_key["abc12345"].thumbnail_url, "https://cdn.example/real.jpg")
        self.assertEqual(
            by_key["abc12345"].canonical_url,
            "https://91porn.com/view_video.php?viewkey=abc12345",
        )

    def test_parser_decodes_double_escaped_title_entities(self) -> None:
        body = """
        <a href="/view_video.php?viewkey=abc12345&amp;c=llzvq">
          <span class="video-title">A girl&amp;#39;s &amp;quot;day date&amp;quot;</span>
        </a>
        """
        videos = crawler.parse_listing(body, crawler.DEFAULT_URL, 1)
        self.assertEqual(videos[0].title, 'A girl\'s "day date"')

    def test_merge_deduplicates_across_pages(self) -> None:
        first = crawler.parse_listing(FIXTURE, crawler.DEFAULT_URL, 1)
        second = crawler.parse_listing(FIXTURE, crawler.DEFAULT_URL, 2)
        merged = crawler.merge_videos([first, second])
        self.assertEqual(len(merged), 2)
        self.assertEqual({tuple(video.source_pages) for video in merged}, {(1, 2)})

    def test_csv_export_keeps_its_columns_when_video_gains_fields(self) -> None:
        # The snapshot CSV is a stable export: internal Video fields must never
        # leak into it, and a new field must not abort the whole crawl.
        video = crawler.Video(
            viewkey="abc12345",
            canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
            thumbnail_url="https://cdn.example/1235752.jpg",
            source_pages=[1, 2],
            asset_id="1235752",
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "pending-videos.csv"
            crawler.write_csv(path, [video])
            rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["viewkey"], "abc12345")
        self.assertEqual(rows[0]["source_pages"], "1,2")
        self.assertNotIn("asset_id", rows[0])

    def test_parser_prefers_the_card_that_agrees_with_its_own_player_id(self) -> None:
        # The listing repeats a viewkey across several cards and some of them
        # render a neighbouring video's thumbnail.  Keeping such a card makes the
        # later player identity check reject a perfectly good detail page.
        body = """
        <a href="/view_video.php?viewkey=abc12345&amp;c=rpxgj">
          <div class="thumb-overlay" id="playvthumb_1235908"></div>
          <img src="/thumb/1235740.jpg" class="img-responsive">
          <span class="duration">00:01:02</span>
          <span class="video-title">mismatched card</span>
        </a>
        <a href="/view_video.php?viewkey=abc12345&amp;c=rpxgj">
          <div class="thumb-overlay" id="playvthumb_1235752"></div>
          <img src="/thumb/1235752.jpg" class="img-responsive">
          <span class="duration">00:02:03</span>
          <span class="video-title">self consistent card</span>
        </a>
        """
        videos = crawler.parse_listing(body, crawler.DEFAULT_URL, 1)
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0].asset_id, "1235752")
        self.assertEqual(videos[0].thumbnail_url, "https://91porn.com/thumb/1235752.jpg")
        self.assertTrue(videos[0].consistent_asset())

    def test_merge_prefers_the_self_consistent_card_across_pages(self) -> None:
        mismatched = """
        <a href="/view_video.php?viewkey=abc12345&amp;c=rpxgj">
          <div class="thumb-overlay" id="playvthumb_1235908"></div>
          <img src="/thumb/1235740.jpg">
          <span class="video-title">mismatched card</span>
        </a>
        """
        consistent = """
        <a href="/view_video.php?viewkey=abc12345&amp;c=rpxgj">
          <div class="thumb-overlay" id="playvthumb_1235752"></div>
          <img src="/thumb/1235752.jpg">
          <span class="duration">00:02:03</span>
          <span class="video-title">self consistent card</span>
        </a>
        """
        merged = crawler.merge_videos([
            crawler.parse_listing(mismatched, crawler.DEFAULT_URL, 1),
            crawler.parse_listing(consistent, crawler.DEFAULT_URL, 2),
        ])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].asset_id, "1235752")
        self.assertEqual(merged[0].thumbnail_url, "https://91porn.com/thumb/1235752.jpg")
        self.assertEqual(merged[0].duration, "00:02:03")
        self.assertEqual(merged[0].source_pages, [1, 2])

    def test_cross_run_history_skips_existing_and_keeps_only_new(self) -> None:
        history = {
            "abc12345": crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                media_url="https://media.example.test/known.mp4",
            )
        }
        current = [
            crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345"),
            crawler.Video(viewkey="xyz98765", canonical_url="https://91porn.com/view_video.php?viewkey=xyz98765"),
        ]
        processing, new_count, retry_count, skipped_count = crawler.select_for_processing(
            current, history, {"abc12345"}
        )
        self.assertEqual([video.viewkey for video in processing], ["xyz98765"])
        self.assertEqual((new_count, retry_count, skipped_count), (1, 0, 1))
        self.assertEqual(current[0].media_url, "https://media.example.test/known.mp4")

    def test_cross_run_history_retries_known_video_when_file_is_missing(self) -> None:
        history = {
            "abc12345": crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                media_url="https://media.example.test/known.mp4",
            )
        }
        current = [crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345")]
        processing, new_count, retry_count, skipped_count = crawler.select_for_processing(
            current, history, set()
        )
        self.assertEqual([video.viewkey for video in processing], ["abc12345"])
        self.assertEqual((new_count, retry_count, skipped_count), (0, 1, 0))

    def test_new_only_queue_skips_known_items_even_when_media_is_missing(self) -> None:
        history = {
            "known123": crawler.Video(
                viewkey="known123",
                canonical_url="https://91porn.com/view_video.php?viewkey=known123",
            )
        }
        current = [
            crawler.Video(viewkey="known123", canonical_url="https://91porn.com/view_video.php?viewkey=known123"),
            crawler.Video(viewkey="new12345", canonical_url="https://91porn.com/view_video.php?viewkey=new12345"),
        ]
        processing, new_count, retry_count, skipped_count = crawler.select_for_processing(
            current, history, set(), new_only=True
        )
        self.assertEqual([video.viewkey for video in processing], ["new12345"])
        self.assertEqual((new_count, retry_count, skipped_count), (1, 0, 1))

    def test_success_history_permanently_skips_an_intentionally_removed_file(self) -> None:
        history = {
            "abc12345": crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                media_url="https://media.example.test/known.mp4",
            )
        }
        current = [crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345")]
        processing, new_count, retry_count, skipped_count = crawler.select_for_processing(
            current, history, set(), {"abc12345"}
        )
        self.assertEqual(processing, [])
        self.assertEqual((new_count, retry_count, skipped_count), (0, 0, 1))

    def test_explicit_ignored_media_is_read_only_and_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ignored-media.json"
            path.write_text(json.dumps({
                "version": 1,
                "items": {
                    "ignored12": {"reason": "user-approved"},
                    "bad": {"reason": "invalid-short-key"},
                },
            }), encoding="utf-8")
            before = path.read_bytes()
            ignored = crawler.load_ignored_media_keys(path)
            self.assertEqual(ignored, {"ignored12"})
            self.assertEqual(path.read_bytes(), before)

            current = [
                crawler.Video(viewkey="ignored12", canonical_url="https://91porn.com/view_video.php?viewkey=ignored12"),
                crawler.Video(viewkey="new12345", canonical_url="https://91porn.com/view_video.php?viewkey=new12345"),
            ]
            processing, new_count, retry_count, skipped_count = crawler.select_for_processing(
                current,
                {},
                set(),
                ignored_keys=ignored,
            )
            self.assertEqual([video.viewkey for video in processing], ["new12345"])
            self.assertEqual((new_count, retry_count, skipped_count), (1, 0, 1))

    def test_success_history_preserves_cached_media_metadata(self) -> None:
        history = {
            "abc12345": crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                media_url="https://media.example.test/known.mp4",
                resolved_at="2026-07-20T12:00:00+08:00",
            )
        }
        current = [crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345")]
        crawler.select_for_processing(current, history, set(), {"abc12345"})
        self.assertEqual(current[0].media_url, "https://media.example.test/known.mp4")
        self.assertEqual(current[0].resolved_at, "2026-07-20T12:00:00+08:00")

    def test_successful_asset_identity_catches_a_changed_viewkey(self) -> None:
        history = {
            "original": crawler.Video(
                viewkey="original",
                canonical_url="https://91porn.com/view_video.php?viewkey=original",
                thumbnail_url="https://cdn.example.test/thumb/1227001.jpg",
            )
        }
        assets = crawler.successful_asset_identifiers(history, {"original"})
        self.assertEqual(assets, {"1227001"})
        current = crawler.Video(
            viewkey="replacement",
            canonical_url="https://91porn.com/view_video.php?viewkey=replacement",
            thumbnail_url="https://cdn.example.test/thumb/1227001.jpg",
        )
        self.assertIn(crawler.media_asset_identifier(current.thumbnail_url), assets)

    def test_asset_duplicate_viewkey_can_be_persisted_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "download-success.txt"
            path.write_text("original\n", encoding="utf-8")
            crawler.append_success_keys(path, {"replacement"})
            self.assertEqual(
                crawler.load_success_keys(path),
                {"original", "replacement"},
            )

    def test_repair_candidates_exclude_completed_and_safety_blocked_items(self) -> None:
        snapshot = {
            "metadata": {
                "resolve_failures": [{
                    "viewkey": "blocked",
                    "kind": "media_mismatch",
                    "error": "详情页媒体与榜单不一致",
                }],
            },
            "videos": [
                {"viewkey": "completed", "canonical_url": "https://91porn.com/view_video.php?viewkey=completed"},
                {"viewkey": "blocked", "canonical_url": "https://91porn.com/view_video.php?viewkey=blocked"},
                {"viewkey": "retry", "canonical_url": "https://91porn.com/view_video.php?viewkey=retry"},
            ],
        }
        candidates, blocked = repair_pending.collect_repair_candidates(snapshot, {"completed"})
        self.assertEqual([item["viewkey"] for item in candidates], ["retry"])
        self.assertEqual(blocked, {"blocked"})

    def test_repair_candidates_exclude_confirmed_unavailable_media(self) -> None:
        snapshot = {
            "metadata": {"resolve_failures": [{
                "viewkey": "unavailable",
                "kind": "media_unavailable",
                "error": "详情页未发现可下载媒体",
            }]},
            "videos": [{
                "viewkey": "unavailable",
                "canonical_url": "https://91porn.com/view_video.php?viewkey=unavailable",
            }],
        }
        candidates, blocked = repair_pending.collect_repair_candidates(snapshot, set())
        self.assertEqual(candidates, [])
        self.assertEqual(blocked, {"unavailable"})

    def test_repair_candidates_exclude_explicitly_ignored_items(self) -> None:
        snapshot = {
            "metadata": {},
            "videos": [
                {"viewkey": "ignored12", "canonical_url": "https://91porn.com/view_video.php?viewkey=ignored12"},
                {"viewkey": "retry123", "canonical_url": "https://91porn.com/view_video.php?viewkey=retry123"},
            ],
        }
        candidates, blocked = repair_pending.collect_repair_candidates(
            snapshot,
            set(),
            ignored_keys={"ignored12"},
        )
        self.assertEqual([item["viewkey"] for item in candidates], ["retry123"])
        self.assertEqual(blocked, set())

    def test_download_time_block_does_not_make_unresolved_count_negative(self) -> None:
        blocked, unresolved = repair_pending.summarize_repair_failures(
            [],
            [{"viewkey": "blocked-at-download", "status": "blocked"}],
        )
        self.assertEqual((blocked, unresolved), (1, 0))

    def test_page_url_preserves_filters_and_replaces_page(self) -> None:
        url = crawler.page_url(
            "https://91porn.com/v.php?category=hot&viewtype=basic&page=9", 2
        )
        self.assertIn("category=hot", url)
        self.assertIn("viewtype=basic", url)
        self.assertIn("page=2", url)
        self.assertNotIn("page=9", url)
        self.assertEqual(crawler.page_url("https://91porn.com/index.php", 2), "https://91porn.com/index.php?page=2")

    def test_cli_accepts_multiple_listing_sources(self) -> None:
        args = crawler.parse_args([
            "--url", "https://91porn.com/v.php?category=top&viewtype=basic",
            "--url", "https://91porn.com/v.php?category=tf&viewtype=basic",
        ])
        self.assertEqual(len(args.urls), 2)
        self.assertIn("category=top", args.urls[0])
        self.assertIn("category=tf", args.urls[1])

    def test_low_pressure_options_are_parsed(self) -> None:
        args = crawler.parse_args([
            "--new-only",
            "--resolve-concurrency", "1",
            "--media-rechecks", "1",
            "--delay", "2",
        ])
        self.assertTrue(args.new_only)
        self.assertEqual(args.resolve_concurrency, 1)
        self.assertEqual(args.media_rechecks, 1)
        self.assertEqual(args.delay, 2)

    def test_listing_url_scope_is_restricted(self) -> None:
        with self.assertRaises(crawler.CrawlerError):
            crawler.validate_listing_url("http://91porn.com/v.php")
        with self.assertRaises(crawler.CrawlerError):
            crawler.validate_listing_url("https://example.com/v.php")
        with self.assertRaises(crawler.CrawlerError):
            crawler.validate_listing_url("https://91porn.com/admin.php")

    def test_media_selection_prefers_signed_video_over_ad(self) -> None:
        body = """
        https://ads.example.test/ad.mp4
        https://media.example.test/video.mp4?st=opaque&amp;f=opaque
        """
        urls = crawler.extract_media_urls(body)
        self.assertEqual(
            crawler.choose_media_url(urls),
            "https://media.example.test/video.mp4?st=opaque&f=opaque",
        )

    def test_pending_metadata_preserves_full_crawl_total(self) -> None:
        metadata = {"raw_detail_links": 735, "unique_videos": 275}
        pending = crawler.pending_output_metadata(metadata, 5)
        self.assertEqual(pending["unique_videos"], 275)
        self.assertEqual(pending["processing_videos"], 5)

    def test_percent_encoded_strencode2_source_is_extracted(self) -> None:
        encoded = (
            "%3Csource%20src%3D%27https%3A%2F%2Fmedia.example.test%2F"
            "video.mp4%3Fst%3Dopaque%26f%3Dopaque%27%3E"
        )
        urls = crawler.extract_media_urls(f'document.write(strencode2("{encoded}"));')
        self.assertEqual(
            urls,
            ["https://media.example.test/video.mp4?st=opaque&f=opaque"],
        )

    def test_media_extraction_ignores_commented_source_and_preroll(self) -> None:
        encoded = (
            "%3Csource%20src%3D%27https%3A%2F%2Fmedia.example.test%2F"
            "actual.mp4%3Fsecure%3Dopaque%26f%3Dopaque%27%3E"
        )
        body = f"""
        <video id="player_one">
          <!-- <source src="https://media.example.test/stale.mp4?st=old&f=old"> -->
          <script>document.write(strencode2("{encoded}"));</script>
        </video>
        <script>
          player.preroll({{src:{{src:"https://ads.example.test/preroll.mp4",type:"video/mp4"}}}});
        </script>
        """
        self.assertEqual(
            crawler.extract_media_urls(body),
            ["https://media.example.test/actual.mp4?secure=opaque&f=opaque"],
        )

    def test_player_identity_rejects_a_mismatched_detail_asset(self) -> None:
        video = crawler.Video(
            viewkey="4f13c14d90b0572e3c5c",
            canonical_url="https://91porn.com/view_video.php?viewkey=4f13c14d90b0572e3c5c",
            thumbnail_url="https://media.example.test/thumb/1225269.jpg",
        )
        with self.assertRaisesRegex(crawler.CrawlerError, "详情页媒体与榜单不一致"):
            crawler.validate_player_identity(
                video,
                ["https://media.example.test/mp43/876125.mp4?st=opaque"],
                ["https://media.example.test/thumb/876125.jpg"],
            )

    def test_player_identity_accepts_the_listing_asset(self) -> None:
        video = crawler.Video(
            viewkey="4f13c14d90b0572e3c5c",
            canonical_url="https://91porn.com/view_video.php?viewkey=4f13c14d90b0572e3c5c",
            thumbnail_url="https://media.example.test/thumb/1225269.jpg",
        )
        crawler.validate_player_identity(
            video,
            ["https://media.example.test/mp43/1225269.mp4?st=opaque"],
            ["https://media.example.test/thumb/1225269.jpg"],
        )

    def test_private_network_media_urls_are_rejected(self) -> None:
        body = "https://127.0.0.1/private.mp4 https://192.168.1.2/private.mp4"
        self.assertEqual(crawler.extract_media_urls(body), [])

    def test_resolved_private_media_endpoint_is_rejected(self) -> None:
        resolved = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
            with self.assertRaisesRegex(download.DownloadError, "非公网"):
                download.ensure_public_endpoint("https://media.example.test/video.mp4")

    def test_resolved_global_media_endpoint_is_accepted(self) -> None:
        resolved = [(2, 1, 6, "", ("8.8.8.8", 443))]
        with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
            self.assertEqual(
                download.ensure_public_endpoint("https://media.example.test/video.mp4"),
                "https://media.example.test/video.mp4",
            )

    def test_auth_cookie_file_is_scoped_to_the_target_site(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth-cookie.txt"
            path.write_text("session=secret; preference=compact\n", encoding="utf-8")
            cookies = list(crawler.load_auth_cookie_jar(path))
        self.assertEqual({cookie.name for cookie in cookies}, {"session", "preference"})
        self.assertTrue(all(cookie.domain == ".91porn.com" for cookie in cookies))
        self.assertTrue(all(cookie.secure for cookie in cookies))

    def test_auth_cookie_placeholder_is_not_treated_as_a_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth-cookie.txt"
            path.write_text("PASTE_NEW_COOKIE_HERE\n", encoding="utf-8")
            self.assertFalse(crawler.auth_cookie_configured(path))

    def test_local_crawler_uses_the_project_cookie_file_by_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(crawler.AUTH_COOKIE_ENV, None)
            path = crawler.configured_auth_cookie_file()
        self.assertEqual(path, Path(crawler.__file__).resolve().parents[1] / "data" / "auth-cookie.txt")

    def test_build_opener_uses_explicit_direct_route_by_default(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://environment-proxy.invalid:8080",
                "HTTPS_PROXY": "http://environment-proxy.invalid:8080",
                "ALL_PROXY": "http://environment-proxy.invalid:8080",
            },
            clear=False,
        ):
            opener = crawler.build_opener()
        # An empty ProxyHandler intentionally contributes no handler methods;
        # its presence while building the opener suppresses urllib's default
        # environment-reading handler.  Therefore no ProxyHandler remains in
        # the final handler list, and the environment proxy cannot be used.
        self.assertFalse(any(isinstance(handler, urllib.request.ProxyHandler) for handler in opener.handlers))

    def test_build_opener_maps_http_proxy_to_both_web_schemes(self) -> None:
        opener = crawler.build_opener(proxy_url="http://host.docker.internal:7897")
        proxy_handlers = [handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)]
        self.assertEqual(len(proxy_handlers), 1)
        self.assertEqual(
            proxy_handlers[0].proxies,
            {
                "http": "http://host.docker.internal:7897",
                "https": "http://host.docker.internal:7897",
            },
        )

    def test_build_opener_rejects_non_http_proxy_url(self) -> None:
        with self.assertRaisesRegex(crawler.CrawlerError, "HTTP/HTTPS"):
            crawler.build_opener(proxy_url="socks5://host.docker.internal:7897")

    def test_expired_media_refresh_uses_the_configured_browser_identity(self) -> None:
        item = {
            "viewkey": "abc12345",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
            "thumbnail_url": "https://media.example/thumb.jpg",
            "media_url": "https://media.example/expired.mp4",
        }

        def resolve(_opener, videos, **kwargs):
            self.assertEqual(kwargs["user_agent"], "Mozilla/5.0 Browser-Matched")
            videos[0].media_url = "https://media.example/fresh.mp4"
            videos[0].resolved_at = "2026-07-31T22:00:00+08:00"

        with mock.patch.object(crawler, "configured_user_agent", return_value="Mozilla/5.0 Browser-Matched"), \
             mock.patch.object(crawler, "resolve_media", side_effect=resolve):
            download.refresh_media(item, 1, 0, lambda *_args, **_kwargs: None)
        self.assertEqual(item["media_url"], "https://media.example/fresh.mp4")

    def test_proxy_fake_ip_is_rejected_without_an_explicitly_trusted_host(self) -> None:
        resolved = [(2, 1, 6, "", ("198.18.0.75", 443))]
        with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
            with mock.patch.dict(os.environ, {"TRUSTED_PROXY_FAKE_IP_HOSTS": ""}):
                with self.assertRaisesRegex(download.RefreshableEndpointError, "非公网"):
                    download.ensure_public_endpoint("https://media.example.test/video.mp4")

    def test_proxy_fake_ip_is_accepted_for_an_explicitly_trusted_host(self) -> None:
        resolved = [(2, 1, 6, "", ("198.18.0.75", 443))]
        with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
            with mock.patch.dict(os.environ, {"TRUSTED_PROXY_FAKE_IP_HOSTS": "media.example.test"}):
                self.assertEqual(
                    download.ensure_public_endpoint("https://media.example.test/video.mp4"),
                    "https://media.example.test/video.mp4",
                )

    def test_proxy_fake_ip_allowlist_does_not_allow_other_reserved_ranges(self) -> None:
        resolved = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
            with mock.patch.dict(os.environ, {"TRUSTED_PROXY_FAKE_IP_HOSTS": "media.example.test"}):
                with self.assertRaisesRegex(download.DownloadError, "非公网"):
                    download.ensure_public_endpoint("https://media.example.test/video.mp4")

    def test_proxy_fake_ip_suffix_covers_rotating_cdn_subdomains(self) -> None:
        resolved = [(2, 1, 6, "", ("198.18.0.75", 443))]
        for allowlist in (".rsc.cdn77.org", "+.rsc.cdn77.org", "la.btc620.com,.rsc.cdn77.org"):
            with self.subTest(allowlist=allowlist):
                with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
                    with mock.patch.dict(os.environ, {"TRUSTED_PROXY_FAKE_IP_HOSTS": allowlist}):
                        self.assertEqual(
                            download.ensure_public_endpoint("https://9999999999.rsc.cdn77.org/video.mp4"),
                            "https://9999999999.rsc.cdn77.org/video.mp4",
                        )

    def test_proxy_fake_ip_suffix_stays_narrow(self) -> None:
        resolved = [(2, 1, 6, "", ("198.18.0.75", 443))]
        cases = (
            (".rsc.cdn77.org", "https://evil-rsc.cdn77.org/video.mp4"),
            (".rsc.cdn77.org", "https://rsc.cdn77.org.attacker.test/video.mp4"),
            (".org", "https://media.example.org/video.mp4"),
        )
        for allowlist, url in cases:
            with self.subTest(allowlist=allowlist, url=url):
                with mock.patch.object(download.socket, "getaddrinfo", return_value=resolved):
                    with mock.patch.dict(os.environ, {"TRUSTED_PROXY_FAKE_IP_HOSTS": allowlist}):
                        with self.assertRaisesRegex(download.RefreshableEndpointError, "非公网"):
                            download.ensure_public_endpoint(url)

    def test_fake_ip_failure_refreshes_media_and_switches_route(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://1470423830.rsc.cdn77.org/old.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        download_calls: list[tuple[str, str]] = []

        def fake_download(opener, candidate, *_args, **_kwargs):
            download_calls.append((opener, candidate["media_url"]))
            if len(download_calls) == 1:
                raise download.RefreshableEndpointError("拒绝非公网媒体地址")
            return {"viewkey": candidate["viewkey"], "status": "downloaded", "bytes": 4}

        def fake_refresh(candidate, *_args, opener=None, **_kwargs):
            self.assertEqual(opener, "resolver:http://proxy.local:7897")
            candidate["media_url"] = "https://la.btc620.com/fresh.mp4"
            candidate["resolved_at"] = "2026-08-23T21:01:00+08:00"

        with mock.patch.object(
            download,
            "build_download_opener",
            side_effect=lambda proxy=None: f"download:{proxy or 'direct'}",
        ), mock.patch.object(
            download,
            "build_route_resolver_opener",
            side_effect=lambda route: f"resolver:{route.proxy_url or 'direct'}",
        ), mock.patch.object(download, "download_one", side_effect=fake_download), \
             mock.patch.object(download, "refresh_media", side_effect=fake_refresh) as refresh, \
             mock.patch.object(download, "_refresh_delay", return_value=0.0), \
             mock.patch.object(download.time, "sleep"):
            result = download.download_with_auto_refresh(
                item,
                selected_route=download.DownloadRoute("direct"),
                media_proxy="http://proxy.local:7897",
                output_dir=Path("/tmp/output"),
                partial_dir=Path("/tmp/partials"),
                timeout=1,
                max_bytes=1024 * 1024,
                retries=0,
                refresh_retries=3,
                link_max_age=180,
            )

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["autoRetries"], 1)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(download_calls, [
            ("download:direct", "https://1470423830.rsc.cdn77.org/old.mp4"),
            ("download:http://proxy.local:7897", "https://la.btc620.com/fresh.mp4"),
        ])

    def test_auto_refresh_stops_at_the_configured_limit(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/old.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        with mock.patch.object(download, "download_one", side_effect=download.RefreshableEndpointError("拒绝非公网媒体地址")) as attempt, \
             mock.patch.object(download, "refresh_media", return_value=None) as refresh, \
             mock.patch.object(download, "_refresh_delay", return_value=0.0), \
             mock.patch.object(download.time, "sleep"):
            with self.assertRaises(download.AutoRefreshExhausted) as raised:
                download.download_with_auto_refresh(
                    item,
                    selected_route=download.DownloadRoute("direct"),
                    media_proxy=None,
                    output_dir=Path("/tmp/output"),
                    partial_dir=Path("/tmp/partials"),
                    timeout=1,
                    max_bytes=1024 * 1024,
                    retries=0,
                    refresh_retries=2,
                    link_max_age=180,
                )
        self.assertEqual(raised.exception.attempts, 2)
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(refresh.call_count, 2)

    def test_auto_refresh_does_not_retry_permanent_download_errors(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        with mock.patch.object(download, "download_one", side_effect=download.DownloadError("磁盘剩余空间不足")) as attempt, \
             mock.patch.object(download, "refresh_media") as refresh:
            with self.assertRaisesRegex(download.DownloadError, "磁盘"):
                download.download_with_auto_refresh(
                    item,
                    selected_route=download.DownloadRoute("direct"),
                    media_proxy=None,
                    output_dir=Path("/tmp/output"),
                    partial_dir=Path("/tmp/partials"),
                    timeout=1,
                    max_bytes=1024 * 1024,
                    retries=0,
                    refresh_retries=3,
                    link_max_age=180,
                )
        self.assertEqual(attempt.call_count, 1)
        refresh.assert_not_called()

    def test_auto_refresh_does_not_retry_without_a_canonical_url(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "",
        }
        with mock.patch.object(download, "download_one", side_effect=download.RefreshableEndpointError("拒绝非公网媒体地址")) as attempt, \
             mock.patch.object(download, "refresh_media") as refresh:
            with self.assertRaises(download.AutoRefreshExhausted) as raised:
                download.download_with_auto_refresh(
                    item,
                    selected_route=download.DownloadRoute("direct"),
                    media_proxy=None,
                    output_dir=Path("/tmp/output"),
                    partial_dir=Path("/tmp/partials"),
                    timeout=1,
                    max_bytes=1024 * 1024,
                    retries=0,
                    refresh_retries=3,
                    link_max_age=180,
                )
        self.assertEqual(raised.exception.attempts, 0)
        self.assertEqual(attempt.call_count, 1)
        refresh.assert_not_called()

    def test_refresh_retry_argument_has_a_bounded_range(self) -> None:
        self.assertEqual(download.parse_args(["input.json", "--refresh-retries", "0"]).refresh_retries, 0)
        self.assertEqual(
            download.parse_args(["input.json", "--refresh-retries", str(download.MAX_REFRESH_RETRIES)]).refresh_retries,
            download.MAX_REFRESH_RETRIES,
        )
        with self.assertRaises(SystemExit):
            download.parse_args(["input.json", "--refresh-retries", "-1"])
        with self.assertRaises(SystemExit):
            download.parse_args(["input.json", "--refresh-retries", str(download.MAX_REFRESH_RETRIES + 1)])

    def test_media_proxy_validation_rejects_socks_and_credentials(self) -> None:
        with self.assertRaises(download.DownloadError):
            download.validate_media_proxy("socks5://host.docker.internal:7897")
        with self.assertRaises(download.DownloadError):
            download.validate_media_proxy("http://user:pass@host.docker.internal:7897")
        self.assertEqual(
            download.validate_media_proxy(" http://host.docker.internal:7897/ "),
            "http://host.docker.internal:7897",
        )

    def test_media_probe_is_bounded_and_does_not_write_download_state(self) -> None:
        body = b"x" * (download.MIN_ROUTE_PROBE_BYTES + 1024)
        response = FakeResponse(body, headers={"Content-Type": "video/mp4"})
        opener = mock.Mock()
        opener.open.return_value = response
        item = {"viewkey": "probe123", "media_url": "https://media.example/probe.mp4"}
        with mock.patch.object(download, "ensure_public_endpoint") as check:
            speed, received = download.probe_media(
                opener,
                item,
                timeout=1,
                max_bytes=download.MIN_ROUTE_PROBE_BYTES,
            )
        request = opener.open.call_args.args[0]
        self.assertEqual(request.headers.get("Range"), f"bytes=0-{download.MIN_ROUTE_PROBE_BYTES - 1}")
        self.assertEqual(received, download.MIN_ROUTE_PROBE_BYTES)
        self.assertGreater(speed, 0)
        check.assert_called_once_with(item["media_url"])

    def test_monitor_preserves_route_probe_fields_without_signed_urls(self) -> None:
        serialized = refresh_monitor.serialize_download_progress({
            "stage": "route-probe",
            "done": 0,
            "total": 2,
            "route": "http-proxy",
            "routeProbe": {"chosen": "http-proxy", "direct": {"speedBytesS": 1}},
        })
        self.assertEqual(serialized["route"], "http-proxy")
        self.assertEqual(serialized["routeProbe"]["chosen"], "http-proxy")

    def test_route_selection_refreshes_both_urls_and_uses_faster_route(self) -> None:
        item = {
            "viewkey": "route123",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=route123",
            "media_url": "https://media.example/old.mp4?e=old",
            "resolved_at": "2026-08-12T20:00:00+08:00",
        }
        seen_urls: list[str] = []

        def fake_refresh(candidate, _timeout, _retries, _callback, *, opener=None):
            uses_proxy = any(
                isinstance(handler, urllib.request.ProxyHandler) and handler.proxies
                for handler in (opener.handlers if opener else [])
            )
            candidate["media_url"] = f"https://media.example/{'proxy' if uses_proxy else 'direct'}.mp4?e=fresh"
            candidate["resolved_at"] = "2026-08-12T21:00:00+08:00"

        def fake_probe(opener, candidate, **_kwargs):
            seen_urls.append(candidate["media_url"])
            return (2_000_000.0 if "proxy" in candidate["media_url"] else 1_000_000.0), download.MIN_ROUTE_PROBE_BYTES

        with mock.patch.object(download, "refresh_media", side_effect=fake_refresh), \
             mock.patch.object(download, "probe_media", side_effect=fake_probe):
            route, record, fresh = download.select_download_route(
                [item],
                "http://host.docker.internal:7897",
                timeout=2,
                retries=1,
                probe_bytes=download.MIN_ROUTE_PROBE_BYTES,
                probe_timeout=1,
            )
        self.assertEqual(route.name, "http-proxy")
        self.assertEqual(record["chosen"], "http-proxy")
        self.assertEqual(seen_urls, [
            "https://media.example/direct.mp4?e=fresh",
            "https://media.example/proxy.mp4?e=fresh",
        ])
        self.assertEqual(fresh["media_url"] if fresh else "", seen_urls[-1])
        self.assertIn("old", item["media_url"])

    def test_route_selection_falls_back_to_direct_when_proxy_probe_fails(self) -> None:
        item = {
            "viewkey": "route124",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=route124",
            "media_url": "https://media.example/old.mp4",
        }

        def fake_probe(opener, _candidate, **_kwargs):
            uses_proxy = any(
                isinstance(handler, urllib.request.ProxyHandler) and handler.proxies
                for handler in opener.handlers
            )
            if uses_proxy:
                raise download.DownloadError("proxy unavailable")
            return 1_000_000.0, download.MIN_ROUTE_PROBE_BYTES

        with mock.patch.object(download, "refresh_media", return_value=None), \
             mock.patch.object(download, "probe_media", side_effect=fake_probe):
            route, record, _fresh = download.select_download_route(
                [item],
                "http://host.docker.internal:7897",
                timeout=2,
                retries=1,
                probe_bytes=download.MIN_ROUTE_PROBE_BYTES,
                probe_timeout=1,
            )
        self.assertEqual(route.name, "direct")
        self.assertEqual(record["chosen"], "direct")

    def test_download_manifest_loader_deduplicates_key_and_url(self) -> None:
        payload = {"videos": [
            {"viewkey": "abc12345", "media_url": "https://media.example/a.mp4", "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345"},
            {"viewkey": "abc12345", "media_url": "https://media.example/b.mp4", "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345"},
            {"viewkey": "xyz98765", "media_url": "https://media.example/a.mp4", "canonical_url": "https://91porn.com/view_video.php?viewkey=xyz98765"},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(len(download.load_items(path)), 1)

    def test_download_loader_preserves_thumbnail_for_identity_validation(self) -> None:
        payload = {"videos": [{
            "viewkey": "abc12345",
            "media_url": "https://media.example/a.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
            "thumbnail_url": "https://media.example/thumb/123.jpg",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(download.load_items(path)[0]["thumbnail_url"], payload["videos"][0]["thumbnail_url"])

    def test_download_loader_rejects_out_of_scope_referer(self) -> None:
        payload = {"videos": [{
            "viewkey": "abc12345",
            "media_url": "https://media.example/a.mp4",
            "canonical_url": "https://example.com/private",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(download.DownloadError):
                download.load_items(path)

    def test_successful_download_is_recorded_in_manifest_and_history(self) -> None:
        payload = {"videos": [{
            "viewkey": "abc12345",
            "media_url": "https://media.example/a.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            manifest_path = root / "manifest.json"
            history_path = root / "success.txt"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            fake_result = {"viewkey": "abc12345", "status": "downloaded", "path": str(root / "abc12345.mp4")}
            with mock.patch.object(download, "select_download_route", return_value=(download.DownloadRoute("direct"), {"chosen": "direct"}, None)), \
                 mock.patch.object(download, "download_one", return_value=fake_result):
                exit_code = download.main([
                    str(input_path),
                    "--output-dir", str(root / "output"),
                    "--work-dir", str(root / "partials"),
                    "--manifest", str(manifest_path),
                    "--success-history", str(history_path),
                    "--delay", "0.5",
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), [fake_result])
            self.assertEqual(history_path.read_text(encoding="utf-8").splitlines(), ["abc12345"])

    def test_auto_refresh_persists_one_final_result_and_the_fresh_url(self) -> None:
        payload = {"videos": [{
            "viewkey": "abc12345",
            "media_url": "https://1470423830.rsc.cdn77.org/old.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }]}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.json"
            manifest_path = root / "manifest.json"
            progress_path = root / "progress.json"
            history_path = root / "success.txt"
            cache_path = root / "cache.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            cache_path.write_text(json.dumps(payload), encoding="utf-8")

            def fake_refresh(item, *_args, **_kwargs):
                item["media_url"] = "https://la.btc620.com/fresh.mp4"
                item["resolved_at"] = "2026-08-23T21:01:00+08:00"

            fake_result = {"viewkey": "abc12345", "status": "downloaded", "bytes": 4}
            with mock.patch.object(download, "select_download_route", return_value=(download.DownloadRoute("direct"), {"chosen": "direct"}, None)), \
                 mock.patch.object(download, "download_one", side_effect=[download.RefreshableEndpointError("拒绝非公网媒体地址"), fake_result]), \
                 mock.patch.object(download, "refresh_media", side_effect=fake_refresh), \
                 mock.patch.object(download, "_refresh_delay", return_value=0.0), \
                 mock.patch.object(download.time, "sleep"):
                exit_code = download.main([
                    str(input_path),
                    "--output-dir", str(root / "output"),
                    "--work-dir", str(root / "partials"),
                    "--manifest", str(manifest_path),
                    "--success-history", str(history_path),
                    "--media-cache", str(cache_path),
                    "--progress", str(progress_path),
                    "--concurrency", "1",
                    "--delay", "0",
                ])

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8")), [{
                **fake_result,
                "autoRetries": 1,
            }])
            self.assertEqual(history_path.read_text(encoding="utf-8").splitlines(), ["abc12345"])
            cached = json.loads(cache_path.read_text(encoding="utf-8"))["videos"][0]
            self.assertEqual(cached["media_url"], "https://la.btc620.com/fresh.mp4")
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual((progress["done"], progress["failed"], progress["stage"]), (1, 0, "complete"))

    def test_content_history_bootstraps_from_download_logs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            digest = hashlib.sha256(b"known-content").hexdigest()
            (logs / "download-old.log").write_text(json.dumps({
                "viewkey": "abc12345",
                "status": "downloaded",
                "bytes": 13,
                "sha256": digest,
            }) + "\n", encoding="utf-8")
            history = download.ContentHistory(root / "content.json")
            self.assertEqual(history.claim(digest, "xyz98765", 13), "abc12345")

    def test_duplicate_content_never_reaches_output_directory(self) -> None:
        item = {
            "viewkey": "xyz98765",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=xyz98765",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            partials = root / "partials"
            output.mkdir()
            partials.mkdir()
            history = download.ContentHistory(root / "content.json")
            digest = hashlib.sha256(b"same-content").hexdigest()
            self.assertIsNone(history.claim(digest, "abc12345", 12))
            response = FakeResponse(b"same-content", headers={"Content-Type": "video/mp4", "Content-Length": "12"})
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(download, "ensure_public_endpoint", return_value=item["media_url"]):
                result = download.download_one(
                    opener,
                    item,
                    output,
                    partials,
                    timeout=1,
                    max_bytes=1024 * 1024,
                    content_history=history,
                )
            self.assertEqual(result["status"], "duplicate")
            self.assertEqual(result["duplicate_of"], "abc12345")
            self.assertFalse((output / "xyz98765.mp4").exists())
            self.assertEqual(list(partials.iterdir()), [])

    def test_download_progress_tracks_active_files_and_completion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            progress_path = Path(directory) / "download-progress.json"
            download.set_dl_progress_target(progress_path, 2)
            download._update_dl_item({
                "viewkey": "abc12345",
                "fileName": "abc12345.mp4",
                "bytesDone": 5 * 1024 * 1024,
                "bytesTotal": 20 * 1024 * 1024,
                "speedBytesS": 2 * 1024 * 1024,
            })

            active_payload = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(active_payload["total"], 2)
            self.assertEqual(active_payload["done"], 0)
            self.assertEqual(len(active_payload["active"]), 1)
            self.assertEqual(active_payload["active"][0]["bytesDone"], 5 * 1024 * 1024)

            download._write_dl_progress("abc12345")
            completed_payload = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(completed_payload["done"], 1)
            self.assertEqual(completed_payload["active"], [])
            download.set_dl_progress_target(None, 0)

    def test_fetch_html_retries_a_transient_network_error(self) -> None:
        response = FakeResponse(
            b"<html>ok</html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            url="https://91porn.com/v.php",
        )
        opener = mock.Mock()
        opener.open.side_effect = [urllib.error.URLError("temporary"), response]
        with mock.patch.object(crawler.time, "sleep"), mock.patch.object(crawler.random, "uniform", return_value=0):
            body = crawler.fetch_html(opener, "https://91porn.com/v.php", timeout=1, user_agent="test", retries=1)
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(opener.open.call_count, 2)

    def test_fetch_html_identifies_a_challenge_served_as_503(self) -> None:
        # Cloudflare also answers with 429/503.  Reading the body only for 403
        # also skipped the cf-mitigated header check, so a challenged 503 looked
        # like a retryable upstream error and never raised the auth signal.
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        headers["cf-mitigated"] = "challenge"
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://91porn.com/v.php", 503, "unavailable", headers, io.BytesIO(b"<html>blocked</html>"),
        )
        with mock.patch.object(crawler.time, "sleep"), mock.patch.object(crawler.random, "uniform", return_value=0):
            with self.assertRaises(crawler.CloudflareChallengeError):
                crawler.fetch_html(opener, "https://91porn.com/v.php", timeout=1, user_agent="test", retries=1)
        self.assertEqual(opener.open.call_count, 1)

    def test_download_failure_kinds_follow_the_exception_type(self) -> None:
        cases = (
            (crawler.MediaMismatchError("详情页媒体与榜单不一致（viewkey=abc12345）"), "media_mismatch", True),
            (crawler.MediaUnavailableError("详情页没有可下载媒体"), "media_unavailable", True),
            (crawler.CloudflareChallengeError("Cloudflare Challenge: /view_video.php"), "auth_challenge", False),
            (download.DownloadError("连接被重置"), "download_error", False),
        )
        for exc, kind, safe in cases:
            with self.subTest(kind=kind):
                self.assertEqual(download.failure_kind(exc), kind)
                self.assertEqual(kind in download.SAFE_BLOCK_KINDS, safe)
                # The same verdict must survive the auto-refresh wrapper.
                self.assertEqual(download.failure_kind(download.AutoRefreshExhausted(exc, 2)), kind)
        self.assertTrue(download.is_media_mismatch_error(crawler.MediaMismatchError("详情页媒体与榜单不一致")))
        self.assertFalse(download.is_media_mismatch_error(crawler.MediaUnavailableError("没有媒体")))

    def test_dead_media_routes_fail_the_queue_once(self) -> None:
        items = [{"viewkey": "abc12345", "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
                  "media_url": "https://cdn.example/abc12345.mp4"}]
        with mock.patch.object(download, "refresh_media", side_effect=crawler.CloudflareChallengeError("Cloudflare Challenge: /x")), \
             mock.patch.object(download, "probe_media", return_value=(0.0, 0)), \
             mock.patch.object(download, "set_dl_progress_route"), \
             mock.patch.object(download, "set_dl_progress_stage"):
            with self.assertRaises(download.RouteProbeFailed) as raised:
                download.select_download_route(items, "http://proxy.local:7897", timeout=5, retries=0)
        self.assertIn("auth_challenge", raised.exception.kinds)
        self.assertTrue(raised.exception.record.get("routesDead"))

        response = FakeResponse(
            b"<html><title>Just a moment</title><script src='/cdn-cgi/challenge-platform/x'></script></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            url="https://91porn.com/v.php",
        )
        opener = mock.Mock()
        opener.open.return_value = response
        with self.assertRaises(crawler.CloudflareChallengeError):
            crawler.fetch_html(opener, "https://91porn.com/v.php", timeout=1, user_agent="test", retries=0)

    def test_fetch_html_accepts_normal_page_with_cloudflare_script(self) -> None:
        response = FakeResponse(
            b"<html><title>91porn</title><script src='/cdn-cgi/challenge-platform/scripts/jsd/main.js'></script><a href='view_video.php?viewkey=abc12345'>video</a></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
            url="https://91porn.com/v.php",
        )
        opener = mock.Mock()
        opener.open.return_value = response
        body = crawler.fetch_html(opener, "https://91porn.com/v.php", timeout=1, user_agent="test", retries=0)
        self.assertIn("viewkey=abc12345", body)

    def test_parallel_media_resolution_propagates_worker_failure(self) -> None:
        videos = [
            crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345"),
            crawler.Video(viewkey="xyz98765", canonical_url="https://91porn.com/view_video.php?viewkey=xyz98765"),
        ]
        with mock.patch.object(crawler, "fetch_html", side_effect=crawler.CrawlerError("network failed")):
            with self.assertRaises(crawler.CrawlerError):
                crawler.resolve_media(crawler.build_opener(), videos, timeout=1, delay=0, user_agent="test", concurrency=2)

    def test_media_mismatch_is_reported_as_a_safe_block(self) -> None:
        videos = [
            crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                media_url="https://media.example.test/stale.mp4",
                resolved_at="2026-07-20T12:00:00+08:00",
            ),
        ]
        with mock.patch.object(crawler, "fetch_html", side_effect=crawler.MediaMismatchError("详情页媒体与榜单不一致")):
            failures = crawler.resolve_media(
                crawler.build_opener(),
                videos,
                timeout=1,
                delay=0,
                user_agent="test",
                concurrency=1,
                continue_on_error=True,
            )
        self.assertEqual(failures[0]["kind"], "media_mismatch")
        self.assertEqual(videos[0].media_url, "")
        self.assertEqual(videos[0].resolved_at, "")
        self.assertTrue(download.is_media_mismatch_error(crawler.MediaMismatchError(failures[0]["error"])))

    def test_media_mismatch_is_refetched_before_being_blocked(self) -> None:
        video = crawler.Video(
            viewkey="abc12345",
            canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
            thumbnail_url="https://media.example.test/thumb/1227000.jpg",
        )
        wrong = '<video poster="https://media.example.test/thumb/800000.jpg"><source src="https://media.example.test/800000.mp4"></video>'
        correct = '<video poster="https://media.example.test/thumb/1227000.jpg"><source src="https://media.example.test/1227000.mp4"></video>'
        with mock.patch.object(crawler, "fetch_html", side_effect=[wrong, correct]) as fetch:
            failures = crawler.resolve_media(
                crawler.build_opener(),
                [video],
                timeout=1,
                delay=0,
                user_agent="test",
                concurrency=1,
                retries=1,
                continue_on_error=True,
            )
        self.assertEqual(failures, [])
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("1227000.mp4", video.media_url)

    def test_missing_media_is_rechecked_and_safely_blocked(self) -> None:
        video = crawler.Video(
            viewkey="abc12345",
            canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
        )
        with mock.patch.object(crawler, "fetch_html", return_value="<html><body>No player</body></html>") as fetch:
            failures = crawler.resolve_media(
                crawler.build_opener(),
                [video],
                timeout=1,
                delay=0,
                user_agent="test",
                concurrency=1,
                retries=2,
                continue_on_error=True,
            )
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(failures[0]["kind"], "media_unavailable")
        self.assertIn("已复核 3 次", failures[0]["error"])

    def test_media_rechecks_can_be_limited_for_low_pressure_runs(self) -> None:
        video = crawler.Video(
            viewkey="abc12345",
            canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
        )
        with mock.patch.object(crawler, "fetch_html", return_value="<html><body>No player</body></html>") as fetch:
            failures = crawler.resolve_media(
                crawler.build_opener(),
                [video],
                timeout=1,
                delay=0,
                user_agent="test",
                concurrency=1,
                retries=4,
                identity_attempts=1,
                continue_on_error=True,
            )
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(failures[0]["kind"], "media_unavailable")
        self.assertIn("已复核 1 次", failures[0]["error"])

    def test_persisted_media_mismatch_skips_only_the_same_listing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "blocked-media.json"
            original = crawler.Video(
                viewkey="abc12345",
                canonical_url="https://91porn.com/view_video.php?viewkey=abc12345",
                thumbnail_url="https://cdn.example.test/thumb/1226511.jpg",
            )
            crawler.update_blocked_media_history(history_path, [original], [{
                "viewkey": "abc12345",
                "kind": "media_mismatch",
                "error": "详情页媒体与榜单不一致",
            }])
            history = crawler.load_blocked_media_history(history_path)

            matching = crawler.matching_blocked_failures([original], history)
            changed = crawler.Video(
                viewkey="abc12345",
                canonical_url=original.canonical_url,
                thumbnail_url="https://cdn.example.test/thumb/1227000.jpg",
            )

            self.assertEqual([item["viewkey"] for item in matching], ["abc12345"])
            self.assertEqual(crawler.matching_blocked_failures([changed], history), [])

    def test_completed_items_are_pruned_from_blocked_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blocked-media.json"
            path.write_text(json.dumps({
                "version": 1,
                "items": {
                    "completed1": {"kind": "media_mismatch"},
                    "pending123": {"kind": "media_unavailable"},
                },
            }), encoding="utf-8")
            removed = crawler.prune_blocked_media_history(path, {"completed1"})
            remaining = crawler.load_blocked_media_history(path)
            self.assertEqual(removed, 1)
            self.assertEqual(set(remaining), {"pending123"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_terminal_failed_progress_is_archived_for_truthful_last_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "download-progress.json"
            destination = root / "last-progress.json"
            source.write_text(json.dumps({
                "stage": "failed",
                "done": 16,
                "total": 16,
                "failed": 3,
            }), encoding="utf-8")

            self.assertTrue(progress_history.archive_completed_progress(source, destination))
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["stage"], "failed")

    def test_resume_restarts_when_content_range_does_not_match(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            partials = root / "partials"
            output.mkdir()
            partials.mkdir()
            partial = partials / "abc12345.mp4.part"
            partial.write_bytes(b"old")
            download._write_meta(partial, {"viewkey": "abc12345", "totalBytes": 10, "etag": "same"})
            mismatched = FakeResponse(b"ignored", status=206, headers={
                "Content-Type": "video/mp4",
                "Content-Length": "7",
                "Content-Range": "bytes 2-8/10",
                "ETag": "same",
            })
            replacement = FakeResponse(b"new!", headers={"Content-Type": "video/mp4", "Content-Length": "4"})
            opener = mock.Mock()
            opener.open.side_effect = [mismatched, replacement]
            with mock.patch.object(download, "ensure_public_endpoint", return_value=item["media_url"]):
                result = download.download_one(opener, item, output, partials, timeout=1, max_bytes=1024 * 1024, retries=1)
            self.assertEqual((output / "abc12345.mp4").read_bytes(), b"new!")
            self.assertEqual(result["status"], "downloaded")
            self.assertFalse(download._meta_path(partial).exists())

    def test_http_416_finalizes_a_complete_partial(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            partials = root / "partials"
            output.mkdir()
            partials.mkdir()
            partial = partials / "abc12345.mp4.part"
            partial.write_bytes(b"abc")
            download._write_meta(partial, {"viewkey": "abc12345", "totalBytes": 0, "etag": "same"})
            headers = Message()
            headers["Content-Range"] = "bytes */3"
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(item["media_url"], 416, "range", headers, None)
            with mock.patch.object(download, "ensure_public_endpoint", return_value=item["media_url"]):
                result = download.download_one(opener, item, output, partials, timeout=1, max_bytes=1024 * 1024)
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual((output / "abc12345.mp4").read_bytes(), b"abc")

    def test_complete_partial_is_finalized_across_filesystems_without_network(self) -> None:
        item = {
            "viewkey": "abc12345",
            "media_url": "https://media.example/video.mp4",
            "canonical_url": "https://91porn.com/view_video.php?viewkey=abc12345",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            partials = root / "partials"
            output.mkdir()
            partials.mkdir()
            partial = partials / "abc12345.mp4.part"
            final = output / "abc12345.mp4"
            partial.write_bytes(b"complete")
            download._write_meta(partial, {"viewkey": "abc12345", "totalBytes": 8, "etag": "same"})
            real_replace = os.replace

            def replace_with_cross_device_error(source, destination):
                if Path(source) == partial and Path(destination) == final:
                    raise OSError(errno.EXDEV, "cross-device link")
                return real_replace(source, destination)

            opener = mock.Mock()
            with mock.patch.object(download.os, "replace", side_effect=replace_with_cross_device_error), mock.patch.object(
                download, "ensure_public_endpoint", return_value=item["media_url"]
            ):
                result = download.download_one(opener, item, output, partials, timeout=1, max_bytes=1024 * 1024)

            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(final.read_bytes(), b"complete")
            self.assertFalse(partial.exists())
            self.assertFalse(download._meta_path(partial).exists())
            opener.open.assert_not_called()

    def test_shared_task_lock_prevents_a_second_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".task.lock"
            first = task_lock.TaskLock(path, "test")
            second = task_lock.TaskLock(path, "test")
            self.assertTrue(first.acquire())
            self.assertTrue(task_lock.lock_is_active(path))
            self.assertFalse(second.acquire())
            first.release()
            self.assertFalse(path.exists())

    def test_daily_history_finds_only_a_successful_download_from_today(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "run-history.jsonl"
            history.write_text("\n".join([
                "not-json",
                json.dumps({"timestamp": "2026-07-19T23:59:00+08:00", "crawlExitCode": 0, "downloadExitCode": 0, "downloadRequested": True}),
                json.dumps({"timestamp": "2026-07-20T08:00:00+08:00", "crawlExitCode": 1, "downloadExitCode": None, "downloadRequested": True}),
                json.dumps({"timestamp": "2026-07-20T09:00:00+08:00", "crawlExitCode": 0, "downloadExitCode": None, "downloadRequested": False}),
                json.dumps({"timestamp": "2026-07-20T10:00:00+08:00", "crawlExitCode": 0, "downloadExitCode": 0, "downloadRequested": True}),
                json.dumps({"timestamp": "2026-07-20T11:00:00+08:00", "crawlExitCode": 0, "downloadExitCode": 0, "downloadRequested": True, "resultStatus": "attention", "listingFailures": 1}),
            ]), encoding="utf-8")

            event = task_history.latest_successful_daily_run(history, today=datetime.date(2026, 7, 20))

            self.assertIsNotNone(event)
            self.assertEqual(event["timestamp"], "2026-07-20T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
