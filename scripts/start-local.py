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

from task_lock import lock_is_active, read_lock, update_lock
from task_history import latest_successful_daily_run

PROJECT = Path(__file__).resolve().parents[1]
REFRESH = PROJECT / "scripts" / "refresh-monitor.py"


def snapshot_has_pending_work(path: Path) -> bool:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        overview = snapshot.get("overview", {})
        return (
            int(overview.get("pendingVideos") or 0) > 0
            or int(overview.get("partialDownloads") or 0) > 0
            or int(overview.get("resolvedVideos") or 0) < int(overview.get("uniqueVideos") or 0)
        )
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def daily_task_completed() -> bool:
    completed = latest_successful_daily_run(PROJECT / "data" / "run-history.jsonl")
    return bool(completed) and not snapshot_has_pending_work(PROJECT / "public" / "status.json")


class TaskHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    start_guard = threading.Lock()
    start_pending = False

    @classmethod
    def clear_start_pending_when_done(cls, process: subprocess.Popen) -> None:
        process.wait()
        with cls.start_guard:
            cls.start_pending = False

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

    def do_POST(self):
        if self.headers.get("Origin") and not self.allowed_origin():
            self.send_json(403, {"error": "拒绝非本地页面发起任务"})
            return
        request_path = urlsplit(self.path).path
        if request_path == "/api/task":
            lock_path = PROJECT / "data" / ".crawling.lock"
            with self.start_guard:
                if type(self).start_pending or lock_is_active(lock_path):
                    self.send_json(409, {"error": "任务已在后台运行", "code": "task_running"})
                    return
                if daily_task_completed():
                    self.send_json(409, {"error": "今日任务已完成，无需重复运行", "code": "completed_today"})
                    return
                log_dir = PROJECT / "data" / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
                log_path = log_dir / f"api-task-{stamp}.log"
                try:
                    with log_path.open("w") as log_file:
                        proc = subprocess.Popen(
                            [sys.executable, str(PROJECT / "scripts" / "run-daily-crawl.py"), "--download"],
                            cwd=PROJECT, start_new_session=True,
                            stdout=log_file, stderr=subprocess.STDOUT,
                        )
                except OSError:
                    self.send_json(500, {"error": "无法启动今日任务"})
                    return
                type(self).start_pending = True
                threading.Thread(target=self.clear_start_pending_when_done, args=(proc,), daemon=True).start()
                os.chmod(log_path, 0o600)
            self.send_json(202, {"success": True, "pid": proc.pid})
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
            self.send_json(200, {"success": True, "state": action})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/api/health":
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

    def do_OPTIONS(self):
        origin = self.allowed_origin()
        if self.headers.get("Origin") and not origin:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        if origin:
            self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.end_headers()

    def log_message(self, format, *args):
        pass


def watcher(stop: threading.Event) -> None:
    data_dir = PROJECT / "data"
    while not stop.is_set():
        subprocess.run([sys.executable, str(REFRESH)], cwd=PROJECT, check=False)
        # Active tasks refresh quickly; terminal progress snapshots stay persistent without causing a busy loop.
        is_active = lock_is_active(data_dir / ".crawling.lock")
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
        stop.wait(interval)


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
