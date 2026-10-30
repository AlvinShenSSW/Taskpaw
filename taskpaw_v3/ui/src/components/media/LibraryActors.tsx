// 片库与规则 · 演员 (#361 §6, §11): search, filters, the 3:4 folder.jpg thumbnails and the drawer
// with every location of a name, its aliases (group-level) and its recent receipts.
import AddIcon from "@mui/icons-material/Add";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import CloseIcon from "@mui/icons-material/Close";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import SearchIcon from "@mui/icons-material/Search";
import { keepPreviousData, useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { useDeferredValue, useState, type FormEvent } from "react";
import { api, type Library, type LibraryPerformer, type PerformerFilter } from "../../api";
import { ActorThumb, Pager, SideDrawer } from "./LibraryParts";
import { actorLibraries, letters, libraryOf, offline, PERFORMER_KEY, PERFORMERS_KEY, rowKeys, useOpenPath } from "./libraryModel";
import { fmtDateTime } from "./mediaModel";
import { Chip, Icon, Notice } from "./parts";
import { errorText, useWorkspace } from "./workspace";

const PAGE_SIZE = 50;
const FILTERS: Array<["all" | PerformerFilter, string]> = [
  ["all", "全部"], ["multi", "多个位置"], ["alias", "有别名"], ["missing", "目录已缺失"], ["nothumb", "没有头像"],
];

// One row per name group (the backend's `name_norm`, so 張 and 张 are one): the grouped listing
// pages over groups and returns every location of each, ordered by `name_norm`.
function byName(items: LibraryPerformer[]): LibraryPerformer[][] {
  const groups = new Map<string, LibraryPerformer[]>();
  for (const p of items) groups.set(p.name_norm, [...(groups.get(p.name_norm) ?? []), p]);
  return [...groups.values()];
}

const sumFilms = (locations: LibraryPerformer[]) => locations.reduce((n, p) => n + (p.film_count ?? 0), 0);

export function ActorsTab({ libraries }: { libraries: Library[] }) {
  const [text, setText] = useState("");
  const q = useDeferredValue(text.trim());
  const [filter, setFilter] = useState<"all" | PerformerFilter>("all");
  const [library, setLibrary] = useState("");
  const [page, setPage] = useState(1);
  const [opened, setOpened] = useState<string | null>(null);
  const query = { q, library_id: library || undefined, filter: filter === "all" ? undefined : filter, page, page_size: PAGE_SIZE };
  const list = useQuery({
    queryKey: [...PERFORMERS_KEY, query], queryFn: () => api.libraryPerformers(query), placeholderData: keepPreviousData,
  });
  const counts = useQueries({
    queries: FILTERS.map(([key]) => ({
      queryKey: [...PERFORMERS_KEY, "count", key],
      queryFn: () => api.libraryPerformers({ filter: key === "all" ? undefined : key, page_size: 1 }),
    })),
    combine: (results) => results.map((r) => r.data?.total),
  });
  const letter = letters(libraries);
  const rows = byName(list.data?.items ?? []);
  const reset = <T,>(set: (v: T) => void) => (v: T) => { set(v); setPage(1); };
  return (
    <div className="stack">
      <div className="toolbar">
        <div className="filters">
          <div className="search-wrap">
            <Icon of={SearchIcon} />
            <input className="input search" type="search" placeholder="搜索名字或别名" aria-label="搜索名字或别名" value={text}
              onChange={(e) => reset(setText)(e.target.value)} />
          </div>
          <select aria-label="按片库筛选" value={library} onChange={(e) => reset(setLibrary)(e.target.value)}>
            <option value="">全部片库</option>
            {actorLibraries(libraries).map((l) => <option key={l.id} value={l.id}>{letter[l.id]} · {l.label}</option>)}
          </select>
        </div>
        <div className="filters">
          {FILTERS.map(([key, label], i) => (
            <button key={key} type="button" className="filter-pill" aria-pressed={filter === key} onClick={() => reset(setFilter)(key)}>
              {label}{counts[i] !== undefined && <span className="n">{counts[i]}</span>}
            </button>
          ))}
        </div>
      </div>
      {list.error && <Notice tone="red">无法读取演员：{errorText(list.error)}</Notice>}
      <div className="panel">
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>演员</th><th>位置</th><th>影片</th><th><span className="sr-only">打开</span></th></tr></thead>
            <tbody>
              {rows.map((locations) => {
                const first = locations.find((p) => p.has_thumb) ?? locations[0];
                const open = () => setOpened(first.id);
                return (
                  <tr key={locations[0].id} tabIndex={0} aria-label={`${first.name} 详情`} onClick={open} onKeyDown={rowKeys(open)}>
                    <td><div className="who">
                      <ActorThumb id={first.id} hasThumb={first.has_thumb} name={first.name} />
                      <div style={{ minWidth: 0 }}><strong>{first.name}</strong>
                        {first.aliases.length > 0 && <div className="aliases">别名：{first.aliases.join("、")}</div>}</div>
                    </div></td>
                    <td><div className="chips">{locations.map((p) => <LocationChip key={p.id} performer={p} libraries={libraries} mark={letter[p.library_id]} />)}</div></td>
                    <td className="mono">{sumFilms(locations)}</td>
                    <td style={{ width: 40, color: "var(--muted)" }}><Icon of={ChevronRightIcon} /></td>
                  </tr>
                );
              })}
              {list.data && rows.length === 0 && (
                <tr><td colSpan={4}><div className="empty" style={{ padding: 30 }}><Icon of={SearchIcon} /><p>没有符合条件的演员。</p></div></td></tr>
              )}
            </tbody>
          </table>
        </div>
        <Pager page={page} total={list.data?.total ?? 0} size={PAGE_SIZE} onPage={setPage}>
          <span>显示 {rows.length} / {list.data?.total ?? 0} 位</span>
          <span>缩略图取演员目录下的 folder.jpg · 同名演员共用一组别名</span>
        </Pager>
      </div>
      <SideDrawer open={opened !== null} onClose={() => setOpened(null)} title="演员详情" subtitle="名字来自 NAS 上的目录名" label="演员详情">
        {opened !== null && <ActorDetail key={opened} id={opened} libraries={libraries} />}
      </SideDrawer>
    </div>
  );
}

function LocationChip({ performer: p, libraries, mark }: { performer: LibraryPerformer; libraries: Library[]; mark?: string }) {
  const lib = libraries.find((l) => l.id === p.library_id);
  const off = lib ? offline(lib) : false;
  return (
    <span className={`loc-chip${p.is_missing ? " miss" : off ? " off" : ""}`} title={p.source_label ?? ""}>
      <b>{mark ?? "—"}</b>{p.film_count ?? "—"} 部{p.is_missing ? " · 已缺失" : off ? " · 离线" : ""}
    </span>
  );
}

function ActorDetail({ id, libraries }: { id: string; libraries: Library[] }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const open = useOpenPath();
  const detail = useQuery({ queryKey: [...PERFORMER_KEY, id], queryFn: () => api.libraryPerformer(id) });
  const [alias, setAlias] = useState("");
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: PERFORMER_KEY });
    void qc.invalidateQueries({ queryKey: PERFORMERS_KEY });
  };
  const add = useMutation({
    mutationFn: (value: string) => api.libraryAddAlias(id, value),
    onSuccess: (created) => { setAlias(""); refresh(); ws.toast(`已添加别名「${created.alias}」`); },
  });
  const remove = useMutation({
    mutationFn: (target: { id: number; alias: string }) => api.libraryDeleteAlias(target.id),
    onSuccess: (_, target) => { refresh(); ws.toast(`已删除别名「${target.alias}」`); },
    onError: (e) => ws.toast(errorText(e)),
  });
  const letter = letters(libraries);
  const a = detail.data;
  if (detail.isLoading) return <p className="muted">正在读取演员…</p>;
  if (detail.error || !a) return <Notice tone="red">无法读取这位演员：{errorText(detail.error)}</Notice>;
  const submit = (e: FormEvent) => { e.preventDefault(); if (alias.trim()) add.mutate(alias.trim()); };
  return (
    <>
      <div style={{ display: "flex", gap: 18, alignItems: "center" }}>
        <ActorThumb id={a.id} hasThumb={a.has_thumb} name={a.name} large />
        <div style={{ minWidth: 0 }}>
          <h2 style={{ fontSize: 22 }}>{a.name}</h2>
          <p className="small muted">{a.locations.length} 个位置 · {sumFilms(a.locations)} 部</p>
          {!a.has_thumb && <p className="tiny t-amber" style={{ marginTop: 4 }}>目录里没有 folder.jpg，显示占位头像</p>}
        </div>
      </div>
      <section>
        <div className="section-h">位置</div>
        <div className="loc-list">
          {a.locations.map((p) => {
            const lib = libraries.find((l) => l.id === p.library_id);
            const off = lib ? offline(lib) : false;
            return (
              <div key={p.id} className="loc-item">
                <div style={{ minWidth: 0 }}>
                  <strong className="small">{letter[p.library_id] ?? "—"} · {p.source_label}</strong>{" "}
                  {p.is_missing ? <Chip tone="amber">目录已缺失</Chip> : off ? <Chip tone="red">离线</Chip> : p.role !== "archive" ? <Chip>只读</Chip> : null}
                  <div className="path tiny">{p.unc_path}</div>
                  {p.is_missing && <div className="tiny muted" style={{ marginTop: 4 }}>上次扫描没找到这个目录。如果是改了名，下次扫描会自动对上新名字；记录不会删除。</div>}
                </div>
                <div className="actions">
                  <span className="mono small">{p.film_count ?? "—"} 部</span>
                  <button type="button" className="btn small ghost" disabled={p.is_missing || off || !p.unc_path || open.isPending}
                    onClick={() => p.unc_path && open.mutate(p.unc_path)}><Icon of={OpenInNewIcon} />打开</button>
                </div>
              </div>
            );
          })}
        </div>
      </section>
      <section>
        <div className="section-h">别名</div>
        <p className="tiny muted" style={{ marginBottom: 10 }}>Miko 刮削出的名字和目录名对不上时，用别名来匹配。同名的所有位置共用这组别名。</p>
        <div className="alias-edit">
          {a.aliases.length ? a.aliases.map((value, i) => (
            <span key={a.alias_ids[i]} className="alias-chip">{value}
              <button type="button" aria-label={`删除别名 ${value}`} disabled={remove.isPending}
                onClick={() => remove.mutate({ id: a.alias_ids[i], alias: value })}><Icon of={CloseIcon} /></button>
            </span>
          )) : <span className="small muted">还没有别名</span>}
        </div>
        <form className="alias-add" onSubmit={submit}>
          <input className="input" placeholder="添加别名，例如日文名或旧艺名" aria-label="新别名" value={alias}
            onChange={(e) => { setAlias(e.target.value); add.reset(); }} />
          <button type="submit" className="btn" disabled={!alias.trim() || add.isPending}><Icon of={AddIcon} />添加</button>
        </form>
        {add.error && <p className="small t-red spacer-top">{errorText(add.error)}</p>}
      </section>
      <section>
        <div className="section-h">最近入库</div>
        <ul className="package-list">
          {a.receipts.length ? a.receipts.map((r) => {
            const lib = libraryOf(libraries, r.target_path);
            return <li key={r.receipt_id}><span className="mono">{r.number}</span><span className="muted">{fmtDateTime(r.accepted_at)}{lib ? ` · ${letter[lib.id] ?? lib.label}` : ""}</span></li>;
          }) : <li><span className="muted">TaskPaw 还没有为这位演员入过库</span></li>}
        </ul>
      </section>
    </>
  );
}
