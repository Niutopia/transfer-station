"use client";

import { useCallback, useEffect, useMemo, useState, useRef } from "react";

type Stage = {
  name: string;
  status: "done" | "active" | "waiting" | "attention";
  value: number;
  note: string;
};

type MonitorData = {
  generatedAt: string;
  source: {
    stagingPath: string;
    crawlerPath: string;
    latestCrawlAt: string | null;
    listingCount: number;
    pagesPerListing: number;
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
  latestRun: { status: "ready" | "attention" | "active"; startedAt: string | null; taskState?: "running" | "paused" | "cancelling" | null; taskControllable?: boolean; stages: Stage[] };
  alerts: Array<{ level: "success" | "warning" | "error"; title: string; detail: string }>;
  recentFiles: Array<{
    name: string;
    fileName: string;
    relativePath: string;
    sizeBytes: number;
    modifiedAt: string;
    status: "complete" | "partial";
  }>;
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
  pendingDownloads: Array<{
    name: string;
    fileName: string;
    sizeBytes?: number;
    status: "waiting" | "resumable";
  }>;
  progress?: {
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
  } | null;
};

type Tab = "overview" | "library";

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

function fullDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "short",
  }).format(new Date(value));
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
  if (stage === "resolving") return "正在解析媒体地址";
  if (stage === "complete") return "下载任务已完成";
  if (stage === "failed") return "任务需要处理";
  if (stage === "cancelled") return "任务已取消";
  return "正在下载视频文件";
}

function stageStatus(status: Stage["status"]) {
  if (status === "done") return "已完成";
  if (status === "active") return "进行中";
  if (status === "attention") return "需检查";
  return "等待中";
}

export default function Home() {
  const [data, setData] = useState<MonitorData | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [startingTask, setStartingTask] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [taskMessage, setTaskMessage] = useState("");
  const [serviceOnline, setServiceOnline] = useState<boolean | null>(null);
  const [realtimeConnected, setRealtimeConnected] = useState(false);
  const [taskControlling, setTaskControlling] = useState(false);
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
      setData((await response.json()) as MonitorData);
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
    setTaskError("");
    setTaskMessage("");
    try {
      const response = await fetch('/api/task', { method: 'POST' });
      if (response.status === 409) {
        setTaskError("今日任务已经在运行，请稍后查看进度");
        return;
      }
      if (!response.ok) throw new Error('Failed to start task');
      setServiceOnline(true);
      setTaskMessage("任务已启动，状态会自动更新");
      window.setTimeout(() => void load(), 500);
    } catch {
      setServiceOnline(false);
      setTaskError("任务服务未连接。历史状态仍可查看，但暂时不能启动新任务。");
    } finally {
      setStartingTask(false);
    }
  }, [load]);

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
    const syncFromHash = () => {
      const hash = window.location.hash.slice(1);
      if (hash === "overview" || hash === "library") setActiveTab(hash);
    };
    const frame = window.requestAnimationFrame(syncFromHash);
    window.addEventListener("hashchange", syncFromHash);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("hashchange", syncFromHash);
    };
  }, []);

  const selectTab = useCallback((tab: Tab) => {
    setActiveTab(tab);
    window.history.replaceState(null, "", `#${tab}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, []);

  useEffect(() => {
    if (!autoRefresh) return;
    const source = new EventSource("/api/events");
    source.onopen = () => {
      setRealtimeConnected(true);
      setServiceOnline(true);
    };
    source.addEventListener("status", (event) => {
      try {
        setData(JSON.parse((event as MessageEvent<string>).data) as MonitorData);
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
  const taskReady = !isCrawling && data.overview.resolvedVideos === data.overview.uniqueVideos && data.overview.uniqueVideos > 0;
  const allDownloaded = taskReady && data.overview.pendingVideos === 0 && data.overview.downloadedVideos >= data.overview.uniqueVideos;
  const downloading = taskReady && !allDownloaded && data.overview.partialDownloads > 0;
  const unresolvedVideos = Math.max(0, data.overview.uniqueVideos - data.overview.resolvedVideos);
  const progressPercent = data.progress?.total ? Math.min(100, data.progress.done / data.progress.total * 100) : 0;
  const aggregateSpeed = data.activeDownloads.reduce((total, item) => total + (item.speedBytesS ?? 0), 0);
  const knownBytePercent = data.progress?.bytesTotalKnown ? Math.min(100, (data.progress.bytesDone ?? 0) / data.progress.bytesTotalKnown * 100) : 0;
  const progressIsActive = Boolean(data.progress && !["complete", "failed", "cancelled"].includes(data.progress.stage));
  const taskPaused = data.latestRun.taskState === "paused";
  const stageInputs = [
    data.overview.rawLinks,
    data.overview.rawLinks,
    data.overview.uniqueVideos,
    data.overview.resolvedVideos,
  ];
  const stageLabels = ["STAGE 01", "STAGE 02", "STAGE 03", "STAGE 04"];

  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand" type="button" aria-label="返回今日概览" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img className="brand-mark" src="/transfer-station-x.svg?v=1" alt="" aria-hidden="true" />
          <span className="brand-text"><strong>TRANSFER STATION</strong><small>LOCAL MEDIA NODE // 01</small></span>
        </button>

        <nav className="nav" role="tablist" aria-label="监控视图">
          <button type="button" role="tab" aria-selected={activeTab === "overview"} className={activeTab === "overview" ? "active" : ""} onClick={() => selectTab("overview")}>今日概览</button>
          <button type="button" role="tab" aria-selected={activeTab === "library"} className={activeTab === "library" ? "active" : ""} onClick={() => selectTab("library")}>视频库</button>
        </nav>

        <div className="top-actions">
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
        <section className="main-column" aria-label={activeTab === "overview" ? "今日概览" : "视频库"}>
          {activeTab === "overview" ? <>
          <section className="hero">
            <div>
              <div className="eyebrow">DAILY QUEST // {fullDate(data.generatedAt)}</div>
              <h1>{isCrawling ? taskPaused ? "任务已暂停，进度已安全保存" : data.progress?.stage === "downloading" ? `正在下载 ${data.progress.done}/${data.progress.total}` : "正在抓取和解析媒体地址…" : allDownloaded ? "每日采集与下载已完成" : downloading ? "每日采集已完成，正在下载中" : data.overview.pendingVideos > 0 ? `${data.overview.pendingVideos} 个文件等待处理` : "今日采集需要检查"}</h1>
              <p>
                已配置 {data.source.listingCount} 个榜单 × 每榜前 {data.source.pagesPerListing} 页；
                {isCrawling ? "状态通过实时连接自动更新，无需手动刷新。" : `最近任务得到 ${data.overview.uniqueVideos} 个唯一视频，`}
                {isCrawling ? "" : allDownloaded ? "已全部进入中转站。" : `已入库 ${data.overview.downloadedVideos} 个，未解析 ${unresolvedVideos} 个，待处理 ${data.overview.pendingVideos} 个。`}
              </p>
              {taskError && <p className="task-feedback error" role="alert">{taskError}</p>}
              {taskMessage && <p className="task-feedback success" role="status">{taskMessage}</p>}
              <div className={`service-health ${serviceOnline === false ? "offline" : serviceOnline === true ? "online" : "checking"}`}>
                <span aria-hidden="true" />
                {serviceOnline === false ? "任务服务离线" : serviceOnline === true ? "任务服务在线" : "正在检查任务服务"}
              </div>
            </div>
            <div className="hero-actions">
              {isCrawling && data.latestRun.taskControllable && <>
                <button className="secondary" type="button" disabled={taskControlling} onClick={() => void controlTask(taskPaused ? "resume" : "pause")}>{taskPaused ? "继续任务" : "暂停任务"}</button>
                <button className="secondary danger" type="button" disabled={taskControlling} onClick={() => void controlTask("cancel")}>取消任务</button>
              </>}
              <button
                className="secondary highlight-btn"
                type="button"
                onClick={startTask}
                disabled={startingTask || data.latestRun.status === "active" || serviceOnline === false}
              >
                {serviceOnline === false ? "任务服务离线" : startingTask || data.latestRun.status === "active" ? "任务运行中…" : data.overview.pendingVideos > 0 ? `处理 ${data.overview.pendingVideos} 个待办` : "立即开始今日任务"}
              </button>
              <button className="secondary" type="button" onClick={() => document.getElementById("collection-stages")?.scrollIntoView({ behavior: "smooth", block: "start" })}>查看阶段</button>
              <button className="primary" type="button" onClick={() => void load()} disabled={refreshing}>
                {refreshing ? "正在刷新" : "刷新状态"}
              </button>
            </div>
          </section>

          {data.progress && data.progress.total > 0 && <section className={`live-task-panel stage-${data.progress.stage}`} aria-labelledby="live-task-title">
            <div className="live-task-head">
              <div><span className="live-kicker"><i />{progressIsActive ? "LIVE TASK" : "LAST TASK"}</span><h2 id="live-task-title">{progressTitle(data.progress.stage)}</h2></div>
              <strong>{progressPercent.toFixed(0)}%</strong>
            </div>
            <div className="progress-caption"><span>文件进度</span><strong>{data.progress.done}/{data.progress.total}</strong></div>
            <div className="live-progress" role="progressbar" aria-valuenow={data.progress.done} aria-valuemin={0} aria-valuemax={data.progress.total}><i style={{ width: `${progressPercent}%` }} /></div>
            {(data.progress.bytesTotalKnown ?? 0) > 0 && <>
              <div className="progress-caption byte-caption"><span>已知字节进度 · {data.progress.knownItems ?? 0} 个文件</span><strong>{formatBytes(data.progress.bytesDone ?? 0)} / {formatBytes(data.progress.bytesTotalKnown ?? 0)}</strong></div>
              <div className="live-progress byte-progress" role="progressbar" aria-label="已知字节下载进度" aria-valuenow={knownBytePercent} aria-valuemin={0} aria-valuemax={100}><i style={{ width: `${knownBytePercent}%` }} /></div>
            </>}
            <div className="live-stats">
              <span><small>已完成</small><strong>{data.progress.done}/{data.progress.total}</strong></span>
              <span><small>活动下载</small><strong>{data.activeDownloads.length}</strong></span>
              <span><small>实时速度</small><strong>{formatBytes(data.progress.speedBytesS ?? aggregateSpeed)}/s</strong></span>
              <span><small>{data.progress.failed ? "失败项目" : progressIsActive ? "预计剩余" : "任务结果"}</small><strong>{data.progress.failed ? data.progress.failed : progressIsActive ? formatDuration(data.progress.etaSeconds) : "已完成"}</strong></span>
            </div>
            {data.activeDownloads.length > 0 && <div className="live-download-list">
              {data.activeDownloads.map((item) => {
                const itemPercent = item.progressPercent ?? 0;
                return <div className="live-download" key={item.fileName}>
                  <div className="live-download-info"><strong>{item.name}</strong><span><b className={`download-state state-${item.status}`}>{downloadStateLabel(item.status)}</b>{item.resumed ? " · 已恢复断点" : ""}{item.attempt ? ` · 重试 ${item.attempt}/${item.retries}` : ""}</span><span>{formatBytes(item.sizeBytes)}{item.totalBytes ? ` / ${formatBytes(item.totalBytes)}` : ""} · {formatBytes(item.speedBytesS ?? 0)}/s · 剩余 {formatDuration(item.etaSeconds)}</span>{item.message && <em>{item.message}</em>}</div>
                  <div className={`live-file-track ${item.totalBytes ? "" : "indeterminate"}`}><i style={item.totalBytes ? { width: `${itemPercent}%` } : undefined} /></div>
                  <span className="live-file-percent">{item.totalBytes ? `${itemPercent.toFixed(0)}%` : "下载中"}</span>
                </div>;
              })}
            </div>}
          </section>}

          <section className="metrics" id="collection-stages" aria-label="采集阶段">
            {data.latestRun.stages.map((stage, index) => (
              <article className="metric" key={stage.name}>
                <div className="metric-head"><span className="metric-label">{stageLabels[index]}</span><span className="metric-name">{stage.name}</span></div>
                <div className="metric-value">{nf.format(stage.value)} <span className="metric-note">{stage.note}</span></div>
                <div className="metric-details" aria-label={`${stage.name}输入输出`}>
                  <div><span>输入</span><strong>{nf.format(stageInputs[index] ?? stage.value)}</strong></div>
                  <span className="metric-arrow" aria-hidden="true">→</span>
                  <div><span>输出</span><strong>{nf.format(stage.value)}</strong></div>
                </div>
                <div className="metric-foot">
                  <span className={`status ${stage.status}`}>{stageStatus(stage.status)}</span>
                  <time>{clock(data.latestRun.startedAt)}</time>
                </div>
              </article>
            ))}
          </section>
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

          <div className="footer-note"><span>{autoRefresh && realtimeConnected ? "实时连接已建立" : autoRefresh ? "实时连接中断，已降级为 10 秒轮询" : "实时更新已暂停"}</span><span>快照生成于 {clock(data.generatedAt)}</span></div>
          </> : <>
          <section className="library-summary" aria-label="视频库摘要">
            <article><span>中转站文件</span><strong>{nf.format(data.overview.totalFiles)}</strong><small>{formatBytes(data.overview.totalBytes)}</small></article>
            <article><span>当前下载</span><strong>{nf.format(data.activeDownloads.length)}</strong><small>{data.activeDownloads.length ? "正在写入分片" : data.overview.partialDownloads ? `${data.overview.partialDownloads} 个分片等待续传` : "暂无活动下载"}</small></article>
            <article><span>待处理</span><strong>{nf.format(data.overview.pendingVideos)}</strong><small>包含等待解析与下载的项目</small></article>
          </section>

          <section className="panel" aria-labelledby="recent-files-title">
            <div className="panel-head">
              <div><h2 id="recent-files-title">最近入库</h2><p>按文件修改时间排序，仅显示已完成文件</p></div>
              <span className="quiet-meta">共 {nf.format(data.overview.totalFiles)} 个文件</span>
            </div>
            {data.recentFiles.length ? <div className="file-list">
              {data.recentFiles.map((file) => <div className="file-row" key={file.relativePath}>
                <span className="file-icon" aria-hidden="true">MP4</span>
                <span className="file-name"><strong>{file.name}</strong><small>{file.fileName}</small></span>
                <span>{formatBytes(file.sizeBytes)}</span>
                <time>{clock(file.modifiedAt)}</time>
                <span className="file-status">已完成</span>
              </div>)}
            </div> : <div className="empty-state"><span aria-hidden="true">↓</span><div><strong>中转站还是空的</strong><p>开始今日任务后，完成的视频会显示在这里。</p></div></div>}
          </section>

          {(data.activeDownloads.length > 0 || data.pendingDownloads.length > 0) && <section className="panel" aria-labelledby="queue-title">
            <div className="panel-head"><div><h2 id="queue-title">下载队列</h2><p>活动项优先显示，随后是等待项目</p></div></div>
            <div className="queue-list">
              {data.activeDownloads.map((item) => <div className="queue-row" key={item.fileName}><span className="queue-state active">{item.progressPercent != null ? `${item.progressPercent.toFixed(0)}%` : downloadStateLabel(item.status)}</span><strong>{item.name}</strong><span>{formatBytes(item.speedBytesS ?? 0)}/s · {formatDuration(item.etaSeconds)}</span></div>)}
              {data.pendingDownloads.slice(0, 20).map((item) => <div className="queue-row" key={item.fileName}><span className={`queue-state ${item.status === "resumable" ? "resume" : ""}`}>{item.status === "resumable" ? "续传" : "等待"}</span><strong>{item.name}</strong><span>{item.sizeBytes ? `${formatBytes(item.sizeBytes)} · ${item.fileName}` : item.fileName}</span></div>)}
            </div>
          </section>}
          <div className="footer-note"><span>仅展示本地文件，不上传媒体内容</span><span>快照生成于 {clock(data.generatedAt)}</span></div>
          </>}
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
          <section className="side-card compact">
            <span className="side-label">最近抓取</span><strong>{clock(data.source.latestCrawlAt)}</strong>
            <p>{data.source.listingCount} 个榜单，每榜 {data.source.pagesPerListing} 页</p>
          </section>
        </aside>
      </div>
    </main>
  );
}
