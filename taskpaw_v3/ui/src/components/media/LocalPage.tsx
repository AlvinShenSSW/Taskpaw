import ArrowForwardIcon from "@mui/icons-material/ArrowForward";
import ComputerIcon from "@mui/icons-material/Computer";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { fmtVram, gpuLabel, outputLimit } from "./mediaModel";
import { FilmRowView } from "./OverviewPage";
import { Chip, Icon, Notice, PageTitle } from "./parts";
import { isPaused, useWorkspace } from "./workspace";

// 本机 (§4.5, reduced for phase 1): the controller is also the only GPU agent. 排空 and
// 停止本机任务 need dispatcher stop semantics and come later.
export function LocalPage() {
  const ws = useWorkspace();
  const status = useQuery({ queryKey: ["agentStatus"], queryFn: api.agentStatus, refetchInterval: 5000, enabled: !ws.readOnly });
  const gpu = ws.data.films.data?.gpu;
  const vram = gpu ? fmtVram(gpu.vram_mib) : null;
  const limit4k = gpu?.max_output === "4K";
  const current = ws.data.films.data?.current;
  const running = current ? ws.data.items.find((i) => i.id === current.film_id) : undefined;
  const machine = ws.readOnly ? ws.server?.snapshot?.machine ?? ws.server?.name : status.data?.machine ?? "本机";
  return (
    <>
      <PageTitle title="本机" />
      <Notice icon={ComputerIcon}>本机兼任主控与 GPU Agent，不需要另一套窗口或第二份设备卡。</Notice>
      <div className="two-col">
        <div className="stack">
          <article className="panel device-card">
            <div className="device-top">
              <div className="device-name">
                <span className="device-icon"><Icon of={ComputerIcon} /></span>
                <div><h3>{machine}</h3><p>Windows · {gpuLabel(gpu) || "GPU 未知"}</p></div>
              </div>
              <Chip tone={isPaused(ws.data) ? "amber" : "green"}>{isPaused(ws.data) ? "新领取已暂停" : "在线"}</Chip>
            </div>
            <div className="metric-grid">
              <div><label>显存容量</label><strong>{vram || "—"}</strong></div>
              <div><label>任务资格</label><strong className="text-value">{gpu ? outputLimit(gpu) : "—"}</strong></div>
            </div>
          </article>
          <section className="panel" aria-labelledby="local-task">
            <div className="panel-head"><h2 id="local-task">本机任务</h2><Chip>只显示本机</Chip></div>
            {running ? <FilmRowView item={running} /> : (
              <div className="empty">
                <h2>本机当前空闲</h2>
                {vram && !limit4k && <p>全局队列中的 4K 影片本阶段不会派给这台 {vram} 设备。</p>}
              </div>
            )}
          </section>
        </div>
        <section className="panel" aria-labelledby="local-scope">
          <div className="panel-head"><h2 id="local-scope">两个控制范围</h2></div>
          <div className="panel-body">
            <h3>主控 · 暂停新领取</h3>
            <p className="small muted spacer-top">作用于全局调度，只阻止新的领取；不会杀死在途任务。</p>
            <div className="divider" />
            <h3>本机 · 排空 / 停止任务 <Chip>后续</Chip></h3>
            <p className="small muted spacer-top">排空只阻止本机新领取，在途任务继续。停止则终止本机的任务并暂停领取，保留来源。</p>
            <div className="divider" />
            <button type="button" className="link-btn" onClick={() => ws.navigate("overview")}>回到概览<Icon of={ArrowForwardIcon} /></button>
          </div>
        </section>
      </div>
    </>
  );
}
