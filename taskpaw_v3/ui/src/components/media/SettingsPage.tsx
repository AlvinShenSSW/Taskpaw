// 设置 (#361 §11, D9): the same 媒体工作流 settings as TaskPaw 设置 (the shared section, not a copy)
// plus the 片库 card — scan schedule (saved to the library's rules), its data file and the import.
import FileDownloadOutlinedIcon from "@mui/icons-material/FileDownloadOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useState } from "react";
import type { LibraryRules } from "../../api";
import { WorkflowSettingsSection } from "../WorkflowSettings";
import { ImportDialog } from "./LibraryImport";
import { Toggle } from "./LibraryParts";
import { actorLibraries, categories, SCAN_EVERY, useLibraries, useRules, useSaveRules } from "./libraryModel";
import { Icon, Notice, PageTitle } from "./parts";
import { errorText, useWorkspace } from "./workspace";

export function SettingsPage() {
  const ws = useWorkspace();
  const [importing, setImporting] = useState(false);
  return (
    <>
      <PageTitle title="设置" summary="与 TaskPaw「设置 → 媒体工作流」是同一份配置，在哪边改都一样" />
      <div className="stack">
        <WorkflowSettingsSection />
        <LibrarySettings onImport={() => setImporting(true)} />
      </div>
      <ImportDialog open={importing} onClose={() => setImporting(false)}
        onView={() => { setImporting(false); ws.navigate("libraries"); }} />
    </>
  );
}

function LibrarySettings({ onImport }: { onImport: () => void }) {
  const libraries = useLibraries();
  const rules = useRules();
  const save = useSaveRules("已保存");
  const items = libraries.data?.items;
  const r = rules.data;
  const error = libraries.error ?? rules.error;
  const actors = actorLibraries(items);
  return (
    <section className="panel" aria-labelledby="mw-settings-library">
      <div className="panel-head"><div><h2 id="mw-settings-library">片库</h2><p>NAS 片库本身在「片库与规则」里管理；这里是扫描和数据</p></div></div>
      <div className="panel-body stack" style={{ gap: 16 }}>
        {error ? <Notice tone="red" icon={WarningAmberIcon}>无法读取片库：{errorText(error)}</Notice> : !r || !items ? (
          <p className="small muted">正在读取片库…</p>
        ) : (
          <>
            <div className="form-grid">
              <label className="field" htmlFor="mw-scan-every">自动重新扫描
                <select id="mw-scan-every" value={r.scan_every_hours} disabled={save.isPending}
                  onChange={(e) => save.mutate({ scan_every_hours: Number(e.target.value) as LibraryRules["scan_every_hours"] })}>
                  {SCAN_EVERY.map(([hours, label]) => <option key={hours} value={hours}>{label}</option>)}
                </select>
                <span className="hint">离线的片库会跳过，恢复在线后补扫</span>
              </label>
              <div className="field"><span>启动时扫描</span>
                <div style={{ display: "flex", alignItems: "center", gap: 12, minHeight: 44 }}>
                  <Toggle checked={r.scan_on_start} label="启动时扫描" disabled={save.isPending}
                    onChange={(v) => save.mutate({ scan_on_start: v })} />
                  <span className="small muted">TaskPaw 启动后扫描一次全部在线片库</span>
                </div>
              </div>
            </div>
            <dl className="facts">
              <dt>片库数据</dt><dd className="mono">{libraries.data?.db_path}</dd>
              <dt>当前规模</dt>
              <dd>{items.length === 0 ? "空" : `${actors.length} 个片库 · ${categories(items).length} 个类型目录 · ${actors.reduce((n, l) => n + l.performer_count, 0)} 个演员目录`}</dd>
            </dl>
            <div className="import-line">
              <div>
                <strong className="small">从 Filmpaw 导入</strong>
                <p className="tiny muted">只读取 Filmpaw 的片库根目录和别名，不会修改它。再次导入只补充新的片库和别名，不会覆盖你在 TaskPaw 里的修改。</p>
              </div>
              <button type="button" className="btn" onClick={onImport}><Icon of={FileDownloadOutlinedIcon} />{items.length === 0 ? "导入" : "再次导入"}</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
