import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import SearchIcon from "@mui/icons-material/Search";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { Dialog } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, HttpError, type FilmDetail, type FilmRevisions, type Performer } from "../../api";
import { packageNote, type StatusKey } from "./mediaModel";
import { EvidenceFold } from "./FilmDrawer";
import { Chip, Cover, Icon, Notice } from "./parts";
import { errorText, MEDIA_FILMS_KEY, mediaFilmKey, mediaTargetKey, useWorkspace } from "./workspace";

const BACKDROP = { sx: { backgroundColor: "#030812BA", backdropFilter: "blur(4px)" } };
const TARGET_BLOCK: Record<string, string> = {
  offline: "目标 NAS 暂时离线，请稍后重试。",
  permission: "目标暂不可写，请检查权限后重试。",
  duplicate: "目标已有同名影片包，不能覆盖；请打开文件夹核对。",
};

export function ModalShell({ open, onClose, titleId, children }: {
  open: boolean; onClose: () => void; titleId: string; children: ReactNode;
}) {
  return (
    <Dialog open={open} onClose={onClose} disableAutoFocus scroll="paper" slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: "mw mw-modal", sx: { backgroundImage: "none" }, "aria-labelledby": titleId } as object}>
      {children}
    </Dialog>
  );
}

export function ModalHead({ id, title, subtitle, onClose }: { id: string; title: string; subtitle: string; onClose: () => void }) {
  const ref = useRef<HTMLHeadingElement>(null);
  useEffect(() => { ref.current?.focus({ preventScroll: true }); }, [title]);
  return (
    <div className="modal-head">
      <div><h2 id={id} tabIndex={-1} ref={ref}>{title}</h2><p>{subtitle}</p></div>
      <button type="button" className="btn ghost icon-btn" aria-label="关闭对话框" onClick={onClose}><Icon of={CloseIcon} /></button>
    </div>
  );
}

// 图文确认 (§6, operator decision 1): exactly one candidate is pre-selected, several are never
// pre-selected, none asks for a manual search. One 「确认入库」, no checkbox.
export function ReviewDialog({ filmId, onClose }: { filmId: string | null; onClose: () => void }) {
  return (
    <ModalShell open={filmId !== null} onClose={onClose} titleId="mw-review-title">
      {filmId !== null && <ReviewPane key={filmId} filmId={filmId} onClose={onClose} />}
    </ModalShell>
  );
}

const sameRevisions = (a: FilmRevisions | null | undefined, b: FilmRevisions | null | undefined) =>
  (a?.final ?? null) === (b?.final ?? null) && (a?.input ?? null) === (b?.input ?? null)
  && (a?.final_hash ?? null) === (b?.final_hash ?? null) && (a?.input_hash ?? null) === (b?.input_hash ?? null);

function ReviewPane({ filmId, onClose }: { filmId: string; onClose: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: mediaFilmKey(filmId), queryFn: () => api.workflowFilm(filmId), refetchInterval: 5000 });
  const target = useQuery({ queryKey: mediaTargetKey(filmId), queryFn: () => api.workflowTarget(filmId), retry: false });
  const film = detail.data;
  const [seenRev, setSeenRev] = useState<FilmRevisions | null | undefined>(undefined);
  const [selected, setSelected] = useState<Performer | null | undefined>(undefined);
  const [phase, setPhase] = useState<"review" | "submitted">("review");
  const item = ws.data.items.find((i) => i.id === filmId);
  const actor = film?.actors[0] ?? "";
  // Candidates: the preflight's, else a performer search for the NFO actor (older backend).
  const search = useQuery({
    queryKey: ["mediaPerformers", actor], queryFn: () => api.performers(actor),
    enabled: target.isError && actor !== "", retry: false,
  });
  const candidates: Performer[] | null = useMemo(
    () => target.data?.candidates ?? (target.isError ? (actor ? search.data?.items ?? null : []) : null),
    [target.data, target.isError, actor, search.data],
  );
  useEffect(() => { if (film && seenRev === undefined) setSeenRev(film.revisions ?? null); }, [film, seenRev]);
  useEffect(() => {
    if (selected === undefined && candidates !== null) setSelected(candidates.length === 1 ? candidates[0] : null);
  }, [candidates, selected]);
  const preflight = useQuery({
    queryKey: [...mediaTargetKey(filmId), selected?.id ?? null],
    queryFn: () => api.workflowTarget(filmId, (selected as Performer).id),
    enabled: Boolean(selected), retry: false,
  });
  const confirm = useMutation({
    mutationFn: (p: Performer) => api.workflowConfirm(filmId, p.id),
    onSuccess: () => {
      setPhase("submitted");
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
    },
  });
  const open = useMutation({ mutationFn: () => api.workflowOpen(filmId, "output"), onError: (e) => ws.toast(errorText(e)) });

  if (detail.isLoading || !film) {
    return (
      <>
        <ModalHead id="mw-review-title" title="确认影片与入库目标" subtitle="入库完成后自动清理本次收件来源" onClose={onClose} />
        <div className="modal-body">{detail.error ? <Notice tone="red">无法读取这部影片。</Notice> : <p className="muted">正在读取…</p>}</div>
      </>
    );
  }
  if (phase === "submitted") return <Receipt film={film} onClose={onClose} />;

  const status: StatusKey | undefined = item?.status;
  const errorCode = preflight.data?.error_code ?? target.data?.error_code ?? null;
  const changed = seenRev !== undefined && !sameRevisions(seenRev, film.revisions);
  const blocked =
    status === "unknown" ? "入库结果待核对，暂不能重新上传。"
      : !film.can_confirm ? "影片尚未完成准备，稍后自动继续。"
        : !film.has_poster || film.files.length === 0 ? "最终封面或文件包尚未准备好，请先处理。"
          : changed ? "成品或来源已更新，请重新查看后确认。"
            : errorCode ? TARGET_BLOCK[errorCode]
              : !selected ? "请选择演员与目标路径。" : "";
  const title = film.title || film.name;
  const filmDir = film.name;
  const targetPath = selected && preflight.data?.target_path ? `${preflight.data.target_path}\\${filmDir}` : null;
  const conflict = confirm.error instanceof HttpError && confirm.error.status === 409;

  return (
    <>
      <ModalHead id="mw-review-title" title="确认影片与入库目标" subtitle="入库完成后自动清理本次收件来源" onClose={onClose} />
      <div className="modal-body">
        <div className="confirmation-layout">
          <Cover filmId={film.film_id} hasPoster={film.has_poster} source={film.poster_source} size="confirm" label={title} />
          <div>
            <div className="eyebrow-id">{film.number || film.name} · MIKO 最终封面</div>
            <h3>{title}</h3>
            <div className="chips">
              {film.resolution && <Chip>{film.resolution}</Chip>}
              <Chip tone="green">本次后处理已完成</Chip>
            </div>
            <p>演员：<strong>{selected?.name ?? "待选择"}</strong></p>
            <p>{film.marks.chinese_subtitles ? "修复成品 · 中文字幕已核对" : "修复成品 · 无语音，不加中文字幕标识"}</p>
          </div>
        </div>
        <Candidates candidates={candidates} selected={selected ?? null} actor={actor} onSelect={setSelected} />
        {blocked && <div className="spacer-top"><Notice tone="amber" icon={WarningAmberIcon}>{blocked}</Notice></div>}
        {confirm.error && <Notice tone="red">{conflict ? "成品或来源已更新，请重新查看后确认。" : errorText(confirm.error)}</Notice>}
        <div className="target-box">
          <h3><Icon of={FolderOpenIcon} />入库到</h3>
          <div className="target-path">{targetPath ?? "尚未选择完整目标"}</div>
          <p className="small muted spacer-top">保留所有已有片源；同名冲突不会覆盖。</p>
        </div>
        <details open>
          <summary className="link-btn">最终文件包 · {film.files.length} 个文件</summary>
          <ul className="package-list">
            {film.files.map((f) => <li key={`${f.role}:${f.path}`}><span className="wrap">{f.path}</span><span>{packageNote(f.role, f.path)}</span></li>)}
          </ul>
        </details>
        <details className="source-box">
          <summary className="link-btn">入库后清理：主控 INBOX / {film.package || film.name} · {film.inputs.length} 个来源文件</summary>
          <ul className="package-list">{film.inputs.map((f) => <li key={f.path} className="wrap">{f.path}</li>)}</ul>
          <p>只处理本次收件来源；若文件发生变化或仍被占用，将保留并显示待清理。</p>
        </details>
        <EvidenceFold film={film} />
      </div>
      <div className="modal-foot">
        <p>{blocked ? "处理后重新确认即可。" : "一次确认即可，剩余步骤由后台完成。"}</p>
        <div className="actions">
          <button type="button" className="btn ghost" disabled={open.isPending} onClick={() => open.mutate()}>打开文件夹</button>
          {changed && <button type="button" className="btn ghost" onClick={() => { setSeenRev(film.revisions ?? null); confirm.reset(); }}>重新查看</button>}
          <button type="button" className="btn primary" disabled={Boolean(blocked) || confirm.isPending}
            onClick={() => { if (selected && !blocked) confirm.mutate(selected); }}>确认入库</button>
        </div>
      </div>
    </>
  );
}

function Candidates({ candidates, selected, actor, onSelect }: {
  candidates: Performer[] | null; selected: Performer | null; actor: string; onSelect: (p: Performer) => void;
}) {
  const [text, setText] = useState(actor);
  const [query, setQuery] = useState("");
  useEffect(() => { const h = setTimeout(() => setQuery(text.trim()), 300); return () => clearTimeout(h); }, [text]);
  const manual = candidates !== null && candidates.length === 0;
  const found = useQuery({
    queryKey: ["mediaPerformers", query], queryFn: () => api.performers(query), enabled: manual && query !== "", retry: false,
  });
  if (candidates === null) return <p className="small muted">正在查找演员与目标…</p>;
  if (candidates.length === 1) return null;
  const list = manual ? found.data?.items ?? [] : candidates;
  return (
    <>
      <h3>选择演员与目标</h3>
      {manual && (
        <div className="search-wrap" style={{ marginTop: 12 }}>
          <Icon of={SearchIcon} />
          <input className="input search" style={{ width: "100%" }} type="search" value={text}
            placeholder="搜索已有演员" aria-label="搜索已有演员" onChange={(e) => setText(e.target.value)} />
        </div>
      )}
      {manual && found.data && list.length === 0 && <p className="small muted spacer-top">没有匹配的演员。不会自动新建演员目录。</p>}
      {manual && found.error && <p className="small t-red spacer-top">演员搜索失败：{errorText(found.error)}</p>}
      <div className="candidate-list" role="group" aria-label="演员与目标候选">
        {list.map((p) => (
          <button key={p.id} type="button" className="candidate" aria-pressed={selected?.id === p.id} onClick={() => onSelect(p)}>
            <strong>{p.name}</strong>
            <span>{[p.source_label, p.unc_path].filter(Boolean).join(" / ")}</span>
            {p.aliases.length > 0 && <span>别名：{p.aliases.join("、")}</span>}
          </button>
        ))}
      </div>
    </>
  );
}

// After 「确认入库」: 「已确认，等待归档」 until the receipt, then 入库完成 or 已入库 · 待清理.
function Receipt({ film, onClose }: { film: FilmDetail; onClose: () => void }) {
  const ws = useWorkspace();
  const archived = film.archive.status === "succeeded" || Boolean(film.receipt_id);
  const cleaned = film.cleanup.input === "succeeded";
  const pendingCleanup = archived && !cleaned && film.cleanup.input !== null && film.cleanup.input !== "pending" && film.cleanup.input !== "running";
  const title = !archived ? "已确认，等待归档" : cleaned ? "入库完成" : pendingCleanup ? "已入库 · 待清理" : "入库完成";
  const next = ws.data.review.find((i) => i.status === "ready" && i.id !== film.film_id);
  return (
    <>
      <ModalHead id="mw-review-title" title={title} subtitle={film.title || film.name} onClose={onClose} />
      <div className="modal-body">
        {archived ? (
          <div className="receipt">
            <h3><Icon of={CheckIcon} /> 成品已入库</h3>
            <p>{film.archive.target_path ?? ""}</p>
            {film.receipt_id && <p className="mono">回执 {film.receipt_id}</p>}
          </div>
        ) : (
          <Notice>后台正在归档并校验；完成后显示入库回执。</Notice>
        )}
        {archived && (
          <p className="small">{cleaned ? "本次收件来源已清理，已有 NAS 片库保持不变。"
            : pendingCleanup ? "来源已保留；后台仅继续处理清理，不重复上传。" : "后台正在检查并清理本次收件来源。"}</p>
        )}
      </div>
      <div className="modal-foot">
        <p>确认绑定本次影片、目标与文件版本。</p>
        <div className="actions">
          <button type="button" className="btn ghost" onClick={() => ws.openDetail(film.film_id)}>查看影片记录</button>
          <button type="button" className="btn primary" onClick={() => (next ? ws.openReview(next.id) : onClose())}>继续下一部</button>
        </div>
      </div>
    </>
  );
}
