// Shared visual vocabulary for the controller queue and worker. These are display keys, not workflow states.
import type { SvgIconComponent } from "@mui/icons-material";
import AccountTreeOutlined from "@mui/icons-material/AccountTreeOutlined";
import AltRouteOutlined from "@mui/icons-material/AltRouteOutlined";
import AutoAwesomeMotionOutlined from "@mui/icons-material/AutoAwesomeMotionOutlined";
import AutoFixHighOutlined from "@mui/icons-material/AutoFixHighOutlined";
import AutoFixNormalOutlined from "@mui/icons-material/AutoFixNormalOutlined";
import BackHandOutlined from "@mui/icons-material/BackHandOutlined";
import CheckCircleOutlineOutlined from "@mui/icons-material/CheckCircleOutlineOutlined";
import CleaningServicesOutlined from "@mui/icons-material/CleaningServicesOutlined";
import CloudOffOutlined from "@mui/icons-material/CloudOffOutlined";
import ComputerOutlined from "@mui/icons-material/ComputerOutlined";
import DeleteSweepOutlined from "@mui/icons-material/DeleteSweepOutlined";
import DownloadOutlined from "@mui/icons-material/DownloadOutlined";
import DriveFileMoveOutlined from "@mui/icons-material/DriveFileMoveOutlined";
import ErrorOutlineOutlined from "@mui/icons-material/ErrorOutlineOutlined";
import FactCheckOutlined from "@mui/icons-material/FactCheckOutlined";
import FileCopyOutlined from "@mui/icons-material/FileCopyOutlined";
import FormatListNumberedOutlined from "@mui/icons-material/FormatListNumberedOutlined";
import GrainOutlined from "@mui/icons-material/GrainOutlined";
import GraphicEqOutlined from "@mui/icons-material/GraphicEqOutlined";
import GridOnOutlined from "@mui/icons-material/GridOnOutlined";
import HelpOutlineOutlined from "@mui/icons-material/HelpOutlineOutlined";
import HourglassEmptyOutlined from "@mui/icons-material/HourglassEmptyOutlined";
import ImageSearchOutlined from "@mui/icons-material/ImageSearchOutlined";
import InsertDriveFileOutlined from "@mui/icons-material/InsertDriveFileOutlined";
import Inventory2Outlined from "@mui/icons-material/Inventory2Outlined";
import LayersOutlined from "@mui/icons-material/LayersOutlined";
import LockOutlined from "@mui/icons-material/LockOutlined";
import MemoryOutlined from "@mui/icons-material/MemoryOutlined";
import MovieOutlined from "@mui/icons-material/MovieOutlined";
import MoveToInboxOutlined from "@mui/icons-material/MoveToInboxOutlined";
import PendingActionsOutlined from "@mui/icons-material/PendingActionsOutlined";
import PersonOffOutlined from "@mui/icons-material/PersonOffOutlined";
import PersonSearchOutlined from "@mui/icons-material/PersonSearchOutlined";
import PlaylistAddOutlined from "@mui/icons-material/PlaylistAddOutlined";
import PowerSettingsNewOutlined from "@mui/icons-material/PowerSettingsNewOutlined";
import PushPinOutlined from "@mui/icons-material/PushPinOutlined";
import ReportProblemOutlined from "@mui/icons-material/ReportProblemOutlined";
import RuleFolderOutlined from "@mui/icons-material/RuleFolderOutlined";
import SendOutlined from "@mui/icons-material/SendOutlined";
import SensorsOutlined from "@mui/icons-material/SensorsOutlined";
import SkipNextOutlined from "@mui/icons-material/SkipNextOutlined";
import StopCircleOutlined from "@mui/icons-material/StopCircleOutlined";
import SyncOutlined from "@mui/icons-material/SyncOutlined";
import TaskOutlined from "@mui/icons-material/TaskOutlined";
import TranslateOutlined from "@mui/icons-material/TranslateOutlined";
import UploadFileOutlined from "@mui/icons-material/UploadFileOutlined";
import VerifiedUserOutlined from "@mui/icons-material/VerifiedUserOutlined";
import VideoSettingsOutlined from "@mui/icons-material/VideoSettingsOutlined";
import WifiOffOutlined from "@mui/icons-material/WifiOffOutlined";
import { Box, useTheme } from "@mui/material";
import type { FilmRemote, WorkerCurrent, WorkerFilm, WorkerTransfer, WorkflowStep } from "../../api";
import { archiveLive, asrLive, isRestoring, restoreLive, restoreStep, type StatusKey } from "./mediaModel";
import { activeWorkerTransfer, matchingWorkerCurrent } from "./workerModel";

const PALETTE = {
  waiting: ["#b6c3d4", "#202d40", "#42526a", "#edf1f6"],
  restore: ["#91bdff", "#182e50", "#1957b0", "#eaf2ff"],
  asr: ["#c7a7ff", "#332348", "#6c35a7", "#f3eaff"],
  translate: ["#f6aad3", "#422438", "#a22869", "#fcecf5"],
  transfer: ["#7edce9", "#173841", "#07647a", "#e5f7fb"],
  organize: ["#b0b7ff", "#292c50", "#4549a6", "#eef0ff"],
  archive: ["#70dfc6", "#173c36", "#086958", "#e3f6f0"],
  cleanup: ["#efb88b", "#3e2c20", "#864316", "#fff0e4"],
  complete: ["#99e5a3", "#203b29", "#216d31", "#eaf7ed"],
  action: ["#f2cb72", "#3f341e", "#775310", "#fff5db"],
  blocked: ["#ffb58f", "#442b24", "#9b3f16", "#fff0e8"],
  error: ["#ffa8ae", "#45252e", "#ae253f", "#ffeef0"],
} as const;
type Tone = keyof typeof PALETTE;
type Meta = readonly [label: string, tone: Tone, icon: SvgIconComponent, waiting?: boolean];
const BASE: Record<StatusKey, Meta> = {
  pending: ["待处理", "waiting", PlaylistAddOutlined, true],
  receiving: ["等待收件检查", "waiting", MoveToInboxOutlined, true],
  inbox: ["排队中", "waiting", FormatListNumberedOutlined, true],
  waiting: ["等待处理", "waiting", HourglassEmptyOutlined, true],
  checking: ["检查入库目标", "waiting", FactCheckOutlined, true],
  restore: ["正在修复", "restore", AutoFixHighOutlined],
  encode: ["LTX · 编码", "restore", VideoSettingsOutlined],
  denoise: ["LTX · 去噪", "restore", GrainOutlined],
  asr: ["语音识别", "asr", GraphicEqOutlined],
  translate: ["字幕翻译", "translate", TranslateOutlined],
  return: ["回传成品", "transfer", UploadFileOutlined],
  scrape: ["整理成品", "organize", AutoAwesomeMotionOutlined],
  match: ["演员匹配", "organize", PersonSearchOutlined],
  ready: ["待确认入库", "action", TaskOutlined],
  awaiting_confirmation: ["待确认入库", "action", TaskOutlined],
  archiving: ["已确认 · 等待归档", "waiting", PendingActionsOutlined, true],
  cleanup: ["已入库 · 待清理", "cleanup", CleaningServicesOutlined, true],
  completed: ["已完成", "complete", CheckCircleOutlineOutlined],
  skipped: ["已跳过 · 保留来源", "waiting", SkipNextOutlined],
  postprocess_error: ["成品封面待处理", "action", ImageSearchOutlined],
  multi: ["多个目标候选", "action", AccountTreeOutlined],
  nomatch: ["暂无演员匹配", "action", PersonOffOutlined],
  intake_attention: ["收件需要处理", "action", RuleFolderOutlined],
  stopping: ["正在确认停止", "blocked", StopCircleOutlined],
  offline: ["目标 NAS 离线", "blocked", CloudOffOutlined],
  permission: ["目标权限不足", "blocked", LockOutlined],
  duplicate: ["入库目标冲突", "blocked", FileCopyOutlined],
  unknown: ["入库结果未知", "error", HelpOutlineOutlined],
  failed: ["处理失败", "error", ErrorOutlineOutlined],
  device_lost: ["设备失联", "error", WifiOffOutlined],
  source_missing: ["片源丢失", "error", InsertDriveFileOutlined],
};
const EXTRA = {
  fetching: ["下载源文件", "transfer", DownloadOutlined],
  staged: ["源文件就绪", "waiting", Inventory2Outlined, true],
  uploading: ["回传中", "transfer", UploadFileOutlined],
  finishing: ["提交结果", "transfer", SendOutlined],
  settling: ["停止未确认", "blocked", StopCircleOutlined],
  loading: ["加载修复模型", "restore", MemoryOutlined],
  scan: ["扫描马赛克", "restore", GridOnOutlined],
  basic: ["BasicVSR++ 修复", "restore", AutoFixNormalOutlined],
  compose: ["LTX · 合成", "restore", LayersOutlined],
  archive_copy: ["正在归档", "archive", DriveFileMoveOutlined],
  archive_verify: ["校验归档", "archive", VerifiedUserOutlined],
  cleanup_running: ["正在清理", "cleanup", DeleteSweepOutlined],
  online: ["在线", "waiting", SensorsOutlined],
  heartbeat_wait: ["等待心跳", "blocked", SyncOutlined, true],
  local: ["本机", "waiting", ComputerOutlined],
  idle: ["空闲", "waiting", PowerSettingsNewOutlined],
  hold_workflow: ["媒体任务占用", "waiting", MovieOutlined],
  hold_manual: ["手动任务占用", "waiting", BackHandOutlined],
  hold_unknown: ["GPU 未知占用", "blocked", ReportProblemOutlined],
  auto: ["自动派单", "waiting", AltRouteOutlined],
  pinned: ["指定机器", "waiting", PushPinOutlined],
} as const satisfies Record<string, Meta>;
export type PresentationKey = StatusKey | keyof typeof EXTRA;
const META: Record<PresentationKey, Meta> = { ...BASE, ...EXTRA };
export type StatusFilm = FilmRemote & { steps?: WorkflowStep[] | null; note?: string | null };
export interface Presentation { key: PresentationKey; percent: number | null }
const percent = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;

// Only refine a compatible status with a current structured step; stale compute data never wins over return/stop.
export function presentFilmStatus(status: StatusKey, film?: StatusFilm): Presentation {
  const plain = { key: status, percent: null };
  if (!film) return plain;
  if (isRestoring(status)) {
    const step = restoreStep(film);
    if (step?.state !== "running") return plain;
    if (step.phase === "uploading" || step.phase === "finishing" || step.phase === "settling") return { key: step.phase, percent: null };
    const live = restoreLive(step);
    if (live?.ltx) {
      const key = live.ltx.stage_name;
      return { key: key === "scan" || key === "compose" || key === "encode" || key === "denoise" ? key : status,
        percent: percent(live.ltx.percent) };
    }
    if (live?.scan) return { key: "scan", percent: percent(live.scan.percent) };
    if (live?.basic) return { key: "basic", percent: percent(live.basic.percent) };
    return { key: live?.phase === "starting" ? "loading" : status, percent: null };
  }
  if (status === "archiving") {
    const step = film.steps?.find((s) => s.key === "archive" && s.state === "running");
    if (!step) return plain;
    const live = archiveLive(step);
    return { key: live?.phase === "verifying" ? "archive_verify" : "archive_copy", percent: percent(live?.percent) };
  }
  if (status === "cleanup") return { key: film.steps?.some((s) => s.key === "cleanup" && s.state === "running")
    ? "cleanup_running" : status, percent: null };
  const step = film.steps?.find((s) => s.key === status && s.state === "running");
  if (!step) return plain;
  if (status === "asr") {
    if (step.phase === "uploading" || step.phase === "finishing" || step.phase === "settling") return { key: step.phase, percent: null };
    return { key: status, percent: percent(asrLive(step)?.percent) };
  }
  return plain;
}

export function presentWorkerStatus(film: WorkerFilm, current?: WorkerCurrent | null, transfers: WorkerTransfer[] = []): Presentation {
  let presentation: Presentation;
  const mine = matchingWorkerCurrent(film, current);
  if (mine) {
    presentation = mine.phase !== "running" ? { key: mine.phase, percent: null }
      : presentFilmStatus(mine.stage, { steps: mine.step ? [{ ...mine.step, phase: "running" }] : [] });
  } else presentation = { key: film.state === "queued" ? "inbox" : film.state === "running" ? "restore" : film.state, percent: null };
  const transfer = activeWorkerTransfer(film.film, presentation.key, transfers);
  return transfer && typeof transfer.done === "number" && Number.isFinite(transfer.done)
    && Number.isFinite(transfer.total) && transfer.total > 0
    ? { ...presentation, percent: percent(transfer.done / transfer.total * 100) } : presentation;
}

export function StatusBadge({ status, label }: { status: PresentationKey; label?: string }) {
  const mode = useTheme().palette.mode;
  const [text, tone, Icon, waiting = false] = META[status] ?? META.unknown;
  const colors = PALETTE[tone];
  const [color, background] = mode === "light" ? colors.slice(2) : colors;
  return <Box component="span" className="status-badge" data-status={status} data-tone={tone} data-waiting={waiting}
    sx={{ display: "inline-flex", alignItems: "center", gap: "6px", px: "8px", py: "3px", maxWidth: "100%",
      border: "1px solid", borderColor: `${color}66`, borderStyle: waiting ? "dashed" : "solid", borderRadius: "5px",
      bgcolor: background, color, fontSize: "12px", lineHeight: 1.6, verticalAlign: "middle", whiteSpace: "normal" }}>
    <Icon aria-hidden sx={{ fontSize: 16, flexShrink: 0 }} />{label ?? text}
  </Box>;
}

export function StatusProgress({ presentation, label }: { presentation: Presentation; label: string }) {
  const mode = useTheme().palette.mode;
  if (presentation.percent === null) return null;
  const value = Math.round(presentation.percent);
  const color = PALETTE[META[presentation.key][1]][mode === "light" ? 2 : 0];
  return <Box sx={{ display: "flex", alignItems: "center", gap: "7px", mt: "5px", maxWidth: 190 }}>
    <Box role="progressbar" aria-label={`${label} 当前阶段进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value}
      sx={{ flex: 1, minWidth: 50, height: 4, bgcolor: `${color}26`, borderRadius: 2, overflow: "hidden" }}>
      <Box sx={{ width: `${presentation.percent}%`, height: "100%", bgcolor: color }} />
    </Box><Box component="span" sx={{ fontSize: 11, color, fontVariantNumeric: "tabular-nums" }}>{value}%</Box>
  </Box>;
}
