import type { SvgIconComponent } from "@mui/icons-material";
import CloseIcon from "@mui/icons-material/Close";
import ComputerIcon from "@mui/icons-material/Computer";
import DesktopWindowsIcon from "@mui/icons-material/DesktopWindows";
import FactCheckIcon from "@mui/icons-material/FactCheck";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import GridViewIcon from "@mui/icons-material/GridView";
import InsightsIcon from "@mui/icons-material/Insights";
import LanIcon from "@mui/icons-material/Lan";
import MenuIcon from "@mui/icons-material/Menu";
import MovieIcon from "@mui/icons-material/Movie";
import PlaylistAddCheckIcon from "@mui/icons-material/PlaylistAddCheck";
import TuneIcon from "@mui/icons-material/Tune";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { Dialog, GlobalStyles, Snackbar, useMediaQuery } from "@mui/material";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { POWER_CSS } from "../power/powerStyles";
import { api as requests, type HubServer } from "../../api";
import { AnalyticsPage } from "./analytics/AnalyticsPage";
import { DevicesPage } from "./DevicesPage";
import { ExceptionsPage } from "./ExceptionsPage";
import { ExplainDialog, type Explain } from "./ExplainDialog";
import { FilmDrawer } from "./FilmDrawer";
import { LaterPage } from "./LaterPages";
import { ControllerOnly, LibraryPage } from "./LibraryPage";
import { LocalPage } from "./LocalPage";
import { MEDIA_CSS } from "./mediaStyles";
import type { StageFilter } from "./mediaModel";
import { OverviewPage } from "./OverviewPage";
import { Chip, Icon, Notice } from "./parts";
import { PendingPage } from "./PendingPage";
import { QueuePage } from "./QueuePage";
import { ReviewDialog } from "./ReviewDialog";
import { ReviewPage } from "./ReviewPage";
import { SettingsPage } from "./SettingsPage";
import {
  errorText, focusOnMount, isPaused, mediaFilmKey, mediaSourceKey, useMediaData, WorkspaceContext, type MediaData, type PageKey, type WorkspaceApi,
} from "./workspace";

// The eleven pages of the workspace, in rail order (build spec §1 + U1 V2). Mac 信息桥 is a later page,
// marked 「后续」 (operator decision 3); 设备 is live since P2-2, 片库与规则 and 设置 since #361, 数据分析
// since the analytics EPIC (C3 #390).
const PAGES: Array<{ key: PageKey; label: string; icon: SvgIconComponent; later?: boolean }> = [
  { key: "overview", label: "概览", icon: GridViewIcon },
  { key: "pending", label: "待处理", icon: PlaylistAddCheckIcon },
  { key: "queue", label: "影片队列", icon: MovieIcon },
  { key: "review", label: "待确认", icon: FactCheckIcon },
  { key: "exceptions", label: "异常", icon: WarningAmberIcon },
  { key: "devices", label: "设备", icon: DesktopWindowsIcon },
  { key: "local", label: "本机", icon: ComputerIcon },
  { key: "libraries", label: "片库与规则", icon: FolderOpenIcon },
  { key: "analytics", label: "数据分析", icon: InsightsIcon },
  { key: "bridge", label: "Mac 信息桥", icon: LanIcon, later: true },
  { key: "settings", label: "设置", icon: TuneIcon },
];
const PREFLIGHT_PAGES = new Set<PageKey>(["overview", "review", "exceptions"]);
const LIVE_PAGES = new Set<PageKey>(["overview", "pending", "queue", "review", "exceptions", "devices", "local"]);
const NAV_BACKDROP = { sx: { backgroundColor: "#030812CC" } };

function badge(key: PageKey, data: MediaData): number | null {
  if (key === "pending") return data.pendingCount;
  if (key === "review") return data.review.length;
  if (key === "exceptions") return data.exceptions.length;
  return null;
}

function NavList({ page, data, onPick, label }: { page: PageKey; data: MediaData; onPick: (p: PageKey) => void; label: string }) {
  return (
    <nav className="nav-group" aria-label={label}>
      {PAGES.map((p) => {
        const count = badge(p.key, data);
        return (
          <button key={p.key} type="button" className="nav-item" data-nav={p.key}
            aria-current={page === p.key ? "page" : undefined} onClick={() => onPick(p.key)}>
            <Icon of={p.icon} /><span>{p.label}</span>
            {count !== null && <span className="nav-count" aria-label={`${count} 项`}>{count}</span>}
            {p.later && <span className="nav-later">后续</span>}
          </button>
        );
      })}
    </nav>
  );
}

// 「媒体处理中心」 (build spec V3 + U1). Phase 1 mounts it as the AgentConsole tab; it does not
// depend on the console, so the Hub can mount the same component later. `onOpenPower` (PWR-2): the
// 集群功耗 card's 「功耗详情」 link, given by a host that has the 功耗 tab (the Agent console only);
// `onOpenSettings` (P2-4): 设备's 「主控设置」 → 设置 · 媒体工作流, likewise.
type Props =
  | { readOnly?: false; server?: never; onOpenPower?: () => void; onOpenSettings?: () => void }
  | { readOnly: true; server: HubServer; onOpenPower?: never; onOpenSettings?: never };

export function MediaWorkspace(props: Props = {}) {
  // Source changes close old drawers/lightboxes and reset navigation to the new source.
  return <MediaWorkspaceContent key={props.server?.id ?? "agent"} {...props} />;
}

function MediaWorkspaceContent({ readOnly = false, server, onOpenPower, onOpenSettings }: Props) {
  const narrow = useMediaQuery("(max-width:900px)");
  const [page, setPage] = useState<PageKey>("overview");
  const [queueStage, setQueueStage] = useState<StageFilter>("all");
  const [drawer, setDrawer] = useState<string | null>(null);
  const [review, setReview] = useState<string | null>(null);
  // The target preflight polls only while a view that shows it is mounted (IR05); 异常 lists the
  // films whose target is offline, unwritable or taken, so it refreshes them too (Codex r6 P2).
  const data = useMediaData(PREFLIGHT_PAGES.has(page) || review !== null, server?.id, readOnly);
  const [explain, setExplain] = useState<Explain>(null);
  const [toast, setToast] = useState<{ text: string; key: number } | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const mainRef = useRef<HTMLElement>(null);
  const [moves, setMoves] = useState(0);
  const navPicked = useRef(false);
  const navTrigger = useRef<HTMLButtonElement>(null);

  // Back to the top of the page; the app bar above the workspace is sticky, so not scrollIntoView.
  const toTop = () => window.scrollTo({ top: 0 });
  const navigate = useCallback((next: PageKey, opts?: { stage?: StageFilter }) => {
    if (opts?.stage) setQueueStage(opts.stage);
    setPage(next);
    setMoves((n) => n + 1);
  }, []);
  // On navigation: focus <main> and bring it to the top (§1); the nav dialog does it once closed.
  useEffect(() => {
    if (moves === 0 || navPicked.current) return;
    mainRef.current?.focus({ preventScroll: true });
    toTop();
  }, [moves]);

  const api: WorkspaceApi = useMemo(() => ({
    data, navigate, queueStage, setQueueStage, readOnly, server,
    readFilm: (id) => server ? requests.hubWorkflowFilm(server.id, id) : requests.workflowFilm(id),
    filmKey: (id) => mediaSourceKey(mediaFilmKey(id), server?.id),
    openDetail: (id) => { setReview(null); setExplain(null); setDrawer(id); },
    openReview: (id) => { if (readOnly) return; setDrawer(null); setExplain(null); setReview(id); },
    openExplain: (kind, id) => { setDrawer(null); setReview(null); setExplain({ kind, filmId: id }); },
    toast: (text) => setToast({ text, key: Date.now() }),
    openPower: readOnly ? undefined : onOpenPower,
    openSettings: readOnly ? undefined : onOpenSettings,
  }), [data, navigate, queueStage, readOnly, server, onOpenPower, onOpenSettings]);

  const current = PAGES.find((p) => p.key === page) ?? PAGES[0];
  const films = data.films.data;
  const running = data.running;
  const paused = isPaused(data);
  const gpuFilm = films?.current;

  let body: ReactNode;
  // 片库与规则, 数据分析 and 设置 read their own routes (数据分析 shows a stopped pipeline itself), so they
  // do not wait for the pipeline; the Hub has none of them (analytics D10)
  if (page === "libraries" || page === "analytics" || page === "settings") {
    body = readOnly ? <ControllerOnly title={current.label} />
      : page === "libraries" ? <LibraryPage /> : page === "analytics" ? <AnalyticsPage /> : <SettingsPage />;
  } else if (!LIVE_PAGES.has(page)) body = <LaterPage />;
  else if (data.films.isLoading) body = <div className="panel empty"><p>正在读取媒体流水线…</p></div>;
  else if (data.films.error || !films) body = <Notice tone="red" icon={WarningAmberIcon}>无法读取媒体流水线，请重试。{data.films.error ? `（${errorText(data.films.error)}）` : ""}</Notice>;
  else if (!running) body = <Notice icon={WarningAmberIcon}>媒体流水线未运行：{films.reason || films.state}</Notice>;
  else {
    body = page === "overview" ? <OverviewPage /> : page === "pending" ? <PendingPage /> : page === "queue" ? <QueuePage />
      : page === "review" ? <ReviewPage /> : page === "exceptions" ? <ExceptionsPage />
        : page === "devices" ? <DevicesPage /> : <LocalPage />;
  }

  return (
    <WorkspaceContext.Provider value={api}>
      <GlobalStyles styles={MEDIA_CSS} />
      <GlobalStyles styles={POWER_CSS} />
      <div className="mw mw-root" data-testid="media-workspace">
        {readOnly && <Notice>只读监控 · 操作请在主控电脑上进行</Notice>}
        <a className="skip-link" href="#mw-main" onClick={(e) => { e.preventDefault(); mainRef.current?.focus(); }}>跳至主要内容</a>
        <div className="scope-banner">
          <div className="scope-left">
            {narrow && (
              <button type="button" className="btn ghost mobile-menu" aria-haspopup="dialog" ref={navTrigger} onClick={() => setNavOpen(true)}>
                <Icon of={MenuIcon} />工作区导航
              </button>
            )}
            <span>TaskPaw / 媒体处理中心 / {current.label}</span>
          </div>
          <div className="health" aria-label="运行状态">
            <Chip tone={running ? "green" : "amber"}>主控 · {running ? "运行中" : "未运行"}</Chip>
            {running && <Chip tone={paused ? "amber" : "green"}>{paused ? "新领取已暂停" : "可领取新任务"}</Chip>}
            {running && <Chip tone={gpuFilm ? "blue" : "neutral"}>GPU · {gpuFilm ? `占用中：${gpuFilm.film}` : "空闲"}</Chip>}
          </div>
        </div>
        <div className="workspace-shell">
          {!narrow && (
            <aside className="workspace-rail">
              <div className="workspace-heading"><strong>媒体处理中心</strong></div>
              <NavList page={page} data={data} onPick={navigate} label="媒体处理中心导航" />
            </aside>
          )}
          <main className="content" id="mw-main" tabIndex={-1} ref={mainRef} aria-label={current.label}>{body}</main>
        </div>
        <Snackbar key={toast?.key} open={toast !== null} autoHideDuration={4800} onClose={() => setToast(null)}
          anchorOrigin={{ vertical: "bottom", horizontal: "center" }}>
          <div role="status" aria-live="polite" style={{
            padding: "13px 18px", background: "#22364A", border: "1px solid #4D6A81", borderRadius: 9,
            color: "#ECF6FF", fontSize: 12, boxShadow: "0 7px 30px #0008", maxWidth: "min(620px, calc(100vw - 28px))",
          }}>{toast?.text}</div>
        </Snackbar>
      </div>
      <Dialog open={navOpen} onClose={() => setNavOpen(false)} disableAutoFocus slotProps={{ backdrop: NAV_BACKDROP }}
        TransitionProps={{ onExited: () => {
          if (navPicked.current) { navPicked.current = false; mainRef.current?.focus({ preventScroll: true }); toTop(); }
          else navTrigger.current?.focus();
        } }}
        PaperProps={{ className: "mw mw-nav", sx: { backgroundImage: "none" }, "aria-labelledby": "mw-nav-title" } as object}>
        <div className="mobile-nav-head">
          <h2 id="mw-nav-title" tabIndex={-1} ref={focusOnMount}>工作台导航</h2>
          <button type="button" className="btn ghost icon-btn" aria-label="关闭主导航" onClick={() => setNavOpen(false)}><Icon of={CloseIcon} /></button>
        </div>
        <NavList page={page} data={data} label="媒体处理中心导航"
          onPick={(p) => { navPicked.current = true; setNavOpen(false); navigate(p); }} />
      </Dialog>
      <FilmDrawer filmId={drawer} onClose={() => setDrawer(null)} />
      {!readOnly && <ReviewDialog filmId={review} onClose={() => setReview(null)} />}
      <ExplainDialog explain={explain} onClose={() => setExplain(null)} />
    </WorkspaceContext.Provider>
  );
}
