// 片库与规则 (#361 §11, preview media-library-v1.html): NAS 片库, 演员, 归档规则 and 归档历史 over
// the controller's own library. Nothing here touches the NAS except 扫描 and 打开, through the routes.
import AddIcon from "@mui/icons-material/Add";
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import EditOutlinedIcon from "@mui/icons-material/EditOutlined";
import FileDownloadOutlinedIcon from "@mui/icons-material/FileDownloadOutlined";
import FolderOutlinedIcon from "@mui/icons-material/FolderOutlined";
import LocalOfferOutlinedIcon from "@mui/icons-material/LocalOfferOutlined";
import LockOutlinedIcon from "@mui/icons-material/LockOutlined";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import RefreshIcon from "@mui/icons-material/Refresh";
import StorageOutlinedIcon from "@mui/icons-material/StorageOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Library, type LibraryKind, type LibraryRole, type ScanStatus } from "../../api";
import { inTauri, pickPath } from "../nativePicker";
import { ActorsTab } from "./LibraryActors";
import { HistoryTab } from "./LibraryHistory";
import { ImportDialog } from "./LibraryImport";
import {
  actorLibraries, categories, letters, offline, SCAN_KEY, scanEveryText, useLibraries, useLibraryRefresh, useOpenPath, useRules,
  useScanStatus,
} from "./libraryModel";
import { RulesTab } from "./LibraryRules";
import { fmtBytes, fmtDateTime } from "./mediaModel";
import { Chip, Icon, Notice, PageTitle } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { errorText, useWorkspace } from "./workspace";

type Tab = "nas" | "actors" | "rules" | "history";
const TABS: Array<[Tab, string]> = [["nas", "NAS 片库"], ["actors", "演员"], ["rules", "归档规则"], ["history", "归档历史"]];
type FormTarget = { kind: LibraryKind; library?: Library } | null;
const SUB = "TaskPaw 自己的片库索引 · 不需要 Filmpaw 运行";

// The Hub's read-only workspace has no library routes: the library lives on the controller.
export function ControllerOnly({ title }: { title: string }) {
  return (
    <>
      <PageTitle title={title} />
      <Notice>只读监控不显示{title}；请在主控电脑上查看和修改。</Notice>
    </>
  );
}

export function LibraryPage() {
  const [tab, setTab] = useState<Tab>("nas");
  const [form, setForm] = useState<FormTarget>(null);
  const [removing, setRemoving] = useState<Library | null>(null);
  const [importing, setImporting] = useState(false);
  const libraries = useLibraries();
  const items = libraries.data?.items;

  let body;
  if (libraries.isLoading) body = <div className="panel empty"><p>正在读取片库…</p></div>;
  else if (libraries.error || !items) body = <Notice tone="red" icon={WarningAmberIcon}>无法读取片库：{errorText(libraries.error)}</Notice>;
  else if (items.length === 0) {
    body = (
      <div className="panel empty">
        <Icon of={StorageOutlinedIcon} />
        <h2>还没有片库</h2>
        <p className="small">TaskPaw 需要知道 NAS 上的演员目录，才能为影片推荐入库位置。如果这台电脑上有 Filmpaw 的片库数据，可以直接导入。</p>
        <div className="actions">
          <button type="button" className="btn primary" onClick={() => setImporting(true)}><Icon of={FileDownloadOutlinedIcon} />从 Filmpaw 导入</button>
          <button type="button" className="btn" onClick={() => setForm({ kind: "actor" })}><Icon of={AddIcon} />手动添加片库</button>
        </div>
        <p className="tiny muted spacer-top">导入只读取 Filmpaw 的数据，不会修改它。之后两边各自独立。</p>
      </div>
    );
  } else {
    body = (
      <>
        <div className="subtabs" role="tablist" aria-label="片库与规则">
          {TABS.map(([key, label]) => (
            <button key={key} type="button" className="subtab" role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>{label}</button>
          ))}
        </div>
        <div role="tabpanel" aria-label={TABS.find(([key]) => key === tab)?.[1]}>
          {tab === "nas" ? <NasTab items={items} onForm={setForm} onRemove={setRemoving} />
            : tab === "actors" ? <ActorsTab libraries={items} />
              : tab === "rules" ? <RulesTab libraries={items} /> : <HistoryTab libraries={items} />}
        </div>
      </>
    );
  }
  return (
    <>
      <PageTitle title="片库与规则" summary={SUB} />
      {body}
      <LibraryForm target={form} onClose={() => setForm(null)} />
      <RemoveDialog library={removing} onClose={() => setRemoving(null)} />
      <ImportDialog open={importing} onClose={() => setImporting(false)} onView={() => { setImporting(false); setTab("nas"); }} />
    </>
  );
}

function NasTab({ items, onForm, onRemove }: {
  items: Library[]; onForm: (t: FormTarget) => void; onRemove: (l: Library) => void;
}) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const rules = useRules().data;
  const scan = useScanStatus().data;
  const actors = actorLibraries(items);
  const cats = categories(items);
  const letter = letters(items);
  const setScan = (status: ScanStatus) => qc.setQueryData(SCAN_KEY, status);
  const scanOne = useMutation({ mutationFn: (id: string) => api.libraryScan(id), onSuccess: setScan, onError: (e) => ws.toast(errorText(e)) });
  const scanAll = useMutation({ mutationFn: () => api.libraryScanAll(), onSuccess: setScan, onError: (e) => ws.toast(errorText(e)) });
  const missing = actors.reduce((n, l) => n + l.missing_count, 0);
  return (
    <div className="stack">
      <div className="stats">
        <div className="panel stat"><div className="label">片库</div>
          <strong>{actors.length}<em>{actors.filter((l) => l.last_scan_ok === true).length} 在线</em></strong>
          <div className="note">{actors.filter((l) => l.role === "archive").length} 个可作入库目标 · 另有 {cats.length} 个类型目录</div></div>
        <div className="panel stat"><div className="label">演员目录</div>
          <strong>{actors.reduce((n, l) => n + l.performer_count, 0)}</strong><div className="note">同名演员在多个片库时分别计数</div></div>
        <div className="panel stat"><div className="label">目录已缺失</div>
          <strong className={missing ? "t-amber" : undefined}>{missing}</strong><div className="note">保留记录，不会删除</div></div>
        <div className="panel stat"><div className="label">自动扫描</div>
          <strong style={{ fontSize: 20, lineHeight: 2.1 }}>{rules ? scanEveryText(rules.scan_every_hours) : "—"}</strong>
          <div className="note"><button type="button" className="link-btn" style={{ minHeight: 0, padding: 0, fontSize: 11 }}
            onClick={() => ws.navigate("settings")}>在设置中修改</button></div></div>
      </div>
      <div className="toolbar">
        <div><h2>演员片库</h2><p className="small muted">根目录的下一级是演员目录：片库\演员\影片。目录消失只标为「已缺失」，从不删除记录。</p></div>
        <div className="actions">
          <button type="button" className="btn" disabled={scanAll.isPending} onClick={() => scanAll.mutate()}><Icon of={RefreshIcon} />全部重新扫描</button>
          <button type="button" className="btn primary" onClick={() => onForm({ kind: "actor" })}><Icon of={AddIcon} />添加片库</button>
        </div>
      </div>
      <div className="lib-grid">
        {actors.map((l) => (
          <LibraryCard key={l.id} library={l} mark={letter[l.id]} scan={scan} onScan={() => scanOne.mutate(l.id)}
            onEdit={() => onForm({ kind: "actor", library: l })} onRemove={() => onRemove(l)} />
        ))}
        <button type="button" className="add-card" onClick={() => onForm({ kind: "actor" })}>
          <Icon of={AddIcon} /><span>添加片库</span><span className="tiny">NAS 共享目录或本机目录</span>
        </button>
      </div>
      <div className="toolbar spacer-top">
        <div><h2>类型目录</h2><p className="small muted">不属于任何演员的影片（写真、合集、单体等）直接放进去：类型目录\影片。设了番号前缀的会自动推荐。</p></div>
        <div className="actions">
          <button type="button" className="btn primary" onClick={() => onForm({ kind: "category" })}><Icon of={AddIcon} />添加类型目录</button>
        </div>
      </div>
      <div className="lib-grid">
        {cats.map((c) => (
          <LibraryCard key={c.id} library={c} scan={scan} onEdit={() => onForm({ kind: "category", library: c })} onRemove={() => onRemove(c)} />
        ))}
        <button type="button" className="add-card" style={{ minHeight: 200 }} onClick={() => onForm({ kind: "category" })}>
          <Icon of={AddIcon} /><span>添加类型目录</span><span className="tiny">例如 写真\某系列、Video Collections\单体</span>
        </button>
      </div>
    </div>
  );
}

const took = (ms: number | null) => (ms == null ? "" : ms < 1000 ? "不到 1 秒" : `${Math.round(ms / 1000)} 秒`);

function LibraryCard({ library: l, mark, scan, onScan, onEdit, onRemove }: {
  library: Library; mark?: string; scan: ScanStatus | undefined; onScan?: () => void; onEdit: () => void; onRemove: () => void;
}) {
  const open = useOpenPath();
  const off = offline(l);
  const cat = l.kind === "category";
  const running = scan?.running === l.id;
  const queued = Boolean(scan?.queued.includes(l.id));
  return (
    <article className={`panel lib-card${off ? " offline" : ""}`} aria-label={l.label}>
      <div className="lib-name">
        <div className="lib-icon">{cat ? <Icon of={FolderOutlinedIcon} /> : mark}</div>
        <div style={{ minWidth: 0 }}><h3>{l.label}</h3><div className="path">{l.path}</div></div>
      </div>
      <div className="chips">
        {off ? <Chip tone="red">离线</Chip> : l.last_scan_ok ? <Chip tone="green">在线</Chip> : <Chip>尚未扫描</Chip>}
        {cat ? (
          <>
            <Chip tone="blue">影片直接放入</Chip>
            {l.prefixes.length ? l.prefixes.map((p) => <Chip key={p}><Icon of={LocalOfferOutlinedIcon} />前缀 {p}</Chip>)
              : <Chip>无前缀 · 只手动指派</Chip>}
          </>
        ) : l.role === "archive" ? <Chip tone="blue">入库目标</Chip> : <Chip><Icon of={LockOutlinedIcon} />只读 · 不作入库目标</Chip>}
      </div>
      {off && <Notice tone="red" icon={WarningAmberIcon}>连不上{cat ? "这个目录" : "这台 NAS"}。{cat ? "" : "演员保留上次索引，不会标记为缺失；"}恢复后自动重新扫描。</Notice>}
      <dl className="facts">
        {!cat && <><dt>演员目录</dt><dd className="mono">{l.performer_count}{l.missing_count > 0 && <span className="t-amber"> · {l.missing_count} 个已缺失</span>}</dd></>}
        <dt>影片目录</dt><dd className="mono">{l.film_count?.toLocaleString() ?? "—"}</dd>
        {!cat && <><dt>可用空间</dt><dd className="mono">{fmtBytes(l.free_bytes)}</dd></>}
        <dt>最后扫描</dt><dd>{fmtDateTime(l.last_scan_at)}{l.last_scan_ms != null && <span className="muted"> · 用时 {took(l.last_scan_ms)}</span>}</dd>
      </dl>
      {(running || queued) && (
        <div>
          <div className={`progress${running ? " indeterminate" : ""}`} role="progressbar" aria-label="扫描进度"><span /></div>
          <div className="scan-line"><span>{running ? (cat ? "正在扫描影片目录…" : "正在扫描演员目录…") : "排队等待扫描"}</span></div>
        </div>
      )}
      <div className="lib-foot">
        {onScan && (
          <button type="button" className="btn small" disabled={running || queued || off} onClick={onScan}>
            <Icon of={RefreshIcon} />{running || queued ? "扫描中" : "重新扫描"}
          </button>
        )}
        <button type="button" className="btn small ghost" onClick={onEdit}><Icon of={EditOutlinedIcon} />编辑</button>
        <button type="button" className="btn small ghost" disabled={off || open.isPending} onClick={() => open.mutate(l.path)}>
          <Icon of={OpenInNewIcon} />打开
        </button>
        <button type="button" className="btn small ghost" style={{ marginLeft: "auto" }} aria-label={`移除 ${l.label}`} onClick={onRemove}>
          <Icon of={DeleteOutlineIcon} />
        </button>
      </div>
    </article>
  );
}

const FORM_TITLE = "mw-library-form-title";
// 「OAE、ABC」 → ["OAE", "ABC"]: any of 、 , ， or whitespace separates; the backend uppercases and dedups too
const parsePrefixes = (text: string) => text.split(/[、,，\s]+/).filter(Boolean).map((p) => p.toUpperCase());

function LibraryForm({ target, onClose }: { target: FormTarget; onClose: () => void }) {
  return (
    <ModalShell open={target !== null} onClose={onClose} titleId={FORM_TITLE} narrow>
      {target && <FormPane key={target.library?.id ?? "new"} target={target} onClose={onClose} />}
    </ModalShell>
  );
}

function FormPane({ target, onClose }: { target: NonNullable<FormTarget>; onClose: () => void }) {
  const ws = useWorkspace();
  const refresh = useLibraryRefresh();
  const editing = target.library;
  const [kind, setKind] = useState<LibraryKind>(editing?.kind ?? target.kind);
  const [label, setLabel] = useState(editing?.label ?? "");
  const [path, setPath] = useState(editing?.path ?? "");
  const [role, setRole] = useState<LibraryRole>(editing?.role ?? "archive");
  const [prefixes, setPrefixes] = useState((editing?.prefixes ?? []).join("、"));
  const cat = kind === "category";
  const save = useMutation({
    mutationFn: () => editing
      ? api.libraryUpdate(editing.id, cat ? { label: label.trim(), prefixes: parsePrefixes(prefixes) } : { label: label.trim(), role })
      : api.libraryAdd({ kind, path: path.trim(), label: label.trim(), ...(cat ? { prefixes: parsePrefixes(prefixes) } : { role }) }),
    onSuccess: (saved) => {
      refresh();
      ws.toast(editing ? "已保存" : cat ? `已添加类型目录「${saved.label}」` : `已添加片库「${saved.label}」，正在扫描`);
      onClose();
    },
  });
  const pick = async () => { const picked = await pickPath(true, "选择根目录"); if (picked) setPath(picked); };
  const title = editing ? `编辑${cat ? "类型目录" : "片库"} · ${editing.label}` : cat ? "添加类型目录" : "添加片库";
  return (
    <>
      <ModalHead id={FORM_TITLE} title={title} onClose={onClose}
        subtitle={cat ? "影片直接放进这个目录：类型目录\\影片" : "根目录的下一级目录会被当作演员目录"} />
      <div className="modal-body drawer-stack">
        {!editing && (
          <div className="field"><span>类型</span>
            <div className="seg" role="radiogroup" aria-label="类型">
              <button type="button" className="seg-opt" role="radio" aria-checked={!cat} onClick={() => setKind("actor")}>演员片库 · 片库\演员\影片</button>
              <button type="button" className="seg-opt" role="radio" aria-checked={cat} onClick={() => setKind("category")}>类型目录 · 目录\影片</button>
            </div>
          </div>
        )}
        <label className="field" htmlFor="lf-name">名称
          <input id="lf-name" className="input" placeholder={cat ? "例如：写真 · 某系列" : "例如：主片库"} value={label}
            onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="field" htmlFor="lf-path">根目录
          <div className="path-field">
            <input id="lf-path" className="input" placeholder="\\NAS\共享\片库" value={path} readOnly={Boolean(editing)}
              onChange={(e) => setPath(e.target.value)} />
            {!editing && inTauri() && <button type="button" className="btn" onClick={() => void pick()}>选择</button>}
          </div>
          <span className="hint">{editing ? "根目录不能修改；要换目录请移除后重新添加，别名按名字保留。" : "支持 NAS 共享路径（\\\\NAS\\共享）和本机目录"}</span>
        </label>
        {cat ? (
          <label className="field" htmlFor="lf-pre">番号前缀（可选）
            <input id="lf-pre" className="input mono" placeholder="例如 OAE、ABC" value={prefixes} onChange={(e) => setPrefixes(e.target.value)} />
            <span className="hint">番号以这些前缀开头的影片，待确认时自动推荐放到这里；不填就只能手动指派</span>
          </label>
        ) : (
          <div className="field"><span>用途</span>
            <div className="seg" role="radiogroup" aria-label="用途">
              <button type="button" className="seg-opt" role="radio" aria-checked={role === "archive"} onClick={() => setRole("archive")}>入库目标</button>
              <button type="button" className="seg-opt" role="radio" aria-checked={role === "readonly"} onClick={() => setRole("readonly")}>
                <Icon of={LockOutlinedIcon} />只读（只用于匹配和查重）
              </button>
            </div>
          </div>
        )}
        {save.error && <Notice tone="red">{errorText(save.error)}</Notice>}
      </div>
      <div className="modal-foot">
        <span />
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!label.trim() || !path.trim() || save.isPending} onClick={() => save.mutate()}>
            {editing ? "保存" : cat ? "添加" : "添加并扫描"}
          </button>
        </div>
      </div>
    </>
  );
}

const REMOVE_TITLE = "mw-library-remove-title";

function RemoveDialog({ library, onClose }: { library: Library | null; onClose: () => void }) {
  return (
    <ModalShell open={library !== null} onClose={onClose} titleId={REMOVE_TITLE} narrow role="alertdialog">
      {library && <RemovePane key={library.id} library={library} onClose={onClose} />}
    </ModalShell>
  );
}

function RemovePane({ library: l, onClose }: { library: Library; onClose: () => void }) {
  const ws = useWorkspace();
  const refresh = useLibraryRefresh();
  const cat = l.kind === "category";
  const remove = useMutation({
    mutationFn: () => api.libraryRemove(l.id),
    onSuccess: () => { refresh(); ws.toast(`已移除「${l.label}」，NAS 上的文件没有改动`); onClose(); },
  });
  return (
    <>
      <ModalHead id={REMOVE_TITLE} title={`移除「${l.label}」？`} subtitle={cat ? "类型目录" : "演员片库"} onClose={onClose} />
      <div className="modal-body drawer-stack">
        <p className="small">{cat ? "待确认里不再列出这个类型目录。" : "TaskPaw 不再扫描这个片库，也不再推荐它作为入库目标。"}<b>NAS 上的文件不会有任何改动。</b></p>
        <dl className="facts">
          <dt>目录</dt><dd className="mono">{l.path}</dd>
          <dt>受影响</dt><dd>{cat ? "归档历史和回执保留" : `${l.performer_count + l.missing_count} 位演员的这个位置会从索引里去掉；归档历史和回执保留`}</dd>
        </dl>
        {remove.error && <Notice tone="red">{errorText(remove.error)}</Notice>}
      </div>
      <div className="modal-foot">
        <span />
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate()}>移除片库</button>
        </div>
      </div>
    </>
  );
}
