import CheckIcon from "@mui/icons-material/Check";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { api, HttpError, type LtxTier, type PlanChoice, type PlanEntry, type PlanMachine, type PlanResult } from "../../api";
import { fmtBytes, fmtDuration, TIER_ORDER, TIER_SHORT, tierLong } from "./mediaModel";
import { Chip, Cover, EmptyState, Icon, Notice, PageTitle } from "./parts";
import { errorText, MEDIA_FILMS_KEY, MEDIA_PLANS_KEY, useWorkspace } from "./workspace";

type Filter = "all" | "gpu" | "nogpu";
type Choice = { tier: LtxTier | null; machine: string };

const recommendedChoice = (e: PlanEntry): Choice => ({ tier: e.needs_gpu ? e.recommended.ltx_model : null, machine: e.recommended.machine });
const complete = (e: PlanEntry) => Boolean(e.recommended.machine) && (!e.needs_gpu || e.recommended.ltx_model !== null);
const entryTitle = (e: PlanEntry) => e.title || e.name;

// 待处理 (U1 V2): unplanned films wait here; the operator keeps or changes the recommended tier
// and device, then confirms each film (or every unchanged one) into the film queue.
export function PendingPage() {
  const ws = useWorkspace();
  const qc = useQueryClient();
  const plans = ws.data.plans;
  const [filter, setFilter] = useState<Filter>("all");
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  // The input revision the operator has looked at; a newer one makes the card stale until the
  // detail is opened again (「收件内容已更新，请重新查看后确认」).
  const [seen, setSeen] = useState<Record<string, number>>({});
  const [rejected, setRejected] = useState<Set<string>>(new Set());
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const focusNext = useRef<string | null>(null);

  const all = useMemo(() => (plans.data?.items ?? []).filter((e) => !hidden.has(e.film_id)), [plans.data, hidden]);
  useEffect(() => {
    setSeen((prev) => {
      const missing = all.filter((e) => prev[e.film_id] === undefined);
      if (missing.length === 0) return prev;
      const next = { ...prev };
      missing.forEach((e) => { next[e.film_id] = e.input_revision; });
      return next;
    });
  }, [all]);
  // A confirmed film stays hidden until the refreshed list no longer has it.
  useEffect(() => {
    const ids = new Set((plans.data?.items ?? []).map((e) => e.film_id));
    setHidden((prev) => {
      const next = new Set([...prev].filter((id) => ids.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [plans.data]);
  useEffect(() => {
    if (focusNext.current === null) return;
    const id = focusNext.current;
    focusNext.current = null;
    const card = id ? document.querySelector<HTMLElement>(`[data-pending="${CSS.escape(id)}"]`) : null;
    const target = card?.querySelector<HTMLButtonElement>('[data-action="confirm"]:not(:disabled)')
      ?? card?.querySelector<HTMLButtonElement>('[data-action="detail"]')
      ?? document.getElementById("mw-main");
    target?.focus({ preventScroll: !card });
  });

  const items = all.filter((e) => (filter === "gpu" ? e.needs_gpu : filter === "nogpu" ? !e.needs_gpu : true));
  const choiceOf = (e: PlanEntry) => choices[e.film_id] ?? recommendedChoice(e);
  const isStale = (e: PlanEntry) => rejected.has(e.film_id) || (seen[e.film_id] !== undefined && seen[e.film_id] !== e.input_revision);
  const unchanged = (e: PlanEntry) => {
    const c = choiceOf(e), r = recommendedChoice(e);
    return c.tier === r.tier && c.machine === r.machine;
  };
  const batchEligible = (e: PlanEntry) => complete(e) && !isStale(e) && unchanged(e);
  const eligible = items.filter(batchEligible);
  const machines = plans.data?.machines ?? [];

  const after = (sent: PlanEntry[], results: PlanResult[] | null, failure?: unknown) => {
    const ok = new Set<string>();
    const stale = new Set<string>();
    if (results) {
      for (const r of results) {
        if (r.ok) ok.add(r.film_id);
        else if (r.status === 409) stale.add(r.film_id);
      }
    } else if (failure instanceof HttpError && failure.status === 409 && sent.length === 1) {
      stale.add(sent[0].film_id);
    }
    const order = items.map((e) => e.film_id);
    const firstIdx = order.indexOf(sent[0]?.film_id ?? "");
    const remaining = order.filter((id) => !ok.has(id));
    focusNext.current = remaining[Math.min(Math.max(firstIdx, 0), remaining.length - 1)] ?? "";
    setHidden((prev) => new Set([...prev, ...ok]));
    if (stale.size) setRejected((prev) => new Set([...prev, ...stale]));
    void qc.invalidateQueries({ queryKey: MEDIA_PLANS_KEY });
    void qc.invalidateQueries({ queryKey: MEDIA_FILMS_KEY });
    return ok;
  };

  const confirm = useMutation({
    mutationFn: (plans: PlanChoice[]) => api.workflowConfirmPlans(plans),
  });
  const send = async (entries: PlanEntry[], chosen: (e: PlanEntry) => Choice) => {
    const body = entries.map((e): PlanChoice => {
      const c = chosen(e);
      return { film_id: e.film_id, input_revision: e.input_revision, ltx_model: e.needs_gpu ? c.tier : null, machine: c.machine };
    });
    try {
      const res = await confirm.mutateAsync(body);
      return after(entries, res.results ?? []);
    } catch (e) {
      after(entries, null, e);
      ws.toast(e instanceof HttpError && e.status === 409 ? "收件内容已更新，请重新查看后确认" : errorText(e));
      return null;
    }
  };
  const confirmOne = async (e: PlanEntry) => {
    const ok = await send([e], choiceOf);
    if (!ok) return;
    const c = choiceOf(e);
    if (ok.has(e.film_id)) ws.toast(`${entryTitle(e)} 已确认入队 · ${tierLong(e.needs_gpu ? c.tier : null)} · ${machineName(machines, c.machine)}`);
    else ws.toast("收件内容已更新，请重新查看后确认");
  };
  const confirmAll = async () => {
    const skipped = items.length - eligible.length;
    const ok = await send(eligible, recommendedChoice);
    if (!ok) return;
    const failed = eligible.length - ok.size;
    const rest = skipped + failed;
    ws.toast(`已确认 ${ok.size} 部，进入影片队列${rest ? `。${rest} 部已调整或需重新查看，请单独确认。` : ""}`);
  };
  const lookup = useMutation({
    mutationFn: (filmId: string) => api.workflowLookup(filmId),
    onSuccess: () => qc.invalidateQueries({ queryKey: MEDIA_PLANS_KEY }),
    onError: (e) => ws.toast(errorText(e)),
  });

  const openDetail = (e: PlanEntry) => {
    setSeen((prev) => ({ ...prev, [e.film_id]: e.input_revision }));
    setRejected((prev) => { const n = new Set(prev); n.delete(e.film_id); return n; });
    ws.openDetail(e.film_id);
  };

  return (
    <>
      <PageTitle title="待处理" actions={
        <button type="button" className="btn ghost" disabled={eligible.length === 0 || confirm.isPending}
          onClick={() => void confirmAll()}>
          <Icon of={CheckIcon} />全部按推荐确认
        </button>
      } />
      <div className="toolbar pending-toolbar">
        <div className="filters" role="group" aria-label="按是否需要修复筛选">
          {([["all", "全部"], ["gpu", "需修复"], ["nogpu", "无需修复"]] as Array<[Filter, string]>).map(([v, l]) => (
            <button key={v} type="button" className="filter-pill" aria-pressed={filter === v} onClick={() => setFilter(v)}>{l}</button>
          ))}
          <Chip>{items.length} 部影片</Chip>
        </div>
      </div>
      {plans.isLoading ? (
        <div className="panel empty"><p>正在读取待处理影片…</p></div>
      ) : plans.error ? (
        <Notice tone="red" icon={WarningAmberIcon}>无法读取待处理列表：{errorText(plans.error)}</Notice>
      ) : items.length ? (
        <div className="pending-grid">
          {items.map((e) => (
            <PendingCard key={e.film_id} entry={e} choice={choiceOf(e)} machines={machines} stale={isStale(e)}
              busy={confirm.isPending} lookupBusy={lookup.isPending && lookup.variables === e.film_id}
              onChoice={(c) => setChoices((prev) => ({ ...prev, [e.film_id]: c }))}
              onConfirm={() => void confirmOne(e)} onDetail={() => openDetail(e)} onLookup={() => lookup.mutate(e.film_id)} />
          ))}
        </div>
      ) : all.length ? (
        <EmptyState title="当前筛选没有待处理影片" description="切换到“全部”查看其余待处理影片。" />
      ) : (
        <EmptyState title="没有待处理影片" description="新投入 INBOX 的影片会先出现在这里。" />
      )}
    </>
  );
}

function machineName(machines: PlanMachine[], id: string): string {
  const m = machines.find((x) => x.id === id);
  return m ? m.label.split(" · ")[0] : id === "local" ? "本机" : id;
}

function PendingCard({ entry: e, choice, machines, stale, busy, lookupBusy, onChoice, onConfirm, onDetail, onLookup }: {
  entry: PlanEntry; choice: Choice; machines: PlanMachine[]; stale: boolean; busy: boolean; lookupBusy: boolean;
  onChoice: (c: Choice) => void; onConfirm: () => void; onDetail: () => void; onLookup: () => void;
}) {
  const id = e.film_id;
  const tid = `pt-${id}`;
  const title = entryTitle(e);
  const rec = e.recommended;
  const options = machines.length ? machines : [{ id: rec.machine, label: rec.machine === "local" ? "本机" : rec.machine }];
  const machineReason = machines.find((m) => m.id === choice.machine)?.reason ?? (choice.machine === rec.machine ? rec.machine_reason : "");
  const ready = Boolean(choice.machine) && (!e.needs_gpu || choice.tier !== null);
  const idLine = [e.number || (e.title ? e.name : null), e.actors.join("、")].filter(Boolean).join(" · ");
  const onKey = (ev: KeyboardEvent<HTMLButtonElement>, tier: LtxTier) => {
    const step = ({ ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 } as Record<string, number>)[ev.key];
    const idx = TIER_ORDER.indexOf(tier);
    let next: number;
    if (step) next = (idx + step + TIER_ORDER.length) % TIER_ORDER.length;
    else if (ev.key === "Home") next = 0;
    else if (ev.key === "End") next = TIER_ORDER.length - 1;
    else return;
    ev.preventDefault();
    onChoice({ ...choice, tier: TIER_ORDER[next] });
    (ev.currentTarget.parentElement?.querySelector(`[data-tier="${TIER_ORDER[next]}"]`) as HTMLElement | null)?.focus();
  };
  return (
    <article className="panel review-card pending-card" data-pending={id} aria-labelledby={tid}>
      <Cover filmId={id} hasPoster={e.has_poster} source={e.poster_source} size="review" label={title} />
      <div className="film-body">
        <div className="film-title"><h3 id={tid}>{title}</h3>{e.resolution && <Chip>{e.resolution}</Chip>}</div>
        {idLine && <p>{idLine}</p>}
        <p className="mono">{fmtDuration(e.duration_s)} · {fmtBytes(e.size_bytes)} · {e.file_count} 个文件</p>
        {(e.lookup === "failed" || e.lookup === "no_number") && (
          <div className="keyline">
            <span>{e.lookup === "no_number" ? "文件名中没有番号" : "未查到资料"}</span>
            <button type="button" className="link-btn" disabled={e.lookup === "no_number" || lookupBusy} onClick={onLookup}>重新查询</button>
          </div>
        )}
        {e.lookup === "pending" && <p>正在查询资料…</p>}
        {stale && <Notice tone="amber" icon={WarningAmberIcon} id={`stale-${id}`}>收件内容已更新，请重新查看后确认</Notice>}
      </div>
      <div className="plan">
        {e.needs_gpu ? (
          <div className="plan-field">
            <span className="plan-label" id={`tier-label-${id}`}>画质</span>
            <div className="seg" role="radiogroup" aria-labelledby={`tier-label-${id}`} aria-describedby={`tier-reason-${id}`}>
              {TIER_ORDER.map((t) => {
                const on = choice.tier === t;
                return (
                  <button key={t} type="button" className="seg-opt" role="radio" aria-checked={on} data-tier={t}
                    tabIndex={on || (choice.tier === null && t === TIER_ORDER[0]) ? 0 : -1}
                    onClick={() => onChoice({ ...choice, tier: t })} onKeyDown={(ev) => onKey(ev, t)}>
                    {TIER_SHORT[t]}{rec.ltx_model === t && <span className="rec-tag">推荐</span>}
                  </button>
                );
              })}
            </div>
            <p className="plan-reason" id={`tier-reason-${id}`}>{rec.ltx_reason}</p>
          </div>
        ) : (
          <div className="plan-field"><span className="plan-label">画质</span><Chip tone="green">无需修复</Chip></div>
        )}
        <div className="plan-field">
          <label className="plan-label" htmlFor={`machine-${id}`}>设备</label>
          <select id={`machine-${id}`} value={choice.machine} aria-describedby={`machine-reason-${id}`}
            onChange={(ev) => onChoice({ ...choice, machine: ev.target.value })}>
            {options.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
          <p className="plan-reason" id={`machine-reason-${id}`}>{machineReason}</p>
          {rec.warning && <p className="plan-reason t-amber">{rec.warning}</p>}
        </div>
      </div>
      <div className="pending-foot">
        <button type="button" className="btn ghost small" data-action="detail" aria-describedby={tid} onClick={onDetail}>查看详情</button>
        <button type="button" className="btn primary small" data-action="confirm" aria-describedby={stale ? `stale-${id}` : tid}
          disabled={stale || busy || !ready} onClick={onConfirm}>确认入队</button>
      </div>
    </article>
  );
}
