import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../../api";
import { exceptionInfo, itemNumber, itemResolution, itemTitle, type WorkItem } from "./mediaModel";
import { EmptyState, Icon, PageTitle, StatusChip } from "./parts";
import { errorText, MEDIA_FILMS_KEY, mediaFilmKey, useIntakeReasons, useWorkspace } from "./workspace";

type Filter = "all" | "unknown" | "cleanup";

// 异常 (§4.4): films and packages that stopped at a recoverable point, each with its one action.
export function ExceptionsPage() {
  const ws = useWorkspace();
  const [filter, setFilter] = useState<Filter>("all");
  const items = ws.data.exceptions.filter((i) => (filter === "all" ? true : i.status === filter));
  return (
    <>
      <PageTitle title="异常" />
      <div className="toolbar">
        <div className="filters" role="group" aria-label="按异常类型筛选">
          {([["all", "所有异常"], ["unknown", "入库结果未知"], ["cleanup", "仅需清理来源"]] as Array<[Filter, string]>).map(([v, l]) => (
            <button key={v} type="button" className="filter-pill" aria-pressed={filter === v} onClick={() => setFilter(v)}>{l}</button>
          ))}
        </div>
      </div>
      {items.length ? items.map((item) => <ExceptionCard key={item.id} item={item} />) : (
        <EmptyState title="当前筛选没有异常" description="处理结果会保留在影片详情的事件记录中。" />
      )}
    </>
  );
}

function ExceptionCard({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const reasons = useIntakeReasons();
  const [title, desc, label, action] = exceptionInfo(item, reasons);
  const run = useMutation({
    mutationFn: async () => {
      if (ws.readOnly) throw new Error("只读监控");
      if (item.kind === "package") return api.workflowOpenPackage(item.pkg.package_id);
      const stage = action === "retry-scrape" ? "scrape" : item.row.retry_stage || item.row.stage || "";
      return api.workflowRetry(item.id, stage);
    },
    onSuccess: () => {
      if (item.kind === "film") {
        ws.toast("已重新排队；原始来源保留。");
        void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
        void qc.invalidateQueries({ queryKey: mediaFilmKey(item.id) });
      }
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const onAction = () => {
    if (ws.readOnly) return;
    if (action === "review") ws.openReview(item.id);
    else if (action === "reconcile" || action === "cleanup") ws.openExplain(action, item.id);
    else run.mutate();
  };
  const sub = [itemNumber(item), itemResolution(item)].filter(Boolean).join(" · ");
  return (
    <article className="panel exception-card" data-exception={item.id}>
      <div className="summary">
        <span className="exception-symbol"><Icon of={WarningAmberIcon} /></span>
        <div>
          <div className="film-title"><h3>{title}</h3><StatusChip status={item.status} /></div>
          <p><strong>{itemTitle(item)}</strong>{sub ? ` · ${sub}` : ""}</p>
          <p className="wrap">{desc}</p>
        </div>
      </div>
      <div className="actions">
        {!ws.readOnly && action !== "none" && <button type="button" className="btn small" disabled={run.isPending} onClick={onAction}>{label}</button>}
        {item.kind === "film" && (
          <button type="button" className="btn ghost small" onClick={() => ws.openDetail(item.id)}>查看详情</button>
        )}
      </div>
    </article>
  );
}
