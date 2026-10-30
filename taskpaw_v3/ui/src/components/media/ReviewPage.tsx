import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { itemActors, itemNumber, itemResolution, itemTitle, reviewLine, type StatusKey } from "./mediaModel";
import { useLibraries } from "./libraryModel";
import { Chip, EmptyState, Icon, ItemCover, Notice, PageTitle, StatusChip } from "./parts";
import { useWorkspace } from "./workspace";

type Filter = "all" | "ready" | "blocked";

// 待确认 (§4.3): one card per film waiting for 确认入库, with its target and what blocks it.
export function ReviewPage() {
  const ws = useWorkspace();
  const [filter, setFilter] = useState<Filter>("all");
  const items = ws.data.review.filter((i) => (filter === "ready" ? i.status === "ready" : filter === "blocked" ? i.status !== "ready" : true));
  // first run (#361 §11): without any library nothing can be recommended
  const libraries = useLibraries(!ws.readOnly);
  return (
    <>
      <PageTitle title="待确认" />
      {libraries.data?.items.length === 0 && (
        <Notice tone="amber" icon={WarningAmberIcon}>还没有片库，暂时无法推荐入库位置。
          <button type="button" className="link-btn" onClick={() => ws.navigate("libraries")}>去设置片库</button>
        </Notice>
      )}
      <Notice icon={VerifiedUserIcon}>入库不会覆盖或删除已有片库。来源暂时无法清理时会保留，并显示“已入库 · 待清理”。</Notice>
      <div className="toolbar">
        {!ws.readOnly && <div className="filters" role="group" aria-label="按核对状态筛选">
          {([["all", "全部待确认"], ["ready", "可核对入库"], ["blocked", "需要先处理"]] as Array<[Filter, string]>).map(([v, l]) => (
            <button key={v} type="button" className="filter-pill" aria-pressed={filter === v} onClick={() => setFilter(v)}>{l}</button>
          ))}
        </div>}
        <span className="small muted">{items.length} 部影片{!ws.readOnly && " · 每次单独确认"}</span>
      </div>
      {items.length ? (
        <div className="review-grid">
          {items.map((item) => {
            const pf = ws.data.preflights[item.id];
            const actor = itemActors(item);
            const candidates = pf?.candidates?.items ?? null;
            // the rules' preselection, which the preflight checked: an actor, or a category directory
            const chosen = candidates?.find((p) => pf?.performer_id != null && String(p.id) === String(pf.performer_id));
            const category = pf?.categories?.find((c) => c.id === pf.category_id);
            const target = item.status !== "ready" ? null
              : chosen ? [chosen.source_label, chosen.name].filter(Boolean).join(" / ") : category ? `类型目录 / ${category.label}` : null;
            return (
              <article key={item.id} className="panel review-card" data-review={item.id}>
                <ItemCover item={item} size="review" />
                <div className="film-body">
                  <div className="film-title"><h3>{itemTitle(item)}</h3>{itemResolution(item) && <Chip>{itemResolution(item)}</Chip>}</div>
                  <p>{[itemNumber(item), actor].filter(Boolean).join(" · ")}</p>
                  <div className="chips"><StatusChip status={item.status} /></div>
                  {!ws.readOnly && <>
                    <div className="keyline"><Icon of={FolderOpenIcon} /><span className="wrap">{target ?? "尚未选定目标"}</span></div>
                    <ReviewLine filmId={item.id} status={item.status} candidates={candidates?.length ?? null} />
                  </>}
                  <div className="actions">
                    <button type="button" className={`btn small${item.status === "ready" ? " primary" : ""}`}
                      onClick={() => ws.readOnly ? ws.openDetail(item.id) : ws.openReview(item.id)}>{ws.readOnly ? "查看详情" : item.status === "ready" ? "图文核对入库" : "查看并处理"}</button>
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

// 「最终成品包 {n} 个文件 · Miko 已完成」 needs the final package, which only the detail has.
function ReviewLine({ filmId, status, candidates }: { filmId: string; status: StatusKey; candidates: number | null }) {
  const ws = useWorkspace();
  const detail = useQuery({
    queryKey: ws.filmKey(filmId), queryFn: () => ws.readFilm(filmId), enabled: status === "ready", staleTime: 30000,
  });
  return <p>{reviewLine(status, detail.data?.files.length ?? null, candidates)}</p>;
}
