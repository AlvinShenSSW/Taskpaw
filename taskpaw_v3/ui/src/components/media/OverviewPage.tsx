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
import { api, type ClusterPower } from "../../api";
import { Bolt, Est, SplitBar, SplitLegend } from "../power/PowerParts";
import { fmtKwhShort, fmtW, splitOf } from "../power/powerModel";
import {
  ACTIVE_STATES, fmtVram, gpuLabel, isRemoteRow, isRestoring, itemActors, itemNumber, itemResolution, itemTitle, machineOf, matchesStage,
  outputLimit, REVIEW_STATES,
  type StageFilter,
  type WorkItem,
} from "./mediaModel";
import { Chip, Icon, ItemCover, Notice, PageTitle, StageBlock, StatusChip } from "./parts";
import { LostRow } from "./RemoteParts";
import { errorText, isPaused, MEDIA_FILMS_KEY, useWorkspace } from "./workspace";

// 概览 (§4.1 + U1 V3): heading actions, the paused notice, four stats, the flow strip, the
// active rows, the cleanup rule, the next films to confirm and the local GPU.
export function OverviewPage() {
  const ws = useWorkspace();
  const { data } = ws;
  const qc = useQueryClient();
  const paused = isPaused(data);
  const pause = useMutation({
    mutationFn: (next: boolean) => {
      if (ws.readOnly) throw new Error("只读监控");
      return api.workflowPause(next);
    },
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
  const cluster = data.films.data?.power ?? null;  // PWR-2: this machine plus the 功耗 tab's peers
  // P2-4 R1/R3: remote mode's 设备失联 films and the films waiting for a machine, with the pass's reason
  const remote = items.some((i) => i.kind === "film" && isRemoteRow(i.row));
  const lost = items.filter((i) => i.status === "device_lost");
  const waitingRows = remote ? items.filter((i) => i.kind === "film" && i.status === "inbox") : [];
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
        {!ws.readOnly && <button type="button" className="btn ghost" disabled={pause.isPending} onClick={() => pause.mutate(!paused)}>
          <Icon of={paused ? PlayArrowIcon : PauseIcon} />{paused ? "恢复新领取" : "暂停新领取"}
        </button>}
        <button type="button" className="btn primary" onClick={() => ws.navigate("review")}>
          待确认 <span className="mono">{review}</span><Icon of={ArrowForwardIcon} />
        </button>
      </>} />
      {paused && (
        <Notice tone="amber" icon={PauseIcon}>
          <strong>主控已暂停新领取</strong><p>在途修复、翻译、回传与已确认入库继续运行。</p>
        </Notice>
      )}
      <div className={`stats${cluster ? " pw-stats" : ""}`}>
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
        {cluster && <ClusterStat power={cluster} onMore={ws.openPower} />}
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
          {lost.length > 0 && (
            <section className="panel lost-panel" aria-labelledby="ov-lost">
              <div className="panel-head">
                <div><h2 id="ov-lost">设备失联 · {lost.length}</h2><p>派单保留，不自动改派；后台持续重连</p></div>
                <Chip tone="red">{lost.length} 部</Chip>
              </div>
              <div className="film-list">{lost.map((item) => <LostRow key={item.id} item={item} />)}</div>
            </section>
          )}
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
          {waitingRows.length > 0 && (
            <section className="panel" aria-labelledby="ov-waiting">
              <div className="panel-head">
                <div><h2 id="ov-waiting">等待 · {waitingRows.length}</h2><p>等待派发或排队中的影片与原因</p></div>
                <button type="button" className="link-btn" onClick={() => ws.navigate("queue", { stage: "inbox" })}>全部<Icon of={ArrowForwardIcon} /></button>
              </div>
              <div className="film-list">
                {waitingRows.slice(0, 4).map((item) => <FilmRowView key={item.id} item={item} />)}
              </div>
              {waitingRows.length > 4 && (
                <div className="queue-summary"><span>另有 {waitingRows.length - 4} 部等待</span></div>
              )}
            </section>
          )}
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
                    onClick={() => ws.readOnly ? ws.openDetail(item.id) : ws.openReview(item.id)}>{ws.readOnly ? "查看详情" : "核对入库"}<Icon of={ArrowForwardIcon} /></button>
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

// 「集群功耗」 (PWR-2): live W of this machine and the 功耗 tab's peers, its GPU / CPU / 其他 split and
// today's energy; every figure is an estimate.
function ClusterStat({ power, onMore }: { power: ClusterPower; onMore?: () => void }) {
  const split = splitOf(power);
  return (
    <div className="panel stat pw-stat" data-testid="cluster-power">
      <div className="label">集群功耗 <Est /><Bolt /></div>
      <strong>{fmtW(power.w)}<em>W · {power.machines} 台计算设备</em></strong>
      <div className="pw-split">
        <SplitBar split={split} />
        <SplitLegend split={split} />
        <div className="note">
          <span>今日约 <span className="mono">{fmtKwhShort(power.today_wh)}</span> 度 · 5 秒刷新</span>
          {onMore && <button type="button" className="link-btn" onClick={onMore}>功耗详情<Icon of={ArrowForwardIcon} /></button>}
        </div>
      </div>
    </div>
  );
}

// One active film row of the overview and the 本机 page. PW12: live W only on the GPU-owning film;
// the other active rows show —.
export function FilmRowView({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  const title = itemTitle(item);
  const live = item.kind === "film" ? item.row.live_power ?? null : null;
  const active = item.kind === "film" && ACTIVE_STATES.includes(item.status);
  return (
    <article className="film-row">
      <ItemCover item={item} />
      <div className="film-body">
        <div className="film-title"><h3>{title}</h3>{itemResolution(item) && <Chip>{itemResolution(item)}</Chip>}</div>
        <div className="meta">
          {itemNumber(item) && <span className="mono">{itemNumber(item)}</span>}
          {item.kind === "film" && isRemoteRow(item.row) && !REVIEW_STATES.includes(item.status)
            ? <span className="machine-name" data-testid="film-machine">{machineOf(item.row)}</span>
            : <span>{REVIEW_STATES.includes(item.status) ? "主控编排" : "本机"}</span>}
          {active && (
            <span className="pw-chip" data-testid="row-power" title={live ? "本机整机功耗 · 估算" : "只有占用 GPU 的影片显示整机功耗"}>
              <Bolt />{live ? `${fmtW(live.w)} W` : "—"}
              <span className="sr-only">{live ? "（本机整机功耗估算）" : "（不占用 GPU，不显示整机功耗）"}</span>
            </span>
          )}
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
