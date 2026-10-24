import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import SearchIcon from "@mui/icons-material/Search";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { Dialog } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, HttpError, type FilmDetail, type FilmRevisions, type Performer, type TargetErrorCode, type TargetPreflight,
} from "../../api";
import { archiveLive, packageNote, runningArchiveStep, type StatusKey } from "./mediaModel";
import { ArchiveLivePanel } from "./ArchiveLivePanel";
import { EvidenceFold } from "./FilmDrawer";
import { Chip, Cover, Icon, Notice } from "./parts";
import {
  errorText, finalRevision, MEDIA_FILMS_KEY, mediaFilmKey, mediaTargetKey, PREFLIGHT_MS, useWorkspace,
} from "./workspace";

const BACKDROP = { sx: { backgroundColor: "#030812BA", backdropFilter: "blur(4px)" } };
const TARGET_BLOCK: Record<TargetErrorCode, string> = {
  offline: "目标 NAS 暂时离线，请稍后重试。",
  permission: "目标暂不可写，请检查权限后重试。",
  missing: "演员记录已不存在，请重新选择。",
  duplicate: "目标已有同名影片包，不能覆盖；请打开文件夹核对。",
};

// `role`: "alertdialog" for a confirmation that cannot be undone (P2-4 判定已停止).
export function ModalShell({ open, onClose, titleId, children, role }: {
  open: boolean; onClose: () => void; titleId: string; children: ReactNode; role?: "dialog" | "alertdialog";
}) {
  return (
    <Dialog open={open} onClose={onClose} disableAutoFocus scroll="paper" slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: "mw mw-modal", sx: { backgroundImage: "none" }, "aria-labelledby": titleId, ...(role ? { role } : {}) } as object}>
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

const sameRevisions = (a: FilmRevisions, b: FilmRevisions) =>
  a.final?.revision === b.final?.revision && a.final?.hash === b.final?.hash
  && a.input?.revision === b.input?.revision && a.input?.hash === b.input?.hash;

function ReviewPane({ filmId, onClose }: { filmId: string; onClose: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: mediaFilmKey(filmId), queryFn: () => api.workflowFilm(filmId), refetchInterval: 5000 });
  const film = detail.data;
  const item = ws.data.items.find((i) => i.id === filmId);
  // F3: every target answer belongs to the final revision it checked; the detail's is the newest
  const final = finalRevision(film?.revisions ?? (item?.kind === "film" ? item.row.revisions : null));
  // the list's query for this film (F4): with one candidate it already checked that target
  // A target answer that is not a pass (error, offline, permission, duplicate, missing) is polled until
  // it recovers, as the list polls its own; a plain candidate list without a chosen performer is not.
  const target = useQuery({
    queryKey: mediaTargetKey(filmId, final), queryFn: () => api.workflowTarget(filmId), retry: false,
    refetchInterval: (q) => (q.state.status === "error" || q.state.data?.error_code
      || (q.state.data?.performer_id != null && !passes(q.state.data)) ? PREFLIGHT_MS : false),
  });
  const [seenRev, setSeenRev] = useState<FilmRevisions | undefined>(undefined);
  // The operator's own choice, bound to the final revision it was made against.
  const [manual, setManual] = useState<{ performer: Performer; rev: string | null } | null>(null);
  const [phase, setPhase] = useState<"review" | "submitted">("review");
  // The detail's update time when 「确认入库」 succeeded: only a later read may end the submitted phase.
  const [submittedAt, setSubmittedAt] = useState(0);
  const actor = film?.actors[0] ?? "";
  // Candidates: the preflight's (Filmpaw searched for every NFO actor); while Filmpaw is offline
  // (null) or the preflight failed, a manual search is offered instead.
  const search = useQuery({
    queryKey: ["mediaPerformers", actor], queryFn: () => api.performers(actor),
    enabled: target.isError && actor !== "", retry: false,
  });
  const candidates: Performer[] | null = useMemo(() => {
    if (target.data) return target.data.candidates?.items ?? [];
    return target.isError ? (actor ? search.data?.items ?? null : []) : null;
  }, [target.data, target.isError, actor, search.data]);
  useEffect(() => { if (film && seenRev === undefined) setSeenRev(film.revisions); }, [film, seenRev]);
  // A new final revision re-derives the choice from its own candidates: a manual choice stays only
  // while that performer is still a candidate (else it is dropped and the picker asks again).
  useEffect(() => {
    if (!manual || manual.rev === final || candidates === null) return;
    const same = candidates.find((p) => String(p.id) === String(manual.performer.id));
    setManual(same ? { performer: same, rev: final } : null);
  }, [manual, final, candidates]);
  // Exactly one candidate is selected automatically (also when it arrives after an empty or failed
  // first load); several are never pre-selected, and an operator's choice is never replaced.
  // The automatic choice is derived from the current candidates, so it always belongs to this revision.
  const selected: Performer | null | undefined = manual && manual.rev === final ? manual.performer
    : candidates === null ? undefined : candidates.length === 1 ? candidates[0] : null;
  // The chosen performer's check: the shared answer when the backend checked that very candidate,
  // else its own query (once the shared one has answered), keyed by the revision as well.
  const shared = Boolean(selected) && target.data?.performer_id != null
    && String(target.data.performer_id) === String(selected?.id);
  const own = useQuery({
    queryKey: [...mediaTargetKey(filmId, final), selected?.id ?? null],
    queryFn: () => api.workflowTarget(filmId, (selected as Performer).id),
    enabled: Boolean(selected) && !shared && (target.data !== undefined || target.isError), retry: false,
    refetchInterval: (q) => (passes(q.state.data) ? false : PREFLIGHT_MS),
  });
  const preflight = shared ? target : own;
  const choose = (p: Performer) => {
    // the same candidate again asks the NAS again
    if (selected && String(selected.id) === String(p.id)) void preflight.refetch();
    setManual({ performer: p, rev: final });
  };
  const confirm = useMutation({
    mutationFn: (p: Performer) => api.workflowConfirm(filmId, p.id),
    onSuccess: () => {
      setPhase("submitted");
      setSubmittedAt(detail.dataUpdatedAt);
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
    },
  });
  const open = useMutation({ mutationFn: () => api.workflowOpen(filmId, "output"), onError: (e) => ws.toast(errorText(e)) });
  // The phase follows the refreshed detail: an archive that then fails (and the film is
  // confirmable again) or ends unknown brings the review back with its reason.
  const archiveStatus = film?.archive.status ?? null;
  const reopened = phase === "submitted" && film !== undefined && detail.dataUpdatedAt > submittedAt
    && (archiveStatus === "unknown" || (archiveStatus === "failed" && film.can_confirm));
  useEffect(() => {
    if (!reopened) return;
    setPhase("review");
    confirm.reset();
  }, [reopened, confirm]);

  if (detail.isLoading || !film) {
    return (
      <>
        <ModalHead id="mw-review-title" title="确认影片与入库目标" subtitle="入库完成后自动清理本次收件来源" onClose={onClose} />
        <div className="modal-body">{detail.error ? <Notice tone="red">无法读取这部影片。</Notice> : <p className="muted">正在读取…</p>}</div>
      </>
    );
  }
  if (phase === "submitted" && !reopened) return <Receipt film={film} onClose={onClose} />;

  const status: StatusKey | undefined = archiveStatus === "unknown" ? "unknown" : item?.status;
  // the chosen performer's own answer only: never an older or another target's
  const errorCode = (selected ? preflight.data?.error_code : target.data?.error_code) ?? null;
  const changed = seenRev !== undefined && !sameRevisions(seenRev, film.revisions);
  const earlier =
    status === "unknown" ? "入库结果待核对，暂不能重新上传。"
      : !film.can_confirm ? "影片尚未完成准备，稍后自动继续。"
        : !film.has_poster || film.files.length === 0 ? "最终封面或文件包尚未准备好，请先处理。"
          : changed ? "成品或来源已更新，请重新查看后确认。" : "";
  const targetBlock = errorCode ? TARGET_BLOCK[errorCode]
    : !selected ? (candidates === null ? "正在检查目标…" : "请选择演员与目标路径。")
      : targetCheck(preflight.data, preflight.isError ? preflight.error : null);
  const blocked = earlier || targetBlock;
  // 重新检查: a target check that did not pass is asked again on demand (it is also polled)
  const rechecking = (selected ? preflight : target).isFetching;
  const recheck = !earlier && targetBlock && (selected || errorCode)
    ? () => void (selected ? preflight : target).refetch() : null;
  const title = film.title || film.name;
  // the backend's target already ends in the film folder (`film_dir`)
  const targetPath = selected ? preflight.data?.target_path ?? null : null;
  // only a real revision conflict (`cas_conflict`) says the film changed; other refusals keep their own message
  const conflict = confirm.error instanceof HttpError && confirm.error.status === 409 && confirm.error.code === "cas_conflict";

  return (
    <>
      <ModalHead id="mw-review-title" title="确认影片与入库目标" subtitle="入库完成后自动清理本次收件来源" onClose={onClose} />
      <div className="modal-body">
        <div className="confirmation-layout">
          <Cover filmId={film.film_id} hasPoster={film.has_poster} source={film.poster_source}
            hasCover={film.has_cover} coverSource={film.cover_source} size="confirm" label={title} />
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
        <Candidates candidates={candidates} selected={selected ?? null} actor={actor} onSelect={choose} />
        {archiveStatus === "failed" && film.can_confirm && (
          <Notice tone="red">上次入库未完成：{film.archive.error || "原因未知"}。可重新确认入库。</Notice>
        )}
        {blocked && <div className="spacer-top"><Notice tone="amber" icon={WarningAmberIcon}>{blocked}</Notice></div>}
        {recheck && (
          <div className="spacer-top">
            <button type="button" className="btn ghost small" disabled={rechecking} onClick={recheck}>重新检查</button>
          </div>
        )}
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
          <summary className="link-btn">入库后清理：主控 INBOX / {film.name} · {film.inputs.length} 个来源文件</summary>
          <ul className="package-list">{film.inputs.map((f) => <li key={f.path} className="wrap">{f.path}</li>)}</ul>
          <p>只处理本次收件来源；若文件发生变化或仍被占用，将保留并显示待清理。</p>
        </details>
        <EvidenceFold film={film} />
      </div>
      <div className="modal-foot">
        <p>{blocked ? "处理后重新确认即可。" : "一次确认即可，剩余步骤由后台完成。"}</p>
        <div className="actions">
          <button type="button" className="btn ghost" disabled={open.isPending} onClick={() => open.mutate()}>打开文件夹</button>
          {changed && <button type="button" className="btn ghost" onClick={() => { setSeenRev(film.revisions); confirm.reset(); }}>重新查看</button>}
          <button type="button" className="btn primary" disabled={Boolean(blocked) || confirm.isPending}
            onClick={() => { if (selected && !blocked) confirm.mutate(selected); }}>确认入库</button>
        </div>
      </div>
    </>
  );
}

// A passed target check: no error, with the target folder and that folder known to be absent.
const passes = (d: TargetPreflight | undefined) => Boolean(d && !d.error_code && d.target_path && d.absent === true);

// 「确认入库」 needs a passed preflight of the chosen performer: its answer, with the target
// folder and that folder known to be absent. Loading or an HTTP error is no pass.
function targetCheck(data: TargetPreflight | undefined, error: unknown): string {
  if (error) return `目标检查失败：${errorText(error)}`;
  if (!data) return "正在检查目标…";
  if (!data.target_path || data.absent !== true) return "目标检查未通过，请重新选择或稍后重试。";
  return "";
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
  const archive = runningArchiveStep(film);
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
        ) : archive && archiveLive(archive) ? <ArchiveLivePanel step={archive} /> : (
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
