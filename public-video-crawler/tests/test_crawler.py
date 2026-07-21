from __future__ import annotations

import errno
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest import mock

import crawler
import download

sys.path.append(str(Path(__file__).resolve().parents[2] / "scripts"))
import task_lock
import task_history
import progress_history
import source_config


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

    def test_merge_deduplicates_across_pages(self) -> None:
        first = crawler.parse_listing(FIXTURE, crawler.DEFAULT_URL, 1)
        second = crawler.parse_listing(FIXTURE, crawler.DEFAULT_URL, 2)
        merged = crawler.merge_videos([first, second])
        self.assertEqual(len(merged), 2)
        self.assertEqual({tuple(video.source_pages) for video in merged}, {(1, 2)})

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

    def test_private_network_media_urls_are_rejected(self) -> None:
        body = "https://127.0.0.1/private.mp4 https://192.168.1.2/private.mp4"
        self.assertEqual(crawler.extract_media_urls(body), [])

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
            with mock.patch.object(download, "download_one", return_value=fake_result):
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

    def test_parallel_media_resolution_propagates_worker_failure(self) -> None:
        videos = [
            crawler.Video(viewkey="abc12345", canonical_url="https://91porn.com/view_video.php?viewkey=abc12345"),
            crawler.Video(viewkey="xyz98765", canonical_url="https://91porn.com/view_video.php?viewkey=xyz98765"),
        ]
        with mock.patch.object(crawler, "fetch_html", side_effect=crawler.CrawlerError("network failed")):
            with self.assertRaises(crawler.CrawlerError):
                crawler.resolve_media(crawler.build_opener(), videos, timeout=1, delay=0, user_agent="test", concurrency=2)

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
            with mock.patch.object(download.os, "replace", side_effect=replace_with_cross_device_error):
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
            ]), encoding="utf-8")

            event = task_history.latest_successful_daily_run(history, today=datetime.date(2026, 7, 20))

            self.assertIsNotNone(event)
            self.assertEqual(event["timestamp"], "2026-07-20T10:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
