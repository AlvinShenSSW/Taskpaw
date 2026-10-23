import AddIcon from "@mui/icons-material/Add";
import ComputerIcon from "@mui/icons-material/Computer";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DesktopWindowsIcon from "@mui/icons-material/DesktopWindows";
import ExpandLessIcon from "@mui/icons-material/ExpandLess";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, type DeviceAssignment, type DeviceProbe, type DeviceProfile, type DeviceRow, type PairingCode } from "../../api";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { Chip, Icon, Notice, PageTitle } from "./parts";
import { clockText, progressText, silentText, stageName, TIER_ORDER, TIER_SHORT, type ChipTone } from "./mediaModel";
import { AttestDialog } from "./RemoteParts";
import { errorText, useWorkspace } from "./workspace";

// 设备 (P2-2 #293 v1, P2-4 #295 §4 as the confirmed preview Devices / Phone-Devices): 本机 and the
// enrolled GPU workers — state (在线 / 待核验 / 失联 N 分钟 / 停领), heartbeat age, GPU, disk and staging,
// tier readiness, version, the current film and 排队 N, an expandable eligibility matrix, the
// unresolved assignments with 判定已停止 (C4), and add, 核验并启用, 停领/恢复, 重新配对, 撤销 and 退休 (all
// confirmed where they cannot be undone). Every command carries the row's version (CAS). With
// workflow.remote_workers off only 撤销 and 退休 remain (the controller refuses the rest).
const DEVICES_KEY = ["workflowDevices"];
const RESTORE_MIN_VRAM_MIB = 15360;
const STAGE_LABEL: Record<string, string> = { restore: "修复", asr: "识别", translate: "翻译" };

const gbVram = (mib: number | null | undefined) => (mib ? `${Math.round(mib / 1024)} GB` : "—");
const gbDisk = (n: number | null | undefined) => {
  if (n == null) return "—";
  return n >= 1e12 ? `${(n / 1e12).toFixed(1)} TB` : `${Math.round(n / 1e9)} GB`;
};

// The enrollment / admission state; null for a plainly enabled worker (its heartbeat says the rest).
// A card under 16 GB cannot be 核验ed (OD5): say so instead of 待核验 forever.
const tooSmall = (row: DeviceRow) => row.enrollment_state === "pending_validation" && !!row.probe && !row.restore_allowed;

function enrollment(row: DeviceRow): [string, ChipTone] | null {
  if (row.admission_state === "revoked") return ["已撤销", "red"];
  if (row.enrollment_state === "pending_pairing") return ["待配对", "amber"];
  if (tooSmall(row)) return ["显存不足，不参与修复", "amber"];
  if (row.enrollment_state === "pending_validation") return ["待核验", "amber"];
  if (row.admission_state === "draining") return ["停领", "amber"];
  if (row.admission_state === "enabled") return null;
  return ["未启用", "neutral"];
}

function heartbeat(row: DeviceRow): [string, ChipTone] {
  if (row.heartbeat.state === "off") return ["远程未启用", "neutral"];
  if (row.heartbeat.state === "online") return ["在线", "green"];
  if (row.heartbeat.state === "waiting") return ["等待心跳", "amber"];
  return [row.heartbeat.age_s == null ? "失联" : `失联 ${silentText(row.heartbeat.age_s)}`, "red"];
}

// 「心跳 2 秒前」, or for a 失联 worker 「最后心跳 20:41 · 后台重连中」.
function beatLine(row: DeviceRow, serverTime: string): string | null {
  const age = row.heartbeat.age_s;
  if (row.heartbeat.state === "online") return `心跳 ${silentText(age ?? 0)}前`;
  if (row.heartbeat.state === "lost") {
    if (age == null) return "从未收到心跳 · 后台重连中";
    const now = Date.parse(serverTime);
    const at = new Date((Number.isFinite(now) ? now : Date.now()) - age * 1000).toISOString();
    return `最后心跳 ${clockText(at, Number.isFinite(now) ? now : Date.now())} · 后台重连中`;
  }
  return null;
}

function gpuName(row: DeviceRow): string {
  const name = row.gpu?.name ?? row.probe?.gpu_model;
  return name ? `${name} · ${gbVram(row.gpu?.vram_mib ?? row.probe?.vram_mib)}` : "—";
}

function gpuLoad(row: DeviceRow): string | null {
  const g = row.gpu;
  if (!g) return null;
  const parts: string[] = [];
  if (g.util != null) parts.push(`${Math.round(g.util)}%`);
  if (g.power_w != null) parts.push(`${Math.round(g.power_w)} W`);
  if (g.used_mib != null) parts.push(`显存 ${(g.used_mib / 1024).toFixed(1)} GB`);
  return parts.length ? parts.join(" · ") : null;
}

// 领取资格 (1080p by tier; 4K waits for P2-5): what this worker would take now.
function eligibilityCell(row: DeviceRow, tier: string): [string, ChipTone] {
  if (!row.eligible) return [row.eligibility, "amber"];
  if (!row.restore_allowed) return ["显存不足", "amber"];
  return row.tiers_ready.includes(tier) ? ["可领取", "green"] : ["缺模型", "amber"];
}

export function DevicesPage() {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const devices = useQuery({ queryKey: DEVICES_KEY, queryFn: api.workflowDevices, refetchInterval: 5000, enabled: !ws.readOnly });
  const [adding, setAdding] = useState(false);
  const [code, setCode] = useState<{ name: string; value: PairingCode } | null>(null);
  const [approving, setApproving] = useState<DeviceRow | null>(null);
  const [revoking, setRevoking] = useState<DeviceRow | null>(null);
  const [retiring, setRetiring] = useState<DeviceRow | null>(null);
  const [repairing, setRepairing] = useState<DeviceRow | null>(null);
  const [attesting, setAttesting] = useState<DeviceAssignment | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: DEVICES_KEY });
  const act = useMutation({
    mutationFn: (run: () => Promise<unknown>) => run(),
    onSuccess: () => refresh(),
    onError: (e) => { ws.toast(errorText(e)); refresh(); },
  });
  const repair = useMutation({
    mutationFn: (row: DeviceRow) => api.workflowRepairDevice(row.worker_id, row.version).then((value) => ({ row, value })),
    onSuccess: ({ row, value }) => { setCode({ name: row.name, value }); refresh(); },
    onError: (e) => ws.toast(errorText(e)),
  });

  if (ws.readOnly) {
    return (
      <>
        <PageTitle title="设备" />
        <Notice icon={InfoOutlinedIcon}>设备在主控电脑上管理。</Notice>
      </>
    );
  }
  const data = devices.data;
  const workers = data?.workers ?? [];
  const summary = data?.remote_workers ? [
    `工作机 ${workers.length}`, `可领取 ${workers.filter((w) => w.eligible).length}`,
    `待核验 ${workers.filter((w) => w.enrollment_state === "pending_validation" && !tooSmall(w)).length}`,
    `失联 ${workers.filter((w) => w.heartbeat.state === "lost").length}`,
  ].join(" · ") : null;
  const progressOf = (filmId: string) => {
    const item = ws.data.items.find((i) => i.id === filmId);
    return item ? progressText(item) : null;
  };
  return (
    <>
      <PageTitle title="设备" summary={summary} actions={
        <button type="button" className="btn primary" disabled={!data?.remote_workers} onClick={() => setAdding(true)}>
          <Icon of={AddIcon} />添加设备
        </button>
      } />
      {devices.error && <Notice tone="red" icon={WarningAmberIcon}>无法读取设备：{errorText(devices.error)}</Notice>}
      {data && !data.remote_workers && (
        <Notice icon={InfoOutlinedIcon}>未开启远程设备：在 设置 · 媒体工作流 中打开「允许 GPU 工作机接入」，重启 TaskPaw 后生效。</Notice>
      )}
      {data && (
        <div className="stack device-list">
          <article className="panel device-card" aria-label="本机">
            <div className="device-top">
              <div className="device-name">
                <span className="device-icon"><Icon of={ComputerIcon} /></span>
                <div><h3>{data.controller.name}</h3><p>主控 · 运行 {data.controller.stages.join("、") || "—"}</p></div>
              </div>
              <div className="device-state">
                <div className="chips"><Chip tone="green">在线</Chip>{data.controller.stages.map((s) => <Chip key={s} tone="blue">{s}</Chip>)}</div>
                {ws.openSettings && (
                  <button type="button" className="btn ghost small" onClick={ws.openSettings}>主控设置</button>
                )}
              </div>
            </div>
            {data.remote_workers && !data.controller.stages.includes("修复") && (
              <p className="small muted">修复交给 GPU 工作机；本机负责{data.controller.stages.join("、") || "其余步骤"}与刮削入库。</p>
            )}
          </article>
          {workers.map((row) => (
            <WorkerCard key={row.worker_id} row={row} remote={data.remote_workers} serverTime={data.server_time}
              busy={act.isPending || repair.isPending} progressOf={progressOf}
              onApprove={() => setApproving(row)}
              onAdmission={(state) => act.mutate(() => api.workflowDeviceAdmission(row.worker_id, row.version, state))}
              onRepair={() => setRepairing(row)}
              onRevoke={() => setRevoking(row)}
              onRetire={() => setRetiring(row)}
              onAttest={(a) => setAttesting(a)} />
          ))}
          {workers.length === 0 && data.remote_workers && (
            <div className="panel empty"><h2>还没有远程设备</h2><p>点「添加设备」，再在工作机的设置里粘贴配对码。</p></div>
          )}
        </div>
      )}
      <AddDialog open={adding} onClose={() => setAdding(false)}
        onAdded={(name, value) => { setAdding(false); setCode({ name, value }); refresh(); }} />
      <CodeDialog code={code} onClose={() => setCode(null)} />
      <ApproveDialog row={approving} onClose={() => setApproving(null)}
        onApproved={() => { setApproving(null); refresh(); }} />
      <RevokeDialog row={revoking} onClose={() => setRevoking(null)}
        onConfirm={(row) => { setRevoking(null); act.mutate(() => api.workflowRevokeDevice(row.worker_id, row.version)); }} />
      <RepairDialog row={repairing} onClose={() => setRepairing(null)}
        onConfirm={(row) => { setRepairing(null); repair.mutate(row); }} />
      <RetireDialog row={retiring} onClose={() => setRetiring(null)}
        onConfirm={(row) => { setRetiring(null); act.mutate(() => api.workflowRetireDevice(row.worker_id, row.version)); }} />
      {attesting?.stop && (
        <AttestDialog open filmId={attesting.film_id} filmName={attesting.film_name || attesting.film_id} stop={attesting.stop}
          progress={progressOf(attesting.film_id)} onClose={() => setAttesting(null)} onDone={() => { void refresh(); }} />
      )}
    </>
  );
}

function WorkerCard({ row, remote, serverTime, busy, progressOf, onApprove, onAdmission, onRepair, onRevoke, onRetire, onAttest }: {
  row: DeviceRow; remote: boolean; serverTime: string; busy: boolean; progressOf: (filmId: string) => string | null;
  onApprove: () => void; onAdmission: (s: "enabled" | "draining") => void;
  onRepair: () => void; onRevoke: () => void; onRetire: () => void; onAttest: (a: DeviceAssignment) => void;
}) {
  const [open, setOpen] = useState(false);
  const state = enrollment(row);
  const [beat, beatTone] = heartbeat(row);
  const revoked = row.admission_state === "revoked";
  const live = remote && !revoked; // remote work off: the controller refuses all but 撤销/退休
  const lost = row.heartbeat.state === "lost";
  const pending = row.enrollment_state === "pending_validation";
  const line = beatLine(row, serverTime);
  const assignments = row.assignments ?? [];
  const queued = Math.max((row.route_depth ?? 0) - assignments.length, 0);
  const current = assignments[0] ?? null;
  const missing = TIER_ORDER.filter((t) => !row.tiers_ready.includes(t));
  // the unresolved assignments of a worker that is not answering (or revoked): 判定已停止 lives here
  const unresolved = (lost || revoked || row.heartbeat.state === "off") && assignments.length > 0;
  const panel = `device-more-${row.worker_id}`;
  const load = gpuLoad(row);
  return (
    <article className="panel device-card" aria-label={row.name} data-worker={row.worker_id}>
      <div className="device-top">
        <div className="device-name">
          <span className="device-icon"><Icon of={DesktopWindowsIcon} /></span>
          <div>
            <h3>{row.name}</h3>
            <p className="mono">{row.address}:{row.port}</p>
          </div>
        </div>
        <div className="device-state">
          <div className="chips">
            {state && <Chip tone={state[1]}>{state[0]}</Chip>}
            {!(pending && row.heartbeat.state === "online") && <Chip tone={beatTone}>{beat}</Chip>}
            {row.agent_version && <Chip tone={row.version_ok === false ? "amber" : "neutral"}>
              v{row.agent_version}{row.version_ok === false ? " · 版本不一致" : ""}</Chip>}
          </div>
          {line && <p className="small muted">{line}</p>}
        </div>
      </div>
      <dl className="facts device-facts">
        <dt>{lost ? "GPU（最后上报）" : "GPU"}</dt><dd>{gpuName(row)}{load && <span className="muted"> · {load}</span>}</dd>
        <dt>{lost ? "磁盘 · 暂存（最后上报）" : "磁盘 · 暂存"}</dt>
        <dd>{row.disk ? `可用 ${gbDisk(row.disk.free)} · 暂存 ${gbDisk(row.disk.staging_used)} / ${gbDisk(row.disk.cap)}` : "—"}</dd>
        <dt>画质就绪</dt>
        <dd>{lost ? "未知（失联）" : (
          <span className="chips tier-chips">
            {TIER_ORDER.map((t) => (
              <Chip key={t} tone={row.tiers_ready.includes(t) ? "green" : "neutral"}>
                {TIER_SHORT[t]}{!row.tiers_ready.includes(t) && <span className="sr-only">（缺模型）</span>}
              </Chip>
            ))}
            {missing.length > 0 && <span className="small amber">缺模型</span>}
          </span>
        )}</dd>
        <dt>资格</dt><dd className={row.eligible ? "green" : "amber"}>{row.eligibility}</dd>
        {!unresolved && (
          <>
            <dt>当前</dt>
            <dd>
              {current
                ? [current.film_name, `${stageName(current.stage)}${progressOf(current.film_id) ? ` ${progressOf(current.film_id)}` : ""}`].join(" · ")
                : row.eligible ? "空闲 · 可领取" : pending ? (tooSmall(row) ? "不参与修复" : "核验前不领取") : "—"}
              {(row.route_depth !== undefined) && <span className="muted"> · 排队 {queued}</span>}
            </dd>
          </>
        )}
        {revoked && row.unresolved > 0 && !unresolved && <><dt>未结束任务</dt><dd>{row.unresolved}</dd></>}
      </dl>
      {unresolved && (
        <section className="device-assignments" aria-label={`${row.name} 未结派单`}>
          <h4>未结派单</h4>
          <ul>
            {assignments.map((a) => (
              <li key={a.assignment_id}>
                <span>{a.film_name || a.film_id} · {stageName(a.stage)}{progressOf(a.film_id) ? ` ${progressOf(a.film_id)}` : ""}</span>
                {a.stop && (
                  <span className="device-line-action">
                    <button type="button" className="btn danger small" disabled={!a.stop.allowed}
                      aria-describedby={!a.stop.allowed && a.stop.reason ? `stop-why-${a.assignment_id}` : undefined}
                      onClick={() => onAttest(a)}>判定已停止</button>
                    {!a.stop.allowed && a.stop.reason && <span className="small amber" id={`stop-why-${a.assignment_id}`}>{a.stop.reason}</span>}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
      <div className="device-actions">
        {live && pending && row.probe && !tooSmall(row) && (
          <button type="button" className="btn primary" disabled={busy} onClick={onApprove}>核验并启用</button>
        )}
        {live && row.enrollment_state === "verified" && row.admission_state === "disabled" && (
          <button type="button" className="btn primary" disabled={busy} onClick={() => onAdmission("enabled")}>启用</button>
        )}
        {live && row.admission_state === "enabled" && !pending && (
          <button type="button" className="btn" disabled={busy} onClick={() => onAdmission("draining")}>停领</button>
        )}
        {live && row.admission_state === "draining" && (
          <button type="button" className="btn" disabled={busy} onClick={() => onAdmission("enabled")}>恢复</button>
        )}
        {live && lost && <button type="button" className="btn ghost" disabled={busy} onClick={onRepair}>重新配对</button>}
        {!revoked && pending && <button type="button" className="btn danger" disabled={busy} onClick={onRevoke}>撤销</button>}
        {revoked && row.can_retire && <button type="button" className="btn danger" disabled={busy} onClick={onRetire}>退休</button>}
        {revoked && !row.can_retire && <p className="small muted">仍有 {row.unresolved} 个未结束的任务，结束后才能退休。</p>}
        {!revoked && (
          <button type="button" className="btn ghost" aria-expanded={open} aria-controls={panel} onClick={() => setOpen((v) => !v)}>
            <Icon of={open ? ExpandLessIcon : ExpandMoreIcon} />详情
          </button>
        )}
      </div>
      {open && !revoked && (
        <div className="device-more" id={panel}>
          <section aria-label={`${row.name} 领取资格`}>
            <h4>领取资格</h4>
            <table className="files-table eligibility-table">
              <thead><tr><th scope="col"><span className="sr-only">分辨率</span></th>
                {TIER_ORDER.map((t) => <th key={t} scope="col">{TIER_SHORT[t]}</th>)}</tr></thead>
              <tbody>
                <tr><th scope="row">1080p</th>{TIER_ORDER.map((t) => {
                  const [text, tone] = eligibilityCell(row, t);
                  return <td key={t}><Chip tone={tone}>{text}</Chip></td>;
                })}</tr>
                <tr><th scope="row">4K</th>{TIER_ORDER.map((t) => <td key={t}><Chip>暂不派发</Chip></td>)}</tr>
              </tbody>
            </table>
            <p className="small muted">4K 派发在后续版本开放。</p>
          </section>
          <section aria-label={`${row.name} 配对与生命周期`}>
            <h4>配对与生命周期</h4>
            <div className="device-actions">
              {live && !lost && <button type="button" className="btn ghost" disabled={busy} onClick={onRepair}>重新配对</button>}
              {!pending && <button type="button" className="btn danger" disabled={busy} onClick={onRevoke}>撤销</button>}
              <span className="small muted">退休需先撤销；撤销后可退休并保留历史。</span>
            </div>
          </section>
        </div>
      )}
    </article>
  );
}

function AddDialog({ open, onClose, onAdded }: {
  open: boolean; onClose: () => void; onAdded: (name: string, code: PairingCode) => void;
}) {
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const add = useMutation({
    mutationFn: () => api.workflowAddDevice(name.trim(), address.trim()),
    onSuccess: (code) => { onAdded(name.trim(), code); setName(""); setAddress(""); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); if (name.trim() && address.trim()) add.mutate(); };
  return (
    <ModalShell open={open} onClose={onClose} titleId="mw-add-device-title">
      <form onSubmit={submit}>
        <ModalHead id="mw-add-device-title" title="添加设备" subtitle="登记后生成一次性配对码（15 分钟内有效）" onClose={onClose} />
        <div className="modal-body stack">
          <label className="field">名称<input className="input" value={name} maxLength={64} required
            onChange={(e) => setName(e.target.value)} /></label>
          <label className="field">地址<input className="input mono" value={address} maxLength={255} required
            placeholder="192.168.1.31" onChange={(e) => setAddress(e.target.value)} /></label>
          {add.error && <Notice tone="red" icon={WarningAmberIcon}>{errorText(add.error)}</Notice>}
        </div>
        <div className="modal-foot">
          <p>工作机在 设置 → 媒体工作流 中粘贴配对码加入。</p>
          <div className="actions">
            <button type="button" className="btn ghost" onClick={onClose}>取消</button>
            <button type="submit" className="btn primary" disabled={add.isPending || !name.trim() || !address.trim()}>生成配对码</button>
          </div>
        </div>
      </form>
    </ModalShell>
  );
}

function CodeDialog({ code, onClose }: { code: { name: string; value: PairingCode } | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!code) return;
    try { await navigator.clipboard.writeText(code.value.code); setCopied(true); } catch { setCopied(false); }
  };
  const expires = code ? new Date(code.value.expires_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <ModalShell open={code !== null} onClose={() => { setCopied(false); onClose(); }} titleId="mw-code-title">
      {code && (
        <>
          <ModalHead id="mw-code-title" title={`${code.name} 的配对码`} subtitle={`${expires} 前有效 · 只能使用一次`} onClose={onClose} />
          <div className="modal-body">
            <p className="code-box mono" aria-label="配对码">{code.value.code}</p>
          </div>
          <div className="modal-foot">
            <p>{copied ? "已复制" : "在工作机的 设置 → 媒体工作流 中粘贴。"}</p>
            <div className="actions">
              <button type="button" className="btn" onClick={copy}><Icon of={ContentCopyIcon} />复制</button>
              <button type="button" className="btn primary" onClick={onClose}>完成</button>
            </div>
          </div>
        </>
      )}
    </ModalShell>
  );
}

const profileKey = (p: DeviceProfile) => `${p.tool} ${p.tool_version} · ${p.model} ${p.model_version} · 上限 ${p.max_width}×${p.max_height}`;

function ProbeRows({ probe, approved }: { probe: DeviceProbe; approved: DeviceProbe | null }) {
  const changed = (a: unknown, b: unknown) => approved !== null && a !== b;
  const rows: Array<[string, string, string | null]> = [
    ["GPU", probe.gpu_model, approved?.gpu_model ?? null],
    ["显存", gbVram(probe.vram_mib), approved ? gbVram(approved.vram_mib) : null],
    ...probe.profiles.map((p): [string, string, string | null] => {
      const old = approved?.profiles.find((o) => o.stage === p.stage);
      return [STAGE_LABEL[p.stage] ?? p.stage, profileKey(p), old ? profileKey(old) : approved ? "—" : null];
    }),
    // CX9: a changed executor setting needs 核验 again; an older probe has no fingerprint
    ...(probe.config_digest
      ? [["配置", probe.config_digest.slice(0, 12), approved ? (approved.config_digest ?? "—").slice(0, 12) : null] as [string, string, string | null]]
      : []),
  ];
  return (
    <table className="files-table probe-table">
      <thead><tr><th scope="col">项目</th><th scope="col">本次探测</th><th scope="col">上次核验</th></tr></thead>
      <tbody>
        {rows.map(([label, now, before]) => {
          const diff = changed(now, before);
          return (
            <tr key={label} className={diff ? "changed" : undefined}>
              <th scope="row">{label}</th>
              <td>{now}{diff && <Chip tone="amber">{label === "配置" ? "配置变更" : "已变化"}</Chip>}</td>
              <td>{before ?? "首次核验"}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ApproveDialog({ row, onClose, onApproved }: { row: DeviceRow | null; onClose: () => void; onApproved: () => void }) {
  return (
    <ModalShell open={row !== null} onClose={onClose} titleId="mw-approve-title">
      {row && <ApprovePane key={`${row.worker_id}:${row.version}`} row={row} onClose={onClose} onApproved={onApproved} />}
    </ModalShell>
  );
}

function ApprovePane({ row, onClose, onApproved }: { row: DeviceRow; onClose: () => void; onApproved: () => void }) {
  const probe = row.probe as DeviceProbe;
  const offers = new Set(probe.profiles.map((p) => p.stage));
  const restoreOk = row.restore_allowed && probe.vram_mib >= RESTORE_MIN_VRAM_MIB && offers.has("restore");
  const [restore, setRestore] = useState(restoreOk);
  // OD7 (#298 §4): a GPU worker restores only; 识别 stays on the controller
  const approve = useMutation({
    mutationFn: () => api.workflowApproveDevice(row.worker_id, row.version, restore && restoreOk),
    onSuccess: () => onApproved(),
  });
  return (
    <>
      <ModalHead id="mw-approve-title" title={`核验并启用 · ${row.name}`} subtitle="对照上次核验，确认本次探测与可接的阶段" onClose={onClose} />
      <div className="modal-body stack">
        <ProbeRows probe={probe} approved={row.approved?.probe ?? null} />
        <div className="switch-row">
          <label className="switch">
            <input type="checkbox" role="switch" checked={restore && restoreOk} disabled={!restoreOk}
              onChange={(e) => setRestore(e.target.checked)} />修复
          </label>
          {!restoreOk && <span className="small amber">{row.restore_allowed ? "未提供修复工具" : "显存不足，不参与修复"}</span>}
        </div>
        <Notice tone="amber" icon={WarningAmberIcon}>启用后修复交给这台 GPU 工作机；识别、翻译和入库仍在主控</Notice>
        {approve.error && <Notice tone="red" icon={WarningAmberIcon}>{errorText(approve.error)}</Notice>}
      </div>
      <div className="modal-foot">
        <p>工具、配置或模型变化后需要再次核验。</p>
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={approve.isPending || !(restore && restoreOk)}
            onClick={() => approve.mutate()}>核验并启用</button>
        </div>
      </div>
    </>
  );
}

function RevokeDialog({ row, onClose, onConfirm }: { row: DeviceRow | null; onClose: () => void; onConfirm: (row: DeviceRow) => void }) {
  return (
    <ModalShell open={row !== null} onClose={onClose} titleId="mw-revoke-title">
      {row && (
        <>
          <ModalHead id="mw-revoke-title" title={`撤销 ${row.name}`} subtitle="凭据立即失效，设备不能再领取" onClose={onClose} />
          <div className="modal-body">
            <Notice tone="amber" icon={WarningAmberIcon}>撤销不会停止已在运行的任务；未结束的任务需核对后才能退休设备。</Notice>
          </div>
          <div className="modal-foot">
            <p>要再次使用这台设备，需要重新添加。</p>
            <div className="actions">
              <button type="button" className="btn ghost" onClick={onClose}>取消</button>
              <button type="button" className="btn danger" onClick={() => onConfirm(row)}>确认撤销</button>
            </div>
          </div>
        </>
      )}
    </ModalShell>
  );
}

// IR2: 重新配对 voids the worker's credential at once, so it asks first. 停领 stays immediate: 恢复 undoes it.
function RepairDialog({ row, onClose, onConfirm }: { row: DeviceRow | null; onClose: () => void; onConfirm: (row: DeviceRow) => void }) {
  return (
    <ModalShell open={row !== null} onClose={onClose} titleId="mw-repair-title">
      {row && (
        <>
          <ModalHead id="mw-repair-title" title={`重新配对 ${row.name}`} subtitle="生成新的一次性配对码" onClose={onClose} />
          <div className="modal-body">
            <Notice tone="amber" icon={WarningAmberIcon}>将生成新的配对码；工作机用新码重新加入后，旧凭据才会失效。需要立即停用请用 撤销。</Notice>
          </div>
          <div className="modal-foot">
            <p>已在运行的任务不受影响。</p>
            <div className="actions">
              <button type="button" className="btn ghost" onClick={onClose}>取消</button>
              <button type="button" className="btn primary" onClick={() => onConfirm(row)}>确认重新配对</button>
            </div>
          </div>
        </>
      )}
    </ModalShell>
  );
}

function RetireDialog({ row, onClose, onConfirm }: { row: DeviceRow | null; onClose: () => void; onConfirm: (row: DeviceRow) => void }) {
  return (
    <ModalShell open={row !== null} onClose={onClose} titleId="mw-retire-title">
      {row && (
        <>
          <ModalHead id="mw-retire-title" title={`退休 ${row.name}`} subtitle="设备从列表中移除，记录保留" onClose={onClose} />
          <div className="modal-body">
            <Notice tone="amber" icon={WarningAmberIcon}>退休不可撤回；要再次使用这台设备，需要重新添加。</Notice>
          </div>
          <div className="modal-foot">
            <p>只有已撤销且没有未结束任务的设备可以退休。</p>
            <div className="actions">
              <button type="button" className="btn ghost" onClick={onClose}>取消</button>
              <button type="button" className="btn danger" onClick={() => onConfirm(row)}>确认退休</button>
            </div>
          </div>
        </>
      )}
    </ModalShell>
  );
}
