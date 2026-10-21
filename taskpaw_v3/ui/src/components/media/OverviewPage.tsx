import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import AutoAwesomeIcon from "@mui/icons-material/AutoAwesome";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import ComputerIcon from "@mui/icons-material/Computer";
import DesktopWindowsIcon from "@mui/icons-material/DesktopWindows";
import FactCheckIcon from "@mui/icons-material/FactCheck";
import PauseIcon from "@mui/icons-material/Pause";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import {
  fmtVram, gpuLabel, isRestoring, itemActors, itemNumber, itemResolution, itemTitle, matchesStage, outputLimit, REVIEW_STATES,
  type StageFilter,
  type WorkItem,
} from "./mediaModel";
import { Chip, Icon, ItemCover, Notice, PageTitle, StageBlock, StatusChip } from "./parts";
import { errorText, isPaused, MEDIA_FILMS_KEY, useWorkspace } from "./workspace";

// 概览 (§4.1 + U1 V3): heading actions, the paused notice, four stats, the flow strip, the
// active rows, the cleanup rule, the next films to confirm and the local GPU.
export function OverviewPage() {
  const ws = useWorkspace();
  const { data } = ws;
  const qc = useQueryClient();
  const paused = isPaused(data);
  const pause = useMutation({
    mutationFn: (next: boolean) => api.workflowPause(next),
    onSuccess: (_r, next) => {
      ws.toast(next ? "已暂停新的领取；所有在途任务继续。" : "已恢复新任务领取。");
      return qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  const items = data.items;
  const count = (pred: (i: WorkItem) => boolean) => items.filter(pred).length;
  const ltx = count((i) => isRestoring(i.status));
  const pending = data.pendingCount;
  const review = data.review.length;
  const waiting = data.films.data?.counts.waiting_for_you ?? pending + review;
  const gpu = data.films.data?.gpu;
  const flow: Array<[string, number, StageFilter | "pending", boolean]> = [
    ["收件", count((i) => i.status === "inbox" || i.status === "receiving"), "inbox", false],
    ["待处理", pending, "pending", true],
    ["修复 / 字幕", count((i) => matchesStage(i, "processing")), "processing", false],
    ["回传 / 刮削", count((i) => matchesStage(i, "preparing")), "preparing", false],
    ["待确认", review, "review", true],
    ["入库 / 清理", count((i) => ["archiving", "unknown", "cleanup"].includes(i.status)), "archive", false],
    ["完成", count((i) => i.status === "completed"), "completed", false],
  ];

  return (
    <>
      <PageTitle title="概览" actions={<>
        <button type="button" className="btn ghost" disabled={pause.isPending} onClick={() => pause.mutate(!paused)}>
          <Icon of={paused ? PlayArrowIcon : PauseIcon} />{paused ? "恢复新领取" : "暂停新领取"}
        </button>
        <button type="button" className="btn primary" onClick={() => ws.navigate("review")}>
          待确认 <span className="mono">{review}</span><Icon of={ArrowForwardIcon} />
        </button>
      </>} />
      {paused && (
        <Notice tone="amber" icon={PauseIcon}>
          <strong>主控已暂停新领取</strong><p>在途修复、翻译、回传与已确认入库继续运行。</p>
        </Notice>
      )}
      <div className="stats">
        <div className="panel stat">
          <div className="label">正在处理<Icon of={AutoAwesomeIcon} /></div>
          <strong>{data.active.length}<em>部影片</em></strong>
          <div className="note">{ltx} 部 LTX · 其余步骤分别计数</div>
        </div>
        <div className="panel stat">
          <div className="label">等待您的确认<Icon of={FactCheckIcon} /></div>
          <strong className="t-amber">{waiting}<em>部影片</em></strong>
          <div className="note">{pending} 部待安排 · {review} 部待入库</div>
        </div>
        <div className="panel stat">
          <div className="label">需要处理<Icon of={WarningAmberIcon} /></div>
          <strong>{data.exceptions.length}<em>项异常</em></strong>
          <div className="note">含入库未知、来源清理待重试</div>
        </div>
        <div className="panel stat">
          <div className="label">计算设备<Icon of={DesktopWindowsIcon} /></div>
          <strong>1<em>/ 1 在线</em></strong>
          <div className="note">{gpuLabel(gpu) || "本机 GPU"}</div>
        </div>
      </div>
      <div className="flow-strip" role="group" aria-label="各流程阶段影片数量">
        {flow.map(([name, n, key, attention]) => (
          <button key={name} type="button" className={`flow-stage${attention ? " attention" : ""}`}
            aria-label={`${name} ${n}`}
            onClick={() => (key === "pending" ? ws.navigate("pending") : key === "review" ? ws.navigate("review")
              : ws.navigate("queue", { stage: key }))}>
            <span>{name}</span><b>{n}</b>
          </button>
        ))}
      </div>
      <div className="two-col">
        <div className="stack">
          <section className="panel" aria-labelledby="ov-active">
            <div className="panel-head">
              <div><h2 id="ov-active">正在处理</h2><p>当前阶段与设备 · 不合成影片总体百分比</p></div>
              <button type="button" className="link-btn" onClick={() => ws.navigate("queue")}>全部队列<Icon of={ArrowForwardIcon} /></button>
            </div>
            <div className="film-list">
              {data.active.slice(0, 4).map((item) => <FilmRowView key={item.id} item={item} />)}
              {data.active.length === 0 && <div className="empty"><p>当前没有正在处理的影片</p></div>}
            </div>
            {data.active.length > 4 && (
              <div className="queue-summary"><span>另有 {data.active.length - 4} 部正在处理</span></div>
            )}
          </section>
          <section className="panel" aria-labelledby="ov-rule">
            <div className="panel-head">
              <div><h2 id="ov-rule">安全抵达之后，再清理来源</h2><p>正式片库永不作为清理目标</p></div>
              <Icon of={VerifiedUserIcon} />
            </div>
            <div className="panel-body">
              <div className="steps-list">
                {["确认目标与清单", "不覆盖提交", "完整校验与回执", "来源身份复核", "清理清单内原片"].map((s) =>
                  <span key={s} className="step">{s}</span>)}
              </div>
            </div>
          </section>
        </div>
        <div className="stack">
          <section className="panel" aria-labelledby="ov-next">
            <div className="panel-head">
              <div><h2 id="ov-next">下一步，需要您</h2><p>图文核对 · 一部一确认</p></div>
              <Chip tone="amber">{review} 待确认</Chip>
            </div>
            {data.review.slice(0, 2).map((item) => (
              <div key={item.id} className="review-peek">
                <div className="top">
                  <ItemCover item={item} size="peek" />
                  <div>
                    <h3>{itemTitle(item)}</h3>
                    <p>{[itemActors(item) || itemNumber(item), itemResolution(item)].filter(Boolean).join(" · ")}</p>
                    <StatusChip status={item.status} />
                  </div>
                </div>
                <div className="actions">
                  <button type="button" className={`btn small${item.status === "ready" ? " primary" : ""}`}
                    onClick={() => ws.openReview(item.id)}>核对入库<Icon of={ArrowForwardIcon} /></button>
                </div>
              </div>
            ))}
            {data.review.length === 0 && <div className="empty"><p>待确认已处理</p></div>}
          </section>
          <section className="panel" aria-labelledby="ov-devices">
            <div className="panel-head"><h2 id="ov-devices">设备协作</h2>
              <button type="button" className="link-btn" onClick={() => ws.navigate("local")}>查看<Icon of={ArrowForwardIcon} /></button>
            </div>
            <div className="device-mini">
              <div className="device-meta">
                <span className="device-icon"><Icon of={ComputerIcon} /></span>
                <div>
                  <div className="name">{gpu?.name ?? "本机 GPU"} {gpu && <span className="muted">{fmtVram(gpu.vram_mib)}</span>}</div>
                  <div className="desc">本机 + 主控{paused ? " · 新领取已暂停" : ""}</div>
                </div>
              </div>
              <Chip>{outputLimit(gpu)}</Chip>
            </div>
          </section>
        </div>
      </div>
    </>
  );
}

// One active film row of the overview and the 本机 page.
export function FilmRowView({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  const title = itemTitle(item);
  return (
    <article className="film-row">
      <ItemCover item={item} />
      <div className="film-body">
        <div className="film-title"><h3>{title}</h3>{itemResolution(item) && <Chip>{itemResolution(item)}</Chip>}</div>
        <div className="meta">
          {itemNumber(item) && <span className="mono">{itemNumber(item)}</span>}
          <span>{REVIEW_STATES.includes(item.status) ? "主控编排" : "本机"}</span>
        </div>
        <StageBlock item={item} />
      </div>
      {item.kind === "film" && (
        <button type="button" className="btn ghost icon-btn" aria-label={`查看 ${title} 详情`} onClick={() => ws.openDetail(item.id)}>
          <Icon of={ChevronRightIcon} />
        </button>
      )}
    </article>
  );
}
