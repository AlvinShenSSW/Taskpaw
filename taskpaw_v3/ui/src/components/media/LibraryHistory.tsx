// 片库与规则 · 归档历史 (#361 §10, D6): every archive receipt with 打开目录 per target, and the
// receipt drawer (files with SHA-256, both targets, cleanup state).
import CheckIcon from "@mui/icons-material/Check";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import ReceiptLongOutlinedIcon from "@mui/icons-material/ReceiptLongOutlined";
import SearchIcon from "@mui/icons-material/Search";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useDeferredValue, useState, type MouseEvent } from "react";
import { api, type HistoryFilter, type HistoryRow, type Library } from "../../api";
import { Pager, SideDrawer } from "./LibraryParts";
import { HISTORY_KEY, letters, libraryOf, parentName, rowKeys, shortHash, useOpenPath } from "./libraryModel";
import { fmtBytes, fmtDateTime } from "./mediaModel";
import { Chip, Icon, Notice } from "./parts";
import { errorText } from "./workspace";

const PAGE_SIZE = 50;  // the route's fixed page size
const FILTERS: Array<["all" | HistoryFilter, string]> = [
  ["all", "全部"], ["pending", "待清理"], ["new", "新建演员"], ["category", "类型目录"], ["manual", "手动入库"],
];

function KindChip({ row, libraries }: { row: HistoryRow; libraries: Library[] }) {
  const second = row.second_target_path ? libraryOf(libraries, row.second_target_path) : undefined;
  return (
    <>
      {row.kind === "new_actor" && <> <Chip tone="green">新建演员</Chip></>}
      {row.kind === "manual" && <> <Chip>手动入库</Chip></>}
      {row.kind === "category" && <> <Chip tone="blue">类型目录</Chip></>}
      {row.second_target_path && <> <Chip tone="blue">+ {second?.label ?? "第二目标"}</Chip></>}
    </>
  );
}

// 演员 · 片库: the actor directory (the film folder's parent) and its library's letter; a category
// receipt names the category directory.
function Who({ row, libraries }: { row: HistoryRow; libraries: Library[] }) {
  const lib = libraryOf(libraries, row.target_path);
  if (row.kind === "category") return <><span className="muted">类型 ·</span> {lib?.label ?? parentName(row.target_path)}</>;
  const actor = row.kind === "manual" ? "—" : parentName(row.target_path);
  const mark = lib ? letters(libraries)[lib.id] ?? lib.label : null;
  return <>{actor}{mark && <span className="muted"> · {mark}</span>}</>;
}

function OpenButtons({ row, libraries, big = false }: { row: HistoryRow; libraries: Library[]; big?: boolean }) {
  const open = useOpenPath();
  const one = (path: string, label: string) => (
    <button key={path} type="button" className={`btn small${big ? " primary" : ""}`} title={path} disabled={open.isPending}
      onClick={(e: MouseEvent) => { e.stopPropagation(); open.mutate(path); }}>
      <Icon of={OpenInNewIcon} />{label}
    </button>
  );
  const second = row.second_target_path;
  if (!second) return one(row.target_path, big ? "打开归档目录" : "打开目录");
  const category = libraryOf(libraries, second)?.label ?? "类型目录";
  return <>{one(row.target_path, big ? "打开演员目录" : "演员目录")}{one(second, big ? `打开「${category}」` : "类型目录")}</>;
}

export function HistoryTab({ libraries }: { libraries: Library[] }) {
  const [text, setText] = useState("");
  const q = useDeferredValue(text.trim());
  const [filter, setFilter] = useState<"all" | HistoryFilter>("all");
  const [page, setPage] = useState(1);
  const [opened, setOpened] = useState<HistoryRow | null>(null);
  const history = useQuery({
    queryKey: [...HISTORY_KEY, filter, q, page],
    queryFn: () => api.libraryHistory(filter === "all" ? null : filter, q, page), placeholderData: keepPreviousData,
  });
  const rows = history.data?.items ?? [];
  return (
    <div className="stack">
      <div className="toolbar">
        <div className="search-wrap">
          <Icon of={SearchIcon} />
          <input className="input search" type="search" placeholder="搜索番号或演员" aria-label="搜索番号或演员" value={text}
            onChange={(e) => { setText(e.target.value); setPage(1); }} />
        </div>
        <div className="filters">
          {FILTERS.map(([key, label]) => (
            <button key={key} type="button" className="filter-pill" aria-pressed={filter === key} onClick={() => { setFilter(key); setPage(1); }}>{label}</button>
          ))}
        </div>
      </div>
      {history.error && <Notice tone="red">无法读取归档历史：{errorText(history.error)}</Notice>}
      <div className="panel">
        <div className="table-wrap">
          <table className="grid">
            <thead><tr><th>完成时间</th><th>影片</th><th>演员 · 片库</th><th>来源清理</th><th>归档目录</th><th><span className="sr-only">回执</span></th></tr></thead>
            <tbody>
              {rows.map((row) => {
                const open = () => setOpened(row);
                return (
                  <tr key={row.receipt_id} tabIndex={0} aria-label={`${row.number} 回执`} onClick={open} onKeyDown={rowKeys(open)}>
                    <td className="mono muted">{fmtDateTime(row.accepted_at)}</td>
                    <td><strong className="mono">{row.number}</strong><KindChip row={row} libraries={libraries} /></td>
                    <td><Who row={row} libraries={libraries} /></td>
                    <td>{row.pending ? <span className="state t-amber">待清理</span> : <span className="state t-green">已清理</span>}</td>
                    <td><div className="actions" style={{ gap: 6, flexWrap: "nowrap" }}><OpenButtons row={row} libraries={libraries} /></div></td>
                    <td style={{ width: 40, color: "var(--muted)" }}><Icon of={ChevronRightIcon} /></td>
                  </tr>
                );
              })}
              {history.data && rows.length === 0 && (
                <tr><td colSpan={6}><div className="empty" style={{ padding: 30 }}><p>没有符合条件的记录。</p></div></td></tr>
              )}
            </tbody>
          </table>
        </div>
        <Pager page={page} total={history.data?.total ?? 0} size={PAGE_SIZE} onPage={setPage}>
          <span>显示 {rows.length} 条 · 回执永久保留</span>
          <span>待清理的影片也会出现在「异常」页</span>
        </Pager>
      </div>
      <SideDrawer open={opened !== null} onClose={() => setOpened(null)} title="归档回执"
        subtitle={<span className="mono">{opened?.receipt_id}</span>} label="归档回执">
        {opened && <Receipt key={opened.receipt_id} id={opened.receipt_id} libraries={libraries} />}
      </SideDrawer>
    </div>
  );
}

function Receipt({ id, libraries }: { id: string; libraries: Library[] }) {
  const receipt = useQuery({ queryKey: [...HISTORY_KEY, "receipt", id], queryFn: () => api.libraryReceipt(id) });
  const r = receipt.data;
  if (receipt.isLoading) return <p className="muted">正在读取回执…</p>;
  if (receipt.error || !r) return <Notice tone="red">无法读取回执：{errorText(receipt.error)}</Notice>;
  const two = r.second_target_path !== null;
  const size = r.files.reduce((n, f) => n + f.size, 0);
  const n = r.files.length;
  return (
    <>
      <div className="receipt">
        <h3><Icon of={ReceiptLongOutlinedIcon} /> {r.number} 已入库</h3>
        <p>{two ? "两份各 " : ""}{n}/{n} 个文件 SHA-256 一致 · {fmtBytes(size)}{two ? " ×2" : ""}</p>
        <div className="actions spacer-top"><OpenButtons row={r} libraries={libraries} big /></div>
      </div>
      <dl className="facts">
        <dt>{r.kind === "category" ? "类型目录" : "演员"}</dt>
        <dd><Who row={r} libraries={libraries} />{r.kind === "new_actor" && <> <Chip tone="green">本次新建目录</Chip></>}
          {r.kind === "manual" && <> <Chip>手动入库</Chip></>}</dd>
        <dt>目标</dt>
        <dd className="mono">{r.target_path}{two && <><br />{r.second_target_path} <Chip tone="blue">第 2 份</Chip></>}</dd>
        <dt>确认方式</dt><dd>{r.kind === "manual" ? "手动选择目录后确认" : "在待确认里点「确认入库」"}</dd>
        <dt>完成</dt><dd className="mono">{fmtDateTime(r.accepted_at)}</dd>
      </dl>
      <section>
        <div className="section-h">文件</div>
        <div className="table-wrap">
          <table className="files-table" aria-label="归档文件">
            <thead><tr><th>文件</th><th>大小</th><th>SHA-256</th><th>校验</th></tr></thead>
            <tbody>
              {r.files.map((f) => (
                <tr key={f.path}><td>{f.path}</td><td className="mono">{fmtBytes(f.size)}</td>
                  <td className="mono muted" title={f.sha256}>{shortHash(f.sha256)}</td><td className="t-green">一致</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
      <section>
        <div className="section-h">来源清理</div>
        {r.pending ? (
          <Notice tone="amber" icon={WarningAmberIcon}>
            <p><b>已入库 · 待清理</b></p>
            <p>来源已保留；只会重试清理，不会重新复制。待清理的影片也会出现在「异常」页。</p>
          </Notice>
        ) : (
          <Notice tone="green" icon={CheckIcon}>已清理主控 INBOX 下本次收件的来源文件。NAS 片库不受影响。</Notice>
        )}
      </section>
    </>
  );
}
