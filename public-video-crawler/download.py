#!/usr/bin/env python3
"""Download public media with safe resume, retries, and live progress."""

from __future__ import annotations

import argparse
import concurrent.futures
import errno
import hashlib
import http.client
import ipaddress
import json
import os
import random
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path


USER_AGENT = "Mozilla/5.0 (compatible; PublicVideoIndexer/1.0)"
CHUNK_SIZE = 1024 * 1024
SYNC_INTERVAL = 64 * 1024 * 1024
EXPIRED_STATUS_CODES = {401, 403, 404, 410, 416}
TRANSIENT_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}

_dl_progress_lock = threading.RLock()
_dl_progress_path: Path | None = None
_dl_progress_total = 0
_dl_progress_done = 0
_dl_progress_active: dict[str, dict[str, object]] = {}
_dl_progress_completed_bytes = 0
_dl_progress_started_at = 0.0
_dl_progress_stage = "idle"
_dl_progress_concurrency = 1
_dl_progress_failed = 0


class DownloadError(RuntimeError):
    pass


class ExpiredMediaError(DownloadError):
    pass


class RestartDownload(DownloadError):
    pass


class TransientDownloadError(DownloadError):
    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def is_media_mismatch_error(exc: BaseException) -> bool:
    return "详情页媒体与榜单不一致" in str(exc)


class RateLimiter:
    def __init__(self, interval: float) -> None:
        self.interval = max(0.0, interval)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self.interval
        if delay:
            time.sleep(delay)


class ContentHistory:
    """Thread-safe history of file hashes that have already reached staging."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            hashes = payload.get("hashes") if isinstance(payload, dict) else None
            if isinstance(hashes, dict):
                self._items = {
                    str(digest): value
                    for digest, value in hashes.items()
                    if re.fullmatch(r"[0-9a-f]{64}", str(digest)) and isinstance(value, dict)
                }
                return
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        self._bootstrap_from_logs()

    def _bootstrap_from_logs(self) -> None:
        if self.path is None:
            return
        log_dir = self.path.parent / "logs"
        for log_path in sorted(log_dir.glob("*download-*.log")):
            try:
                lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                digest = str(row.get("sha256") or "") if isinstance(row, dict) else ""
                viewkey = str(row.get("viewkey") or "") if isinstance(row, dict) else ""
                if not re.fullmatch(r"[0-9a-f]{64}", digest) or not viewkey:
                    continue
                self._items.setdefault(digest, {
                    "viewkey": viewkey,
                    "bytes": int(row.get("bytes") or 0),
                    "recordedAt": str(row.get("recordedAt") or ""),
                })
        if self._items:
            self._persist()

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "hashes": self._items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    def claim(self, digest: str, viewkey: str, total_bytes: int) -> str | None:
        """Reserve a content hash, returning the first viewkey when it is duplicate."""
        with self._lock:
            existing = self._items.get(digest)
            if existing:
                original = str(existing.get("viewkey") or "")
                return original if original and original != viewkey else None
            self._items[digest] = {
                "viewkey": viewkey,
                "bytes": total_bytes,
                "recordedAt": _iso_now(),
            }
            self._persist()
            return None


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _progress_snapshot() -> dict[str, object]:
    active = list(_dl_progress_active.values())
    active_done = sum(int(item.get("bytesDone") or 0) for item in active)
    active_total = sum(int(item.get("bytesTotal") or 0) for item in active)
    speed = sum(float(item.get("speedBytesS") or 0) for item in active)
    known_remaining = max(0, active_total - active_done)
    elapsed = max(0.0, time.monotonic() - _dl_progress_started_at) if _dl_progress_started_at else 0.0
    remaining_items = max(0, _dl_progress_total - _dl_progress_done)
    item_eta = 0.0
    if _dl_progress_done and elapsed:
        item_eta = elapsed / _dl_progress_done * remaining_items / max(1, _dl_progress_concurrency)
    byte_eta = known_remaining / speed if speed > 0 and active_total else 0.0
    eta = byte_eta or item_eta
    return {
        "total": _dl_progress_total,
        "done": _dl_progress_done,
        "stage": _dl_progress_stage,
        "active": active,
        "bytesDone": _dl_progress_completed_bytes + active_done,
        "bytesTotalKnown": _dl_progress_completed_bytes + active_total,
        "knownItems": _dl_progress_done + sum(1 for item in active if int(item.get("bytesTotal") or 0) > 0),
        "failed": _dl_progress_failed,
        "speedBytesS": round(speed, 1),
        "etaSeconds": round(eta) if eta > 0 else None,
        "startedAt": datetime.fromtimestamp(time.time() - elapsed).astimezone().isoformat(timespec="seconds") if _dl_progress_started_at else None,
        "updatedAt": _iso_now(),
    }


def _persist_dl_progress() -> None:
    if _dl_progress_path is None:
        return
    _dl_progress_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _dl_progress_path.with_suffix(_dl_progress_path.suffix + ".tmp")
    temporary.write_text(json.dumps(_progress_snapshot(), ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, _dl_progress_path)


def set_dl_progress_target(path: Path | None, total: int, concurrency: int = 1) -> None:
    global _dl_progress_path, _dl_progress_total, _dl_progress_done
    global _dl_progress_active, _dl_progress_completed_bytes, _dl_progress_started_at
    global _dl_progress_stage, _dl_progress_concurrency
    global _dl_progress_failed
    with _dl_progress_lock:
        _dl_progress_path = path
        _dl_progress_total = total
        _dl_progress_done = 0
        _dl_progress_active = {}
        _dl_progress_completed_bytes = 0
        _dl_progress_started_at = time.monotonic()
        _dl_progress_stage = "downloading"
        _dl_progress_concurrency = max(1, concurrency)
        _dl_progress_failed = 0
        _persist_dl_progress()


def _update_dl_item(payload: dict[str, object]) -> None:
    viewkey = str(payload.get("viewkey") or "")
    if not viewkey:
        return
    with _dl_progress_lock:
        previous = _dl_progress_active.get(viewkey, {})
        _dl_progress_active[viewkey] = {**previous, **payload}
        _persist_dl_progress()


def _write_dl_progress(viewkey: str, result: dict[str, object] | None = None) -> None:
    global _dl_progress_done, _dl_progress_completed_bytes, _dl_progress_failed
    with _dl_progress_lock:
        active = _dl_progress_active.pop(viewkey, {})
        _dl_progress_done += 1
        result_bytes = int((result or {}).get("bytes") or active.get("bytesDone") or 0)
        _dl_progress_completed_bytes += max(0, result_bytes)
        if (result or {}).get("status") == "failed":
            _dl_progress_failed += 1
        _persist_dl_progress()


def finish_dl_progress(failed: bool = False) -> None:
    global _dl_progress_stage
    with _dl_progress_lock:
        _dl_progress_active.clear()
        _dl_progress_stage = "failed" if failed else "complete"
        _persist_dl_progress()


def public_https_url(raw: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise DownloadError("媒体 URL 无效") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
        raise DownloadError("媒体 URL 必须是不含用户凭据的 HTTPS URL")
    if parsed.hostname.lower() == "localhost":
        raise DownloadError("拒绝本地媒体地址")
    try:
        if ipaddress.ip_address(parsed.hostname).is_private:
            raise DownloadError("拒绝私网媒体地址")
    except ValueError:
        pass
    return raw


def ensure_public_endpoint(raw: str) -> str:
    """Resolve a media endpoint and reject every non-global address."""
    public_https_url(raw)
    parsed = urllib.parse.urlsplit(raw)
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise DownloadError("媒体 URL 端口无效") from exc
    try:
        results = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DownloadError("媒体域名解析失败") from exc
    addresses = {
        str(sockaddr[0]).split("%", 1)[0]
        for _family, _type, _proto, _canonname, sockaddr in results
        if sockaddr
    }
    if not addresses:
        raise DownloadError("媒体域名没有可用地址")
    for address in addresses:
        try:
            endpoint = ipaddress.ip_address(address)
        except ValueError as exc:
            raise DownloadError("媒体域名返回了无效地址") from exc
        if not endpoint.is_global:
            raise DownloadError("拒绝非公网媒体地址")
    return raw


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        ensure_public_endpoint(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_download_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(SafeRedirect())


def load_items(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    videos = data.get("videos")
    if not isinstance(videos, list):
        raise DownloadError("JSON 缺少 videos 数组")
    output: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    seen_urls: set[str] = set()
    for raw in videos:
        if not isinstance(raw, dict):
            continue
        viewkey = str(raw.get("viewkey", ""))
        media_url = str(raw.get("media_url", ""))
        canonical_url = str(raw.get("canonical_url", ""))
        if not viewkey or not media_url or viewkey in seen_keys or media_url in seen_urls:
            continue
        if not all(char.isalnum() or char in "_-" for char in viewkey) or len(viewkey) > 64:
            raise DownloadError("viewkey 包含非法字符")
        public_https_url(media_url)
        if canonical_url:
            parsed = urllib.parse.urlsplit(canonical_url)
            if parsed.scheme != "https" or parsed.hostname not in {"91porn.com", "www.91porn.com"}:
                raise DownloadError("canonical_url 超出授权站点范围")
        output.append({
            "viewkey": viewkey,
            "media_url": media_url,
            "canonical_url": canonical_url,
            "thumbnail_url": str(raw.get("thumbnail_url") or ""),
            "resolved_at": str(raw.get("resolved_at") or ""),
        })
        seen_keys.add(viewkey)
        seen_urls.add(media_url)
    return output


def extension_for(url: str) -> str:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".mp4", ".m4v", ".webm", ".ts"} else ".mp4"


def _meta_path(partial: Path) -> Path:
    return partial.with_suffix(partial.suffix + ".meta.json")


def _load_meta(partial: Path) -> dict[str, object]:
    try:
        payload = json.loads(_meta_path(partial).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


def _write_meta(partial: Path, payload: dict[str, object]) -> None:
    path = _meta_path(partial)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _reset_partial(partial: Path) -> None:
    partial.unlink(missing_ok=True)
    _meta_path(partial).unlink(missing_ok=True)


def _finalize_partial(partial: Path, final: Path) -> None:
    """Move a completed part atomically, copying when Docker mounts differ."""
    try:
        os.replace(partial, final)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        temporary = final.with_name(f".{final.name}.{os.getpid()}.finalizing")
        temporary.unlink(missing_ok=True)
        try:
            with partial.open("rb") as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, CHUNK_SIZE)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, final)
            partial.unlink()
        finally:
            temporary.unlink(missing_ok=True)
    _meta_path(partial).unlink(missing_ok=True)


def _parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value or "", re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), 0 if match.group(3) == "*" else int(match.group(3))


def _unsatisfied_total(value: str | None) -> int:
    match = re.fullmatch(r"bytes\s+\*/(\d+)", value or "", re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _backoff(attempt: int, retry_after: float = 0.0) -> float:
    return retry_after or min(30.0, (2 ** attempt) + random.uniform(0.0, 0.75))


def _report(
    callback: Callable[[dict[str, object]], None] | None,
    item: dict[str, str],
    final: Path,
    *,
    state: str,
    bytes_done: int = 0,
    bytes_total: int = 0,
    speed: float = 0.0,
    attempt: int = 0,
    retries: int = 0,
    message: str = "",
    resumed: bool = False,
) -> None:
    if not callback:
        return
    remaining = max(0, bytes_total - bytes_done)
    callback({
        "viewkey": item["viewkey"],
        "fileName": final.name,
        "bytesDone": bytes_done,
        "bytesTotal": bytes_total,
        "speedBytesS": round(speed, 1),
        "etaSeconds": round(remaining / speed) if speed > 0 and bytes_total else None,
        "state": state,
        "attempt": attempt,
        "retries": retries,
        "message": message,
        "resumed": resumed,
    })


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def _finalize_verified_partial(
    partial: Path,
    final: Path,
    item: dict[str, str],
    started_at: float,
    resumed: bool,
    content_history: ContentHistory | None,
) -> dict[str, object]:
    digest, total_bytes = _hash_file(partial)
    duplicate_of = content_history.claim(digest, item["viewkey"], total_bytes) if content_history else None
    elapsed = max(0.001, time.monotonic() - started_at)
    if duplicate_of:
        _reset_partial(partial)
        return {
            "viewkey": item["viewkey"],
            "status": "duplicate",
            "duplicate_of": duplicate_of,
            "bytes": total_bytes,
            "sha256": digest,
            "duration_s": round(elapsed, 1),
            "speed_bytes_s": round(total_bytes / elapsed, 1),
            "resumed": resumed,
        }
    _finalize_partial(partial, final)
    return {
        "viewkey": item["viewkey"],
        "status": "downloaded",
        "path": str(final),
        "bytes": total_bytes,
        "sha256": digest,
        "duration_s": round(elapsed, 1),
        "speed_bytes_s": round(total_bytes / elapsed, 1),
        "resumed": resumed,
    }


def download_one(
    opener: urllib.request.OpenerDirector,
    item: dict[str, str],
    output_dir: Path,
    partial_dir: Path,
    *,
    timeout: float,
    max_bytes: int,
    retries: int = 5,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    rate_limiter: RateLimiter | None = None,
    content_history: ContentHistory | None = None,
) -> dict[str, object]:
    final = output_dir / f"{item['viewkey']}{extension_for(item['media_url'])}"
    partial = partial_dir / f"{final.name}.part"
    started_at = time.monotonic()
    if final.exists() and final.stat().st_size > 0:
        return {"viewkey": item["viewkey"], "status": "skipped", "path": str(final), "bytes": final.stat().st_size}
    final.unlink(missing_ok=True)
    resumed_any = partial.exists() and partial.stat().st_size > 0

    attempt = 0
    while attempt <= retries:
        offset = partial.stat().st_size if partial.exists() else 0
        saved_meta = _load_meta(partial)
        if offset and (
            str(saved_meta.get("viewkey") or "") != item["viewkey"]
            or not (saved_meta.get("etag") or saved_meta.get("lastModified"))
        ):
            _report(progress_callback, item, final, state="restarting", bytes_done=offset, retries=retries, message="旧分片缺少可验证信息，安全重新下载", resumed=True)
            _reset_partial(partial)
            offset = 0
            saved_meta = {}
            resumed_any = False
        saved_total = int(saved_meta.get("totalBytes") or 0)
        if offset and saved_total == offset:
            _report(progress_callback, item, final, state="verifying", bytes_done=offset, bytes_total=offset, attempt=attempt, retries=retries, resumed=True)
            return _finalize_verified_partial(partial, final, item, started_at, True, content_history)
        headers = {"User-Agent": USER_AGENT, "Accept": "video/*,application/octet-stream"}
        if item.get("canonical_url"):
            headers["Referer"] = item["canonical_url"]
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = str(saved_meta.get("etag") or saved_meta.get("lastModified") or "")
            if validator:
                headers["If-Range"] = validator
        request = urllib.request.Request(item["media_url"], headers=headers)
        try:
            if rate_limiter:
                rate_limiter.wait()
            ensure_public_endpoint(item["media_url"])
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                response_headers = exc.headers
                exc.close()
                if status_code == 416 and offset:
                    remote_total = _unsatisfied_total(response_headers.get("Content-Range") if response_headers else None)
                    if remote_total and remote_total == offset:
                        _report(progress_callback, item, final, state="verifying", bytes_done=offset, bytes_total=offset, attempt=attempt, retries=retries, resumed=True)
                        return _finalize_verified_partial(partial, final, item, started_at, True, content_history)
                if status_code in EXPIRED_STATUS_CODES:
                    raise ExpiredMediaError(f"媒体链接失效: HTTP {status_code}") from exc
                if status_code in TRANSIENT_STATUS_CODES:
                    retry_after_raw = response_headers.get("Retry-After") if response_headers else None
                    try:
                        retry_after = min(30.0, float(retry_after_raw)) if retry_after_raw else 0.0
                    except ValueError:
                        retry_after = 0.0
                    raise TransientDownloadError(f"媒体服务暂时不可用: HTTP {status_code}", retry_after) from exc
                raise DownloadError(f"媒体请求失败: HTTP {status_code}") from exc

            with response:
                public_https_url(response.geturl())
                status = int(getattr(response, "status", response.getcode()))
                response_type = response.headers.get_content_type()
                if response_type in {"text/html", "application/json"}:
                    raise ExpiredMediaError(f"媒体响应类型异常: {response_type}")
                declared_raw = response.headers.get("Content-Length")
                declared = int(declared_raw) if declared_raw and declared_raw.isdigit() else 0
                current_etag = response.headers.get("ETag") or ""
                current_last_modified = response.headers.get("Last-Modified") or ""
                content_range = _parse_content_range(response.headers.get("Content-Range"))

                if offset and status == 206:
                    if not content_range or content_range[0] != offset:
                        raise RestartDownload("服务器返回的断点位置不一致")
                    if saved_meta.get("totalBytes") and content_range[2] and int(saved_meta["totalBytes"]) != content_range[2]:
                        raise RestartDownload("远端文件大小已改变")
                    if saved_meta.get("etag") and current_etag and saved_meta["etag"] != current_etag:
                        raise RestartDownload("远端文件 ETag 已改变")
                    if saved_meta.get("lastModified") and current_last_modified and saved_meta["lastModified"] != current_last_modified:
                        raise RestartDownload("远端文件修改时间已改变")
                    expected_total = content_range[2] or offset + declared
                    mode = "ab"
                elif status == 206:
                    if not content_range or content_range[0] != 0:
                        raise DownloadError("服务器返回了无法使用的局部响应")
                    expected_total = content_range[2] or declared
                    mode = "wb"
                else:
                    offset = 0
                    expected_total = declared
                    mode = "wb"

                if expected_total and expected_total > max_bytes:
                    raise DownloadError("媒体超过 --max-bytes 限制")
                free_bytes = shutil.disk_usage(partial_dir).free
                required_bytes = max(0, expected_total - offset) if expected_total else 0
                if required_bytes and required_bytes + 64 * 1024 * 1024 > free_bytes:
                    raise DownloadError("磁盘剩余空间不足")

                _write_meta(partial, {
                    "viewkey": item["viewkey"],
                    "canonicalUrl": item.get("canonical_url", ""),
                    "mediaUrlHash": hashlib.sha256(item["media_url"].encode("utf-8")).hexdigest(),
                    "etag": current_etag,
                    "lastModified": current_last_modified,
                    "totalBytes": expected_total,
                    "updatedAt": _iso_now(),
                })
                total = offset
                last_report = time.monotonic()
                last_bytes = offset
                smoothed_speed = 0.0
                next_sync = total + SYNC_INTERVAL
                _report(progress_callback, item, final, state="resuming" if offset else "downloading", bytes_done=total, bytes_total=expected_total, attempt=attempt, retries=retries, resumed=bool(offset))
                with partial.open(mode) as handle:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        handle.write(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise DownloadError("媒体超过 --max-bytes 限制")
                        now = time.monotonic()
                        if total >= next_sync:
                            handle.flush()
                            os.fsync(handle.fileno())
                            next_sync = total + SYNC_INTERVAL
                        if now - last_report >= 0.5:
                            interval = now - last_report
                            instant_speed = (total - last_bytes) / interval if interval > 0 else 0.0
                            smoothed_speed = instant_speed if smoothed_speed <= 0 else smoothed_speed * 0.75 + instant_speed * 0.25
                            _report(progress_callback, item, final, state="resuming" if offset else "downloading", bytes_done=total, bytes_total=expected_total, speed=smoothed_speed, attempt=attempt, retries=retries, resumed=bool(offset))
                            last_report = now
                            last_bytes = total
                    handle.flush()
                    os.fsync(handle.fileno())
                if expected_total and total != expected_total:
                    raise TransientDownloadError(f"下载不完整: {total}/{expected_total} 字节")
                _report(progress_callback, item, final, state="verifying", bytes_done=total, bytes_total=expected_total or total, speed=smoothed_speed, attempt=attempt, retries=retries, resumed=resumed_any)
                return _finalize_verified_partial(partial, final, item, started_at, resumed_any, content_history)
        except RestartDownload as exc:
            _report(progress_callback, item, final, state="restarting", bytes_done=offset, attempt=attempt, retries=retries, message=str(exc), resumed=True)
            _reset_partial(partial)
            resumed_any = False
            continue
        except ExpiredMediaError:
            raise
        except (TransientDownloadError, urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, http.client.IncompleteRead) as exc:
            if attempt >= retries:
                raise DownloadError(f"重试 {retries} 次后仍失败: {exc}") from exc
            retry_after = exc.retry_after if isinstance(exc, TransientDownloadError) else 0.0
            wait_seconds = _backoff(attempt, retry_after)
            _report(progress_callback, item, final, state="retrying", bytes_done=partial.stat().st_size if partial.exists() else 0, bytes_total=int(_load_meta(partial).get("totalBytes") or 0), attempt=attempt + 1, retries=retries, message=f"{wait_seconds:.1f} 秒后重试", resumed=partial.exists())
            print(f"[{item['viewkey']}] 网络异常: {exc}; {wait_seconds:.1f}s 后重试 ({attempt + 1}/{retries})", file=sys.stderr)
            time.sleep(wait_seconds)
            attempt += 1
    raise DownloadError("下载未完成")


def _resolved_age_seconds(value: str) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0.0, (datetime.now().astimezone() - parsed).total_seconds())
    except ValueError:
        return None


def refresh_media(item: dict[str, str], timeout: float, retries: int, progress_callback) -> None:
    import crawler

    final = Path(f"{item['viewkey']}{extension_for(item['media_url'])}")
    _report(progress_callback, item, final, state="refreshing", message="正在刷新过期链接", retries=retries)
    video = crawler.Video(
        viewkey=item["viewkey"],
        canonical_url=item.get("canonical_url") or crawler.canonical_video_url(crawler.DEFAULT_URL, item["viewkey"]),
        thumbnail_url=item.get("thumbnail_url", ""),
    )
    crawler.resolve_media(
        crawler.build_opener(),
        [video],
        timeout=timeout,
        delay=0,
        user_agent=crawler.DEFAULT_USER_AGENT,
        concurrency=1,
        retries=retries,
    )
    if not video.media_url:
        raise DownloadError("重新解析后仍未找到媒体地址")
    item["media_url"] = video.media_url
    item["resolved_at"] = video.resolved_at or _iso_now()


def update_media_cache(path: Path | None, items: list[dict[str, str]], blocked_keys: set[str] | None = None) -> None:
    if path is None or not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    videos = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(videos, list):
        return
    refreshed = {item["viewkey"]: item for item in items if item.get("resolved_at")}
    blocked = blocked_keys or set()
    for video in videos:
        if not isinstance(video, dict):
            continue
        viewkey = str(video.get("viewkey") or "")
        if viewkey in blocked:
            video["media_url"] = ""
            video["resolved_at"] = ""
        elif viewkey in refreshed:
            source = refreshed[viewkey]
            video["media_url"] = source["media_url"]
            video["resolved_at"] = source["resolved_at"]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="并发下载 crawler.py 解析出的公开媒体")
    parser.add_argument("input", type=Path, help="videos-with-media.json")
    parser.add_argument("--output-dir", type=Path, default=Path("downloads"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--success-history", type=Path)
    parser.add_argument("--content-history", type=Path, help="已成功内容的 SHA-256 索引，用于拦截换编号的重复视频")
    parser.add_argument("--media-cache", type=Path, help="持久化刷新后的媒体链接")
    parser.add_argument("--blocked-history", type=Path, help="持久化已确认的错误媒体身份")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=0.5, help="新请求启动的最小间隔")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--max-bytes", type=int, default=4 * 1024 * 1024 * 1024)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--link-max-age", type=float, default=180.0, help="超过该秒数后下载前刷新媒体链接")
    parser.add_argument("--progress", type=Path)
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit 不能为负数")
    if not 0 <= args.delay <= 60:
        parser.error("--delay 必须在 0 到 60 秒之间")
    if args.max_bytes < 1024 * 1024:
        parser.error("--max-bytes 至少为 1 MiB")
    if not 1 <= args.concurrency <= 16:
        parser.error("--concurrency 必须在 1 到 16 之间")
    if not 0 <= args.retries <= 10:
        parser.error("--retries 必须在 0 到 10 之间")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    items = load_items(args.input)
    if args.limit:
        items = items[:args.limit]
    manifest = args.manifest or args.input.resolve().parent / "download-manifest.json"
    partial_dir = args.work_dir or manifest.parent / "partials"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    partial_dir.mkdir(parents=True, exist_ok=True)
    set_dl_progress_target(args.progress, len(items), args.concurrency)

    results_by_key: dict[str, dict[str, object]] = {}
    results_lock = threading.Lock()
    limiter = RateLimiter(args.delay)
    content_history = ContentHistory(args.content_history)

    def process_item(item: dict[str, str]) -> None:
        opener = build_download_opener()
        result: dict[str, object]
        try:
            age = _resolved_age_seconds(item.get("resolved_at", ""))
            if item.get("canonical_url") and age is not None and age > args.link_max_age:
                refresh_media(item, args.timeout, args.retries, _update_dl_item)
            try:
                result = download_one(opener, item, args.output_dir, partial_dir, timeout=args.timeout, max_bytes=args.max_bytes, retries=args.retries, progress_callback=_update_dl_item, rate_limiter=limiter, content_history=content_history)
            except ExpiredMediaError:
                refresh_media(item, args.timeout, args.retries, _update_dl_item)
                result = download_one(build_download_opener(), item, args.output_dir, partial_dir, timeout=args.timeout, max_bytes=args.max_bytes, retries=args.retries, progress_callback=_update_dl_item, rate_limiter=limiter, content_history=content_history)
        except Exception as exc:
            mismatch = is_media_mismatch_error(exc)
            result = {
                "viewkey": item.get("viewkey"),
                "status": "blocked" if mismatch else "failed",
                "error": str(exc),
            }
            if mismatch:
                item["media_url"] = ""
                item["resolved_at"] = ""
            print(f"[{item.get('viewkey')}] error: {exc}", file=sys.stderr)
        _write_dl_progress(item["viewkey"], result)
        with results_lock:
            results_by_key[item["viewkey"]] = result
            print(json.dumps(result, ensure_ascii=False), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(process_item, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            future.result()

    results = [results_by_key[item["viewkey"]] for item in items]
    failed = any(result.get("status") == "failed" for result in results)
    blocked_keys = {
        str(result.get("viewkey") or "")
        for result in results
        if result.get("status") == "blocked"
    }
    blocked_keys.discard("")
    finish_dl_progress(failed)
    temporary = manifest.with_suffix(manifest.suffix + ".tmp")
    temporary.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, manifest)
    update_media_cache(args.media_cache, items, blocked_keys)
    if blocked_keys and args.blocked_history:
        import crawler

        videos = [
            crawler.Video(
                viewkey=item["viewkey"],
                canonical_url=item.get("canonical_url", ""),
                thumbnail_url=item.get("thumbnail_url", ""),
            )
            for item in items
            if item["viewkey"] in blocked_keys
        ]
        failures = [
            {
                "viewkey": str(result.get("viewkey") or ""),
                "kind": "media_mismatch",
                "error": str(result.get("error") or ""),
            }
            for result in results
            if result.get("status") == "blocked"
        ]
        crawler.update_blocked_media_history(args.blocked_history, videos, failures)

    if args.success_history:
        existing = set()
        if args.success_history.exists():
            existing = {line.strip() for line in args.success_history.read_text(encoding="utf-8").splitlines() if line.strip()}
        existing.update(str(result["viewkey"]) for result in results if result.get("status") in {"downloaded", "skipped", "duplicate"} and result.get("viewkey"))
        args.success_history.parent.mkdir(parents=True, exist_ok=True)
        temporary_history = args.success_history.with_suffix(args.success_history.suffix + ".tmp")
        temporary_history.write_text("".join(f"{key}\n" for key in sorted(existing)), encoding="utf-8")
        os.chmod(temporary_history, 0o600)
        os.replace(temporary_history, args.success_history)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (DownloadError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
