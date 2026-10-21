import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import { useState } from "react";
import { itemActors, itemNumber, itemResolution, itemTitle, reviewLine } from "./mediaModel";
import { Chip, EmptyState, Icon, ItemCover, Notice, PageTitle, StatusChip } from "./parts";
import { useWorkspace } from "./workspace";

type Filter = "all" | "ready" | "blocked";

// 待确认 (§4.3): one card per film waiting for 确认入库, with its target and what blocks it.
export function ReviewPage() {
  const ws = useWorkspace();
  const [filter, setFilter] = useState<Filter>("all");
  const items = ws.data.review.filter((i) => (filter === "ready" ? i.status === "ready" : filter === "blocked" ? i.status !== "ready" : true));
  return (
    <>
      <PageTitle title="待确认" />
      <Notice icon={VerifiedUserIcon}>入库不会覆盖或删除已有片库。来源暂时无法清理时会保留，并显示“已入库 · 待清理”。</Notice>
      <div className="toolbar">
        <div className="filters" role="group" aria-label="按核对状态筛选">
          {([["all", "全部待确认"], ["ready", "可核对入库"], ["blocked", "需要先处理"]] as Array<[Filter, string]>).map(([v, l]) => (
            <button key={v} type="button" className="filter-pill" aria-pressed={filter === v} onClick={() => setFilter(v)}>{l}</button>
          ))}
        </div>
        <span className="small muted">{items.length} 部影片 · 每次单独确认</span>
      </div>
      {items.length ? (
        <div className="review-grid">
          {items.map((item) => {
            const pf = ws.data.preflights[item.id];
            const actor = itemActors(item);
            const candidates = pf?.candidates ?? null;
            const target = item.status === "ready" && candidates?.length === 1
              ? [candidates[0].source_label, candidates[0].name].filter(Boolean).join(" / ") : null;
            return (
              <article key={item.id} className="panel review-card" data-review={item.id}>
                <ItemCover item={item} size="review" />
                <div className="film-body">
                  <div className="film-title"><h3>{itemTitle(item)}</h3>{itemResolution(item) && <Chip>{itemResolution(item)}</Chip>}</div>
                  <p>{[itemNumber(item), actor].filter(Boolean).join(" · ")}</p>
                  <div className="chips"><StatusChip status={item.status} /></div>
                  <div className="keyline"><Icon of={FolderOpenIcon} /><span className="wrap">{target ?? "尚未选定目标"}</span></div>
                  <p>{reviewLine(item.status, null, candidates?.length ?? null)}</p>
                  <div className="actions">
                    <button type="button" className={`btn small${item.status === "ready" ? " primary" : ""}`}
                      onClick={() => ws.openReview(item.id)}>{item.status === "ready" ? "图文核对入库" : "查看并处理"}</button>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyState title="当前没有待确认影片" description="已确认的影片可在队列中查看入库回执与清理结果。" />
      )}
    </>
  );
}
