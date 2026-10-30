// 片库与规则 · 归档规则 (#361 §8): every switch is saved on its own (「规则已保存」); the fixed rules
// have no switch.
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown";
import KeyboardArrowUpIcon from "@mui/icons-material/KeyboardArrowUp";
import LockOutlinedIcon from "@mui/icons-material/LockOutlined";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Library, type LibraryRules, type MultiLocation } from "../../api";
import { Toggle } from "./LibraryParts";
import { actorLibraries, LIBRARIES_KEY, letters, RULES_KEY, useRules, useSaveRules } from "./libraryModel";
import { Chip, Icon, Notice } from "./parts";
import { errorText, useWorkspace } from "./workspace";

type Flag = { [K in keyof LibraryRules]: LibraryRules[K] extends boolean ? K : never }[keyof LibraryRules];

const MULTI: Array<[MultiLocation, string]> = [["priority", "按片库优先级"], ["most", "影片最多的位置"], ["recent", "最近入库的位置"]];
const FIXED: Array<[string, string]> = [
  ["正式片库只追加", "不覆盖、不删除、不移动 NAS 上已有的任何文件。"],
  ["同名影片目录已存在就拒绝", "目标目录（演员目录或类型目录）下已有同名影片目录时不入库，放进「异常」由你核对。"],
  ["逐个文件校验后才算入库", "复制完逐个比对 SHA-256，全部一致并写下回执才算完成。"],
  ["先有回执，再清理来源", "只清理本次收件清单里的文件；文件变了或被占用就保留为「已入库 · 待清理」。"],
  ["有第二目标时两份都要成功", "两份都校验通过、写下回执后才清理来源；只重试失败的那一份，已成功的不再复制。"],
  ["只重试没完成的那一步", "清理失败只重试清理，不重新复制，也不重新修复或刮削。"],
  ["只读片库不作入库目标", "只读片库只用来匹配和查重，不会往里写。"],
];

// The new order of every actor library after moving `id` past its archive-role neighbour (read-only
// libraries keep their places: they are never targets, so they are not ranked).
function moved(actors: Library[], id: string, step: -1 | 1): string[] {
  const ids = actors.map((l) => l.id);
  const ranked = actors.filter((l) => l.role === "archive").map((l) => l.id);
  const other = ranked[ranked.indexOf(id) + step];
  const i = ids.indexOf(id);
  const j = ids.indexOf(other);
  [ids[i], ids[j]] = [ids[j], ids[i]];
  return ids;
}

export function RulesTab({ libraries }: { libraries: Library[] }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const rules = useRules();
  const save = useSaveRules();
  const order = useMutation({
    mutationFn: (ids: string[]) => api.libraryOrder(ids),
    onSuccess: (list) => {
      qc.setQueryData(LIBRARIES_KEY, list);
      void qc.invalidateQueries({ queryKey: RULES_KEY });  // an unset 新演员 default follows the order
      ws.toast("规则已保存");
    },
    // a refused order (another window changed the libraries) shows the stored one again
    onError: (e) => { void qc.invalidateQueries({ queryKey: LIBRARIES_KEY }); ws.toast(errorText(e)); },
  });
  const r = rules.data;
  if (rules.isLoading) return <p className="muted">正在读取归档规则…</p>;
  if (rules.error || !r) return <Notice tone="red" icon={WarningAmberIcon}>无法读取归档规则：{errorText(rules.error)}</Notice>;
  const actors = actorLibraries(libraries);
  const archive = actors.filter((l) => l.role === "archive");
  const letter = letters(libraries);
  const newLibrary = archive.find((l) => l.id === r.new_actor_library);
  const flag = (key: Flag, title: string, text: string) => (
    <div className="rule-row">
      <div><h3 id={`rule-${key}`}>{title}</h3><p>{text}</p></div>
      <Toggle checked={r[key]} labelledBy={`rule-${key}`} disabled={save.isPending} onChange={(v) => save.mutate({ [key]: v })} />
    </div>
  );
  const archiveRank = new Map(archive.map((l, i) => [l.id, i + 1]));
  return (
    <div className="rules-grid">
      <div className="stack">
        <section className="panel">
          <div className="panel-head"><div><h2>演员在多个片库时</h2><p>预选哪个位置作为入库目标；待确认里随时可以改选</p></div></div>
          <div className="panel-body">
            <div className="seg" role="radiogroup" aria-label="多位置时的默认目标">
              {MULTI.map(([key, label]) => (
                <button key={key} type="button" className="seg-opt" role="radio" aria-checked={r.multi_location === key}
                  disabled={save.isPending} onClick={() => save.mutate({ multi_location: key })}>
                  {label}{key === "priority" && <span className="rec-tag">推荐</span>}
                </button>
              ))}
            </div>
            {r.multi_location === "priority" ? (
              <>
                <p className="tiny muted" style={{ marginTop: 14 }}>片库优先级（同时决定「新演员」下拉里的顺序）</p>
                <ol className="prio" aria-label="片库优先级">
                  {actors.map((l) => {
                    const ro = l.role !== "archive";
                    return (
                      <li key={l.id} className={ro ? "ro" : undefined}>
                        <span className="rank">{archiveRank.get(l.id) ?? "—"}</span>
                        <div className="grow"><strong className="small">{letter[l.id]} · {l.label}</strong><div className="path tiny">{l.path}</div></div>
                        {ro ? <Chip>只读</Chip> : (
                          <>
                            <button type="button" className="btn ghost icon-btn small" aria-label={`上移 ${l.label}`}
                              disabled={order.isPending || archiveRank.get(l.id) === 1}
                              onClick={() => order.mutate(moved(actors, l.id, -1))}><Icon of={KeyboardArrowUpIcon} /></button>
                            <button type="button" className="btn ghost icon-btn small" aria-label={`下移 ${l.label}`}
                              disabled={order.isPending || archiveRank.get(l.id) === archive.length}
                              onClick={() => order.mutate(moved(actors, l.id, 1))}><Icon of={KeyboardArrowDownIcon} /></button>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ol>
              </>
            ) : (
              <div className="example">例：同一位演员在 A 有 18 部、在 B 有 9 部 → 预选 <b>{r.multi_location === "most" ? "A（影片最多）" : "最近一次入库所在的位置"}</b></div>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><div><h2>匹配</h2><p>根据 Miko 刮削出的演员名，在片库里找候选</p></div></div>
          <div className="panel-body">
            {flag("alias_match", "用别名匹配", "名字对不上时，再查每位演员的别名。")}
            {flag("preselect_single", "只有一个候选时自动预选", "仍然需要你点「确认入库」，不会自动归档。")}
            {flag("fuzzy", "显示相近名字", "大小写、空格、全角半角不同的名字也列为候选，但不预选。")}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><div><h2>新演员</h2><p>片库里还没有这位演员时</p></div></div>
          <div className="panel-body">
            {flag("allow_new_actor", "确认时允许新建演员目录", "在待确认里选片库、填名字；点「确认入库」时才创建，不会自动新建。")}
            {r.allow_new_actor && (
              <>
                <div className="form-grid" style={{ marginTop: 16 }}>
                  <label className="field" htmlFor="rule-new-lib">默认放到
                    <select id="rule-new-lib" value={r.new_actor_library ?? ""} disabled={save.isPending || archive.length === 0}
                      onChange={(e) => save.mutate({ new_actor_library: e.target.value })}>
                      {archive.length === 0 && <option value="">没有可入库的演员片库</option>}
                      {archive.map((l) => <option key={l.id} value={l.id}>{letter[l.id]} · {l.label}</option>)}
                    </select>
                  </label>
                  <label className="field" htmlFor="rule-new-name">目录名
                    <select id="rule-new-name" value={r.new_actor_name} disabled={save.isPending}
                      onChange={(e) => save.mutate({ new_actor_name: e.target.value as LibraryRules["new_actor_name"] })}>
                      <option value="actor">Miko 刮削出的演员名</option>
                      <option value="ask">每次手动填写</option>
                    </select>
                  </label>
                </div>
                {newLibrary && (
                  <div className="example">例：<span className="path">{newLibrary.path}<span style={{ color: "#fde68a" }}>演员名</span>{"\\"}番号</span></div>
                )}
              </>
            )}
          </div>
        </section>
        <section className="panel">
          <div className="panel-head"><div><h2>类型目录</h2><p>写真系列、合集、单体等；番号前缀在「NAS 片库 → 类型目录」里设置</p></div></div>
          <div className="panel-body">
            {flag("category_prefix", "演员不在片库时，按番号前缀预选类型目录", "例：番号前缀命中「写真 · 某系列」的前缀、演员又不在你的收藏里 → 预选这个类型目录。没命中前缀就不预选，由你来选。")}
            {flag("second_target_hint", "演员已在片库时，前缀命中就提示「第二目标」", "默认进演员自己的目录，下方提示可以再复制一份到命中的类型目录；加不加由你决定。")}
            {flag("category_always", "待确认里总是列出类型目录", "有演员匹配时，也可以改放到类型目录，代替演员目录。")}
          </div>
        </section>
      </div>
      <div className="stack">
        <section className="panel">
          <div className="panel-head"><div><h2>固定规则</h2><p>保护片库的底线，不提供开关</p></div><Icon of={LockOutlinedIcon} /></div>
          <div className="panel-body fixed">
            {FIXED.map(([title, text]) => (
              <div key={title} className="fixed-rule"><Icon of={LockOutlinedIcon} /><div><h3>{title}</h3><p>{text}</p></div></div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
