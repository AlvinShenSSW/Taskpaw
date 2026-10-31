// 从 Filmpaw 导入 (#361 §7): a read-only preview of Filmpaw's roots and aliases, then the import and
// the scans it queued. Filmpaw's library is never written; a re-import only adds what is new.
import CheckIcon from "@mui/icons-material/Check";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, HttpError, type ImportPreview, type ImportResult } from "../../api";
import { letters, useLibraries, useLibraryRefresh, useScanStatus } from "./libraryModel";
import { Chip, Icon, Notice } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { errorText } from "./workspace";

const TITLE_ID = "mw-import-title";

export function ImportDialog({ open, onClose, onView }: { open: boolean; onClose: () => void; onView: () => void }) {
  return (
    <ModalShell open={open} onClose={onClose} titleId={TITLE_ID} narrow>
      {open && <ImportPane onClose={onClose} onView={onView} />}
    </ModalShell>
  );
}

function ImportPane({ onClose, onView }: { onClose: () => void; onView: () => void }) {
  const refresh = useLibraryRefresh();
  const [path, setPath] = useState("");
  // the source read: "" = Filmpaw's default `%APPDATA%\Filmpaw\library.db`
  const [source, setSource] = useState("");
  const preview = useQuery({
    queryKey: ["libraryImportPreview", source], queryFn: () => api.libraryImportPreview(source), retry: false,
  });
  const run = useMutation({ mutationFn: () => api.libraryImport(source), onSuccess: refresh });
  if (run.data) return <ImportRun result={run.data} onView={onView} onClose={onClose} />;

  const data = preview.data;
  const fresh = data ? data.roots.some((r) => r.status === "new") || data.new_aliases > 0 : false;
  const missing = preview.error instanceof HttpError && preview.error.status === 404;
  return (
    <>
      <ModalHead id={TITLE_ID} title="从 Filmpaw 导入" subtitle="第 1 步，共 2 步 · 只读取，不修改 Filmpaw" onClose={onClose} />
      <div className="modal-body drawer-stack">
        {preview.isLoading && <p className="small muted">正在读取 Filmpaw 片库…</p>}
        {preview.error && (
          <>
            <Notice tone="amber" icon={WarningAmberIcon}>
              {missing ? `没有找到 Filmpaw 片库${source ? `：${source}` : "（默认位置 %APPDATA%\\Filmpaw\\library.db）"}。`
                : `无法读取 Filmpaw 片库：${errorText(preview.error)}`}
            </Notice>
            <label className="field" htmlFor="import-path">Filmpaw 片库位置
              <div className="path-field">
                <input id="import-path" className="input" value={path} placeholder="library.db 或它所在的目录"
                  onChange={(e) => setPath(e.target.value)} />
                <button type="button" className="btn" disabled={!path.trim()} onClick={() => { const next = path.trim(); if (next === source) void preview.refetch(); else setSource(next); }}>重新读取</button>
              </div>
            </label>
          </>
        )}
        {data && <PreviewBody data={data} fresh={fresh} />}
        {run.error && <Notice tone="red">导入失败：{errorText(run.error)}</Notice>}
      </div>
      <div className="modal-foot">
        <span />
        <div className="actions">
          <button type="button" className="btn ghost" onClick={onClose}>取消</button>
          <button type="button" className="btn primary" disabled={!fresh || run.isPending} onClick={() => run.mutate()}>导入并扫描</button>
        </div>
      </div>
    </>
  );
}

function PreviewBody({ data, fresh }: { data: ImportPreview; fresh: boolean }) {
  return (
    <>
      <dl className="facts"><dt>找到</dt><dd className="mono">{data.path}</dd></dl>
      <div>
        <div className="section-h">导入哪些</div>
        <ul className="check-list">
          {data.roots.map((r) => (
            <li key={r.path}>
              <div className="grow"><strong>{r.label}</strong><div className="path tiny">{r.path}</div></div>
              {r.status === "new" ? <span className="mono tiny muted">{r.performers} 位</span>
                : r.status === "exists" ? <Chip>已存在</Chip> : <Chip tone="amber">与已有片库重叠，跳过</Chip>}
            </li>
          ))}
          <li>
            <div className="grow"><strong>别名</strong><div className="tiny muted">{data.aliases} 组，按演员名对应</div></div>
            <span className="mono tiny muted">{data.new_aliases} 组新的</span>
          </li>
        </ul>
      </div>
      {fresh ? (
        <Notice icon={InfoOutlinedIcon}>演员目录不从 Filmpaw 复制，导入后由 TaskPaw 重新扫描 NAS，结果以 NAS 上的实际目录为准。Filmpaw 之后照常可用，两边互不影响。</Notice>
      ) : <Notice>没有新的片库或别名需要导入。</Notice>}
    </>
  );
}

// Step 2: what was imported, then each new library's scan as the scanner reports it.
function ImportRun({ result, onView, onClose }: { result: ImportResult; onView: () => void; onClose: () => void }) {
  const scan = useScanStatus().data;
  const libraries = useLibraries().data?.items;
  const letter = letters(libraries);
  const label = (id: string) => libraries?.find((l) => l.id === id)?.label ?? "";
  const scans = result.added.map((id) => {
    const outcome = scan?.results[id];
    const state = outcome ? (outcome.ok ? "done" : "offline") : scan?.running === id ? "run" : "wait";
    return { id, state };
  });
  const finished = scan !== undefined && scans.every((s) => s.state === "done" || s.state === "offline");
  const offline = scans.filter((s) => s.state === "offline").length;
  const steps: Array<{ key: string; title: string; state: string; note: string }> = [
    { key: "read", title: "读取 Filmpaw 片库设置", state: "done",
      note: `${result.added.length} 个新片库${result.skipped.length ? ` · 跳过 ${result.skipped.length} 个` : ""}` },
    { key: "aliases", title: "导入别名", state: "done", note: `${result.aliases_added} 组` },
    ...scans.map((s) => ({
      key: s.id, title: `扫描 ${letter[s.id] ?? ""} · ${label(s.id)}`, state: s.state,
      note: s.state === "done" ? "已完成" : s.state === "offline" ? "离线，恢复后自动补扫" : s.state === "run" ? "进行中…" : "等待",
    })),
  ];
  return (
    <>
      <ModalHead id={TITLE_ID} title={finished ? "导入完成" : "正在导入"} subtitle="第 2 步，共 2 步" onClose={onClose} />
      <div className="modal-body drawer-stack">
        <ul className="run-list" aria-label="导入进度">
          {steps.map((s, i) => (
            <li key={s.key}>
              <span className={`mark${s.state === "done" || s.state === "offline" ? " done" : s.state === "run" ? " run" : ""}`}>
                {s.state === "done" || s.state === "offline" ? <Icon of={CheckIcon} /> : i + 1}
              </span>
              <div className="grow"><strong className="small">{s.title}</strong>
                <div className={`tiny ${s.state === "offline" ? "t-amber" : "muted"}`}>{s.note}</div></div>
            </li>
          ))}
        </ul>
        {result.skipped.length > 0 && (
          <Notice tone="amber" icon={WarningAmberIcon}>
            {result.skipped.map((s) => <p key={s.path}><span className="mono">{s.path}</span>：{s.reason}</p>)}
          </Notice>
        )}
        {finished && (
          <Notice tone="green" icon={CheckIcon}>
            已导入 {result.added.length} 个片库、{result.aliases_added} 组别名。{offline > 0 && `${offline} 个片库离线，恢复在线后自动扫描。`}
          </Notice>
        )}
      </div>
      <div className="modal-foot">
        <p>扫描在后台继续，可以先关闭。</p>
        <div className="actions"><button type="button" className="btn primary" onClick={onView}>查看片库</button></div>
      </div>
    </>
  );
}
