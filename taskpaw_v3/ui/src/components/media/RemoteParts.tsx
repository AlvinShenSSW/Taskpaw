// P2-4 (#295) §4: the multi-machine parts of the 媒体处理中心 — the drawer's 设备 line with 改派 and
// 判定已停止 (C4: both channels dead for 5 min), and the 概览 rows of 设备失联 films. The Mac's read-only
// workspace shows the machine names and the 失联 state; every mutation is hidden there.
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, HttpError, type FilmDetail, type FilmStopAction, type ReassignOption } from "../../api";
import {
  clockText, energyText, lastBeat, liveChip, ltxOf, percentOf, progressText, silentText, stageName, unroutedMachine,
  type ChipTone, type WorkItem,
} from "./mediaModel";
import { Chip, Icon, ItemCover, Notice, StatusChip } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { errorText, MEDIA_FILMS_KEY, MEDIA_PLANS_KEY, mediaFilmKey, useWorkspace } from "./workspace";

// ── the drawer's 设备 line (V1, R7, R8) ─────────────────────────────────
export function DeviceSection({ film }: { film: FilmDetail }) {
  const ws = useWorkspace();
  const [reassigning, setReassigning] = useState(false);
  const [attesting, setAttesting] = useState(false);
  const route = film.route ?? null;
  const stop = film.actions?.stop ?? null;
  const canReassign = Boolean(film.actions?.can_reassign && film.pin && film.revisions.input);
  // §9 CX30: with no route and no remote claim, 「待派发」 only while the restore waits for a device,
  // 本机 for the controller's own stages afterwards, and no 设备 line once the film is done
  const fallback = route || stop ? null : unroutedMachine(film);
  if (!route && !stop && fallback === null) return null;
  const now = Date.now();
  const name = route?.machine.name || stop?.worker.name || fallback || "";
  const local = route ? route.live === "local" : !stop && fallback === "本机";
  const [state, tone]: [string, ChipTone] = route ? liveChip(route)
    : stop ? ((stop.heartbeat_silent_s ?? 0) >= 60 ? [`失联 ${silentText(stop.heartbeat_silent_s)}`, "red"] : ["在线", "green"])
      : local ? ["本机", "neutral"] : ["待派发", "amber"];
  const lost = route?.live === "lost" || (!route && stop !== null && (stop.heartbeat_silent_s ?? 0) >= 60);
  const silent = route?.silent_s ?? stop?.heartbeat_silent_s ?? null;
  const reason = lost ? `心跳中断 · 后台重连中 · 最后心跳 ${lastBeat(silent, now)}` : route?.reason || film.note || null;
  const reassignWhy = canReassign ? null
    : stop ? `已开始${stageName(stop.stage)}，需先判定已停止` : "当前阶段不能改派";
  const id = film.film_id;
  return (
    <section className="panel device-line spacer-top" aria-label="设备" data-testid="device-line">
      <dl className="facts">
        <dt>设备</dt>
        <dd><span className="device-line-name">{name}</span> {!local && <Chip tone={tone}>{state}</Chip>}</dd>
        {reason && <><dt>原因</dt><dd>{reason}</dd></>}
        {film.pin && <><dt>派单</dt><dd>{film.pin.worker_id === null ? "自动" : `指定 ${film.pin.name}`}</dd></>}
      </dl>
      {!ws.readOnly && (
        <div className="device-line-actions">
          <div className="device-line-action">
            <button type="button" className="btn" disabled={!canReassign} aria-describedby={reassignWhy ? `reassign-why-${id}` : undefined}
              onClick={() => setReassigning(true)}>改派</button>
            {reassignWhy && <span className="small muted" id={`reassign-why-${id}`}>{reassignWhy}</span>}
          </div>
          {stop && (
            <div className="device-line-action">
              <button type="button" className="btn danger" disabled={!stop.allowed}
                aria-describedby={!stop.allowed && stop.reason ? `attest-why-${id}` : undefined}
                onClick={() => setAttesting(true)}>判定已停止</button>
              {!stop.allowed && stop.reason && <span className="small amber" id={`attest-why-${id}`}>{stop.reason}</span>}
            </div>
          )}
        </div>
      )}
      {!ws.readOnly && canReassign && (
        <ReassignDialog open={reassigning} film={film} current={local ? name : `${name} · ${state}`} onClose={() => setReassigning(false)} />
      )}
      {!ws.readOnly && stop && (
        <AttestDialog open={attesting} filmId={id} filmName={film.title || film.name} stop={stop}
          progress={stop.stage === "restore" ? percentOf(ltxOf(film)) : null} onClose={() => setAttesting(false)} />
      )}
    </section>
  );
}

// 「预计 22:05 在 5070 Ti 完成 · 约 0.9 度」 (自动 names its device), as 待处理's select (预计: advisory).
function estimateText(o: ReassignOption, now: number): string | null {
  const where = o.worker_id === null && o.machine?.name ? ` 在 ${o.machine.name}` : "";
  const finish = o.finish_at ? `预计 ${clockText(o.finish_at, now)}${where} 完成` : null;
  return [finish, energyText(o.energy_wh)].filter(Boolean).join(" · ") || null;
}
const optionLabel = (o: ReassignOption, now: number) =>
  [o.worker_id === null ? "自动（推荐）" : o.name, o.reason ?? estimateText(o, now), o.current ? "当前" : null]
    .filter(Boolean).join(" · ");

// 改派 (R7): the drawer's choices (`reassign_options`) — 自动 first, then each device or why it cannot
// take the film now. A claim that wins the race answers 「已开始处理」 and nothing changes.
function ReassignDialog({ open, film, current, onClose }: { open: boolean; film: FilmDetail; current: string; onClose: () => void }) {
  return (
    <ModalShell open={open} onClose={onClose} titleId="mw-reassign-title">
      {open && <ReassignPane film={film} current={current} onClose={onClose} />}
    </ModalShell>
  );
}

function ReassignPane({ film, current, onClose }: { film: FilmDetail; current: string; onClose: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const options = film.reassign_options ?? [];
  const current0 = options.find((o) => o.current);
  const [pick, setPick] = useState<string>(() => (current0?.worker_id ?? ""));
  const chosen = options.find((o) => (o.worker_id ?? "") === pick);
  const title = film.title || film.name;
  const route = film.route ?? null;
  const now = Date.now();
  // §9 CX29: 自动 again for a film routed to a 失联 worker withdraws that route, so the next pass
  // routes it elsewhere (the backend's reassign(worker_id=None) on a lost route)
  const reroute = Boolean(chosen?.current && chosen.worker_id === null && route?.live === "lost");
  // IR9: why 确认改派 is disabled
  const blocked = !chosen ? "请选择机器" : chosen.reason !== null ? chosen.reason
    : chosen.current && !reroute ? "与当前派单相同" : null;
  // IR4: the route moves to another worker (a new pin elsewhere, or 自动 off a 失联 one), which then
  // releases its staging (P2-3 R10); 自动 off a healthy route keeps it (IR8)
  const remote = route && route.live !== "local" ? route : null;
  const moves = Boolean(remote && chosen && (reroute || (chosen.worker_id !== null && chosen.worker_id !== remote.machine.id)));
  const cleanup = moves && remote ? `；${remote.machine.name}${remote.live === "lost" ? " 恢复后" : " "}会自动清理它的暂存` : "";
  const restore = (film.steps ?? []).find((s) => s.key === "restore");
  const stage = restore && restore.state !== "done" ? "修复" : "识别";
  const save = useMutation({
    mutationFn: () => {
      if (ws.readOnly || !film.pin || !film.revisions.input || !chosen) throw new Error("只读监控");
      return api.workflowPin(film.film_id, film.revisions.input.revision, film.pin.version, chosen.worker_id);
    },
    onSuccess: () => {
      ws.toast(reroute ? `${title} 已重新自动派单` : `${title} 已改派到 ${chosen?.worker_id === null ? "自动" : chosen?.name ?? ""}`);
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: MEDIA_PLANS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(film.film_id) });
      onClose();
    },
    onError: (e) => {
      // IR1: a claim won the race; the drawer refreshes to the claimed state and the dialog closes
      if (e instanceof HttpError && e.code === "route_claimed") {
        void qc.invalidateQueries({ queryKey: mediaFilmKey(film.film_id) });
        void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
        ws.toast("已开始处理，需先判定已停止");
        onClose();
        return;
      }
      ws.toast(errorText(e));
    },
  });
  return (
    <>
      <ModalHead id="mw-reassign-title" title={`改派 ${title}`} subtitle={`当前 ${current}`} onClose={onClose} />
      <div className="modal-body stack">
        <div className="field">
          <label htmlFor="mw-reassign-select">改派到</label>
          <select id="mw-reassign-select" value={pick} aria-describedby="mw-reassign-note" onChange={(e) => setPick(e.target.value)}>
            {options.map((o) => (
              <option key={o.worker_id ?? ""} value={o.worker_id ?? ""} disabled={o.reason !== null && !o.current}>{optionLabel(o, now)}</option>
            ))}
          </select>
        </div>
        <p className="small muted" id="mw-reassign-note">还没开始{stage}，可以直接改派：新机器重新下载源文件{cleanup}。</p>
      </div>
      <div className="modal-foot">
        <p>改派只改这部影片的派单；已在运行的任务不受影响。</p>
        <div className="actions">
          {blocked && <span className="small muted" id="mw-reassign-blocked">{blocked}</span>}
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={blocked !== null || save.isPending}
            aria-describedby={blocked ? "mw-reassign-blocked" : undefined}
            onClick={() => save.mutate()}>{reroute ? "重新自动派单" : "确认改派"}</button>
        </div>
      </div>
    </>
  );
}

// How the status channel's last read ended (`StatusQuiet.outcome`).
const STATUS_OUTCOME: Record<string, string> = {
  timeout: "超时", unreachable: "无法连接", idle: "空闲", busy: "任务运行中", no_busy: "有应答",
  unauthorized: "令牌不一致", not_found: "接口不存在", invalid: "应答无效",
};

// 判定已停止 (R8, C4): only when both channels have been dead for 5 min. The stage counts as cancelled
// (a free retry), the film is routed again and whatever the machine hands back later is discarded.
export function AttestDialog({ open, filmId, filmName, stop, progress, onClose, onDone }: {
  open: boolean; filmId: string; filmName: string; stop: FilmStopAction; progress: string | null;
  onClose: () => void; onDone?: () => void;
}) {
  return (
    <ModalShell open={open} onClose={onClose} titleId="mw-attest-title" role="alertdialog">
      {open && <AttestPane filmId={filmId} filmName={filmName} stop={stop} progress={progress} onClose={onClose} onDone={onDone} />}
    </ModalShell>
  );
}

function AttestPane({ filmId, filmName, stop, progress, onClose, onDone }: {
  filmId: string; filmName: string; stop: FilmStopAction; progress: string | null; onClose: () => void; onDone?: () => void;
}) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const now = Date.now();
  // IR8: the safe choice has the initial focus (after ModalHead's own focus on the title)
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { cancel.current?.focus({ preventScroll: true }); }, []);
  const attest = useMutation({
    mutationFn: () => {
      if (ws.readOnly) throw new Error("只读监控");
      return api.workflowAttest(filmId, stop.assignment_id, stop.expected_version);
    },
    onSuccess: () => {
      ws.toast(`已判定 ${stop.worker.name} 已停止；${filmName} 将重新派单。`);
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
      onDone?.();
      onClose();
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const stage = [stageName(stop.stage), progress].filter(Boolean).join(" ");
  // 「192.168.1.34:5680 · 超时」: where the status channel reads and how its last read ended
  const channel = [stop.address, stop.status_state ? STATUS_OUTCOME[stop.status_state] ?? stop.status_state : null]
    .filter(Boolean).join(" · ");
  return (
    <>
      <ModalHead id="mw-attest-title" title={`判定 ${stop.worker.name} 已停止`} subtitle={`${filmName} · ${stage}`} onClose={onClose} />
      <div className="modal-body">
        <Notice tone="amber" icon={WarningAmberIcon}>两个通道都联系不上这台机器。只有在它确实已关机，或任务已经停下时才确认。</Notice>
        <dl className="facts">
          <dt>心跳</dt>
          <dd>已中断 {silentText(stop.heartbeat_silent_s)}<span className="muted"> · 最后 {lastBeat(stop.heartbeat_silent_s, now, true)}</span></dd>
          <dt>状态接口</dt><dd>无响应 {silentText(stop.status_quiet_s)}{channel && <span className="muted"> · {channel}</span>}</dd>
        </dl>
        <p className="small spacer-top">本阶段按取消计（免费重试），影片将重新派单，该机器之后交回的结果会被丢弃。</p>
      </div>
      <div className="modal-foot">
        <p>机器若仍在运行，请等它恢复心跳，不要确认。</p>
        <div className="actions">
          <button type="button" className="btn ghost" ref={cancel} onClick={onClose}>取消</button>
          <button type="button" className="btn danger" disabled={attest.isPending} onClick={() => attest.mutate()}>我确认该机器已关机或任务已停止</button>
        </div>
      </div>
    </>
  );
}

// ── 概览: a 设备失联 film (R3) and a film waiting for its machine (R1) ──
export function LostRow({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  if (item.kind !== "film") return null;
  const row = item.row;
  const title = row.title || row.name;
  const route = row.route ?? null;
  const progress = progressText(item);
  return (
    <article className="film-row lost-row" data-lost={item.id}>
      <ItemCover item={item} />
      <div className="film-body">
        <div className="film-title"><h3>{title}</h3>{row.resolution && <Chip>{row.resolution}</Chip>}<StatusChip status={item.status} /></div>
        {row.note && <p className="small t-red">{row.note}</p>}
        <div className="meta">
          <span>{stageName(row.stage)}{progress ? ` · 最后进度 ${progress}` : ""}</span>
          {route?.silent_s != null && <span>最后心跳 {silentText(route.silent_s)}前</span>}
          <span className="machine-name">{route?.machine.name || "工作机"}</span>
        </div>
      </div>
      <button type="button" className="btn small" aria-label={`${ws.readOnly ? "查看详情" : "处理"} ${title}`} onClick={() => ws.openDetail(item.id)}>
        <Icon of={WarningAmberIcon} />{ws.readOnly ? "查看详情" : "处理"}
      </button>
    </article>
  );
}
