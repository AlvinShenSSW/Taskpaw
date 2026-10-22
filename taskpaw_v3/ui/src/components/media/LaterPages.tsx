import FolderIcon from "@mui/icons-material/Folder";
import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { EmptyState, Icon, Notice, PageTitle } from "./parts";
import { useWorkspace } from "./workspace";

// 片库与规则 / Mac 信息桥 / 设置 / 迁移 (§4.6): stay in the rail marked 「后续」.
const LATER = "此功能将在多机工作流阶段提供";
const PURPOSE = {
  libraries: ["片库与规则", "NAS 库根与演员索引的映射向导。"],
  bridge: ["Mac 信息桥", "Mac 上只读的状态、事件与有界脱敏日志，不参与派单。"],
  settings: ["设置 / 迁移", "收件结构、设备角色、目录边界与迁移预检向导。"],
} as const;
export type LaterKey = keyof typeof PURPOSE;

const RULES: Array<[string, string]> = [
  ["输入与成品分开建清单", "原始收件影片、既有 NFO / srt 与处理成品拥有独立身份，不能把成品目录当作原片目录。"],
  ["先验证完整入库，再清理", "目标文件包须完整校验、生成持久回执，再检查原始输入身份；确认后才删除清单内来源文件。"],
  ["只重试未完成的清理", "来源被占用或机器离线时保留“已入库 · 待清理”；恢复后不重新修复、刮削或上传。"],
  ["正式片库永不成为清理根", "仅允许注册收件根内的清单文件；映射盘、别名、路径重叠与替换文件都必须重新校验。"],
];

export function LaterPage({ page }: { page: LaterKey }) {
  const ws = useWorkspace();
  const [title, purpose] = PURPOSE[page];
  return (
    <>
      <PageTitle title={title} />
      {page === "settings" && !ws.readOnly && <SettingsFacts />}
      <EmptyState title={LATER} description={purpose} />
      {page === "libraries" && (
        <section className="panel spacer-top" aria-labelledby="rules-title">
          <div className="panel-head"><h2 id="rules-title">入库与来源清理规则</h2><Icon of={VerifiedUserIcon} /></div>
          <div className="panel-body rule-list">
            {RULES.map(([h, p]) => <div key={h} className="rule"><h3>{h}</h3><p>{p}</p></div>)}
          </div>
        </section>
      )}
    </>
  );
}

// 设置: the INBOX notice and the read-only directories and tools of this controller.
function SettingsFacts() {
  const config = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const wf = (config.data?.workflow ?? {}) as Record<string, unknown>;
  const value = (key: string) => (typeof wf[key] === "string" && wf[key] ? String(wf[key]) : "未设置");
  const rows: Array<[string, string]> = [
    ["INBOX", value("inbox_root")], ["工作目录", value("data_dir")], ["Jasna", value("jasna_exe_path")],
    ["WhisperJAV", value("whisperjav_exe_path")], ["ffprobe", value("ffprobe_path")], ["Miko (MDCX)", value("mdcx_root")],
  ];
  return (
    <>
      <Notice icon={FolderIcon}>
        <strong>只投到主控的一个普通收件目录。</strong>INBOX 为目录别名（例如本机 TODO 文件夹），无需专用投递器或 ready 文件。主控自动检查并分配；各机 WORK 只是工作目录。
      </Notice>
      <section className="panel" aria-labelledby="paths-title" style={{ marginBottom: 20 }}>
        <div className="panel-head"><div><h2 id="paths-title">目录与工具</h2><p>只读 · 在 TaskPaw 设置中修改</p></div></div>
        <div className="panel-body">
          <dl className="facts">{rows.map(([k, v]) => [<dt key={`${k}-t`}>{k}</dt>, <dd key={`${k}-d`} className="mono">{v}</dd>])}</dl>
        </div>
      </section>
    </>
  );
}
