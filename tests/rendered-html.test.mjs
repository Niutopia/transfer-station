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
  assert.equal(dailyConfig.sources.length, 5);
  assert.equal(new Set(dailyConfig.sources.map((source) => source.url)).size, 5);
});

test("dashboard keeps a single focused monitor and the shared favicon", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /今日概览/);
  assert.doesNotMatch(source, /采集阶段|查看阶段|className="metrics"|className="metric-details"/);
  assert.doesNotMatch(source, /视频库|role="tablist"|activeTab|selectTab|window\.location\.hash/);
  assert.doesNotMatch(source, /\{ id: "runs", label:/);
  assert.doesNotMatch(source, /className="panel run-panel"/);
  assert.match(source, /!isCrawling && !completedToday && <button/);
  assert.match(source, /payload\.code === "completed_today"/);
  assert.match(source, /src="\/transfer-station-x\.svg\?v=1"/);
  assert.match(source, /className="brand-title">TRANSFER STATION/);
  assert.doesNotMatch(source, /className="topbar-title"/);
  assert.doesNotMatch(source, /LOCAL MEDIA NODE|className="brand-text"/);
  const styles = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(styles, /\.hero-actions\s*\{[^}]*flex-wrap:\s*nowrap/);
  assert.match(styles, /\.topbar\s*\{[^}]*grid-template-columns:\s*1fr auto/);
  assert.doesNotMatch(styles, /\.nav(?:\s|\{|:)/);
  const layout = await readFile(new URL("../app/layout.tsx", import.meta.url), "utf8");
  assert.equal((layout.match(/transfer-station-x\.svg\?v=1/g) ?? []).length, 3);
});

test("dashboard uses realtime events with a polling fallback", async () => {
  const source = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(source, /new EventSource\("\/api\/events"\)/);
  assert.match(source, /实时更新中/);
  assert.match(source, /已降级为 10 秒轮询/);
  assert.match(source, /live-task-panel/);
  assert.match(source, /speedBytesS/);
  assert.match(source, /已知字节进度/);
  assert.match(source, /预计剩余/);
  assert.match(source, /断点续传/);
  assert.match(source, /\/api\/task\/control/);
});
