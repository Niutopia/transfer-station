"use client";

import { useCallback, useEffect, useMemo, useState, useRef, type FormEvent } from "react";

type ProgressData = {
  stage: string;
  done: number;
  total: number;
  active?: Array<{
    viewkey: string;
    fileName: string;
    bytesDone: number;
    bytesTotal: number;
    speedBytesS: number;
  }>;
  bytesDone?: number;
  bytesTotalKnown?: number;
  knownItems?: number;
  failed?: number;
  speedBytesS?: number;
  etaSeconds?: number | null;
  startedAt?: string | null;
  updatedAt?: string | null;
};

type MonitorData = {
  generatedAt: string;
  source: {
    stagingPath: string;
    crawlerPath: string;
    latestCrawlAt: string | null;
    listingCount: number;
    pagesPerListing: number;
    sources: Array<{ name: string; url: string }>;
  };
  overview: {
    rawLinks: number;
    uniqueVideos: number;
    duplicatesRemoved: number;
    resolvedVideos: number;
    downloadedVideos: number;
    pendingVideos: number;
    partialDownloads: number;
    todayFiles: number;
    todayBytes: number;
    totalFiles: number;
    totalBytes: number;
    downloadRate: number;
  };
  storage: { usedBytes: number; diskFreeBytes: number; diskTotalBytes: number; diskUsedPercent: number };
  daily: Array<{ date: string; label: string; files: number; bytes: number }>;
  latestRun: {
    status: "ready" | "attention" | "active";
    startedAt: string | null;
    taskState?: "running" | "paused" | "cancelling" | null;
    taskControllable?: boolean;
    completedToday?: boolean;
    completedAt?: string | null;
    result?: {
      status: "active" | "success" | "failed" | "none";
      startedAt?: string | null;
      finishedAt?: string | null;
      durationSeconds?: number | null;
      rawLinks: number;
      uniqueVideos: number;
      skippedVideos: number;
      newVideos: number;
      retryVideos: number;
      downloadedVideos: number;
      duplicateVideos: number;
      failedVideos: number;
      downloadedBytes: number;
    };
  };
  alerts: Array<{ level: "success" | "warning" | "error"; title: string; detail: string }>;
  activeDownloads: Array<{
    name: string;
    fileName: string;
    sizeBytes: number;
    totalBytes?: number;
    progressPercent?: number | null;
    modifiedAt: string;
    speedBytesS?: number;
    etaSeconds?: number | null;
    attempt?: number;
    retries?: number;
    message?: string;
    resumed?: boolean;
    status: "downloading" | "resuming" | "retrying" | "refreshing" | "verifying" | "restarting";
  }>;
  progress?: ProgressData | null;
  currentProgress?: ProgressData | null;
  lastProgress?: ProgressData | null;
};

const nf = new Intl.NumberFormat("zh-CN");

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const amount = value / 1024 ** index;
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

function clock(value: string | null) {
  if (!value) return "尚未开始";
  const date = new Date(value);
  if (isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatDuration(seconds?: number | null) {
  if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return "计算中";
  const rounded = Math.round(seconds);
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const secs = rounded % 60;
  if (hours) return `${hours} 小时 ${minutes} 分`;
  if (minutes) return `${minutes} 分 ${secs} 秒`;
  return `${secs} 秒`;
}

function downloadStateLabel(state: MonitorData["activeDownloads"][number]["status"]) {
  const labels: Record<string, string> = {
    downloading: "下载中",
    resuming: "断点续传",
    retrying: "等待重试",
    refreshing: "刷新链接",
    verifying: "校验文件",
    restarting: "重新开始",
  };
  return labels[state] ?? "处理中";
}

function progressTitle(stage: string) {
  if (stage === "crawling") return "正在抓取榜单页面";
  if (stage === "resolving") return "正在解析媒体地址";
  if (stage === "finalizing") return "正在整理任务结果";
  if (stage === "complete") return "下载任务已完成";
  if (stage === "failed") return "任务需要处理";
  if (stage === "cancelled") return "任务已取消";
  return "正在下载视频文件";
}

export default function Home() {
  const [data, setData] = useState<MonitorData | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [startingTask, setStartingTask] = useState(false);
  const [taskLaunching, setTaskLaunching] = useState(false);
  const [taskMessage, setTaskMessage] = useState("");
  const [serviceOnline, setServiceOnline] = useState<boolean | null>(null);
  const [realtimeConnected, setRealtimeConnected] = useState(false);
  const [taskControlling, setTaskControlling] = useState(false);
  const [sourceName, setSourceName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceSaving, setSourceSaving] = useState(false);
  const [sourceDeleting, setSourceDeleting] = useState("");
  const [sourceFeedback, setSourceFeedback] = useState("");
  const [sourceError, setSourceError] = useState("");
  const abortControllerRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const response = await fetch(`/status.json?t=${Date.now()}`, {
        cache: "no-store",
        signal: controller.signal
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const nextData = (await response.json()) as MonitorData;
      setData(nextData);
      if (nextData.latestRun.status === "active") setTaskLaunching(false);
      setError("");
    } catch (err: unknown) {
      if (err instanceof Error && err.name === "AbortError") return;
      setError("暂时无法读取本地状态快照");
    } finally {
      if (abortControllerRef.current === controller) {
        setRefreshing(false);
        abortControllerRef.current = null;
      }
    }
  }, []);

  const [taskError, setTaskError] = useState("");

  const controlTask = useCallback(async (action: "pause" | "resume" | "cancel") => {
    setTaskControlling(true);
    setTaskError("");
    try {
      const response = await fetch("/api/task/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const payload = await response.json().catch(() => ({})) as { error?: string };
      if (!response.ok) throw new Error(payload.error || "任务控制失败");
      setTaskMessage(action === "pause" ? "任务已暂停，可从当前进度继续" : action === "resume" ? "任务已继续" : "正在安全取消任务");
      window.setTimeout(() => void load(), 300);
    } catch (err) {
      setTaskError(err instanceof Error ? err.message : "任务控制失败");
    } finally {
      setTaskControlling(false);
    }
  }, [load]);
  const loadServiceHealth = useCallback(async () => {
    try {
      const response = await fetch(`/api/health?t=${Date.now()}`, { cache: "no-store" });
      setServiceOnline(response.ok);
    } catch {
      setServiceOnline(false);
    }
  }, []);

  const startTask = useCallback(async () => {
    setStartingTask(true);
    setTaskLaunching(true);
    setTaskError("");
    setTaskMessage("");
    try {
      const response = await fetch('/api/task', { method: 'POST' });
      const payload = await response.json().catch(() => ({})) as { error?: string; code?: string };
      if (payload.code === "no_sources") {
        setServiceOnline(true);
        setTaskLaunching(false);
        setTaskError(payload.error || "请先添加抓取链接");
        return;
      }
      if (response.status === 409) {
        setTaskMessage("任务已在后台运行，正在同步状态");
        window.setTimeout(() => setTaskLaunching(false), 8_000);
        await load();
        return;
      }
      if (!response.ok) throw new Error(payload.error || '无法启动任务');
      setServiceOnline(true);
      setTaskMessage("任务已启动，状态会自动更新");
      window.setTimeout(() => void load(), 120);
      window.setTimeout(() => setTaskLaunching(false), 8_000);
    } catch {
      setServiceOnline(false);
      setTaskLaunching(false);
      setTaskError("任务服务未连接。历史状态仍可查看，但暂时不能启动新任务。");
    } finally {
      setStartingTask(false);
    }
  }, [load]);

  const applySources = useCallback((sources: Array<{ name: string; url: string }>) => {
    setData((current) => current ? {
      ...current,
      source: { ...current.source, sources, listingCount: sources.length },
    } : current);
  }, []);

  const addCrawlSource = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSourceSaving(true);
    setSourceError("");
    setSourceFeedback("");
    try {
      const response = await fetch("/api/sources", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: sourceName, url: sourceUrl }),
      });
      const payload = await response.json().catch(() => ({})) as {
        error?: string;
        sources?: Array<{ name: string; url: string }>;
      };
      if (!response.ok || !payload.sources) throw new Error(payload.error || "无法添加抓取链接");
      applySources(payload.sources);
      setSourceName("");
      setSourceUrl("");
      setSourceFeedback("已添加，将在下一次手动任务中生效");
      window.setTimeout(() => void load(), 150);
    } catch (err) {
      setSourceError(err instanceof Error ? err.message : "无法添加抓取链接");
    } finally {
      setSourceSaving(false);
    }
  }, [applySources, load, sourceName, sourceUrl]);

  const deleteCrawlSource = useCallback(async (source: { name: string; url: string }) => {
    if (!window.confirm(`删除抓取链接“${source.name}”？\n已下载文件和历史记录不会被删除。`)) return;
    setSourceDeleting(source.url);
    setSourceError("");
    setSourceFeedback("");
    try {
      const response = await fetch("/api/sources", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: source.url }),
      });
      const payload = await response.json().catch(() => ({})) as {
        error?: string;
        sources?: Array<{ name: string; url: string }>;
      };
      if (!response.ok || !payload.sources) throw new Error(payload.error || "无法删除抓取链接");
      applySources(payload.sources);
      setSourceFeedback(`已删除“${source.name}”，下次任务不再抓取`);
      window.setTimeout(() => void load(), 150);
    } catch (err) {
      setSourceError(err instanceof Error ? err.message : "无法删除抓取链接");
    } finally {
      setSourceDeleting("");
    }
  }, [applySources, load]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      void load();
      void loadServiceHealth();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      abortControllerRef.current?.abort();
    };
  }, [load, loadServiceHealth]);

  useEffect(() => {
    if (!autoRefresh) return;
    const source = new EventSource("/api/events");
    source.onopen = () => {
      setRealtimeConnected(true);
      setServiceOnline(true);
    };
    source.addEventListener("status", (event) => {
      try {
        const nextData = JSON.parse((event as MessageEvent<string>).data) as MonitorData;
        setData(nextData);
        if (nextData.latestRun.status === "active") setTaskLaunching(false);
        setError("");
        setRealtimeConnected(true);
      } catch {
        setRealtimeConnected(false);
      }
    });
    source.onerror = () => setRealtimeConnected(false);
    return () => source.close();
  }, [autoRefresh]);

  useEffect(() => {
    if (!autoRefresh || realtimeConnected) return;
    let timer: number;
    let isMounted = true;

    const tick = async () => {
      await load();
      if (isMounted && autoRefresh && !abortControllerRef.current?.signal.aborted) {
        timer = window.setTimeout(tick, 10_000);
      }
    };

    timer = window.setTimeout(tick, 10_000);
    return () => {
      isMounted = false;
      window.clearTimeout(timer);
    };
  }, [autoRefresh, load, realtimeConnected]);

  const visibleDaily = useMemo(() => data?.daily.slice(-7) ?? [], [data]);
  const maxDaily = useMemo(() => Math.max(1, ...visibleDaily.map((day) => day.files)), [visibleDaily]);

  if (!data) {
    return (
      <main className="loading-shell">
        <div className="loading-mark" aria-hidden="true" />
        <p>正在读取 Transfer station 状态…</p>
        {error && <span>{error}</span>}
      </main>
    );
  }

  const isCrawling = data.latestRun.status === "active";
  const taskAppearsActive = isCrawling || taskLaunching;
  const completedToday = data.latestRun.completedToday === true;
  const currentProgress = data.currentProgress ?? (isCrawling ? data.progress : null);
  const lastProgress = data.lastProgress ?? (!isCrawling ? data.progress : null);
  const currentPercent = currentProgress?.total ? Math.min(100, currentProgress.done / currentProgress.total * 100) : 0;
  const lastProgressPercent = lastProgress?.total ? Math.min(100, lastProgress.done / lastProgress.total * 100) : 0;
  const aggregateSpeed = data.activeDownloads.reduce((total, item) => total + (item.speedBytesS ?? 0), 0);
  const currentKnownBytePercent = currentProgress?.bytesTotalKnown ? Math.min(100, (currentProgress.bytesDone ?? 0) / currentProgress.bytesTotalKnown * 100) : 0;
  const lastKnownBytePercent = lastProgress?.bytesTotalKnown ? Math.min(100, (lastProgress.bytesDone ?? 0) / lastProgress.bytesTotalKnown * 100) : 0;
  const taskPaused = data.latestRun.taskState === "paused";
  const taskResult = data.latestRun.result;
  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand" type="button" aria-label="返回页面顶部" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="brand-mark" src="/transfer-station-x.svg?v=1" alt="" aria-hidden="true" />
          <strong className="brand-title">TRANSFER STATION</strong>
        </button>

        <div className="top-actions">
          <div className={`service-health ${serviceOnline === false ? "offline" : serviceOnline === true ? "online" : "checking"}`}>
            <span aria-hidden="true" />
            {serviceOnline === false ? "任务离线" : serviceOnline === true ? "任务在线" : "检查服务"}
          </div>
          <button
            className={`live-button ${autoRefresh ? "active" : ""}`}
            type="button"
            aria-pressed={autoRefresh}
            onClick={() => setAutoRefresh((value) => !value)}
          >
            <span className="live-dot" aria-hidden="true" />
            {autoRefresh ? realtimeConnected ? "实时更新中" : "正在重新连接" : "实时更新已暂停"}
          </button>
          <button className="icon-button" type="button" aria-label="刷新监控数据" onClick={() => void load()} disabled={refreshing}>
            {refreshing ? "…" : "↻"}
          </button>
        </div>
      </header>

      {error && <div className="inline-error" role="alert">{error}</div>}

      <div className="content">
        <section className="main-column" aria-label="任务监控">
          <section className={`current-task-panel ${taskAppearsActive ? "is-active" : "is-idle"}`} aria-labelledby="current-task-title">
            <div className="current-task-head">
              <div>
                <div className="current-label-row"><span className="current-kicker"><i />CURRENT TASK</span><span className={`status ${taskAppearsActive ? taskPaused ? "waiting" : "active" : "done"}`}>{taskLaunching && !isCrawling ? "启动中" : isCrawling ? taskPaused ? "已暂停" : "运行中" : "空闲"}</span></div>
                <h2 id="current-task-title">{taskLaunching && !isCrawling ? "正在准备抓取任务" : isCrawling && currentProgress ? taskPaused ? "任务已暂停" : progressTitle(currentProgress.stage) : "等待手动开始任务"}</h2>
              </div>
              <div className="current-task-actions">
                {isCrawling && data.latestRun.taskControllable && <>
                  <button className="secondary" type="button" disabled={taskControlling} onClick={() => void controlTask(taskPaused ? "resume" : "pause")}>{taskPaused ? "继续任务" : "暂停任务"}</button>
                  <button className="secondary danger" type="button" disabled={taskControlling} onClick={() => void controlTask("cancel")}>取消任务</button>
                </>}
                {!taskAppearsActive && <button
                  className="secondary highlight-btn"
                  type="button"
                  onClick={startTask}
                  disabled={startingTask || serviceOnline === false || data.source.listingCount === 0}
                >
                  {data.source.listingCount === 0 ? "请先添加抓取链接" : serviceOnline === false ? "任务服务离线" : startingTask ? "正在启动…" : data.overview.pendingVideos > 0 ? `处理 ${data.overview.pendingVideos} 个待办` : completedToday ? "再次抓取最新内容" : "开始抓取任务"}
                </button>}
              </div>
            </div>
            <div className="current-task-grid">
              <span><small>当前阶段</small><strong>{taskLaunching && !isCrawling ? "任务准备" : !isCrawling || !currentProgress ? "等待启动" : currentProgress.stage === "downloading" ? "下载入库" : currentProgress.stage === "resolving" ? "媒体解析" : currentProgress.stage === "finalizing" ? "结果整理" : "榜单抓取"}</strong></span>
              <span><small>抓取范围</small><strong>{data.source.listingCount} 榜 × {data.source.pagesPerListing} 页</strong></span>
              <span><small>处理进度</small><strong>{taskLaunching && !isCrawling ? "正在连接" : isCrawling && currentProgress ? currentProgress.total > 0 ? `${currentProgress.done}/${currentProgress.total}` : "准备中" : data.overview.pendingVideos > 0 ? `${data.overview.pendingVideos} 个待处理` : "暂无任务"}</strong></span>
              <span><small>启动时间</small><strong>{taskLaunching && !isCrawling ? "刚刚" : isCrawling && currentProgress ? clock(currentProgress.startedAt ?? data.latestRun.startedAt) : "等待手动启动"}</strong></span>
            </div>
            {taskAppearsActive && <div className={`current-task-track ${(taskLaunching || currentProgress?.total === 0) ? "indeterminate" : ""}`} role={isCrawling && (currentProgress?.total ?? 0) > 0 ? "progressbar" : undefined} aria-valuenow={isCrawling && (currentProgress?.total ?? 0) > 0 ? currentProgress?.done : undefined} aria-valuemin={isCrawling && (currentProgress?.total ?? 0) > 0 ? 0 : undefined} aria-valuemax={isCrawling && (currentProgress?.total ?? 0) > 0 ? currentProgress?.total : undefined}><i style={isCrawling && (currentProgress?.total ?? 0) > 0 ? { width: `${currentPercent}%` } : undefined} /></div>}
            {isCrawling && currentProgress?.stage === "downloading" && <div className="current-task-meta"><span>活动下载 {data.activeDownloads.length}</span><span>实时速度 {formatBytes(currentProgress.speedBytesS ?? aggregateSpeed)}/s</span><span>预计剩余 {formatDuration(currentProgress.etaSeconds)}</span></div>}
            {isCrawling && currentProgress && (currentProgress.bytesTotalKnown ?? 0) > 0 && <>
              <div className="current-byte-caption"><span>已知字节进度 · {currentProgress.knownItems ?? 0} 个文件</span><strong>{formatBytes(currentProgress.bytesDone ?? 0)} / {formatBytes(currentProgress.bytesTotalKnown ?? 0)}</strong></div>
              <div className="current-task-track byte-track" role="progressbar" aria-label="当前任务已知字节进度" aria-valuenow={currentKnownBytePercent} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${currentKnownBytePercent}%` }} /></div>
            </>}
            {isCrawling && data.activeDownloads.length > 0 && <div className="live-download-list current-download-list">
              {data.activeDownloads.map((item) => {
                const itemPercent = item.progressPercent ?? 0;
                return <div className="live-download" key={item.fileName}>
                  <div className="live-download-info"><strong>{item.name}</strong><span><b className={`download-state state-${item.status}`}>{downloadStateLabel(item.status)}</b>{item.resumed ? " · 已恢复断点" : ""}{item.attempt ? ` · 重试 ${item.attempt}/${item.retries}` : ""}</span><span>{formatBytes(item.sizeBytes)}{item.totalBytes ? ` / ${formatBytes(item.totalBytes)}` : ""} · {formatBytes(item.speedBytesS ?? 0)}/s · 剩余 {formatDuration(item.etaSeconds)}</span>{item.message && <em>{item.message}</em>}</div>
                  <div className={`live-file-track ${item.totalBytes ? "" : "indeterminate"}`}><i style={item.totalBytes ? { width: `${itemPercent}%` } : undefined} /></div>
                  <span className="live-file-percent">{item.totalBytes ? `${itemPercent.toFixed(0)}%` : "下载中"}</span>
                </div>;
              })}
            </div>}
          </section>

          {lastProgress && lastProgress.total > 0 && <section className={`live-task-panel stage-${lastProgress.stage}`} aria-labelledby="last-task-title">
            <div className="live-task-head">
              <div><span className="live-kicker"><i />LAST TASK</span><h2 id="last-task-title">{progressTitle(lastProgress.stage)}</h2></div>
              <strong>{lastProgressPercent.toFixed(0)}%</strong>
            </div>
            <div className="progress-caption"><span>文件进度</span><strong>{lastProgress.done}/{lastProgress.total}</strong></div>
            <div className="live-progress" role="progressbar" aria-valuenow={lastProgress.done} aria-valuemin={0} aria-valuemax={lastProgress.total}><i style={{ width: `${lastProgressPercent}%` }} /></div>
            {(lastProgress.bytesTotalKnown ?? 0) > 0 && <>
              <div className="progress-caption byte-caption"><span>已知字节进度 · {lastProgress.knownItems ?? 0} 个文件</span><strong>{formatBytes(lastProgress.bytesDone ?? 0)} / {formatBytes(lastProgress.bytesTotalKnown ?? 0)}</strong></div>
              <div className="live-progress byte-progress" role="progressbar" aria-label="上一任务已知字节进度" aria-valuenow={lastKnownBytePercent} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${lastKnownBytePercent}%` }} /></div>
            </>}
            <div className="live-stats">
              <span><small>已完成</small><strong>{lastProgress.done}/{lastProgress.total}</strong></span>
              <span><small>任务耗时</small><strong>{lastProgress.startedAt && lastProgress.updatedAt ? formatDuration((new Date(lastProgress.updatedAt).getTime() - new Date(lastProgress.startedAt).getTime()) / 1000) : "已记录"}</strong></span>
              <span><small>下载容量</small><strong>{formatBytes(lastProgress.bytesDone ?? 0)}</strong></span>
              <span><small>任务结果</small><strong>已完成</strong></span>
            </div>
          </section>}

          <section className="panel trend-panel" aria-labelledby="trend-title">
            <div className="panel-head">
              <div><h2 id="trend-title">近 7 天入库趋势</h2><p>按“中转站”中文件修改时间统计</p></div>
              <div className="trend-summary" aria-label="入库摘要">
                <span><strong>{data.overview.todayFiles}</strong>今日入库</span>
                <span><strong>{formatBytes(data.overview.totalBytes)}</strong>总容量</span>
              </div>
            </div>
            <div className="trend-chart">
              {visibleDaily.map((day) => (
                <div className="trend-column" key={day.date} title={`${day.date}：${day.files} 个，${formatBytes(day.bytes)}`}>
                  <span className="trend-value">{day.files || ""}</span>
                  <div className="trend-track"><i style={{ height: `${day.files ? Math.max(7, day.files / maxDaily * 100) : 2}%` }} /></div>
                  <small>{day.label}</small>
                </div>
              ))}
            </div>
          </section>

          <section className="source-manager" aria-labelledby="source-manager-title">
            <div className="source-manager-head">
              <div>
                <span className="source-manager-kicker"><i />CRAWL SOURCES</span>
                <h2 id="source-manager-title">抓取链接任务栏</h2>
                <p>管理下一次手动任务要检查的榜单链接，每个链接抓取前 {data.source.pagesPerListing} 页。</p>
              </div>
              <span className={`status ${taskAppearsActive ? "active" : (data.source.sources?.length ?? 0) === 0 ? "waiting" : "done"}`}>{taskAppearsActive ? "任务中·已锁定" : `${data.source.sources?.length ?? 0} 个链接`}</span>
            </div>
            <form className="source-form" onSubmit={addCrawlSource}>
              <label>
                <span>名称（可选）</span>
                <input value={sourceName} onChange={(event) => setSourceName(event.target.value)} maxLength={40} placeholder="例如：最近热门" disabled={taskAppearsActive || sourceSaving} />
              </label>
              <label>
                <span>HTTPS 榜单 / 首页链接</span>
                <input type="url" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} required placeholder="https://91porn.com/index.php 或 /v.php?..." disabled={taskAppearsActive || sourceSaving} />
              </label>
              <button className="source-add" type="submit" disabled={taskAppearsActive || sourceSaving || !sourceUrl.trim()}>{sourceSaving ? "正在保存…" : "+ 添加链接"}</button>
            </form>
            {sourceFeedback && <p className="source-feedback success" role="status">{sourceFeedback}</p>}
            {sourceError && <p className="source-feedback error" role="alert">{sourceError}</p>}
            <div className="source-list" aria-label="已配置抓取链接">
              {(data.source.sources?.length ?? 0) === 0 && <div className="source-empty"><strong>暂无抓取链接</strong><span>添加榜单或首页链接后，才能启动下一次手动任务。</span></div>}
              {(data.source.sources ?? []).map((source, index) => <article className="source-item" key={source.url}>
                <span className="source-index">{String(index + 1).padStart(2, "0")}</span>
                <div className="source-copy"><strong>{source.name}</strong><code title={source.url}>{source.url}</code></div>
                <button
                  className="source-delete"
                  type="button"
                  onClick={() => void deleteCrawlSource(source)}
                  disabled={taskAppearsActive || sourceDeleting === source.url}
                  title={`删除 ${source.name}`}
                >{sourceDeleting === source.url ? "删除中…" : "删除"}</button>
              </article>)}
            </div>
          </section>

          <div className="footer-note"><span>{autoRefresh && realtimeConnected ? "实时连接已建立" : autoRefresh ? "实时连接中断，已降级为 10 秒轮询" : "实时更新已暂停"}</span><span>快照生成于 {clock(data.generatedAt)}</span></div>
        </section>

        <aside className="side-column" aria-label="运行状态">
          <section className="side-card">
            <div className="side-card-head"><div><span>磁盘空间</span><strong>{data.storage.diskUsedPercent}% 已使用</strong></div><span>{formatBytes(data.storage.diskFreeBytes)} 可用</span></div>
            <div className="storage-track" role="progressbar" aria-label="磁盘使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={data.storage.diskUsedPercent}><i style={{ width: `${Math.min(100, data.storage.diskUsedPercent)}%` }} /></div>
            <div className="side-stats"><span><small>中转站</small><strong>{formatBytes(data.overview.totalBytes)}</strong></span><span><small>文件数</small><strong>{nf.format(data.overview.totalFiles)}</strong></span></div>
          </section>
          <section className="side-card">
            <div className="side-title"><h2>运行状态</h2><span className={`status ${data.latestRun.status === "active" ? "active" : data.latestRun.status === "attention" ? "attention" : "done"}`}>{data.latestRun.status === "active" ? "运行中" : data.latestRun.status === "attention" ? "需检查" : "就绪"}</span></div>
            <div className="alert-list">{data.alerts.map((alert, index) => <div className={`alert-item ${alert.level}`} key={`${alert.title}-${index}`}><span aria-hidden="true" /><div><strong>{alert.title}</strong><p>{alert.detail}</p></div></div>)}</div>
          </section>
          <section className={`side-card task-result-card result-${taskAppearsActive ? "active" : taskResult?.status ?? "none"}`} aria-labelledby="task-result-title">
            <div className="side-title">
              <h2 id="task-result-title">本次任务结果</h2>
              <span className={`status ${taskAppearsActive ? "active" : taskResult?.status === "failed" ? "attention" : "done"}`}>{taskAppearsActive ? "进行中" : taskResult?.status === "failed" ? "失败" : taskResult?.status === "success" ? "已完成" : "暂无"}</span>
            </div>
            <strong className="task-result-summary">{taskLaunching && !isCrawling ? "正在连接任务服务" : isCrawling ? progressTitle(currentProgress?.stage ?? "crawling") : taskResult?.status === "success" ? taskResult.newVideos || taskResult.retryVideos ? "采集与下载处理完成" : "检查完成，暂无新内容" : taskResult?.status === "failed" ? "任务未能完整完成" : "尚未运行任务"}</strong>
            {taskAppearsActive ? <div className="task-result-grid">
              <span><small>当前阶段</small><strong>{taskLaunching && !isCrawling ? "准备中" : currentProgress?.stage === "downloading" ? "下载入库" : currentProgress?.stage === "resolving" ? "媒体解析" : "榜单抓取"}</strong></span>
              <span><small>处理进度</small><strong>{currentProgress?.total ? `${currentProgress.done}/${currentProgress.total}` : "计算中"}</strong></span>
              <span><small>活动下载</small><strong>{data.activeDownloads.length}</strong></span>
              <span><small>实时速度</small><strong>{formatBytes(currentProgress?.speedBytesS ?? aggregateSpeed)}/s</strong></span>
            </div> : taskResult && taskResult.status !== "none" ? <>
              <div className="task-result-grid">
                <span><small>检查链接</small><strong>{nf.format(taskResult.rawLinks)}</strong></span>
                <span><small>新发现</small><strong>{nf.format(taskResult.newVideos)}</strong></span>
                <span><small>本次入库</small><strong>{nf.format(taskResult.downloadedVideos)}</strong></span>
                <span><small>{taskResult.failedVideos ? "失败" : taskResult.duplicateVideos ? "内容重复" : "重试"}</small><strong>{nf.format(taskResult.failedVideos || taskResult.duplicateVideos || taskResult.retryVideos)}</strong></span>
              </div>
              <p className="task-result-detail">去重后 {nf.format(taskResult.uniqueVideos)} 个视频，跳过 {nf.format(taskResult.skippedVideos)} 个已知编号{taskResult.duplicateVideos ? `，内容指纹拦截 ${nf.format(taskResult.duplicateVideos)} 个重复` : ""}{taskResult.downloadedBytes ? `，实际入库 ${formatBytes(taskResult.downloadedBytes)}` : ""}。</p>
              <div className="task-result-time"><span>完成于 {clock(taskResult.finishedAt ?? null)}</span><span>{taskResult.durationSeconds ? `耗时 ${formatDuration(taskResult.durationSeconds)}` : "耗时未记录"}</span></div>
            </> : <p className="task-result-detail">点击“开始抓取任务”后，这里会显示本次检查与下载数据。</p>}
            {taskAppearsActive && taskMessage && <p className="task-result-feedback success" role="status">{taskMessage}</p>}
            {taskError && <p className="task-result-feedback error" role="alert">{taskError}</p>}
          </section>
        </aside>
      </div>
    </main>
  );
}
