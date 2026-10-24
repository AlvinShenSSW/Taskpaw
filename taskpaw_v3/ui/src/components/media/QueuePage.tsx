import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import SearchIcon from "@mui/icons-material/Search";
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type WorkflowFilms } from "../../api";
import { fmtKwhShort } from "../power/powerModel";
import {
  fmtBytes, fmtHoursMinutes, isRemoteRow, isRestoring, itemActors, itemNumber, itemResolution, itemTitle, machineOf, matchesStage,
  readFilmTiming, STAGE_OPTIONS, stepDurations, workItems, type StageFilter, type WorkItem,
} from "./mediaModel";
import { Chip, EmptyState, Icon, ItemCover, PageTitle, StageBlock, StatusChip, StepLine } from "./parts";
import { errorText, useWorkspace } from "./workspace";

type Resolution = "all" | "4K" | "1080p";

// 影片队列 (§4.2): every film past 待处理, plus the INBOX packages still arriving; packages that
// need the operator are on 异常.
export function QueuePage() {
  const ws = useWorkspace();
  const [resolution, setResolution] = useState<Resolution>("all");
  const [search, setSearch] = useState("");
  const completed = ws.queueStage === "completed";
  const [page, setPage] = useState(1);
  const [lastGood, setLastGood] = useState<WorkflowFilms | undefined>(ws.data.films.data);
  const history = useQuery({
    queryKey: ["mediaHistory", ws.server?.id, page],
    queryFn: () => ws.server ? api.hubWorkflowFilms(ws.server.id, page) : api.workflowFilms(page),
    enabled: completed, refetchInterval: 5000, placeholderData: keepPreviousData, gcTime: 0, retry: false,
  });
  useEffect(() => {
    if (history.data && !history.isPlaceholderData && !history.isError) {
      setLastGood(history.data);
    }
  }, [history.data, history.isPlaceholderData, history.isError]);
  const historyData = history.data ?? lastGood ?? ws.data.films.data;
  const paging = historyData?.done_paging;
  const changingPage = history.isPlaceholderData || (history.isFetching && page !== paging?.page);
  const pickPage = (next: number) => { if (next === page) void history.refetch(); else setPage(next); };
  const items = completed ? workItems(historyData, {}, ws.readOnly) : ws.data.filmItems;
  const q = search.trim().toLowerCase();
  const films = items.filter((i) => i.status !== "intake_attention"
    && matchesStage(i, ws.queueStage)
    && (completed || resolution === "all" || itemResolution(i) === resolution)
    && (completed || !q || `${itemTitle(i)} ${itemNumber(i) ?? ""} ${i.kind === "film" ? i.row.name : ""} ${itemActors(i)}`.toLowerCase().includes(q)));
  return (
    <>
      <PageTitle title="影片队列" actions={
        <button type="button" className="btn ghost" onClick={() => ws.navigate("settings")}><Icon of={FolderOpenIcon} />收件规则</button>
      } />
      <div className="toolbar">
        <div className="filters">
          <select aria-label="按影片阶段筛选" value={ws.queueStage} onChange={(e) => ws.setQueueStage(e.target.value as StageFilter)}>
            {STAGE_OPTIONS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          {!completed && <select aria-label="按分辨率筛选" value={resolution} onChange={(e) => setResolution(e.target.value as Resolution)}>
            <option value="all">全部分辨率</option><option value="4K">4K</option><option value="1080p">1080p</option>
          </select>}
          <Chip>{completed ? `已完成 · ${paging?.total ?? ws.data.films.data?.counts.done ?? 0}` : `${films.length} 部`}</Chip>
        </div>
        {!completed && <div className="search-wrap">
          <Icon of={SearchIcon} />
          <input className="input search" type="search" placeholder="搜索影片、演员或编号" aria-label="搜索影片、演员或编号"
            value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>}
      </div>
      {completed && paging && (
        <div className="actions" style={{ flexWrap: "wrap", marginBottom: 16 }} aria-label="已完成分页">
          <button type="button" className="btn ghost" disabled={changingPage || paging.page <= 1}
            onClick={() => pickPage(paging.page - 1)}>上一页</button>
          <span>第 {paging.page} 页 / 共 {paging.pages} 页</span>
          <button type="button" className="btn ghost" disabled={changingPage || paging.page >= paging.pages}
            onClick={() => pickPage(paging.page + 1)}>下一页</button>
        </div>
      )}
      {completed && history.isError && <p role="status">暂时无法读取该页，保留上次结果。</p>}
      {films.length ? (
        <div className="queue-grid">{films.map((item) => <FilmCard key={item.id} item={item} />)}</div>
      ) : (
        <EmptyState title="没有符合条件的影片" description="试试更改阶段、分辨率或搜索词。" />
      )}
    </>
  );
}

function FilmCard({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  const title = itemTitle(item);
  const open = useMutation({
    mutationFn: () => {
      if (ws.readOnly) throw new Error("只读监控");
      return item.kind === "package" ? api.workflowOpenPackage(item.pkg.package_id) : api.workflowOpen(item.id, "inbox");
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const runner = ws.data.films.data?.intake;
  const intake = item.kind === "package" && runner && runner.package_id === item.pkg.package_id && runner.total > 0 ? runner : null;
  const intakePercent = intake ? Math.min(100, Math.floor((intake.bytes_completed / intake.total) * 100)) : 0;
  const meta = [itemNumber(item), itemResolution(item)].filter(Boolean).join(" · ");
  const timing = item.kind === "film" ? readFilmTiming(item.row.timing) : null;  // U1e
  const run = timing?.total_restore_to_translate ?? null;
  const energy = item.kind === "film" ? item.row.energy ?? null : null;  // PWR-2
  return (
    <article className="panel film-card" data-film={item.id}>
      <div className="top">
        <ItemCover item={item} size="card" />
        <div className="film-body">
          <div className="film-title"><h3>{title}</h3></div>
          {meta && <div className="meta"><span>{meta}</span></div>}
          {itemActors(item) && <div className="meta">{itemActors(item)}</div>}
          <div style={{ marginTop: 8 }} className="chips"><StatusChip status={item.status} /></div>
        </div>
      </div>
      <div>
        <StageBlock item={item} />
        {intake && (
          <>
            <div className="progress" role="progressbar" aria-label={`${title} 收件复制进度`} aria-valuemin={0}
              aria-valuemax={100} aria-valuenow={intakePercent}><span style={{ width: `${intakePercent}%` }} /></div>
            <div className="stage-detail"><span className="mono">{fmtBytes(intake.bytes_completed)} / {fmtBytes(intake.total)}</span></div>
          </>
        )}
      </div>
      <StepLine steps={item.kind === "film" ? item.row.steps : item.pkg.steps} title={title} durations={stepDurations(timing)} />
      <div className="card-foot">
        <span>
          {item.kind === "package" ? "INBOX" : isRemoteRow(item.row)
            ? machineOf(item.row) !== null && <span className="machine-name" data-testid="film-machine">{machineOf(item.row)}</span>
            : isRestoring(item.status) ? "本机" : "INBOX"}
          {run !== null && <span className="mono run-total" title="从首次开始修复到翻译完成"> · 全程 {fmtHoursMinutes(run)}</span>}
          {run !== null && energy && !energy.running && (
            <span className="pw-foot" title="能耗为估算"> · <strong>约 {fmtKwhShort(energy.total_wh)} 度</strong>{energy.partial ? "（部分）" : ""}</span>
          )}
        </span>
        {item.kind === "film" ? (
          <button type="button" className="btn ghost small" aria-label={`查看 ${title} 详情`} onClick={() => ws.openDetail(item.id)}>查看详情</button>
        ) : (
          !ws.readOnly && <button type="button" className="btn ghost small" disabled={open.isPending} onClick={() => open.mutate()}>打开文件夹</button>
        )}
      </div>
    </article>
  );
}
