# P3-6 — Dispatch queue: 自动 = first free machine, 指定 = wait on the controller (#305), plus #306

This is the frozen contract for Taskpaw issues #305 and #306, part of EPIC #271.

The operator approved the UI on 2026-10-24 in the preview canvas, board 「队列 · 派单状态」.

Binding rules:
- OD1–OD7 apply.
- Windows only.
- Home LAN, single user.
- 不要过度工程 (no over-engineering).

## 1. Operator report and intent

- **Report 1:** a film routed to a GPU worker shows 「待领取」 while it waits.
- **Report 2:** pinning a second film to the same machine shows 「设备队列已满」.
- **Intent for 自动:** whichever machine becomes free takes the film.
- **Intent for 指定:** the film stays on the controller until that machine is free.

## 2. Today (facts to locate and verify in code)

- `workflow/scheduler.py`:
  - `MAX_OPEN_ROUTES = 2` (one active route plus one prefetch per worker).
  - `QUEUE_FULL = "设备队列已满"` is shown when a worker already holds two open routes (around lines 581, 950 and 987–994).
  - 自动 picks the eligible worker with the earliest predicted finish and opens a route to it, possibly into its prefetch slot. A film can therefore be bound to a busy worker while another worker is idle.
- `ui/src/components/media/mediaModel.ts:23`: `inbox: ["待领取", "blue"]` is the phase-1 label for a queued, unstarted film. The board's waiting notes carry 「等待设备：…」 reasons.
- Routes, assignments and claims live in the store: `film_routes`, `route_store.py` and `worker_store.py` claim.

## 3. Requirements

**Q1 — one controller queue.**
- Confirmed films queue on the controller in confirm order, with no limit.
- No film is ever refused or labelled "full" because of how many films are pinned to a machine.

**Q2 — 自动.**
- A route opens only when an eligible worker has a **free active slot**, meaning it is not restoring and holds no unstarted route.
- That worker gets the first queued 自动 film it may take, judged by the existing eligibility: VRAM, 4K via the approved profile, tiers ready, disk, 核验 and so on.
- 自动 films are never prefetched and never bound early.
- When several workers are free at once, the existing earliest-finish / energy tie-break chooses among the free ones.

**Q3 — 指定 (pinned).**
- A pinned film waits on the controller until its worker can take it.
- While that worker is restoring, the next film pinned to it, in queue order, may take the worker's **single prefetch slot**: the route opens, the source downloads, and the film starts as soon as the current one finishes.
- Order on a worker: its pinned films first, in queue order, then 自动 films. 自动 films only ever use the active slot.

**Q4 — states and copy (board, queue, drawer, overview).**

| State | Copy |
|---|---|
| 自动, not yet routed | 「排队中（自动）」, note 「第 N 位 · 哪台先空给哪台」 (4K adds 「只给 16 GB 卡」 when relevant) |
| 指定, not yet routed | 「排队中 · 等 <机器>」, note 「第 N 位」 |
| Routed, downloading | 「下载到 <机器> · NN%」 |
| Routed and downloaded, waiting for the current film | 「已到 <机器> · 等开始」 |
| Running | 「修复中 · <机器>」 (unchanged otherwise) |

- N is the 1-based position among films waiting for the same thing: the 自动 queue, or the films pinned to that machine.
- Remove 「待领取」 and 「设备队列已满」 everywhere they reach the operator, including waiting notes and 待处理 device reasons. A device holding two routes is simply not offered as "free"; its reason stays factual, e.g. 「正在修复，下一部已排好」.
- When no eligible device exists, the existing reasons stay as they are (「4K 需重新核验」, 「离线」, …).

**Q5 — what must not change.**
- Claim, lease, renew and finish protocols.
- Attempt inventory.
- 失联 / 改派 / 判定已停止.
- Per-film 停止并重新排队.
- Local (single-machine) mode.
- The worker API.

This is scheduler policy plus copy only. Use no store migration unless unavoidable.

**P1 (#306) — 功耗 shows why a peer has no reading.**
- `core/power_fleet.py` already tells `PeerError("unauthorized")` (401/403) apart from network errors.
- Carry that reason to the 功耗 machine list:
  - unauthorized: 「令牌不一致：在 <机器> 的 设置 → 网络与访问 粘贴主控的访问令牌」
  - unreachable: 「连不上（检查网络 / 防火墙）」
- Show it in place of the bare 离线.
- Make no other power change.

## 4. Out of scope

- Queue reordering or drag-and-drop.
- Priorities.
- Prefetching more than one film.
- 自动 prefetch.
- Rebinding films that are already routed.
- Worker-side changes.
- INBOX loose-file wrapping (#307, backlog).

## 5. Verification

- TDD; scheduler tests use fakes. Cover:
  1. 自动 goes to whichever worker frees first, even if another was predicted earlier;
  2. 自动 never takes a prefetch slot;
  3. three films pinned to one worker: one running, one prefetched, one waiting 「第 1 位」, never "full";
  4. a pinned film never goes to another worker;
  5. mixed pinned and 自动 on one worker: pinned first;
  6. the state and copy mapping;
  7. no 「待领取」 or 「设备队列已满」 strings left in the UI or the board.
- #306: tests for the reason mapping.
- Run ruff, mypy ×3, the UI test/lint/tsc/build, and the full repo pytest.

## 6. Dispositions

| ID | Finding | Disposition |
|---|---|---|
