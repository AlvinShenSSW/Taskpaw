import AddIcon from "@mui/icons-material/Add";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import FolderOpenIcon from "@mui/icons-material/FolderOpen";
import PanToolAltOutlinedIcon from "@mui/icons-material/PanToolAltOutlined";
import SearchIcon from "@mui/icons-material/Search";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { Dialog } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  api, HttpError, type FilmDetail, type FilmRevisions, type Performer, type TargetCategory, type TargetErrorCode,
  type TargetPreflight,
} from "../../api";
import { archiveLive, packageNote, runningArchiveStep, type StatusKey } from "./mediaModel";
import { inTauri, pickPath } from "../nativePicker";
import { ArchiveLivePanel } from "./ArchiveLivePanel";
import { EvidenceFold } from "./FilmDrawer";
import { ActorThumb } from "./LibraryParts";
import { actorLibraries, categories as categoryLibraries, letters, offline, useLibraries } from "./libraryModel";
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
  actor_exists: "片库里已有同名演员目录，请直接选择该演员，或换一个名字。",
};

// `role`: "alertdialog" for a confirmation that cannot be undone (P2-4 判定已停止);
// `narrow`: the 560px form dialogs of 片库与规则 (#361).
export function ModalShell({ open, onClose, titleId, children, role, narrow }: {
  open: boolean; onClose: () => void; titleId: string; children: ReactNode; role?: "dialog" | "alertdialog"; narrow?: boolean;
}) {
  return (
    <Dialog open={open} onClose={onClose} disableAutoFocus scroll="paper" slotProps={{ backdrop: BACKDROP }}
      PaperProps={{ className: `mw mw-modal${narrow ? " narrow" : ""}`, sx: { backgroundImage: "none" }, "aria-labelledby": titleId, ...(role ? { role } : {}) } as object}>
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

// 图文确认 (§6, operator decision 1): the rules' preselection (#361 §9.6: one actor, or a category by
// the number's prefix) is taken and nothing else; several actors ask, none offers a search, a new actor
// directory or a category. One 「确认入库」, no checkbox.
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

// The primary target (§9.1): an actor (bound to the final revision it was chosen against), a category
// directory, a new actor directory (its form below the candidates) or a manually picked directory.
type Choice =
  | { kind: "performer"; performer: Performer; rev: string | null }
  | { kind: "category"; id: string }
  | { kind: "new_actor" }
  | { kind: "manual"; dir: string };
type Category = Pick<TargetCategory, "id" | "label" | "path" | "prefixes">;

function ReviewPane({ filmId, onClose }: { filmId: string; onClose: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: mediaFilmKey(filmId), queryFn: () => api.workflowFilm(filmId), refetchInterval: 5000 });
  const film = detail.data;
  const item = ws.data.items.find((i) => i.id === filmId);
  // F3: every target answer belongs to the final revision it checked; the detail's is the newest
  const final = finalRevision(film?.revisions ?? (item?.kind === "film" ? item.row.revisions : null));
  // the list's query for this film (F4): with a preselected target it already checked that target.
  // A target answer that is not a pass (error, offline, permission, duplicate, missing) is polled until
  // it recovers, as the list polls its own; a plain candidate list without a chosen target is not.
  const target = useQuery({
    queryKey: mediaTargetKey(filmId, final), queryFn: () => api.workflowTarget(filmId), retry: false,
    refetchInterval: (q) => (q.state.status === "error" || q.state.data?.error_code
      || ((q.state.data?.performer_id != null || q.state.data?.category_id != null) && !passes(q.state.data)) ? PREFLIGHT_MS : false),
  });
  const libraries = useLibraries();
  const [seenRev, setSeenRev] = useState<FilmRevisions | undefined>(undefined);
  const [choice, setChoice] = useState<Choice | null>(null);
  const [newActor, setNewActor] = useState<{ name: string; library_id: string } | null>(null);
  const [secondId, setSecondId] = useState<string | null>(null);
  const [phase, setPhase] = useState<"review" | "submitted">("review");
  // The detail's update time when 「确认入库」 succeeded: only a later read may end the submitted phase.
  const [submittedAt, setSubmittedAt] = useState(0);
  // the second copy confirmed with it, for the receipt (the film detail names the first one only)
  const [boundSecond, setBoundSecond] = useState<string | null>(null);
  const actor = film?.actors[0] ?? "";
  // Candidates: the preflight's (the library searched for every NFO actor); while it is offline
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
  // A new final revision re-derives the choice from its own candidates: a chosen actor stays only
  // while that performer is still a candidate (else it is dropped and the picker asks again).
  useEffect(() => {
    if (choice?.kind !== "performer" || choice.rev === final || candidates === null) return;
    const same = candidates.find((p) => String(p.id) === String(choice.performer.id));
    setChoice(same ? { kind: "performer", performer: same, rev: final } : null);
  }, [choice, final, candidates]);
  // The automatic choice is the rules' preselection, which the preflight checked in the same call
  // (also when it arrives after an empty or failed first load); while the preflight failed, a sole
  // search result. It is derived from the current answer, so it always belongs to this revision, and
  // an operator's choice is never replaced.
  const auto: Choice | null | undefined = useMemo(() => {
    if (candidates === null) return undefined;
    if (target.data?.category_id) return { kind: "category", id: target.data.category_id };
    const id = target.data ? target.data.performer_id : candidates.length === 1 ? candidates[0].id : null;
    const hit = id == null ? undefined : candidates.find((p) => String(p.id) === String(id));
    return hit ? { kind: "performer", performer: hit, rev: final } : null;
  }, [candidates, target.data, final]);
  const sel = choice && (choice.kind !== "performer" || choice.rev === final) ? choice : auto;
  const selected = sel?.kind === "performer" ? sel.performer : null;
  const isNew = sel?.kind === "new_actor";
  const isManual = sel?.kind === "manual";
  // 新建演员目录: the rules' defaults (§8) until edited, in an archive-role actor library
  const archiveLibs = actorLibraries(libraries.data?.items).filter((l) => l.role === "archive");
  const letter = letters(libraries.data?.items);
  const form = newActor ?? {
    name: target.data?.new_actor?.name ?? "", library_id: target.data?.new_actor?.library_id ?? archiveLibs[0]?.id ?? "",
  };
  const name = form.name.trim();
  // the duplicate check follows the typing, 300 ms after it stops
  const [checkedName, setCheckedName] = useState(name);
  useEffect(() => { const h = setTimeout(() => setCheckedName(name), 300); return () => clearTimeout(h); }, [name]);
  const typing = isNew && (!name || !form.library_id || name !== checkedName);
  // The chosen target's check: the shared answer when the backend checked that very target,
  // else its own query (once the shared one has answered), keyed by the revision as well.
  const shared = (sel?.kind === "performer" && target.data?.performer_id != null && !target.data.category_id
    && String(target.data.performer_id) === String(sel.performer.id))
    || (sel?.kind === "category" && target.data?.category_id === sel.id);
  const ownKey = !sel ? null : sel.kind === "performer" ? sel.performer.id : sel.kind === "manual" ? `manual:${sel.dir}`
    : sel.kind === "category" ? `category:${sel.id}` : `new:${form.library_id}:${checkedName}`;
  const own = useQuery({
    queryKey: [...mediaTargetKey(filmId, final), ownKey],
    queryFn: () => {
      if (sel?.kind === "manual") return api.workflowTarget(filmId, undefined, sel.dir);
      if (sel?.kind === "category") return api.workflowTarget(filmId, undefined, undefined, { category_id: sel.id });
      if (sel?.kind === "new_actor") {
        return api.workflowTarget(filmId, undefined, undefined, { library_id: form.library_id, actor_name: checkedName });
      }
      return api.workflowTarget(filmId, (selected as Performer).id);
    },
    enabled: Boolean(sel) && !shared && !typing && (target.data !== undefined || target.isError), retry: false,
    refetchInterval: (q) => (passes(q.state.data) ? false : PREFLIGHT_MS),
  });
  const preflight = shared ? target : own;
  const hasTarget = Boolean(sel);
  // 第二目标 (§9.4): one category directory, only beside an actor or a new actor directory. Every
  // category directory is offered (the rules' `category_always` concerns the primary target only).
  const secondCats: Category[] = libraries.data ? categoryLibraries(libraries.data.items) : target.data?.categories ?? [];
  const canSecond = (sel?.kind === "performer" || isNew) && secondCats.length > 0;
  const second = canSecond ? secondCats.find((c) => c.id === secondId) ?? null : null;
  const secondPf = useQuery({
    queryKey: [...mediaTargetKey(filmId, final), `second:${second?.id ?? ""}`],
    queryFn: () => api.workflowTarget(filmId, undefined, undefined, { category_id: (second as Category).id }),
    enabled: second !== null, retry: false,
    refetchInterval: (q) => (passes(q.state.data) ? false : PREFLIGHT_MS),
  });
  const hint = canSecond ? secondCats.find((c) => c.id === target.data?.second_hint) ?? null : null;
  const choose = (next: Choice) => {
    // the same target again asks the NAS again
    if (sel && sameChoice(sel, next)) void preflight.refetch();
    // a category or a manual directory takes no second target
    if (next.kind === "category" || next.kind === "manual") setSecondId(null);
    setChoice(next);
  };
  const chooseDirectory = async () => {
    const path = await pickPath(true, "选择入库目录");
    if (path !== null) choose({ kind: "manual", dir: path });
  };
  const confirm = useMutation({
    mutationFn: () => {
      const extra = second ? { second_category_id: second.id } : undefined;
      if (sel?.kind === "manual") return api.workflowConfirm(filmId, undefined, sel.dir);
      if (sel?.kind === "category") return api.workflowConfirm(filmId, undefined, undefined, { category_id: sel.id });
      if (sel?.kind === "new_actor") {
        return api.workflowConfirm(filmId, undefined, undefined, { new_actor: { library_id: form.library_id, name }, ...extra });
      }
      const id = (selected as Performer).id;
      return extra ? api.workflowConfirm(filmId, id, undefined, extra) : api.workflowConfirm(filmId, id);
    },
    onSuccess: () => {
      setPhase("submitted");
      setSubmittedAt(detail.dataUpdatedAt);
      setBoundSecond(second ? secondPf.data?.target_path ?? null : null);
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(filmId) });
    },
  });
  const open = useMutation({ mutationFn: () => api.workflowOpen(filmId, "output"), onError: (e) => ws.toast(errorText(e)) });
  const play = useMutation({ mutationFn: () => api.workflowPlay(filmId), onError: (e) => ws.toast(errorText(e)) });
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
  if (phase === "submitted" && !reopened) return <Receipt film={film} second={boundSecond} onClose={onClose} />;

  const status: StatusKey | undefined = archiveStatus === "unknown" ? "unknown" : item?.status;
  // the chosen target's own answer only: never an older or another target's
  const errorCode = (hasTarget ? (typing ? null : preflight.data?.error_code) : target.data?.error_code) ?? null;
  const changed = seenRev !== undefined && !sameRevisions(seenRev, film.revisions);
  const earlier =
    status === "unknown" ? "入库结果待核对，暂不能重新上传。"
      : !film.can_confirm ? "影片尚未完成准备，稍后自动继续。"
        : !film.has_poster || film.files.length === 0 ? "最终封面或文件包尚未准备好，请先处理。"
          : changed ? "成品或来源已更新，请重新查看后确认。" : "";
  const libLabel = archiveLibs.find((l) => l.id === form.library_id)?.label ?? "所选片库";
  // a new actor's refusal is told live under its name (an offline library below, as any target's)
  const newError = isNew && errorCode && errorCode !== "offline"
    ? errorCode === "actor_exists" ? `${libLabel}里已经有「${name}」，请直接选它，或换一个名字。`
      : preflight.data?.message || TARGET_BLOCK[errorCode] : "";
  const primaryBlock = isNew && !name ? "请填写演员名。"
    : isNew && !form.library_id ? "没有可入库的演员片库，请先在「片库与规则」中添加。"
      : typing ? "正在检查目标…"
        : newError ? newError
          : errorCode ? isManual && errorCode === "duplicate" ? "目标目录下已有同名目录"
            : isManual && errorCode === "missing" ? "目标目录已不存在，请重新选择。"
              : errorCode === "missing" && sel?.kind === "category" ? preflight.data?.message || TARGET_BLOCK.missing
                : TARGET_BLOCK[errorCode]
            : !hasTarget ? (candidates === null ? "正在检查目标…" : "请选择演员与目标路径。")
              : targetCheck(preflight.data, preflight.isError ? preflight.error : null);
  const secondCode = second ? secondPf.data?.error_code ?? null : null;
  const secondCheck = !second ? "" : secondCode ? secondPf.data?.message || TARGET_BLOCK[secondCode]
    : targetCheck(secondPf.data, secondPf.isError ? secondPf.error : null);
  const secondBlock = secondCheck && `第 2 份：${secondCheck}`;
  const targetBlock = primaryBlock || secondBlock;
  const blocked = earlier || targetBlock;
  // the new actor form tells its own state live; the notice tells the rest (an offline NAS, the second copy)
  const formTells = isNew && Boolean(primaryBlock) && errorCode !== "offline" && !preflight.isError;
  const notice = earlier || (formTells ? secondBlock : targetBlock);
  // 重新检查: a target check that did not pass is asked again on demand (it is also polled)
  const rechecking = (hasTarget ? preflight : target).isFetching || secondPf.isFetching;
  const recheck = !earlier && targetBlock && !typing && (hasTarget || errorCode)
    ? () => { void (hasTarget ? preflight : target).refetch(); if (second) void secondPf.refetch(); } : null;
  const title = film.title || film.name;
  // the backend's target already ends in the film folder (`film_dir`)
  const targetPath = hasTarget && !typing ? preflight.data?.target_path ?? null : null;
  // a retry after a failed second copy: the first copy is this engine's verified one (§9.5)
  const archived = hasTarget && !typing && passes(preflight.data) && preflight.data?.archived === true;
  // only a real revision conflict (`cas_conflict`) says the film changed; other refusals keep their own message
  const conflict = confirm.error instanceof HttpError && confirm.error.status === 409 && confirm.error.code === "cas_conflict";
  const categories = target.data?.categories ?? [];
  const who = selected?.name ?? (isNew ? name || "—" : sel ? actor || "—" : "待选择");

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
            <p>演员：<strong>{who}</strong></p>
            <p>{film.marks.chinese_subtitles ? "修复成品 · 中文字幕已核对" : "修复成品 · 无语音，不加中文字幕标识"}</p>
          </div>
        </div>
        <Candidates candidates={candidates} selected={selected} actor={actor}
          onSelect={(p) => choose({ kind: "performer", performer: p, rev: final })}
          newActor={target.data?.new_actor ? { chosen: isNew, onSelect: () => choose({ kind: "new_actor" }) } : null} />
        {isNew && (
          <div className="new-form">
            <label className="field" htmlFor="rv-new-name">演员名（目录名）
              <input id="rv-new-name" className="input" value={form.name} onChange={(e) => setNewActor({ ...form, name: e.target.value })} />
            </label>
            <label className="field" htmlFor="rv-new-lib">放到
              <select id="rv-new-lib" value={form.library_id} onChange={(e) => setNewActor({ ...form, library_id: e.target.value })}>
                {archiveLibs.map((l) => (
                  <option key={l.id} value={l.id}>{letter[l.id]} · {l.label}{offline(l) ? "（离线）" : ""}</option>
                ))}
              </select>
            </label>
            <p className={`wide tiny ${newError ? "t-red" : "muted"}`} role="status">
              {!name ? "请填写演员名。" : !form.library_id ? "没有可入库的演员片库，请先在「片库与规则」中添加。"
                : newError || (!typing && passes(preflight.data)
                ? `${libLabel}里没有同名目录，可以新建。名字也会记为该演员的正式名，下次直接匹配。` : "正在检查是否已有同名目录…")}
            </p>
          </div>
        )}
        {categories.length > 0 && (
          <>
            <h3>{(candidates?.length ?? 0) > 0 ? "或改放到类型目录（代替演员目录）" : "或放到类型目录"}</h3>
            <div className="candidate-list" role="group" aria-label="类型目录">
              {categories.map((c) => (
                <CategoryButton key={c.id} category={c} pressed={sel?.kind === "category" && sel.id === c.id}
                  onClick={() => choose({ kind: "category", id: c.id })} />
              ))}
            </div>
          </>
        )}
        {inTauri() && !ws.readOnly && (
          <div className="spacer-top">
            <button type="button" className="btn ghost" onClick={() => void chooseDirectory()}>手动入库</button>
          </div>
        )}
        {archiveStatus === "failed" && film.can_confirm && (
          <Notice tone="red">上次入库未完成：{film.archive.error || "原因未知"}。可重新确认入库。</Notice>
        )}
        {notice && <div className="spacer-top"><Notice tone="amber" icon={WarningAmberIcon}>{notice}</Notice></div>}
        {recheck && (
          <div className="spacer-top">
            <button type="button" className="btn ghost small" disabled={rechecking} onClick={recheck}>重新检查</button>
          </div>
        )}
        {confirm.error && <Notice tone="red">{conflict ? "成品或来源已更新，请重新查看后确认。" : errorText(confirm.error)}</Notice>}
        <div className="target-box">
          <h3><Icon of={FolderOpenIcon} />入库到{second ? "（第 1 份）" : ""}
            {isNew && targetPath && <Chip tone="amber">含新建目录</Chip>}
            {sel?.kind === "category" && <Chip tone="blue">类型目录</Chip>}
          </h3>
          <div className="target-path">{targetPath ?? "尚未选择完整目标"}{targetPath && preflight.data?.manual && (
            <PanToolAltOutlinedIcon fontSize="small" aria-label="手动入库" titleAccess="手动入库" />
          )}</div>
          {archived && <p className="small t-green spacer-top">{second ? "第 1 份已入库，本次只复制第二份" : "已入库，本次只核对，不再复制"}</p>}
          <p className="small muted spacer-top">保留所有已有片源；同名冲突不会覆盖。</p>
        </div>
        {canSecond && (
          <SecondTarget categories={secondCats} hint={hint} number={film.number || film.name} second={second}
            path={secondPf.data?.target_path ?? null} onSet={setSecondId} onClear={() => setSecondId(null)} />
        )}
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
        <p>{blocked ? "处理后重新确认即可。" : second ? "将复制两份：演员目录和类型目录各一份。" : "一次确认即可，剩余步骤由后台完成。"}</p>
        <div className="actions">
          {!ws.readOnly && <button type="button" className="btn ghost" disabled={play.isPending} onClick={() => play.mutate()}>预览播放</button>}
          <button type="button" className="btn ghost" disabled={open.isPending} onClick={() => open.mutate()}>打开文件夹</button>
          {changed && <button type="button" className="btn ghost" onClick={() => { setSeenRev(film.revisions); confirm.reset(); }}>重新查看</button>}
          <button type="button" className="btn primary" disabled={Boolean(blocked) || confirm.isPending}
            onClick={() => { if (hasTarget && !blocked) confirm.mutate(); }}>确认入库</button>
        </div>
      </div>
    </>
  );
}

const sameChoice = (a: Choice, b: Choice) =>
  a.kind === "performer" ? b.kind === "performer" && String(a.performer.id) === String(b.performer.id)
    : a.kind === "category" ? b.kind === "category" && a.id === b.id
      : a.kind === "manual" ? b.kind === "manual" && a.dir === b.dir : a.kind === b.kind;

// A passed target check: no error, with the target folder and that folder known to be absent — or
// holding this engine's verified copy of this package (`archived`, a retry, §9.5).
const passes = (d: TargetPreflight | undefined) =>
  Boolean(d && !d.error_code && d.target_path && (d.absent === true || d.archived === true));

// 「确认入库」 needs a passed preflight of the chosen target: its answer, with the target folder
// and that folder known to be absent (or archived). Loading or an HTTP error is no pass.
function targetCheck(data: TargetPreflight | undefined, error: unknown): string {
  if (error) return `目标检查失败：${errorText(error)}`;
  if (!data) return "正在检查目标…";
  if (!passes(data)) return "目标检查未通过，请重新选择或稍后重试。";
  return "";
}

function Candidates({ candidates, selected, actor, onSelect, newActor }: {
  candidates: Performer[] | null; selected: Performer | null; actor: string; onSelect: (p: Performer) => void;
  newActor: { chosen: boolean; onSelect: () => void } | null;
}) {
  const [text, setText] = useState(actor);
  const [query, setQuery] = useState("");
  useEffect(() => { const h = setTimeout(() => setQuery(text.trim()), 300); return () => clearTimeout(h); }, [text]);
  const manual = candidates !== null && candidates.length === 0;
  const found = useQuery({
    queryKey: ["mediaPerformers", query], queryFn: () => api.performers(query), enabled: manual && query !== "", retry: false,
  });
  if (candidates === null) return <p className="small muted">正在查找演员与目标…</p>;
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
      {manual && found.data && list.length === 0 && (
        <p className="small muted spacer-top">{newActor ? "没有匹配的演员。可以新建演员目录，或放到类型目录。" : "没有匹配的演员。不会自动新建演员目录。"}</p>
      )}
      {manual && found.error && <p className="small t-red spacer-top">演员搜索失败：{errorText(found.error)}</p>}
      <div className="candidate-list" role="group" aria-label="演员与目标候选">
        {list.map((p) => {
          // a read-only location is listed for information only (§9.6)
          const meta = [p.film_count != null ? `${p.film_count} 部` : null, p.aliases.length ? `别名：${p.aliases.join("、")}` : null,
            p.fuzzy ? "相似名" : null, p.role === "readonly" ? "只读片库，仅供参考" : null].filter(Boolean).join(" · ");
          return (
            <button key={p.id} type="button" className="candidate" aria-pressed={String(selected?.id) === String(p.id)}
              disabled={p.role === "readonly"} onClick={() => onSelect(p)}>
              <ActorThumb id={String(p.id)} hasThumb={p.has_thumb} name={p.name} />
              <div style={{ minWidth: 0 }}>
                <strong>{p.name}</strong>
                <span>{[p.source_label, p.unc_path].filter(Boolean).join(" / ")}</span>
                {meta && <span>{meta}</span>}
              </div>
            </button>
          );
        })}
        {newActor && (
          <button type="button" className="candidate new" aria-pressed={newActor.chosen} onClick={newActor.onSelect}>
            <div className="avatar" aria-hidden="true"><Icon of={AddIcon} /></div>
            <div><strong>新建演员目录</strong><span>点「确认入库」时才创建</span></div>
          </button>
        )}
      </div>
    </>
  );
}

function CategoryButton({ category, pressed, onClick }: {
  category: Category & Partial<Pick<TargetCategory, "film_count" | "prefix_match">>; pressed?: boolean; onClick: () => void;
}) {
  const meta = [category.film_count != null ? `${category.film_count} 部` : null,
    category.prefixes.length ? `前缀 ${category.prefixes.join("、")}` : null].filter(Boolean).join(" · ");
  return (
    <button type="button" className="candidate" aria-pressed={pressed} onClick={onClick}>
      <div className="kicon" aria-hidden="true"><Icon of={FolderOpenIcon} /></div>
      <div style={{ minWidth: 0 }}>
        <strong>{category.label}</strong>
        <span className="mono">{category.path}</span>
        {(meta || category.prefix_match) && (
          <span>{meta}{category.prefix_match && <>{meta && " · "}<b className="t-green">本片命中</b></>}</span>
        )}
      </div>
    </button>
  );
}

// 「第二目标（可选）」: the prefix hint is a button, never added by itself (§8 `second_target_hint`).
function SecondTarget({ categories, hint, number, second, path, onSet, onClear }: {
  categories: Category[]; hint: Category | null; number: string; second: Category | null; path: string | null;
  onSet: (id: string) => void; onClear: () => void;
}) {
  const [picking, setPicking] = useState(false);
  if (second) {
    return (
      <div className="target-box second">
        <div className="section-h">
          <h3><Icon of={FolderOpenIcon} />第 2 份 · 类型目录</h3>
          <button type="button" className="btn small ghost" onClick={onClear}><Icon of={CloseIcon} />不要第二目标</button>
        </div>
        <div className="target-path">{path ?? "正在检查目标…"}</div>
        <p className="tiny muted spacer-top">同一个成品包再复制一份。两份都校验通过、写下回执后，才清理来源。</p>
      </div>
    );
  }
  const prefix = hint?.prefixes.find((p) => number.toUpperCase().startsWith(p.toUpperCase()));
  return (
    <div className="second-add">
      <div style={{ minWidth: 0, flex: "1 1 260px" }}>
        <strong className="small">第二目标（可选）</strong>
        <p className="tiny muted">再复制一份到某个类型目录，例如写真系列。</p>
        {hint && prefix && <p className="tiny t-blue">番号前缀 {prefix} 对应「{hint.label}」</p>}
      </div>
      <div className="actions">
        {hint && <button type="button" className="btn small primary" onClick={() => onSet(hint.id)}><Icon of={AddIcon} />加入「{hint.label}」</button>}
        <button type="button" className="btn small" aria-expanded={picking} onClick={() => setPicking(!picking)}>{picking ? "收起" : "选择类型目录"}</button>
      </div>
      {picking && (
        <div className="candidate-list" role="group" aria-label="第二目标类型目录" style={{ flex: "1 1 100%", margin: 0 }}>
          {categories.map((c) => <CategoryButton key={c.id} category={c} onClick={() => { onSet(c.id); setPicking(false); }} />)}
        </div>
      )}
    </div>
  );
}

// After 「确认入库」: 「已确认，等待归档」 until the receipt, then 入库完成 or 已入库 · 待清理; with a
// second target both copies are listed.
function Receipt({ film, second, onClose }: { film: FilmDetail; second: string | null; onClose: () => void }) {
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
            <h3><Icon of={CheckIcon} /> 成品已入库{second ? " · 两份都已校验" : ""}</h3>
            <p>{second ? "第 1 份：" : ""}{film.archive.target_path ?? ""}</p>
            {second && <p>第 2 份：{second}</p>}
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
