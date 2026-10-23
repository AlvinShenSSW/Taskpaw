// PWR-3: 「估算参数」 (power-v3.html). This machine's estimate settings (`power:` in agent.yaml, applied
// live), the plug calibration helper computed here in the browser (PW07: 其他基线 = 插座读数 × 效率 −
// GPU − CPU) and the peer list, name + address (PW06). Measured CPU sources are shown disabled (PW10).
import CloseIcon from "@mui/icons-material/Close";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import { Dialog } from "@mui/material";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, type LivePower, type PowerPeerRow, type PowerPeerSetting, type PowerSettings } from "../../api";
import { Chip, Icon, Notice } from "../media/parts";
import { focusOnMount } from "../media/workspace";
import { fmtW } from "./powerModel";
import { platformDefaults } from "./powerView";

const BACKDROP = { sx: { backgroundColor: "#030812BA", backdropFilter: "blur(4px)" } };
const EFFICIENCIES: Array<[string, string]> = [["", "不计"], ["0.85", "85 %"], ["0.88", "88 %"], ["0.9", "90 %"], ["0.92", "92 %"], ["0.94", "94 %"]];
const PEER_ADDRESS = /^(\[[0-9a-fA-F:.]+\]|[A-Za-z0-9.-]+|[0-9a-fA-F:]+)(:\d{1,5})?$/;

const text = (v: number | null | undefined) => (v == null ? "" : String(v));
const num = (v: string) => (v.trim() === "" ? null : Number(v));

// The CPU watts under the settings being edited (not the saved model's live.cpu_w): none → 0, else
// idle + (tdp − idle) × the live utilisation, with the platform defaults for a blank field. An agent
// that reports no utilisation falls back to its own live CPU reading.
function candidateCpuW(
  source: "estimate" | "none", tdp: number | null, idle: number | null,
  defaults: { tdp: number; idle: number }, live: LivePower | null,
): number {
  if (source === "none") return 0;
  if (typeof live?.cpu_pct !== "number") return live?.cpu_w ?? 0;
  const t = tdp != null && Number.isFinite(tdp) ? tdp : defaults.tdp;
  const i = Math.min(idle != null && Number.isFinite(idle) ? idle : defaults.idle, t);
  return i + (t - i) * (Math.min(100, Math.max(0, live.cpu_pct)) / 100);
}

// `autoPeers` (P2-4 R11): enrolled GPU workers listed by themselves — shown, never removable here.
export function PowerParams({ open, onClose, settings, live, machineName, os, onSaved, autoPeers = [] }: {
  open: boolean; onClose: () => void; settings: PowerSettings | undefined; live: LivePower | null;
  machineName: string; os: string | undefined; onSaved: (text: string) => void; autoPeers?: PowerPeerRow[];
}) {
  return (
    <Dialog open={open} onClose={onClose} maxWidth={false} disableAutoFocus slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: "mw mw-modal", sx: { backgroundImage: "none" }, "aria-labelledby": "pw-params-title" } as object}>
      {open && <ParamsBody onClose={onClose} settings={settings} live={live} machineName={machineName} os={os} onSaved={onSaved}
        autoPeers={autoPeers} />}
    </Dialog>
  );
}

function ParamsBody({ onClose, settings, live, machineName, os, onSaved, autoPeers }: {
  onClose: () => void; settings: PowerSettings | undefined; live: LivePower | null;
  machineName: string; os: string | undefined; onSaved: (text: string) => void; autoPeers: PowerPeerRow[];
}) {
  const qc = useQueryClient();
  const defaults = platformDefaults(os);
  const [other, setOther] = useState(text(settings?.other_w));
  const [eff, setEff] = useState(text(settings?.psu_efficiency));
  const [cpuSource, setCpuSource] = useState<"estimate" | "none">(settings?.cpu_source ?? "estimate");
  const [tdp, setTdp] = useState(text(settings?.tdp_w));
  const [idle, setIdle] = useState(text(settings?.idle_w));
  const [peers, setPeers] = useState<PowerPeerSetting[]>(settings?.peers ?? []);
  const [peerName, setPeerName] = useState("");
  const [peerAddress, setPeerAddress] = useState("");
  const [peerError, setPeerError] = useState<string | null>(null);
  const [plug, setPlug] = useState("");
  const [error, setError] = useState<string | null>(null);
  const otherRef = useRef<HTMLInputElement>(null);

  const gpu = live?.gpu_w ?? 0;
  const cpu = candidateCpuW(cpuSource, num(tdp), num(idle), defaults, live);
  const effPct = eff ? Math.round(Number(eff) * 100) : 100;
  const plugW = Number(plug);
  const suggested = plugW > 0 && live ? Math.round((plugW * effPct) / 100 - gpu - cpu) : null;
  const calibText = suggested === null ? (plugW > 0 && !live ? "本机还没有实时读数，稍后再试。" : "")
    : suggested >= 0 ? `建议其他基线：${plugW} × ${effPct}% − GPU ${fmtW(gpu)} − CPU ${fmtW(cpu)} = ${suggested} W`
      : "插座读数低于当前 GPU + CPU 估算，请在机器空闲时重新读数。";

  const save = useMutation({
    mutationFn: (peers: PowerPeerSetting[]) => api.updateConfig({
      power: {
        other_w: num(other), psu_efficiency: num(eff), cpu_source: cpuSource, tdp_w: num(tdp), idle_w: num(idle), peers,
      },
    }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["power"] });
      void qc.invalidateQueries({ queryKey: ["agentConfig"] });
      onSaved("估算参数已保存，实时功耗已按新参数估算。");
      onClose();
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });

  // The peer list with the typed row appended, or null (and the hint shown) when that row is invalid.
  const withTyped = (): PowerPeerSetting[] | null => {
    const name = peerName.trim(), address = peerAddress.trim();
    if (!name && !address) return peers;
    if (!name || !address) { setPeerError("请填写名称和地址。"); return null; }
    if (peers.some((p) => p.name === name)) { setPeerError("名称已存在。"); return null; }
    if (!PEER_ADDRESS.test(address) || address.includes("://")) { setPeerError("地址格式：主机或 IP，可带 :端口（默认 5680）。"); return null; }
    setPeerName(""); setPeerAddress(""); setPeerError(null);
    return [...peers, { name, address }];
  };
  const addPeer = () => {
    if (!peerName.trim() && !peerAddress.trim()) { setPeerError("请填写名称和地址。"); return; }
    const next = withTyped();
    if (next) setPeers(next);
  };
  // IR07: a row typed but not 添加-ed is saved too; an incomplete one blocks the save with its hint.
  const onSave = () => {
    const next = withTyped();
    if (!next) return;
    setPeers(next);
    save.mutate(next);
  };

  return (
    <>
      <div className="modal-head">
        <div>
          <h2 id="pw-params-title" tabIndex={-1} ref={focusOnMount}>估算参数</h2>
          <p>按机器设置；保存后实时功耗与之后的能耗按新参数估算</p>
        </div>
        <button type="button" className="btn ghost icon-btn" aria-label="关闭估算参数" onClick={onClose}><Icon of={CloseIcon} /></button>
      </div>
      <div className="modal-body">
        <Notice icon={InfoOutlinedIcon}>
          整机功耗 = GPU（nvidia-smi 实测）+ CPU（按占用 × TDP 估算）+ 其他基线（主板、内存、硬盘、风扇），可选再除以电源效率。用插座校准基线后，电源损耗已包含在基线里，电源效率保持「不计」即可。
        </Notice>
        <fieldset className="param">
          <legend><span className="swatch" style={{ background: "#3987e5" }} aria-hidden="true" />{machineName} <small>本机</small></legend>
          <div className="param-grid">
            <div className="field">
              <label htmlFor="pw-other">其他功耗基线</label>
              <div className="unit-input"><input id="pw-other" ref={otherRef} className="input" type="number" inputMode="numeric" min={0} max={2000}
                placeholder={String(defaults.other)} value={other} onChange={(e) => setOther(e.target.value)} /><span>W</span></div>
            </div>
            <div className="field">
              <label htmlFor="pw-eff">电源效率</label>
              <select id="pw-eff" value={eff} onChange={(e) => setEff(e.target.value)}>
                {EFFICIENCIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <span className="hint">基线按插座校准时选「不计」</span>
            </div>
            <div className="field">
              <label htmlFor="pw-src">CPU 功耗来源</label>
              <select id="pw-src" value={cpuSource} onChange={(e) => setCpuSource(e.target.value as "estimate" | "none")}>
                <option value="measured" disabled>实测（暂不支持）</option>
                <option value="estimate">按占用估算</option>
                <option value="none">无</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="pw-tdp">CPU TDP</label>
              <div className="unit-input"><input id="pw-tdp" className="input" type="number" inputMode="numeric" min={0} max={2000}
                placeholder={String(defaults.tdp)} value={tdp} disabled={cpuSource !== "estimate"} aria-describedby="pw-tdp-hint"
                onChange={(e) => setTdp(e.target.value)} /><span>W</span></div>
              <span className="hint" id="pw-tdp-hint">仅「按占用估算」时使用</span>
            </div>
            <div className="field">
              <label htmlFor="pw-idle">CPU 空闲功耗</label>
              <div className="unit-input"><input id="pw-idle" className="input" type="number" inputMode="numeric" min={0} max={2000}
                placeholder={String(defaults.idle)} value={idle} disabled={cpuSource !== "estimate"}
                onChange={(e) => setIdle(e.target.value)} /><span>W</span></div>
            </div>
          </div>
          <p className="src">
            GPU：nvidia-smi 实测 · CPU 当前：{cpuSource === "none" ? "不计" : "按占用 × TDP 估算"} · 当前读数 GPU {live?.gpu_w == null ? "—" : fmtW(live.gpu_w)} W · CPU {fmtW(live?.cpu_w)} W
          </p>
        </fieldset>
        <section className="calib" aria-labelledby="pw-calib-title">
          <h3 id="pw-calib-title">按插座读数校准其他基线</h3>
          <p>让机器空闲约 10 分钟，读米家插座当前功率填入下方：其他基线 = 插座读数 × 电源效率 − 当前 GPU − 当前 CPU。满载时再对一次，两次相差超过 10% 时以满载为准。</p>
          <div className="calib-grid">
            <div className="field">
              <label htmlFor="pw-calib-machine">机器</label>
              <select id="pw-calib-machine" value="local" onChange={() => undefined}><option value="local">{machineName}</option></select>
            </div>
            <div className="field">
              <label htmlFor="pw-plug">插座读数</label>
              <div className="unit-input"><input id="pw-plug" className="input" type="number" inputMode="decimal" min={0} max={2000}
                placeholder="例如 61" value={plug} onChange={(e) => setPlug(e.target.value)} /><span>W</span></div>
            </div>
            <button type="button" className="btn" disabled={suggested === null || suggested < 0}
              onClick={() => { setOther(String(suggested)); otherRef.current?.focus(); onSaved(`已填入 ${machineName} 的其他基线，保存后生效。`); }}>
              填入基线
            </button>
          </div>
          <p className="calib-out" aria-live="polite">{calibText}</p>
        </section>
        <fieldset className="param spacer-top">
          <legend>其他机器 <small>名称 + 地址；使用本机的 API Token 只读对方的 /status 与 /power/history</small></legend>
          {peers.length + autoPeers.length > 0 ? (
            <ul className="peer-list" aria-label="其他机器">
              {peers.map((p) => (
                <li key={p.name} className="peer-row">
                  <span>{p.name}</span><span className="mono">{p.address}</span>
                  <button type="button" className="btn ghost small" aria-label={`移除 ${p.name}`}
                    onClick={() => setPeers(peers.filter((x) => x.name !== p.name))}>移除</button>
                </li>
              ))}
              {autoPeers.filter((a) => !peers.some((p) => p.name === a.name)).map((p) => (
                <li key={`auto:${p.name}`} className="peer-row" data-auto-peer={p.name}>
                  <span>{p.name}</span><span className="mono">{p.address}</span>
                  <Chip tone="blue">工作机 · 自动加入</Chip>
                </li>
              ))}
            </ul>
          ) : <p className="src">还没有添加其他机器。各机器的估算参数在该机器的 TaskPaw 中设置。</p>}
          <div className="peer-add">
            <div className="field">
              <label htmlFor="pw-peer-name">名称</label>
              <input id="pw-peer-name" className="input" maxLength={64} placeholder="例如 GPU-02" value={peerName}
                onChange={(e) => setPeerName(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="pw-peer-address">地址</label>
              <input id="pw-peer-address" className="input" maxLength={255} placeholder="192.168.1.20:5680" value={peerAddress}
                onChange={(e) => setPeerAddress(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") addPeer(); }} />
            </div>
            <button type="button" className="btn" onClick={addPeer}>添加</button>
          </div>
          {peerError && <p className="form-error" role="alert">{peerError}</p>}
          <p className="src">只运行 Hub 的 Mac 没有 TaskPaw Agent，暂时不能加入；公网地址会被拒绝。</p>
        </fieldset>
        {error && <p className="form-error" role="alert">保存失败：{error}</p>}
      </div>
      <div className="modal-foot">
        <p>不涉及电价；只用于看分布和每片能耗。</p>
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={save.isPending} onClick={onSave}>保存</button>
        </div>
      </div>
    </>
  );
}
