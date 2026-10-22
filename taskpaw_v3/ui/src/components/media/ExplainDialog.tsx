import VerifiedUserIcon from "@mui/icons-material/VerifiedUser";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api";
import { Notice } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { mediaFilmKey } from "./workspace";

export type Explain = { kind: "reconcile" | "cleanup"; filmId: string } | null;

// 入库结果未知 and 已入库 · 待清理 (§4.4): the backend reconciles and retries by itself, so these
// dialogs explain what it does and what it never does; they have no action of their own.
export function ExplainDialog({ explain, onClose }: { explain: Explain; onClose: () => void }) {
  return (
    <ModalShell open={explain !== null} onClose={onClose} titleId="mw-explain-title">
      {explain && <ExplainPane key={`${explain.kind}:${explain.filmId}`} explain={explain} onClose={onClose} />}
    </ModalShell>
  );
}

function ExplainPane({ explain, onClose }: { explain: NonNullable<Explain>; onClose: () => void }) {
  const detail = useQuery({ queryKey: mediaFilmKey(explain.filmId), queryFn: () => api.workflowFilm(explain.filmId) });
  const film = detail.data;
  const title = film ? film.title || film.name : explain.filmId;
  const target = film?.archive.target_path || "尚未确定目标";
  if (explain.kind === "reconcile") {
    return (
      <>
        <ModalHead id="mw-explain-title" title="先核对入库结果" subtitle="结果未知期间：禁止重复上传，禁止来源清理" onClose={onClose} />
        <div className="modal-body">
          <Notice tone="amber" icon={WarningAmberIcon}>{title} 的目标提交后未收到最终回执。后台会先核对目标与记录，再决定下一步。</Notice>
          <dl className="facts">
            <dt>目标</dt><dd>{target}</dd>
            <dt>本地记录</dt><dd>提交意图存在，最终回执未收到</dd>
            <dt>必须核对</dt><dd>目标影片包、文件身份、完整校验与入库事务一致</dd>
            <dt>不允许</dt><dd>盲目重传、覆盖同名包、先删来源</dd>
          </dl>
        </div>
        <div className="modal-foot">
          <p>核对由后台自动完成，结果会显示在影片详情中。</p>
          <div className="actions"><button type="button" className="btn ghost" onClick={onClose}>关闭</button></div>
        </div>
      </>
    );
  }
  const kept = film?.cleanup.kept ?? [];
  return (
    <>
      <ModalHead id="mw-explain-title" title="仅重试来源清理" subtitle="不重新修复、不重新刮削、不重复上传" onClose={onClose} />
      <div className="modal-body">
        <div className="receipt">
          <h3>成品包已验证入库</h3>
          <p>{film?.receipt_id ? `回执 ${film.receipt_id} · ` : ""}{target}</p>
        </div>
        <dl className="facts">
          <dt>允许清理</dt><dd>主控 INBOX / {film?.name || explain.filmId} 内 {film?.inputs.length ?? 0} 个原始文件</dd>
          <dt>未能清理</dt><dd>{kept.length ? kept.map((k) => k.path).join("、") : "—"}</dd>
          <dt>重试检查</dt><dd>当前回执、收件根授权、输入身份与内容、唯一清理持有者</dd>
          <dt>若文件变了</dt><dd>立即保留来源并转人工核对</dd>
        </dl>
        <div className="spacer-top">
          <Notice tone="amber" icon={VerifiedUserIcon}>不会遍历删除收件目录，也不会把 NAS 正式片库当作清理根。</Notice>
        </div>
      </div>
      <div className="modal-foot">
        <p>后台会自动重试清理；无需重复上传。</p>
        <div className="actions"><button type="button" className="btn ghost" onClick={onClose}>关闭</button></div>
      </div>
    </>
  );
}
