import AddIcon from "@mui/icons-material/Add";
import ComputerIcon from "@mui/icons-material/Computer";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DesktopWindowsIcon from "@mui/icons-material/DesktopWindows";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, type DeviceProbe, type DeviceProfile, type DeviceRow, type PairingCode } from "../../api";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { Chip, Icon, Notice, PageTitle } from "./parts";
import type { ChipTone } from "./mediaModel";
import { errorText, useWorkspace } from "./workspace";

// 设备 v1 (P2-2 #293, contract §5): 本机 and the enrolled GPU workers with their enrollment,
// heartbeat, GPU, disk, tier readiness, version and eligibility; add, 核验并启用, 停领/恢复,
// 重新配对, 撤销 and 退休 (both confirmed). Every command carries the row's version (CAS). With
// workflow.remote_workers off only 撤销 and 退休 remain (the controller refuses the rest).
const DEVICES_KEY = ["workflowDevices"];
const RESTORE_MIN_VRAM_MIB = 15360;
const TIER_LABEL: Record<string, string> = { standard: "标准", good: "良好", high: "高" };
const STAGE_LABEL: Record<string, string> = { restore: "修复", asr: "识别", translate: "翻译" };

const gbVram = (mib: number | null | undefined) => (mib ? `${Math.round(mib / 1024)} GB` : "—");
const gbDisk = (n: number | null | undefined) => (n == null ? "—" : `${Math.round(n / 1e9)} GB`);

function enrollment(row: DeviceRow): [string, ChipTone] {
  if (row.admission_state === "revoked") return ["已撤销", "red"];
  if (row.enrollment_state === "pending_pairing") return ["待配对", "amber"];
  if (row.enrollment_state === "pending_validation") return ["待核验", "amber"];
  if (row.admission_state === "draining") return ["停领", "amber"];
  if (row.admission_state === "enabled") return ["已启用", "green"];
  return ["未启用", "neutral"];
}

function heartbeat(row: DeviceRow): [string, ChipTone] {
  if (row.heartbeat.state === "off") return ["远程未启用", "neutral"];
  if (row.heartbeat.state === "online") return [`在线 ${row.heartbeat.age_s ?? 0}s 前`, "green"];
  if (row.heartbeat.state === "waiting") return ["等待心跳", "amber"];
  return ["失联", "red"];
}

function gpuText(row: DeviceRow): string {
  const g = row.gpu;
  const name = g?.name ?? row.probe?.gpu_model;
  if (!name) return "—";
  const parts = [name, gbVram(g?.vram_mib ?? row.probe?.vram_mib)];
  if (g?.util != null) parts.push(`${Math.round(g.util)}%`);
  if (g?.power_w != null) parts.push(`${Math.round(g.power_w)}W`);
  return parts.join(" · ");
}

function tiersText(row: DeviceRow): string {
  const tiers = ["standard", "good", "high"].map((t) => `${TIER_LABEL[t]}${row.tiers_ready.includes(t) ? "✓" : "✗"}`);
  return `${tiers.join(" ")} · 识别${row.asr_ready ? "✓" : "✗"}`;
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
  return (
    <>
      <PageTitle title="设备" actions={
        <button type="button" className="btn primary" disabled={!data?.remote_workers} onClick={() => setAdding(true)}>
          <Icon of={AddIcon} />添加设备
        </button>
      } />
      {devices.error && <Notice tone="red" icon={WarningAmberIcon}>无法读取设备：{errorText(devices.error)}</Notice>}
      {data && !data.remote_workers && (
        <Notice icon={InfoOutlinedIcon}>未开启远程设备：在 agent.yaml 设置 workflow.remote_workers: true 后重启。</Notice>
      )}
      {data && (
        <div className="stack device-list">
          <article className="panel device-card" aria-label="本机">
            <div className="device-top">
              <div className="device-name">
                <span className="device-icon"><Icon of={ComputerIcon} /></span>
                <div><h3>{data.controller.name}</h3><p>主控 · 运行 {data.controller.stages.join("、") || "—"}</p></div>
              </div>
              <div className="chips">{data.controller.stages.map((s) => <Chip key={s} tone="blue">{s}</Chip>)}</div>
            </div>
          </article>
          {data.workers.map((row) => (
            <WorkerCard key={row.worker_id} row={row} remote={data.remote_workers} busy={act.isPending || repair.isPending}
              onApprove={() => setApproving(row)}
              onAdmission={(state) => act.mutate(() => api.workflowDeviceAdmission(row.worker_id, row.version, state))}
              onRepair={() => repair.mutate(row)}
              onRevoke={() => setRevoking(row)}
              onRetire={() => setRetiring(row)} />
          ))}
          {data.workers.length === 0 && data.remote_workers && (
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
      <RetireDialog row={retiring} onClose={() => setRetiring(null)}
        onConfirm={(row) => { setRetiring(null); act.mutate(() => api.workflowRetireDevice(row.worker_id, row.version)); }} />
    </>
  );
}

function WorkerCard({ row, remote, busy, onApprove, onAdmission, onRepair, onRevoke, onRetire }: {
  row: DeviceRow; remote: boolean; busy: boolean; onApprove: () => void; onAdmission: (s: "enabled" | "draining") => void;
  onRepair: () => void; onRevoke: () => void; onRetire: () => void;
}) {
  const [state, stateTone] = enrollment(row);
  const [beat, beatTone] = heartbeat(row);
  const revoked = row.admission_state === "revoked";
  const live = remote && !revoked; // remote work off: the controller refuses all but 撤销/退休
  const version = row.agent_version ? `${row.agent_version}${row.version_ok === false ? " · 版本不一致" : ""}` : "—";
  return (
    <article className="panel device-card" aria-label={row.name} data-worker={row.worker_id}>
      <div className="device-top">
        <div className="device-name">
          <span className="device-icon"><Icon of={DesktopWindowsIcon} /></span>
          <div><h3>{row.name}</h3><p className="mono">{row.address}:{row.port}</p></div>
        </div>
        <div className="chips"><Chip tone={stateTone}>{state}</Chip><Chip tone={beatTone}>{beat}</Chip></div>
      </div>
      <dl className="facts">
        <dt>GPU</dt><dd>{gpuText(row)}</dd>
        <dt>磁盘</dt><dd>{row.disk ? `可用 ${gbDisk(row.disk.free)} · 暂存 ${gbDisk(row.disk.staging_used)} / ${gbDisk(row.disk.cap)}` : "—"}</dd>
        <dt>画质模型</dt><dd>{tiersText(row)}</dd>
        <dt>版本</dt><dd className={row.version_ok === false ? "amber" : undefined}>{version}</dd>
        <dt>资格</dt><dd className={row.eligible ? "green" : "amber"}>{row.eligibility}</dd>
        {row.unresolved > 0 && <><dt>未结束任务</dt><dd>{row.unresolved}</dd></>}
      </dl>
      <div className="device-actions">
        {live && row.enrollment_state === "pending_validation" && row.probe && (
          <button type="button" className="btn primary" disabled={busy} onClick={onApprove}>核验并启用</button>
        )}
        {live && row.enrollment_state === "verified" && row.admission_state === "disabled" && (
          <button type="button" className="btn primary" disabled={busy} onClick={() => onAdmission("enabled")}>启用</button>
        )}
        {live && row.admission_state === "enabled" && (
          <button type="button" className="btn" disabled={busy} onClick={() => onAdmission("draining")}>停领</button>
        )}
        {live && row.admission_state === "draining" && (
          <button type="button" className="btn" disabled={busy} onClick={() => onAdmission("enabled")}>恢复</button>
        )}
        {live && <button type="button" className="btn ghost" disabled={busy} onClick={onRepair}>重新配对</button>}
        {!revoked && <button type="button" className="btn danger" disabled={busy} onClick={onRevoke}>撤销</button>}
        {revoked && row.can_retire && <button type="button" className="btn danger" disabled={busy} onClick={onRetire}>退休</button>}
        {revoked && !row.can_retire && <p className="small muted">仍有 {row.unresolved} 个未结束的任务，结束后才能退休。</p>}
      </div>
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
  const [asr, setAsr] = useState(offers.has("asr"));
  const approve = useMutation({
    mutationFn: () => api.workflowApproveDevice(row.worker_id, row.version, restore && restoreOk, asr),
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
        <div className="switch-row">
          <label className="switch">
            <input type="checkbox" role="switch" checked={asr} disabled={!offers.has("asr")}
              onChange={(e) => setAsr(e.target.checked)} />识别
          </label>
        </div>
        <Notice tone="amber" icon={WarningAmberIcon}>启用后，修复与识别交给远程设备，主控只做翻译等其余步骤。</Notice>
        {approve.error && <Notice tone="red" icon={WarningAmberIcon}>{errorText(approve.error)}</Notice>}
      </div>
      <div className="modal-foot">
        <p>工具、配置或模型变化后需要再次核验。</p>
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={approve.isPending || (!(restore && restoreOk) && !asr)}
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
