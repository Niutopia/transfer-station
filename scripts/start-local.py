#!/usr/bin/env python3
"""Refresh dashboard data in the background while the local site is running."""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import http.server
import socketserver
import json
import os
import signal
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from source_config import SourceConfigError, add_source, load_source_config, remove_source
from task_lock import lock_is_active, read_lock, update_lock

PROJECT = Path(__file__).resolve().parents[1]
REFRESH = PROJECT / "scripts" / "refresh-monitor.py"
SOURCE_CONFIG = PROJECT / "config" / "daily-sources.json"
SNAPSHOT_WAKEUP = threading.Event()

class TaskHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    start_guard = threading.Lock()
    source_guard = threading.Lock()
    start_pending = False

    @classmethod
    def clear_start_pending_when_done(cls, process: subprocess.Popen) -> None:
        process.wait()
        with cls.start_guard:
            cls.start_pending = False
        SNAPSHOT_WAKEUP.set()

    def allowed_origin(self) -> str | None:
        origin = self.headers.get("Origin")
        if not origin:
            return None
        try:
            parsed = urlsplit(origin)
        except ValueError:
            return ""
        return origin if parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"} else ""

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        origin = self.allowed_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self, maximum: int = 4096) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise SourceConfigError("请求长度无效") from exc
        if length <= 0 or length > maximum:
            raise SourceConfigError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SourceConfigError("请求内容格式无效") from exc
        if not isinstance(payload, dict):
            raise SourceConfigError("请求内容格式无效")
        return payload

    def source_edit_blocked(self) -> bool:
        return type(self).start_pending or lock_is_active(PROJECT / "data" / ".crawling.lock")

    def launch_task(self, command: list[str], log_prefix: str) -> subprocess.Popen | None:
        log_dir = PROJECT / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"{log_prefix}-{stamp}.log"
        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(
                    command,
                    cwd=PROJECT,
                    start_new_session=True,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
        except OSError:
            return None
        type(self).start_pending = True
        threading.Thread(target=self.clear_start_pending_when_done, args=(process,), daemon=True).start()
        SNAPSHOT_WAKEUP.set()
        return process

    def do_POST(self):
        if self.headers.get("Origin") and not self.allowed_origin():
            self.send_json(403, {"error": "拒绝非本地页面发起任务"})
            return
        request_path = urlsplit(self.path).path
        if request_path == "/api/sources":
            try:
                body = self.read_json_body()
                with self.start_guard:
                    if self.source_edit_blocked():
                        self.send_json(409, {"error": "任务运行中，暂时不能修改抓取链接"})
                        return
                    with self.source_guard:
                        sources = add_source(SOURCE_CONFIG, str(body.get("name") or ""), str(body.get("url") or ""))
            except SourceConfigError as exc:
                self.send_json(400, {"error": str(exc)})
                return
            SNAPSHOT_WAKEUP.set()
            self.send_json(201, {"success": True, "sources": sources})
        elif request_path == "/api/task":
            lock_path = PROJECT / "data" / ".crawling.lock"
            with self.start_guard:
                if type(self).start_pending or lock_is_active(lock_path):
                    self.send_json(409, {"error": "任务已在后台运行", "code": "task_running"})
                    return
                try:
                    source_config = load_source_config(SOURCE_CONFIG)
                except SourceConfigError as exc:
                    self.send_json(500, {"error": str(exc), "code": "source_config_invalid"})
                    return
                configured_sources = source_config.get("sources")
                if not isinstance(configured_sources, list) or not configured_sources:
                    self.send_json(409, {"error": "请先添加至少一个抓取链接", "code": "no_sources"})
                    return
                proc = self.launch_task(
                    [sys.executable, str(PROJECT / "scripts" / "run-daily-crawl.py"), "--download"],
                    "api-crawl",
                )
                if proc is None:
                    self.send_json(500, {"error": "无法启动今日任务"})
                    return
            self.send_json(202, {"success": True, "pid": proc.pid})
        elif request_path == "/api/task/repair":
            lock_path = PROJECT / "data" / ".crawling.lock"
            with self.start_guard:
                if type(self).start_pending or lock_is_active(lock_path):
                    self.send_json(409, {"error": "任务已在后台运行", "code": "task_running"})
                    return
                proc = self.launch_task(
                    [sys.executable, str(PROJECT / "scripts" / "repair_pending.py")],
                    "api-repair",
                )
                if proc is None:
                    self.send_json(500, {"error": "无法启动失败项修复"})
                    return
            self.send_json(202, {"success": True, "pid": proc.pid, "mode": "repair"})
        elif request_path == "/api/task/control":
            lock_path = PROJECT / "data" / ".crawling.lock"
            if not lock_is_active(lock_path):
                self.send_json(409, {"error": "当前没有运行中的任务"})
                return
            try:
                length = min(1024, int(self.headers.get("Content-Length") or 0))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (ValueError, UnicodeError, json.JSONDecodeError):
                self.send_json(400, {"error": "控制请求无效"})
                return
            action = str(body.get("action") or "") if isinstance(body, dict) else ""
            payload = read_lock(lock_path)
            if not payload.get("controllable") or not payload.get("pgid"):
                self.send_json(409, {"error": "该任务不是由 Web 启动，无法远程控制"})
                return
            pgid = int(payload["pgid"])
            try:
                if action == "pause":
                    update_lock(lock_path, state="paused")
                    os.killpg(pgid, signal.SIGSTOP)
                elif action == "resume":
                    os.killpg(pgid, signal.SIGCONT)
                    update_lock(lock_path, state="running")
                elif action == "cancel":
                    update_lock(lock_path, state="cancelling")
                    if payload.get("state") == "paused":
                        os.killpg(pgid, signal.SIGCONT)
                    os.killpg(pgid, signal.SIGTERM)
                    for name in ("crawl-progress.json", "download-progress.json"):
                        progress_path = PROJECT / "data" / name
                        try:
                            progress = json.loads(progress_path.read_text(encoding="utf-8"))
                            if isinstance(progress, dict) and progress.get("stage") not in {"complete", "failed"}:
                                progress.update({"stage": "cancelled", "active": [], "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds")})
                                temporary = progress_path.with_suffix(progress_path.suffix + ".tmp")
                                temporary.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")
                                os.chmod(temporary, 0o600)
                                os.replace(temporary, progress_path)
                        except (OSError, json.JSONDecodeError):
                            continue
                else:
                    self.send_json(400, {"error": "只支持 pause、resume 或 cancel"})
                    return
            except (ProcessLookupError, PermissionError, OSError):
                self.send_json(409, {"error": "任务进程已经结束或无法控制"})
                return
            SNAPSHOT_WAKEUP.set()
            self.send_json(200, {"success": True, "state": action})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/sources":
            try:
                config = load_source_config(SOURCE_CONFIG)
            except SourceConfigError as exc:
                self.send_json(500, {"error": str(exc)})
                return
            self.send_json(200, config)
        elif request_path == "/api/health":
            lock_path = PROJECT / "data" / ".crawling.lock"
            status_path = PROJECT / "public" / "status.json"
            lock_payload = read_lock(lock_path) if lock_is_active(lock_path) else {}
            self.send_json(200, {
                "status": "ok",
                "taskRunning": bool(lock_payload),
                "taskState": lock_payload.get("state") if lock_payload else None,
                "snapshotReady": status_path.exists(),
            })
        elif request_path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            origin = self.allowed_origin()
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            status_path = PROJECT / "public" / "status.json"
            last_marker = -1
            last_keepalive = 0.0
            try:
                while True:
                    try:
                        marker = status_path.stat().st_mtime_ns
                        if marker != last_marker:
                            payload = json.loads(status_path.read_text(encoding="utf-8"))
                            message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                            self.wfile.write(f"event: status\ndata: {message}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            last_marker = marker
                            last_keepalive = time.monotonic()
                    except (OSError, json.JSONDecodeError):
                        pass
                    if time.monotonic() - last_keepalive >= 15:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_keepalive = time.monotonic()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self.send_json(404, {"error": "Not found"})

    def do_DELETE(self):
        if self.headers.get("Origin") and not self.allowed_origin():
            self.send_json(403, {"error": "拒绝非本地页面修改配置"})
            return
        if urlsplit(self.path).path != "/api/sources":
            self.send_json(404, {"error": "Not found"})
            return
        try:
            body = self.read_json_body()
            with self.start_guard:
                if self.source_edit_blocked():
                    self.send_json(409, {"error": "任务运行中，暂时不能修改抓取链接"})
                    return
                with self.source_guard:
                    sources = remove_source(SOURCE_CONFIG, str(body.get("url") or ""))
        except SourceConfigError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        SNAPSHOT_WAKEUP.set()
        self.send_json(200, {"success": True, "sources": sources})

    def do_OPTIONS(self):
        origin = self.allowed_origin()
        if self.headers.get("Origin") and not origin:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.end_headers()

    def log_message(self, format, *args):
        pass


def watcher(stop: threading.Event) -> None:
    data_dir = PROJECT / "data"
    while not stop.is_set():
        subprocess.run([sys.executable, str(REFRESH)], cwd=PROJECT, check=False)
        # Active tasks refresh quickly; terminal progress snapshots stay persistent without causing a busy loop.
        is_active = TaskHandler.start_pending or lock_is_active(data_dir / ".crawling.lock")
        if not is_active:
            for name in ("crawl-progress.json", "download-progress.json"):
                try:
                    progress = json.loads((data_dir / name).read_text(encoding="utf-8"))
                    if progress.get("stage") not in {"complete", "failed", "cancelled"}:
                        is_active = True
                        break
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue
        interval = 1 if is_active else 10
        SNAPSHOT_WAKEUP.wait(interval)
        SNAPSHOT_WAKEUP.clear()


class TaskServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def api_server(stop: threading.Event, host: str, port: int) -> None:
    with TaskServer((host, port), TaskHandler) as httpd:
        httpd.timeout = 1
        while not stop.is_set():
            httpd.handle_request()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 Transfer station 本地服务")
    parser.add_argument("--services-only", action="store_true", help="仅运行状态刷新和任务 API")
    parser.add_argument("--host", default=os.environ.get("MONITOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MONITOR_PORT", "3001")))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stop = threading.Event()
    thread_watcher = threading.Thread(target=watcher, args=(stop,), daemon=True)
    thread_api = threading.Thread(target=api_server, args=(stop, args.host, args.port), daemon=True)
    thread_watcher.start()
    thread_api.start()
    try:
        if args.services_only:
            while not stop.wait(1):
                pass
            return 0
        return subprocess.run(["npm", "run", "dev"], cwd=PROJECT, check=False).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        stop.set()
        thread_watcher.join(timeout=2)
        thread_api.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
