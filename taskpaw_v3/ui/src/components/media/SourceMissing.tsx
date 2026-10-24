// P3 E1–E3 (#304), board Drawer-SourceMissing: 片源丢失 — the INBOX files of a film that has not started
// are gone. The overview row, the drawer's alert (原路径, 发现时间, 重新检查) and 删除此任务, which is also
// offered for any film with no running or claimed stage that is not archived.
import DeleteOutlineIcon from "@mui/icons-material/DeleteOutline";
import RefreshIcon from "@mui/icons-material/Refresh";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type FilmDetail } from "../../api";
import { clockText, itemTitle, type WorkItem } from "./mediaModel";
import { Chip, Icon, ItemCover, Notice, StatusChip } from "./parts";
import { ModalHead, ModalShell } from "./ReviewDialog";
import { errorText, MEDIA_FILMS_KEY, mediaFilmKey, useWorkspace } from "./workspace";

const HINT = "如果只是移走了，把文件放回原处后点「重新检查」即可继续。";
const REMOVE_BODY = "只删除任务和它在工作目录里的临时文件，不会动 INBOX、成品目录或 NAS 上的任何文件。删除后不能恢复。";

// 概览: a 片源丢失 film, its badge and why it stopped.
export function SourceMissingRow({ item }: { item: WorkItem }) {
  const ws = useWorkspace();
  if (item.kind !== "film") return null;
  const title = itemTitle(item);
  return (
    <article className="film-row lost-row" data-source-missing={item.id}>
      <ItemCover item={item} />
      <div className="film-body">
        <div className="film-title"><h3>{title}</h3>{item.row.resolution && <Chip>{item.row.resolution}</Chip>}<StatusChip status={item.status} /></div>
        {item.row.note && <p className="small t-red">{item.row.note}</p>}
      </div>
      <button type="button" className="btn small" aria-label={`${ws.readOnly ? "查看详情" : "处理"} ${title}`} onClick={() => ws.openDetail(item.id)}>
        <Icon of={WarningAmberIcon} />{ws.readOnly ? "查看详情" : "处理"}
      </button>
    </article>
  );
}

// The drawer's alert: the original path(s), when it was found, how to get back and the two actions.
export function SourceMissingAlert({ film, onRemoved }: { film: FilmDetail; onRemoved: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const missing = film.source_missing;
  const recheck = useMutation({
    mutationFn: () => {
      if (ws.readOnly) throw new Error("只读监控");
      return api.workflowRecheckSource(film.film_id);
    },
    onSuccess: (r) => {
      ws.toast(r.source_missing ? "文件仍然不存在。" : "文件已找回，影片继续排队。");
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(film.film_id) });
    },
    onError: (e) => ws.toast(errorText(e)),
  });
  if (!missing) return null;
  return (
    <Notice tone="red" icon={WarningAmberIcon}>
      <h3>片源丢失</h3>
      <dl className="facts">
        <dt>原路径</dt>
        <dd>{missing.paths.map((p) => <div key={p} className="mono wrap">{p}</div>)}</dd>
        <dt>发现时间</dt><dd>{clockText(missing.found_at, Date.now())}</dd>
      </dl>
      <p className="small">{HINT}</p>
      {!ws.readOnly && (
        <div className="actions spacer-top">
          <button type="button" className="btn" disabled={recheck.isPending} onClick={() => recheck.mutate()}>
            <Icon of={RefreshIcon} />重新检查
          </button>
          <RemoveTask film={film} onRemoved={onRemoved} />
        </div>
      )}
    </Notice>
  );
}

// 删除此任务 with its confirmation. The backend refuses (409) while a stage runs or is claimed, or the
// film was confirmed or archived; its reason is shown and nothing changes.
export function RemoveTask({ film, onRemoved }: { film: FilmDetail; onRemoved: () => void }) {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const [asking, setAsking] = useState(false);
  const code = film.number || film.name;
  const remove = useMutation({
    mutationFn: () => {
      if (ws.readOnly) throw new Error("只读监控");
      return api.workflowRemove(film.film_id);
    },
    onSuccess: () => {
      setAsking(false);
      ws.toast(`已删除任务 ${code}。`);
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      onRemoved();
    },
    onError: (e) => {
      setAsking(false);
      ws.toast(errorText(e));
      void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
      void qc.invalidateQueries({ queryKey: mediaFilmKey(film.film_id) });
    },
  });
  return (
    <>
      <button type="button" className="btn danger" onClick={() => setAsking(true)}><Icon of={DeleteOutlineIcon} />删除此任务</button>
      <ModalShell open={asking} onClose={() => setAsking(false)} titleId="mw-remove-title" role="alertdialog">
        <ModalHead id="mw-remove-title" title={`删除 ${code} 这个任务？`} subtitle={film.title || film.name} onClose={() => setAsking(false)} />
        <div className="modal-body"><p>{REMOVE_BODY}</p></div>
        <div className="modal-foot">
          <p />
          <div className="actions">
            <button type="button" className="btn ghost" onClick={() => setAsking(false)}>取消</button>
            <button type="button" className="btn danger" disabled={remove.isPending} onClick={() => remove.mutate()}>删除</button>
          </div>
        </div>
      </ModalShell>
    </>
  );
}
