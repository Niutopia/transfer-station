"use client";

import { useCallback, useEffect, useMemo, useState, useRef, type FormEvent } from "react";

type ProgressData = {
  stage: string;
  mode?: "crawl" | "repair";
  route?: "direct" | "http-proxy" | null;
  routeProbe?: Record<string, unknown>;
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
  blocked?: number;
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
    blockedVideos: number;
    ignoredVideos: number;
    pendingVideos: number;
    repairableVideos: number;
    partialDownloads: number;
    todayFiles: number;
    todayBytes: number;
    totalFiles: number;
    totalBytes: number;
    todayIngestedFiles: number;
    todayIngestedBytes: number;
    ingestedFiles: number;
    ingestedBytes: number;
    downloadRate: number;
  };
  storage: { usedBytes: number; diskFreeBytes: number; diskTotalBytes: number; diskUsedPercent: number };
  daily: Array<{ date: string; label: string; files: number; bytes: number }>;
  latestRun: {
    status: "ready" | "attention" | "active";
    startedAt: string | null;
    lastCrawlAt?: string | null;
    lastRepairAt?: string | null;
    taskState?: "running" | "paused" | "cancelling" | null;
    taskKind?: "crawl" | "repair" | null;
    taskControllable?: boolean;
    completedToday?: boolean;
    completedAt?: string | null;
    result?: {
      status: "active" | "success" | "attention" | "failed" | "none";
      taskType?: "crawl" | "repair";
      startedAt?: string | null;
      finishedAt?: string | null;
      durationSeconds?: number | null;
      rawLinks: number;
      uniqueVideos: number;
      skippedVideos: number;
      newVideos: number;
      retryVideos: number;
      downloadedVideos: number;
      alreadyPresentVideos?: number;
      duplicateVideos: number;
      blockedVideos: number;
      ignoredVideos: number;
      failedVideos: number;
      downloadedBytes: number;
      listingFailures: number;
      autoRetryAttempts?: number;
      autoRetriedVideos?: number;
      autoRecoveredVideos?: number;
    };
  };
  alerts: Array<{ level: "success" | "info" | "warning" | "error"; scope?: "task" | "backlog"; title: string; detail: string }>;
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
};

type AuthCookieStatus = {
  configured: boolean;
  valid: boolean | null;
  hasClearance: boolean;
  cookieCount: number;
  updatedAt: string | null;
  browser: string | null;
  error: string | null;
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
  // The crawler reports the initial listing scan as `listing`.
  if (stage === "listing" || stage === "crawling") return "正在抓取榜单页面";
  if (stage === "resolving") return "正在解析媒体地址";
  if (stage === "route-probe") return "正在测试下载路线";
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
  const [startingTask, setStartingTask] = useState<"crawl" | "repair" | null>(null);
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
  const [authCookie, setAuthCookie] = useState<AuthCookieStatus | null>(null);
  const [authCookieValue, setAuthCookieValue] = useState("");
  const [authCookieChecking, setAuthCookieChecking] = useState(false);
  const [authCookieSaving, setAuthCookieSaving] = useState(false);
  const [authCookieFeedback, setAuthCookieFeedback] = useState("");
  const [authCookieError, setAuthCookieError] = useState("");
  const abortControllerRef = useRef<AbortController | null>(null);
  const authCookieFeedbackTimerRef = useRef<number | null>(null);
  const realtimeEverConnectedRef = useRef(false);
  const reloadAfterReconnectRef = useRef(false);
  const previousTaskStatusRef = useRef<MonitorData["latestRun"]["status"] | null>(null);

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

  const loadAuthCookie = useCallback(async () => {
    setAuthCookieChecking(true);
    try {
      const response = await fetch(`/api/auth-cookie?t=${Date.now()}`, { cache: "no-store" });
      const payload = await response.json().catch(() => ({})) as { cookie?: AuthCookieStatus; error?: string };
      if (!response.ok || !payload.cookie) throw new Error(payload.error || "Cookie 状态读取失败");
      setAuthCookie(payload.cookie);
    } catch (err) {
      setAuthCookie((current) => current ? { ...current, valid: null, error: err instanceof Error ? err.message : "Cookie 状态读取失败" } : null);
    } finally {
      setAuthCookieChecking(false);
    }
  }, []);

  const startTask = useCallback(async (mode: "crawl" | "repair") => {
    setStartingTask(mode);
    setTaskLaunching(true);
    setTaskError("");
    setTaskMessage("");
    setSourceFeedback("");
    try {
      const response = await fetch(mode === "repair" ? "/api/task/repair" : "/api/task", { method: "POST" });
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
      setTaskMessage(mode === "repair" ? "待处理项复核已启动，不会重新抓取榜单" : "抓取任务已启动，状态会自动更新");
      window.setTimeout(() => void load(), 120);
      window.setTimeout(() => setTaskLaunching(false), 8_000);
    } catch (err) {
      if (err instanceof TypeError) setServiceOnline(false);
      else void loadServiceHealth();
      setTaskLaunching(false);
      setTaskError(err instanceof Error ? err.message : "无法启动任务");
    } finally {
      setStartingTask(null);
    }
  }, [load, loadServiceHealth]);

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

  const updateAuthCookie = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAuthCookieSaving(true);
    setAuthCookieFeedback("");
    setAuthCookieError("");
    try {
      const response = await fetch("/api/auth-cookie", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookie: authCookieValue, userAgent: window.navigator.userAgent }),
      });
      const payload = await response.json().catch(() => ({})) as { cookie?: AuthCookieStatus; error?: string };
      if (!response.ok || !payload.cookie) throw new Error(payload.error || "Cookie 更新失败");
      setAuthCookie(payload.cookie);
      setAuthCookieFeedback("已保存；下一次人工抓取将确认 Cookie 状态");
      if (authCookieFeedbackTimerRef.current !== null) window.clearTimeout(authCookieFeedbackTimerRef.current);
      authCookieFeedbackTimerRef.current = window.setTimeout(() => {
        setAuthCookieFeedback("");
        authCookieFeedbackTimerRef.current = null;
      }, 4000);
    } catch (err) {
      setAuthCookieError(err instanceof Error ? err.message : "Cookie 更新失败，现有凭证未改变");
    } finally {
      // Never retain a pasted credential in React state after a validation attempt.
      setAuthCookieValue("");
      setAuthCookieSaving(false);
    }
  }, [authCookieValue]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      void load();
      void loadServiceHealth();
      void loadAuthCookie();
    });
    return () => {
      window.cancelAnimationFrame(frame);
      if (authCookieFeedbackTimerRef.current !== null) window.clearTimeout(authCookieFeedbackTimerRef.current);
      abortControllerRef.current?.abort();
    };
  }, [load, loadAuthCookie, loadServiceHealth]);

  useEffect(() => {
    if (!sourceFeedback) return;
    const timer = window.setTimeout(() => setSourceFeedback(""), 3_500);
    return () => window.clearTimeout(timer);
  }, [sourceFeedback]);

  useEffect(() => {
    const nextStatus = data?.latestRun.status ?? null;
    if (previousTaskStatusRef.current === "active" && nextStatus && nextStatus !== "active") {
      void loadAuthCookie();
    }
    previousTaskStatusRef.current = nextStatus;
  }, [data?.latestRun.status, loadAuthCookie]);

  useEffect(() => {
    if (!autoRefresh) return;
    const source = new EventSource("/api/events");
    source.onopen = () => {
      if (reloadAfterReconnectRef.current) {
        window.location.reload();
        return;
      }
      realtimeEverConnectedRef.current = true;
      setRealtimeConnected(true);
      setServiceOnline(true);
      void load();
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
    source.onerror = () => {
      if (realtimeEverConnectedRef.current) reloadAfterReconnectRef.current = true;
      setRealtimeConnected(false);
    };
    return () => source.close();
  }, [autoRefresh, load]);

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
  const listingInProgress = isCrawling && (currentProgress?.stage === "listing" || currentProgress?.stage === "crawling");
  const currentPercent = currentProgress?.total ? Math.min(100, currentProgress.done / currentProgress.total * 100) : 0;
  const aggregateSpeed = data.activeDownloads.reduce((total, item) => total + (item.speedBytesS ?? 0), 0);
  const currentKnownBytePercent = currentProgress?.bytesTotalKnown ? Math.min(100, (currentProgress.bytesDone ?? 0) / currentProgress.bytesTotalKnown * 100) : 0;
  const taskPaused = data.latestRun.taskState === "paused";
  const repairing = taskAppearsActive && (data.latestRun.taskKind === "repair" || currentProgress?.mode === "repair");
  const taskResult = data.latestRun.result;
  const resultIsRepair = taskResult?.taskType === "repair";
  const resultIsBlockedReview = Boolean(
    resultIsRepair
    && taskResult
    && taskResult.blockedVideos > 0
    && taskResult.downloadedVideos === 0
    && taskResult.failedVideos === 0
  );
  const cookieHealthClass = authCookie?.valid === true ? "online" : authCookie?.valid === false ? "offline" : "checking";
  const cookieHealthLabel = authCookieChecking ? "Cookie 状态读取中" : authCookie?.valid === true ? "Cookie 可用" : authCookie?.valid === false ? "Cookie 需更新" : authCookie?.configured ? "Cookie 已配置" : "Cookie 待配置";
  const cookieStatusDetail = authCookieChecking
    ? "正在读取最近一次手动抓取结果"
    : authCookie?.valid === true
      ? `最近一次手动抓取成功 · Cookie 更新于 ${clock(authCookie.updatedAt)}`
      : authCookie?.valid === false
        ? authCookie.error || "凭据不可用，请更新后再开始任务"
        : "已配置 · 完成人工抓取后确认状态";
  return (
    <main className="app-shell">
      <header className="topbar">
        <button className="brand" type="button" aria-label="返回页面顶部" onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
          <img className="brand-mark" src="/transfer-station-x.svg?v=1" alt="" aria-hidden="true" />
          <strong className="brand-title">TRANSFER STATION</strong>
        </button>

        <div className="top-actions">
          <div className={`service-health ${serviceOnline === false ? "offline" : serviceOnline === true ? "online" : "checking"}`}>
            <span aria-hidden="true" />
            {serviceOnline === false ? "任务离线" : serviceOnline === true ? "任务在线" : "检查服务"}
          </div>
          <button
            className={`service-health cookie-health ${cookieHealthClass}`}
            type="button"
            onClick={() => {
              const manager = document.getElementById("auth-cookie-manager") as HTMLDetailsElement | null;
              if (manager) {
                manager.open = true;
                manager.scrollIntoView({ behavior: "smooth", block: "center" });
              }
            }}
            aria-label={`${cookieHealthLabel}，前往 Cookie 任务栏`}
          >
            <span aria-hidden="true" />
            {cookieHealthLabel}
          </button>
          <button
            className={`live-button ${autoRefresh ? realtimeConnected ? "active" : "checking" : ""}`}
            type="button"
            aria-pressed={autoRefresh}
            onClick={() => setAutoRefresh((value) => !value)}
          >
            <span className="live-dot" aria-hidden="true" />
            {autoRefresh ? realtimeConnected ? "实时更新中" : "实时连接中" : "实时更新已暂停"}
          </button>
          <button className="icon-button" type="button" aria-label="刷新监控数据" onClick={() => { void load(); void loadAuthCookie(); }} disabled={refreshing || authCookieChecking}>
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
                {!taskAppearsActive && <>
                  {data.overview.repairableVideos > 0 && <button
                    className="secondary repair-btn"
                    type="button"
                    onClick={() => void startTask("repair")}
                    disabled={startingTask !== null || serviceOnline === false}
                    title="仅复核现有待处理项，串行低频访问详情页，不重新抓取榜单"
                  >
                    {serviceOnline === false ? "任务服务离线" : startingTask === "repair" ? "正在启动复核…" : `复核 ${data.overview.repairableVideos} 个待处理项`}
                  </button>}
                  <button
                    className="secondary highlight-btn"
                    type="button"
                    onClick={() => void startTask("crawl")}
                    disabled={startingTask !== null || serviceOnline === false || data.source.listingCount === 0}
                  >
                    {data.source.listingCount === 0 ? "请先添加抓取链接" : serviceOnline === false ? "任务服务离线" : startingTask === "crawl" ? "正在启动抓取…" : completedToday ? "再次抓取最新内容" : "开始抓取任务"}
                  </button>
                </>}
              </div>
            </div>
            <div className="current-task-grid">
              <span><small>当前阶段</small><strong>{taskLaunching && !isCrawling ? "任务准备" : !isCrawling || !currentProgress ? "等待启动" : currentProgress.stage === "downloading" ? "下载入库" : currentProgress.stage === "route-probe" ? "路线测速" : currentProgress.stage === "resolving" ? "媒体解析" : currentProgress.stage === "finalizing" ? "结果整理" : "榜单抓取"}</strong></span>
              <span><small>{repairing ? "复核范围" : "抓取范围"}</small><strong>{repairing ? `${data.overview.repairableVideos} 个待处理项` : `${data.source.listingCount} 榜 × ${data.source.pagesPerListing} 页`}</strong></span>
              <span><small>{taskAppearsActive ? listingInProgress ? "列表进度" : "处理进度" : "历史待处理"}</small><strong>{taskLaunching && !isCrawling ? "正在连接" : isCrawling && currentProgress ? currentProgress.total > 0 ? `${currentProgress.done}/${currentProgress.total}${listingInProgress ? " 页" : ""}` : "准备中" : data.overview.repairableVideos > 0 ? `${data.overview.repairableVideos} 个待复核` : "暂无任务"}</strong></span>
              <span><small>{taskAppearsActive ? "启动时间" : "最近抓取"}</small><strong>{taskLaunching && !isCrawling ? "刚刚" : isCrawling && currentProgress ? clock(currentProgress.startedAt ?? data.latestRun.startedAt) : clock(data.latestRun.lastCrawlAt ?? data.latestRun.completedAt ?? data.source.latestCrawlAt ?? null)}</strong></span>
            </div>
            {taskAppearsActive && <div className={`current-task-track ${(taskLaunching || currentProgress?.total === 0) ? "indeterminate" : ""}`} role={isCrawling && (currentProgress?.total ?? 0) > 0 ? "progressbar" : undefined} aria-valuenow={isCrawling && (currentProgress?.total ?? 0) > 0 ? currentProgress?.done : undefined} aria-valuemin={isCrawling && (currentProgress?.total ?? 0) > 0 ? 0 : undefined} aria-valuemax={isCrawling && (currentProgress?.total ?? 0) > 0 ? currentProgress?.total : undefined}><i style={isCrawling && (currentProgress?.total ?? 0) > 0 ? { width: `${currentPercent}%` } : undefined} /></div>}
            {isCrawling && currentProgress?.stage === "route-probe" && <div className="current-task-meta"><span>正在比较直连与 HTTP 代理</span><span>完成后自动选择更快路线</span></div>}
            {isCrawling && currentProgress?.stage === "downloading" && <div className="current-task-meta"><span>活动下载 {data.activeDownloads.length}</span><span>路线 {currentProgress.route === "http-proxy" ? "HTTP 代理" : "直连"}</span><span>实时速度 {formatBytes(currentProgress.speedBytesS ?? aggregateSpeed)}/s</span><span>预计剩余 {formatDuration(currentProgress.etaSeconds)}</span></div>}
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

          <section className="panel trend-panel" aria-labelledby="trend-title">
            <div className="panel-head">
              <div><h2 id="trend-title">近 7 天入库趋势</h2><p>按首次成功下载记录统计，删除文件不影响历史</p></div>
              <div className="trend-summary" aria-label="入库摘要">
                <span><strong>{data.overview.todayIngestedFiles}</strong>今日入库</span>
                <span><strong>{nf.format(data.overview.ingestedFiles)}</strong>历史入库</span>
                <span><strong>{formatBytes(data.overview.ingestedBytes)}</strong>累计下载</span>
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

          <details id="auth-cookie-manager" className={`cookie-manager settings-panel ${authCookie?.valid === false ? "needs-attention" : ""}`} aria-label="登录 Cookie 任务栏">
            <summary className="settings-summary">
              <div>
                <h2 id="auth-cookie-title">登录 Cookie</h2>
                <p>{cookieStatusDetail}</p>
              </div>
              <span className={`status ${authCookieChecking ? "active" : authCookie?.valid === true ? "done" : authCookie?.valid === false ? "attention" : "waiting"}`}>{authCookieChecking ? "读取中" : authCookie?.valid === true ? "可用" : authCookie?.valid === false ? "需更新" : "已配置"}</span>
            </summary>
            <div className="cookie-control-grid">
              <div className={`cookie-status-panel ${authCookie?.valid === false ? "invalid" : authCookie?.valid === true ? "valid" : "unknown"}`} aria-live="polite">
                <div className="cookie-status-title"><span aria-hidden="true" /><div><small>当前状态</small><strong>{authCookieChecking ? "正在读取" : authCookie?.valid === true ? "上次抓取成功" : authCookie?.valid === false ? "上次抓取被拦截" : "等待人工抓取确认"}</strong></div></div>
                <p>{authCookie?.valid === true ? "最近一次人工抓取成功，无需更新。" : authCookie?.valid === false ? "最近一次人工抓取确认被 Cloudflare 拦截，请更新 Cookie。" : "系统不会单独检测；状态以人工抓取结果为准。"}</p>
                <button className="cookie-check" type="button" onClick={() => void loadAuthCookie()} disabled={authCookieChecking || authCookieSaving}>{authCookieChecking ? "正在读取…" : "刷新状态"}</button>
              </div>
              <form className="cookie-form" onSubmit={updateAuthCookie} autoComplete="off" title="保存后由下一次人工抓取确认状态">
                <label>
                  <span>新 Cookie</span>
                  <textarea
                    value={authCookieValue}
                    onChange={(event) => { setAuthCookieValue(event.target.value); setAuthCookieFeedback(""); setAuthCookieError(""); }}
                    rows={6}
                    maxLength={65536}
                    required
                    autoComplete="off"
                    autoCapitalize="off"
                    spellCheck={false}
                    placeholder="name=value; name2=value2"
                    disabled={taskAppearsActive || authCookieSaving}
                  />
                </label>
                <div className="cookie-form-footer">
                  <div aria-live="polite">
                    {authCookieFeedback && <p className="source-feedback success cookie-feedback-transient" role="status">{authCookieFeedback}</p>}
                    {authCookieError && <p className="source-feedback error" role="alert">{authCookieError}</p>}
                    {!authCookieFeedback && !authCookieError && <p>仅保存在本机；下一次人工抓取会确认是否可用。</p>}
                  </div>
                  <button className="cookie-submit" type="submit" disabled={taskAppearsActive || authCookieSaving || !authCookieValue.trim()}>{taskAppearsActive ? "任务中·已锁定" : authCookieSaving ? "正在保存…" : "保存 Cookie"}</button>
                </div>
              </form>
            </div>
          </details>

          <details className="source-manager settings-panel" aria-label="抓取链接任务栏">
            <summary className="settings-summary">
              <div>
                <h2 id="source-manager-title">抓取链接</h2>
                <p>{data.source.sources?.length ?? 0} 个来源 · 每个抓取前 {data.source.pagesPerListing} 页</p>
              </div>
              <span className={`status ${taskAppearsActive ? "active" : (data.source.sources?.length ?? 0) === 0 ? "waiting" : "done"}`}>{taskAppearsActive ? "任务中·已锁定" : `${data.source.sources?.length ?? 0} 个链接`}</span>
            </summary>
            <div className="settings-body"><form className="source-form" onSubmit={addCrawlSource}>
              <label>
                <span>名称（可选）</span>
                <input value={sourceName} onChange={(event) => { setSourceName(event.target.value); setSourceFeedback(""); setSourceError(""); }} maxLength={40} placeholder="例如：最近热门" disabled={taskAppearsActive || sourceSaving} />
              </label>
              <label>
                <span>HTTPS 榜单 / 首页链接</span>
                <input type="url" value={sourceUrl} onChange={(event) => { setSourceUrl(event.target.value); setSourceFeedback(""); setSourceError(""); }} required placeholder="https://91porn.com/index.php 或 /v.php?..." disabled={taskAppearsActive || sourceSaving} />
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
            </div>
          </details>

          <div className="footer-note"><span>{autoRefresh && realtimeConnected ? "实时连接已建立" : autoRefresh ? "实时连接未建立，已降级为 10 秒轮询" : "实时更新已暂停"}</span><span>快照生成于 {clock(data.generatedAt)}</span></div>
        </section>

        <aside className="side-column" aria-label="运行状态">
          <section className={`side-card task-result-card result-${taskAppearsActive ? "active" : taskResult?.status ?? "none"}`} aria-labelledby="task-result-title">
            <div className="side-title">
              <h2 id="task-result-title">本次任务结果</h2>
              <span className={`status ${taskAppearsActive ? "active" : taskResult?.status === "failed" || taskResult?.status === "attention" ? "attention" : "done"}`}>{taskAppearsActive ? "进行中" : taskResult?.status === "failed" ? "失败" : taskResult?.status === "attention" ? "部分完成" : taskResult?.status === "success" ? "已完成" : "暂无"}</span>
            </div>
            <strong className="task-result-summary">{taskLaunching && !isCrawling ? "正在连接任务服务" : isCrawling ? progressTitle(currentProgress?.stage ?? "crawling") : taskResult?.status === "success" ? resultIsBlockedReview ? "错误媒体复核完成" : resultIsRepair ? "待处理项复核完成" : taskResult.newVideos || taskResult.retryVideos ? "采集与下载处理完成" : "检查完成，暂无新内容" : taskResult?.status === "attention" ? "部分榜单页面抓取失败" : taskResult?.status === "failed" ? resultIsRepair ? "部分待处理项仍待复核" : "任务未能完整完成" : "尚未运行任务"}</strong>
            {taskAppearsActive ? <div className="task-result-grid">
              <span><small>当前阶段</small><strong>{taskLaunching && !isCrawling ? "准备中" : currentProgress?.stage === "downloading" ? "下载入库" : currentProgress?.stage === "route-probe" ? "路线测速" : currentProgress?.stage === "resolving" ? "媒体解析" : repairing ? "修复准备" : "榜单抓取"}</strong></span>
              <span><small>{listingInProgress ? "列表进度" : "处理进度"}</small><strong>{currentProgress?.total ? `${currentProgress.done}/${currentProgress.total}${listingInProgress ? " 页" : ""}` : "计算中"}</strong></span>
              <span><small>活动下载</small><strong>{data.activeDownloads.length}</strong></span>
              <span><small>实时速度</small><strong>{formatBytes(currentProgress?.speedBytesS ?? aggregateSpeed)}/s</strong></span>
              {currentProgress?.route ? <span><small>下载线路</small><strong>{currentProgress.route === "direct" ? "直连" : "代理"}</strong></span> : null}
            </div> : taskResult && taskResult.status !== "none" ? <>
              <div className="task-result-grid">
                <span><small>{resultIsRepair ? "复核项目" : "检查链接"}</small><strong>{nf.format(resultIsRepair ? taskResult.retryVideos : taskResult.rawLinks)}</strong></span>
                <span><small>{resultIsRepair ? "已恢复" : "新发现"}</small><strong>{nf.format(resultIsRepair ? taskResult.downloadedVideos : taskResult.newVideos)}</strong></span>
                <span><small>本次入库</small><strong>{nf.format(taskResult.downloadedVideos)}</strong></span>
                <span><small>{resultIsRepair ? taskResult.failedVideos ? "仍未完成" : taskResult.blockedVideos ? "安全跳过" : taskResult.duplicateVideos ? "内容重复" : "复核项" : taskResult.failedVideos ? "失败" : taskResult.blockedVideos ? "媒体不匹配" : taskResult.duplicateVideos ? "内容重复" : "重试"}</small><strong>{nf.format(taskResult.failedVideos || taskResult.blockedVideos || taskResult.duplicateVideos || taskResult.retryVideos)}</strong></span>
              </div>
              <p className="task-result-detail">{resultIsRepair ? `仅复核现有快照中的 ${nf.format(taskResult.retryVideos)} 个待处理项目，未重新抓取榜单` : `去重后 ${nf.format(taskResult.uniqueVideos)} 个视频，跳过 ${nf.format(taskResult.skippedVideos)} 个已知编号`}{taskResult.listingFailures ? `，${nf.format(taskResult.listingFailures)} 个榜单页面未成功读取` : ""}{taskResult.blockedVideos ? `，确认并永久跳过 ${nf.format(taskResult.blockedVideos)} 个错误媒体地址` : ""}{taskResult.duplicateVideos ? `，内容指纹拦截 ${nf.format(taskResult.duplicateVideos)} 个重复` : ""}{taskResult.alreadyPresentVideos ? `，${nf.format(taskResult.alreadyPresentVideos)} 个文件已存在无需重复下载` : ""}{taskResult.autoRetryAttempts ? `，自动重试 ${nf.format(taskResult.autoRetryAttempts)} 次，自动恢复 ${nf.format(taskResult.autoRecoveredVideos ?? 0)} 个下载` : taskResult.autoRecoveredVideos ? `，自动恢复 ${nf.format(taskResult.autoRecoveredVideos)} 个下载` : ""}{taskResult.downloadedBytes ? `，实际入库 ${formatBytes(taskResult.downloadedBytes)}` : ""}。</p>
              <div className="task-result-time"><span>完成于 {clock(taskResult.finishedAt ?? null)}</span><span>{taskResult.durationSeconds ? `耗时 ${formatDuration(taskResult.durationSeconds)}` : "耗时未记录"}</span></div>
            </> : <p className="task-result-detail">点击“开始抓取任务”后，这里会显示本次检查与下载数据。</p>}
            {taskAppearsActive && taskMessage && <p className="task-result-feedback success" role="status">{taskMessage}</p>}
            {taskError && <p className="task-result-feedback error" role="alert">{taskError}</p>}
          </section>
          <section className="side-card">
            <div className="side-card-head"><div><span>磁盘空间</span><strong>{data.storage.diskUsedPercent}% 已使用</strong></div><span>{formatBytes(data.storage.diskFreeBytes)} 可用</span></div>
            <div className="storage-track" role="progressbar" aria-label="磁盘使用率" aria-valuemin={0} aria-valuemax={100} aria-valuenow={data.storage.diskUsedPercent}><i style={{ width: `${Math.min(100, data.storage.diskUsedPercent)}%` }} /></div>
            <div className="side-stats"><span><small>中转站</small><strong>{formatBytes(data.overview.totalBytes)}</strong></span><span><small>文件数</small><strong>{nf.format(data.overview.totalFiles)}</strong></span></div>
          </section>
          <section className="side-card">
            <div className="side-title"><h2>运行状态</h2><span className={`status ${data.latestRun.status === "active" ? "active" : data.latestRun.status === "attention" ? "attention" : "done"}`}>{data.latestRun.status === "active" ? "运行中" : data.latestRun.status === "attention" ? "需检查" : "就绪"}</span></div>
            <div className="alert-list">{data.alerts.map((alert, index) => <div className={`alert-item ${alert.level}`} key={`${alert.title}-${index}`}><span aria-hidden="true" /><div><strong>{alert.title}</strong><p>{alert.detail}</p></div></div>)}</div>
          </section>
        </aside>
      </div>
    </main>
  );
}
