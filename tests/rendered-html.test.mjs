import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the local monitor shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>Transfer station<\/title>/i);
  assert.match(html, /正在读取 Transfer station 状态/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("monitor snapshot is real, local, and internally consistent", async () => {
  const status = JSON.parse(await readFile(new URL("../public/status.json", import.meta.url), "utf8"));
  assert.equal(status.source.stagingPath.endsWith("/中转站"), true);
  assert.equal(status.overview.uniqueVideos >= 0, true);
  assert.equal(status.overview.resolvedVideos <= status.overview.uniqueVideos, true);
  assert.equal(status.overview.downloadedVideos <= status.overview.totalFiles, true);
  assert.equal(status.overview.pendingVideos, status.overview.uniqueVideos - status.overview.downloadedVideos);
  assert.equal(status.overview.rawLinks - status.overview.uniqueVideos, status.overview.duplicatesRemoved);
  assert.equal(status.daily.length, 14);
  assert.equal(Array.isArray(status.recentFiles), true);
  assert.equal(Object.hasOwn(status, "currentProgress"), true);
  assert.equal(Object.hasOwn(status, "lastProgress"), true);
  assert.equal(typeof status.latestRun.result, "object");
  assert.equal(status.latestRun.result.rawLinks >= status.latestRun.result.uniqueVideos, true);
  if (status.lastProgress) {
    assert.equal(status.lastProgress.stage, "complete");
    assert.equal(status.lastProgress.done, status.lastProgress.total);
  }
  if (status.latestRun.status !== "active") {
    assert.equal(status.activeDownloads.length, 0);
    if (status.overview.partialDownloads > 0) {
      assert.equal(status.pendingDownloads.some((item) => item.status === "resumable"), true);
    }
  }
  const stagingFiles = (await readdir(new URL("../中转站/", import.meta.url))).filter(name => !name.startsWith('.'));
  assert.equal(stagingFiles.every((name) => /\.(?:mp4|m4v|webm|ts|mkv|mov|avi)$/i.test(name)), true);
  assert.equal(status.overview.totalFiles <= stagingFiles.length, true);
  const dailyConfig = JSON.parse(await readFile(new URL("../config/daily-sources.json", import.meta.url), "utf8"));
  assert.equal(Array.isArray(dailyConfig.sources), true);
  assert.equal(new Set(dailyConfig.sources.map((source) => source.url)).size, dailyConfig.sources.length);
  assert.deepEqual(status.source.sources, dailyConfig.sources);
});

test("dashboard keeps a single focused monitor and the shared favicon", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /任务监控/);
  assert.doesNotMatch(source, /采集阶段|查看阶段|className="metrics"|className="metric-details"/);
  assert.doesNotMatch(source, /视频库|role="tablist"|activeTab|selectTab|window\.location\.hash/);
  assert.doesNotMatch(source, /\{ id: "runs", label:/);
  assert.doesNotMatch(source, /className="panel run-panel"/);
  assert.match(source, /!taskAppearsActive && <button/);
  assert.match(source, /再次抓取最新内容/);
  assert.doesNotMatch(source, /payload\.code === "completed_today"|!completedToday && <button/);
  assert.match(source, /src="\/transfer-station-x\.svg\?v=1"/);
  assert.match(source, /className="brand-title">TRANSFER STATION/);
  assert.doesNotMatch(source, /className="topbar-title"/);
  assert.doesNotMatch(source, /LOCAL MEDIA NODE|className="brand-text"/);
  assert.doesNotMatch(source, /className="hero"|className="eyebrow"|className="hero-actions"/);
  assert.match(source, /任务在线/);
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(styles, /\.topbar\s*\{[^}]*grid-template-columns:\s*1fr auto/);
  assert.doesNotMatch(styles, /\.hero(?:\s|\{|:)|\.hero-actions|\.eyebrow/);
  assert.doesNotMatch(styles, /\.nav(?:\s|\{|:)/);
  const taskService = await readFile(new URL("../scripts/start-local.py", import.meta.url), "utf8");
  assert.match(taskService, /start_pending/);
  assert.match(taskService, /SNAPSHOT_WAKEUP/);
  assert.doesNotMatch(taskService, /completed_today|daily_task_completed/);
  const readme = await readFile(new URL("../README.md", import.meta.url), "utf8");
  assert.match(readme, /项目不会定时抓取/);
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.equal((layout.match(/transfer-station-x\.svg\?v=1/g) ?? []).length, 3);
});

test("dashboard uses realtime events with a polling fallback", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /new EventSource\("\/api\/events"\)/);
  assert.match(source, /实时更新中/);
  assert.match(source, /已降级为 10 秒轮询/);
  assert.match(source, /live-task-panel/);
  assert.match(source, /current-task-panel/);
  assert.doesNotMatch(source, /\{isCrawling && currentProgress && <section className="current-task-panel"/);
  assert.match(source, /等待手动开始任务/);
  assert.match(source, /className="current-task-actions"/);
  assert.match(source, /task-result-card/);
  assert.match(source, /本次任务结果/);
  assert.match(source, /检查完成，暂无新内容/);
  assert.match(source, /taskLaunching/);
  assert.match(source, /正在准备抓取任务/);
  assert.match(source, /currentProgress/);
  assert.match(source, /lastProgress/);
  assert.match(source, /CURRENT TASK/);
  assert.match(source, /LAST TASK/);
  assert.match(source, /speedBytesS/);
  assert.match(source, /已知字节进度/);
  assert.match(source, /预计剩余/);
  assert.match(source, /断点续传/);
  assert.match(source, /\/api\/task\/control/);
});

test("crawl sources can be managed safely from the dashboard", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /抓取链接任务栏/);
  assert.match(source, /fetch\("\/api\/sources"/);
  assert.match(source, /method: "DELETE"/);
  assert.match(source, /source-manager/);
  assert.match(source, /任务中·已锁定/);
  assert.match(source, /index\.php/);
  assert.match(source, /暂无抓取链接/);

  const taskService = await readFile(new URL("../scripts/start-local.py", import.meta.url), "utf8");
  assert.match(taskService, /add_source/);
  assert.match(taskService, /remove_source/);
  assert.match(taskService, /source_edit_blocked/);
  assert.match(taskService, /do_DELETE/);
  assert.match(taskService, /no_sources/);

  const compose = await readFile(new URL("../compose.yaml", import.meta.url), "utf8");
  assert.match(compose, /\.\/config:\/app\/config\s/);
  assert.doesNotMatch(compose, /\.\/config:\/app\/config:ro/);
});
