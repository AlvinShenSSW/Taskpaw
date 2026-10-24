import CloseIcon from "@mui/icons-material/Close";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { Drawer } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type FilmDetail, type PlanEntry, type WorkflowStep } from "../../api";
import { LtxDownload } from "../LtxDownload";
import { EnergyBlock, LivePowerRow, PredictBlock } from "../power/PowerParts";
import { predictFromRates } from "../power/powerModel";
import {
  CLEANUP_STATE, EXCEPTION_STATES, exceptionInfo, fmtBytes, isRemoteRow, packageNote, readFilmTiming, REVIEW_STATES, restoreStep,
  machineLabel, STATUS_META, stageName, stepDurations, tierLong, unroutedMachine, type WorkItem,
} from "./mediaModel";
import { DeviceSection } from "./RemoteParts";
import { Chip, Cover, Icon, Notice, StatusChip, StepLine } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { RestoreLivePanel } from "./RestoreLivePanel";
import { RemoveTask, SourceMissingAlert } from "./SourceMissing";
import { TimingBlock } from "./TimingParts";
import { errorText, MEDIA_FILMS_KEY, mediaFilmKey, useIntakeReasons, useWorkspace } from "./workspace";

const BACKDROP = { sx: { backgroundColor: "#030812BA", backdropFilter: "blur(4px)" } };

// 影片详情 (§5 + U1 V4): a right drawer, min(660px, 100%). MUI's modal keeps the focus inside,
// closes on Escape and returns focus to the trigger; the title takes focus on open.
export function FilmDrawer({ filmId, onClose }: { filmId: string | null; onClose: () => void }) {
  return (
    <Drawer anchor="right" open={filmId !== null} onClose={onClose} ModalProps={{ disableAutoFocus: true }}
      slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: "mw mw-drawer", sx: { backgroundImage: "none" }, role: "dialog", "aria-modal": true, "aria-labelledby": "mw-drawer-title" } as object}>
      {filmId !== null && <DrawerPane key={filmId} filmId={filmId} onClose={onClose} />}
    </Drawer>
  );
}

function DrawerPane({ filmId, onClose }: { filmId: string; onClose: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const titleRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => { titleRef.current?.focus({ preventScroll: true }); }, []);
  const detail = useQuery({ queryKey: ws.filmKey(filmId), queryFn: () => ws.readFilm(filmId), refetchInterval: 5000 });
  const film = detail.data;
  const item = ws.data.items.find((i) => i.id === filmId);
  const plan = ws.data.plans.data?.plans.find((e) => e.film_id === filmId);
  const open = useMutation({ mutationFn: () => {
    if (ws.readOnly) throw new Error("只读监控");
    return api.workflowOpen(filmId, "inbox");
  }, onError: (e) => ws.toast(errorText(e)) });
  const retry = useMutation({
    mutationFn: (stage: string) => {
      if (ws.readOnly) throw new Error("只读监控");
      return api.workflowRetry(filmId, stage);
    },
    onSuccess: () => {
      ws.toast("已重新排队；原始来源保留。");
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const reasons = useIntakeReasons();
  const status = item?.status ?? (film?.group === "planning" ? "pending" : "receiving");
  const primary = () => {
    if (ws.readOnly) return <button type="button" className="btn ghost" onClick={onClose}>关闭</button>;
    if (REVIEW_STATES.includes(status)) {
      return <button type="button" className="btn primary" onClick={() => ws.openReview(filmId)}>查看并确认入库</button>;
    }
    if (EXCEPTION_STATES.includes(status) && item && !["none", "detail"].includes(exceptionInfo(item, reasons)[3])) {
      const [, , label, action] = exceptionInfo(item, reasons);
      const onClick = () => {
        if (action === "review") ws.openReview(filmId);
        else if (action === "reconcile" || action === "cleanup") ws.openExplain(action, filmId);
        else if (item.kind === "film") retry.mutate(action === "retry-scrape" ? "scrape" : item.row.retry_stage || item.row.stage || "");
      };
      return <button type="button" className="btn primary" disabled={retry.isPending} onClick={onClick}>{label}</button>;
    }
    return <button type="button" className="btn ghost" onClick={onClose}>关闭</button>;
  };

  return (
    <>
      <div className="modal-head">
        <div>
          <h2 id="mw-drawer-title" tabIndex={-1} ref={titleRef}>影片详情</h2>
          <p className="mono">{film ? film.number || film.name : filmId}</p>
        </div>
        <button type="button" className="btn ghost icon-btn" aria-label="关闭对话框" onClick={onClose}><Icon of={CloseIcon} /></button>
      </div>
      <div className="modal-body">
        {detail.isLoading ? <p className="muted">正在读取影片详情…</p>
          : detail.error || !film ? <Notice tone="red">无法读取这部影片。</Notice>
            : <DrawerBody film={film} item={item} status={status} plan={plan} onClose={onClose} />}
      </div>
      <div className="modal-foot">
        <p>阶段结果与来源清理分别记录。</p>
        <div className="actions">
          {/* P3 E3: any film with no running or claimed stage that is not archived (a 片源丢失 film has it in its alert) */}
          {!ws.readOnly && film?.can_remove && !film.source_missing && <RemoveTask film={film} onRemoved={onClose} />}
          {!ws.readOnly && <button type="button" className="btn ghost" disabled={open.isPending} onClick={() => open.mutate()}>打开文件夹</button>}
          {primary()}
        </div>
      </div>
    </>
  );
}

function DrawerBody({ film, item, status, plan, onClose }: {
  film: FilmDetail; item: WorkItem | undefined; status: keyof typeof STATUS_META; plan: PlanEntry | undefined; onClose: () => void;
}) {
  const ws = useWorkspace();
  // Before Miko, an unplanned film's names come from the lookup preview (precedence: final > lookup > input).
  const title = film.title || plan?.title || film.name;
  const actors = film.actors.length ? film.actors : plan?.actors ?? [];
  const restore = restoreStep(film);
  const running = restore?.state === "running" ? restore : null; // U1c: always shown alive
  // P2-3 R15: a restore or ASR running on a worker (its 设备, and 停止并重新排队)
  const remote = (film.steps ?? []).find((s) => s.state === "running" && (s.key === "restore" || s.key === "asr") && s.machine) ?? null;
  // P2-4 §4: remote mode's 设备 line (route, 失联, 改派, 判定已停止)
  const multi = isRemoteRow(film);
  // §9 CX30: with no route, the pin only while the restore waits for a device; then 本机, none once done
  const unrouted = unroutedMachine(film);
  const routed = film.route ? (film.route.live === "local" ? "本机" : film.route.machine.name)
    : unrouted === "待派发" && film.pin ? (film.pin.worker_id === null ? "自动" : film.pin.name) : unrouted;
  const download = restore?.download ?? null;
  const timing = readFilmTiming(film.timing);
  const changed = film.cleanup.kept.some((k) => k.status === "blocked_changed" || k.status === "blocked_identity");
  const final = film.files.length > 0;
  const subtitle = film.marks.chinese_subtitles ? "字幕：已验证的非空中文字幕。"
    : final ? "本片无语音，不宣称已完成中文字幕。" : "字幕成果待后处理核对。";
  const receipt = film.receipt_id ?? (film.archive.status === "succeeded" ? "已入库" : null);
  // PWR-2: the 能耗 block, or 待处理's prediction on the recommended machine and tier
  const gpuName = ws.data.films.data?.gpu?.name;
  const machine = gpuName ? `本机 · ${gpuName}` : "本机";
  const energy = film.energy ?? null;
  const predicted = energy?.running
    ? predictFromRates(ws.data.films.data?.energy_rates, film.plan?.machine, film.resolution,
      film.plan ? film.plan.ltx_model ?? "none" : "standard", film.duration_ms)
    : null;
  const energyBlock = status === "pending"
    ? plan?.prediction && (
      <PredictBlock filmId={film.film_id} prediction={plan.prediction} tier={plan.recommendation.ltx_model}
        needsGpu={plan.needs_gpu} machine={machineLabel(ws.data.plans.data?.choices.machines.find((m) => m.id === plan.recommendation.machine))} />
    )
    : energy && <EnergyBlock filmId={film.film_id} energy={energy} machine={machine} predicted={predicted}
      runSeconds={timing?.total_restore_to_translate ?? null} />;
  const plannedFacts = status === "pending" ? (
    <>
      <dt>画质</dt><dd>待安排{plan && <span className="muted"> · 推荐 {plan.needs_gpu ? tierLong(plan.recommendation.ltx_model) : "无需修复"}</span>}</dd>
      <dt>设备</dt><dd>待安排{plan && <span className="muted"> · 推荐 {machineLabel(ws.data.plans.data?.choices.machines.find((m) => m.id === plan.recommendation.machine))}</span>}</dd>
    </>
  ) : film.plan ? (
    <>
      <dt>画质</dt><dd>{tierLong(film.plan.ltx_model)}</dd>
      {(!multi || remote?.machine || routed !== null) && (
        <><dt>设备</dt><dd>{remote?.machine ? remote.machine.name : multi ? routed
          : machineLabel(ws.data.plans.data?.choices.machines.find((m) => m.id === film.plan?.machine), "本机")}</dd></>
      )}
    </>
  ) : (
    <><dt>画质</dt><dd>待安排</dd><dt>设备</dt><dd>待安排</dd></>
  );
  return (
    <>
      <div className="detail-hero">
        <Cover filmId={film.film_id} hasPoster={film.has_poster} source={film.poster_source}
          hasCover={film.has_cover} coverSource={film.cover_source} size="large" label={title} />
        <div>
          <h2>{title}</h2>
          <div className="chips">
            <StatusChip status={status} />
            {changed && <Chip tone="red">输入变化 · 禁止清理</Chip>}
            {film.resolution && <Chip>{film.resolution}</Chip>}
          </div>
          {actors.length > 0 && <p>{actors.join("、")}</p>}
          <p>来源：主控收件目录 INBOX · 本机</p>
        </div>
      </div>
      <div className="detail-steps"><StepLine steps={film.steps} title={title} durations={stepDurations(timing)} /></div>
      <LivePowerRow power={film.live_power} />
      {multi && <DeviceSection film={film} />}
      <SourceMissingAlert film={film} onRemoved={onClose} />
      {remote && <RemoteStep filmId={film.film_id} step={remote} bare={multi} />}
      {running ? (
        <div>
          <RestoreLivePanel step={running} />
          <p className="small muted" style={{ marginTop: 10 }}>由阶段日志提供；不包含翻译、回传、刮削与入库。</p>
        </div>
      ) : download ? (
        <LtxDownload value={download} onRespond={ws.readOnly ? undefined : (requestId, approved) => api.workflowLtxDownload(requestId, approved)}
          onError={(e) => ws.toast(errorText(e))} />
      ) : film.source_missing ? null : (
        <Notice icon={InfoOutlinedIcon}>
          {status === "pending" ? "待处理 · 确认画质与设备后才进入影片队列。"
            : [STATUS_META[status][0], item?.kind === "film" ? item.row.note : film.note].filter(Boolean).join("：")}
        </Notice>
      )}
      {energyBlock || null}
      <TimingBlock timing={timing} />
      <section className="panel final-panel spacer-top">
        <h3>{final ? "本次 Miko 最终成品" : "Miko 完整后处理"}</h3>
        <p className="small spacer-top">{subtitle}</p>
        {final && (
          <ul className="package-list">
            {film.files.map((f) => <li key={`${f.role}:${f.path}`}><span>{f.path}</span><span>{packageNote(f.role, f.path)}</span></li>)}
          </ul>
        )}
      </section>
      <dl className="facts spacer-top">
        {plannedFacts}
        <dt>目标</dt><dd>{film.archive.target_path || "尚未确定目标"}</dd>
        <dt>原始来源</dt><dd>主控 INBOX / {film.name} · 本次收件 {film.inputs.length} 项</dd>
        <dt>入库结果</dt><dd>{receipt || "尚未入库"}</dd>
        <dt>来源清理</dt><dd>{film.cleanup.input ? CLEANUP_STATE[film.cleanup.input] ?? film.cleanup.input : "入库完成后自动检查并清理本次来源"}</dd>
      </dl>
      <EvidenceFold film={film} />
    </>
  );
}

// 「查看后台检查记录」: the intake check, the versions, the cleanup boundary and the files.
export function EvidenceFold({ film }: { film: FilmDetail }) {
  const { t } = useTranslation();
  const rev = film.revisions;
  const intakeDone = (film.steps ?? []).some((s) => s.key === "intake" && s.state === "done");
  return (
    <details className="spacer-top">
      <summary className="link-btn">查看后台检查记录</summary>
      <dl className="facts spacer-top">
        <dt>收件检查</dt><dd>{intakeDone ? "已通过：稳定检查、写占用检查、媒体探测及一致副本校验" : "等待检查；尚未进入计算"}</dd>
        <dt>成品版本</dt><dd className="mono">{rev.final ? `R${rev.final.revision} · ${rev.final.hash.slice(0, 16)}` : "尚无成品"}</dd>
        <dt>来源版本</dt><dd className="mono">{rev.input ? `R${rev.input.revision} · ${rev.input.hash.slice(0, 16)}` : "—"}</dd>
        <dt>清理边界</dt><dd>只处理本次输入清单；新增文件、其他影片、父目录和 NAS 正式片库均不删除。</dd>
      </dl>
      {film.files.length > 0 && (
        <table className="files-table" aria-label="成品文件">
          <thead><tr><th>角色</th><th>文件</th><th>大小</th></tr></thead>
          <tbody>{film.files.map((f) => <tr key={`${f.role}:${f.path}`}><td>{f.role}</td><td className="mono">{f.path}</td><td className="mono">{fmtBytes(f.size)}</td></tr>)}</tbody>
        </table>
      )}
      {film.inputs.length > 0 && (
        <table className="files-table" aria-label="原始输入">
          <thead><tr><th>原始输入</th><th>大小</th></tr></thead>
          <tbody>{film.inputs.map((f) => <tr key={f.path}><td className="mono">{f.path}</td><td className="mono">{fmtBytes(f.size)}</td></tr>)}</tbody>
        </table>
      )}
      {film.cleanup.kept.length > 0 && (
        <table className="files-table" aria-label="保留的来源文件">
          <thead><tr><th>保留的文件</th><th>原因</th></tr></thead>
          <tbody>{film.cleanup.kept.map((k) => <tr key={k.path}><td className="mono">{k.path}</td><td>{t(`media.keptStatus.${k.status}`, { defaultValue: k.status })}</td></tr>)}</tbody>
        </table>
      )}
      {film.stages.some((s) => s.note) && (
        <dl className="facts spacer-top">
          {film.stages.filter((s) => s.note).flatMap((s) => [<dt key={`${s.stage}-t`}>{stageName(s.stage)}</dt>, <dd key={`${s.stage}-d`}>{s.note}</dd>])}
        </dl>
      )}
    </details>
  );
}

// P2-3 (#294) R9: a restore or ASR running on a worker — its 设备 and 停止并重新排队 (OD3), which
// the worker carries out at its next heartbeat: the Job stops, the attempt ends as a free retry and
// the film is routed again (a pinned one to the same worker).
// `bare`: the 设备 line above already names the machine (remote mode, P2-4), so only the action shows.
function RemoteStep({ filmId, step, bare = false }: { filmId: string; step: WorkflowStep; bare?: boolean }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const [asking, setAsking] = useState(false);
  const requeue = useMutation({
    mutationFn: () => {
      if (ws.readOnly || !step.attempt_id) throw new Error("只读监控");
      return api.workflowRequeue(filmId, step.attempt_id);
    },
    onSuccess: () => {
      setAsking(false);
      ws.toast("已请求停止；设备停止任务后重新排队。");
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const name = step.machine?.name ?? "";
  const stage = stageName(step.key);
  // P2-3 §11 CX5: once the worker uploads or finishes, the stage settled ok and its finished GPU
  // work is delivered, never discarded — no stop is offered (the controller refuses one too).
  const stoppable = !step.phase || step.phase === "running" || step.phase === "settling";
  if (bare && (ws.readOnly || !step.attempt_id || !stoppable)) return null;
  return (
    <section className={bare ? "spacer-top" : "panel spacer-top"} aria-label={bare ? `${name} 上的${stage}` : "设备"} data-testid="remote-step">
      {!bare && (
        <dl className="facts">
          <dt>设备</dt><dd>{name} · {stage}{step.phase === "uploading" || step.phase === "finishing" ? " · 回传中" : ""}</dd>
        </dl>
      )}
      {!ws.readOnly && step.attempt_id && stoppable && (
        <div className={bare ? "actions" : "actions spacer-top"}>
          <button type="button" className="btn ghost" onClick={() => setAsking(true)}>停止并重新排队</button>
        </div>
      )}
      <ModalShell open={asking} onClose={() => setAsking(false)} titleId="mw-requeue-title">
        <ModalHead id="mw-requeue-title" title="停止并重新排队" subtitle={`${name} 上的${stage}`} onClose={() => setAsking(false)} />
        <div className="modal-body">
          <Notice tone="amber" icon={WarningAmberIcon}>该设备会停止本次{stage}，已运行的部分不保留；影片随后重新排队，不计入失败次数。</Notice>
        </div>
        <div className="modal-foot">
          <p>设备确认停止前，影片仍显示在运行。</p>
          <div className="actions">
            <button type="button" className="btn ghost" onClick={() => setAsking(false)}>取消</button>
            <button type="button" className="btn danger" disabled={requeue.isPending} onClick={() => requeue.mutate()}>确认停止</button>
          </div>
        </div>
      </ModalShell>
    </section>
  );
}
