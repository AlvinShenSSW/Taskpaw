"""数据分析 read (EPIC #387 C2, design §6.4, §7) over a real test runtime.

Archived films are written straight into the runtime's temporary
`workflow.db` with the row shapes the analytics reads use (invented names
and numbers; foreign keys hold, so the file still opens). One real archive
end to end is in `test_workflow_ui_contract.py`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from taskpaw_v3.core import tasklog as tl
from taskpaw_v3.core.tasklog import TaskLog
from taskpaw_v3.tests.test_workflow_return_step import make_runtime
from taskpaw_v3.workflow import analytics as an
from taskpaw_v3.workflow.device_history import Span
from taskpaw_v3.workflow.store_core import timestamp

T0 = datetime(2026, 10, 24, tzinfo=timezone.utc)
H = 3600.0
NAS = "\\\\nas-a\\actors"


def z(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _id() -> str:
    return str(uuid.uuid4())


def _hash() -> str:
    return uuid.uuid4().hex * 2


@dataclass
class Try:
    """One attempt: offsets in seconds from T0; `end` None while running."""

    stage: str
    status: str = "succeeded"
    worker: str | None = None
    start: float = 0.0
    end: float | None = 60.0
    kind: str | None = None
    note: str | None = None
    energy: tuple[float, float] | None = None  # (wh, seconds)
    cues: int | None = None


@dataclass
class FilmSpec:
    code: str = "FAKE-001"
    created: float = 0.0
    accepted: float | None = 40 * H  # None: not archived
    size: tuple[int, int] | None = (1920, 1080)
    duration_ms: int | None = 7_260_000
    main_bytes: tuple[int, ...] = (5_000_000_000,)
    other_bytes: tuple[int, ...] = (2_000,)  # subtitles etc. (INBOX, not input)
    marks: tuple[str, ...] = ("chinese_subtitles",)
    scheme: str = "library_performer"
    relative: str = "FAKE-001"
    export: str = NAS + "\\Actor A"
    plan: tuple[float, bool] | None = None  # (confirmed offset, backfilled)
    removed: bool = False
    tries: list[Try] = field(default_factory=list)
    stages: dict[str, str] = field(default_factory=dict)  # status overrides


class Synth:
    """Writes films into a test runtime's workflow.db through its own
    connection, as the store would have left them."""

    def __init__(self, rt: Any):
        self.rt = rt
        self.ctrl = rt.identity.worker_id
        self.principal = rt.operator.principal_id
        self.conn = sqlite3.connect(rt.db_path, timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def bulk(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def worker(
        self,
        name: str,
        *,
        model: str | None = "NVIDIA GeForce RTX 5060 Ti",
        vram: int | None = 16311,
        boots: tuple[float, ...] = (),
        retired: bool = False,
        probes: tuple[tuple[str, int], ...] = (),
    ) -> str:
        """A GPU worker cloned from the controller's row; `probes` are older
        probes, the (model, vram) one is the latest."""
        row = self.conn.execute(
            "SELECT * FROM workers WHERE worker_id=?", (self.ctrl,)
        ).fetchone()
        cols = [d[1] for d in self.conn.execute("PRAGMA table_info(workers)")]
        values = dict(zip(cols, row))
        worker_id = _id()
        body = json.loads(values["body_json"])
        state = "retired" if retired else "verified"
        body.update(
            worker_id=worker_id,
            device_id=_id(),
            name=name,
            enrollment_state=state,
            capability=None,
            physical_binding=None,
        )
        values.update(
            worker_id=worker_id,
            device_id=body["device_id"],
            name=name,
            enrollment_state=state,
            retired_at=timestamp(T0) if retired else None,
            body_json=json.dumps(body),
        )
        self.conn.execute(
            f"INSERT INTO workers({','.join(values)}) VALUES "
            f"({','.join('?' for _ in values)})",
            tuple(values.values()),
        )
        latest = () if model is None else ((model, vram),)
        for revision, (gpu, mib) in enumerate((*probes, *latest), start=1):
            self.conn.execute(
                "INSERT INTO capabilities(worker_id,kind,revision,created_at,body_json)"
                " VALUES (?,?,?,?,?)",
                (
                    worker_id,
                    "probed",
                    revision,
                    timestamp(T0),
                    json.dumps({"gpu_model": gpu, "vram_mib": mib}),
                ),
            )
        for seconds in boots:
            self.conn.execute(
                "INSERT INTO worker_boots(worker_id,boot_id,observed_at,body_json)"
                " VALUES (?,?,?,'{}')",
                (worker_id, _id(), timestamp(T0 + timedelta(seconds=seconds))),
            )
        return worker_id

    def film(self, spec: FilmSpec | None = None, **kw: Any) -> str:
        spec = spec or FilmSpec(**kw)
        c, film_id = self.conn, _id()
        at = lambda s: timestamp(T0 + timedelta(seconds=s))  # noqa: E731
        body = {"removed_at": at(spec.accepted or 0) if spec.removed else None}
        c.execute(
            "INSERT INTO films(film_id,intake_session_id,display_name,policy_revision,"
            "current_input_revision,current_final_revision,created_at,body_json)"
            " VALUES (?,?,?,1,1,?,?,?)",
            (
                film_id,
                _id(),
                spec.code,
                1 if spec.accepted is not None else None,
                at(spec.created),
                json.dumps(body),
            ),
        )
        probes = (
            [
                {
                    "width": spec.size[0],
                    "height": spec.size[1],
                    "duration_ms": spec.duration_ms,
                }
            ]
            if spec.size
            else []
        )
        c.execute(
            "INSERT INTO manifests(film_id,kind,revision,manifest_hash,created_at,body_json)"
            " VALUES (?,'input',1,?,?,?)",
            (
                film_id,
                _hash(),
                at(spec.created),
                json.dumps({"readiness": {"media_probes": probes}}),
            ),
        )
        items = [("main_video", n) for n in spec.main_bytes] + [
            ("source_subtitle", n) for n in spec.other_bytes
        ]
        for n, (role, size) in enumerate(items):
            c.execute(
                "INSERT INTO manifest_items(film_id,kind,revision,item_id,role,"
                "relative_path,path_key,size_bytes,sha256,source_root_id,body_json)"
                " VALUES (?,'input',1,?,?,?,?,?,?,?,'{}')",
                (film_id, _id(), role, f"f{n}", f"f{n}", size, _hash(), "root"),
            )
        if spec.plan is not None:
            c.execute(
                "INSERT INTO film_plans(film_id,input_revision,ltx_model,confirmed_at,"
                "backfilled) VALUES (?,1,'standard',?,?)",
                (film_id, at(spec.plan[0]), int(spec.plan[1])),
            )
        by_stage: dict[str, list[Try]] = {}
        for t in spec.tries:
            by_stage.setdefault(t.stage, []).append(t)
        for stage in (*by_stage, *(s for s in spec.stages if s not in by_stage)):
            tries = by_stage.get(stage, [])
            status = spec.stages.get(stage) or (
                "succeeded"
                if any(t.status == "succeeded" for t in tries)
                else tries[-1].status
            )
            job_id = _id()
            c.execute(
                "INSERT INTO stages(job_id,film_id,input_revision,input_hash,stage,"
                "status,body_json) VALUES (?,?,1,?,?,?,'{}')",
                (job_id, film_id, _hash(), stage, status),
            )
            for number, t in enumerate(tries, start=1):
                kind = t.kind or (
                    "exec_succeeded"
                    if t.status == "succeeded"
                    else "exec_failed:needs_attention"
                )
                summary: dict[str, Any] = {"note": t.note, "item_count": t.cues}
                if t.energy is not None:
                    summary["energy"] = {"wh": t.energy[0], "seconds": t.energy[1]}
                evidence = [{"kind": "claimed", "summary": {}}] if number % 2 else []
                evidence.append({"kind": kind, "summary": summary})
                c.execute(
                    "INSERT INTO attempts(attempt_id,job_id,attempt_number,"
                    "owner_principal_id,worker_id,boot_id,epoch,fence,input_hash,status,"
                    "started_at,ended_at,body_json) VALUES (?,?,?,?,?,'b',1,1,?,?,?,?,?)",
                    (
                        _id(),
                        job_id,
                        number,
                        self.principal,
                        t.worker,
                        _hash(),
                        t.status,
                        at(t.start),
                        None if t.end is None else at(t.end),
                        json.dumps({"evidence": evidence}),
                    ),
                )
        if spec.accepted is not None:
            final = _hash()
            c.execute(
                "INSERT INTO manifests(film_id,kind,revision,manifest_hash,input_revision,"
                "created_at,body_json) VALUES (?,'final',1,?,1,?,?)",
                (
                    film_id,
                    final,
                    at(spec.accepted),
                    json.dumps({"postprocess": {"actual_marks": list(spec.marks)}}),
                ),
            )
            self.archive(film_id, final, spec)
        return film_id

    def archive(self, film_id: str, final: str, spec: FilmSpec) -> str:
        operation_id = _id()
        at = timestamp(T0 + timedelta(seconds=spec.accepted or 0))
        self.conn.execute(
            "INSERT INTO operations(operation_id,film_id,kind,owner_principal_id,status,"
            "created_at,updated_at,resolved_at,body_json)"
            " VALUES (?,?,'archive',?,'resolved',?,?,?,'{}')",
            (operation_id, film_id, self.principal, at, at, at),
        )
        target = {
            "export_identity": spec.export,
            "relative_path": spec.relative,
            "native_identity": {"scheme": spec.scheme},
        }
        self.conn.execute(
            "INSERT INTO receipts(receipt_id,kind,operation_id,producer_id,manifest_hash,"
            "accepted_at,body_json) VALUES (?,'archive',?,?,?,?,?)",
            (
                _id(),
                operation_id,
                self.principal,
                final,
                at,
                json.dumps({"target": target}),
            ),
        )
        return operation_id


def standard(worker: str | None, start: float = 2 * H) -> list[Try]:
    """A run that went through every fact stage at the first try."""
    return [
        Try(
            "restore",
            worker=worker,
            start=start,
            end=start + 4 * H,
            energy=(800.0, 14400.0),
        ),
        Try(
            "asr",
            start=start + 4 * H,
            end=start + 4 * H + 540,
            energy=(20.0, 540.0),
            note="字幕来源：generated_ja 1",
        ),
        Try(
            "translate",
            start=start + 5 * H,
            end=start + 5 * H + 52,
            energy=(1.5, 52.0),
            note="翻译完成：1 卷",
            cues=1834,
        ),
        Try("return", start=start + 6 * H, end=start + 6 * H + 30, energy=(0.5, 30.0)),
        Try(
            "scrape", start=start + 7 * H, end=start + 7 * H + 178, energy=(1.0, 178.0)
        ),
        Try("archive", start=start + 8 * H, end=start + 8 * H + 40),
    ]


@pytest.fixture
def world(tmp_path):
    rt = make_runtime(tmp_path / "wf")
    synth = Synth(rt)
    try:
        yield rt, synth
    finally:
        synth.close()
        rt.close()


def read(rt, cache=None, **kw) -> dict[str, Any]:
    return an.build(rt, cache or an.AnalyticsFacts(), **{"since": None, **kw})


# ── film facts (§7, §7.1, D4) ───────────────────────────────────────────
def test_a_film_fact_follows_the_field_rules(world):
    rt, s = world
    gpu = s.worker("Snowleopard")
    s.film(
        code="FAKE-001",
        created=0,
        accepted=40 * H,
        size=(3840, 2160),
        main_bytes=(4_000_000_000, 1_000_000_000),
        marks=("chinese_subtitles", "4k"),
        plan=(1 * H, False),
        tries=standard(gpu),
    )
    (film,) = read(rt)["films"]
    assert film == {
        "code": "FAKE-001",
        "archived_at": z(40 * H),
        "created_at": z(0),
        "res": "4K",
        "duration_s": 7260,
        "input_bytes": 5_000_000_000,
        "worker": gpu,
        "queue_s": 3600,
        "restore_s": 14400,
        "asr_s": 540,
        "translate_s": 52,
        "translated": True,
        "cues": 1834,
        "subtitles": {"generated_ja": 1},
        "scrape_s": 178,
        "archive_s": 40,
        "lead_s": 144000,
        "restore_wh": [[gpu, 800.0, 14400.0]],
        "controller_wh": 23.0,
        "energy_partial": False,
        "target": "performer",
        "actor": "Actor A",
        "second": False,
        "marks": ["chinese_subtitles", "4k"],
    }


@pytest.mark.parametrize(
    "plan,expected",
    [
        ((1 * H, False), 3600),  # confirmed → the restore's first attempt start
        ((1 * H, True), None),  # a backfilled plan is no operator choice
        (None, None),  # no plan
        ((3 * H, False), None),  # confirmed after the start: no wait
    ],
)
def test_the_queue_wait_needs_a_confirmed_plan(world, plan, expected):
    rt, s = world
    tries = standard(None)
    tries.insert(0, Try("restore", "failed", start=2 * H, end=2.5 * H))
    tries[1].start = 3 * H  # the retry starts later; the wait ends at the first
    s.film(plan=plan, tries=tries)
    assert read(rt)["films"][0]["queue_s"] == expected


@pytest.mark.parametrize(
    "note,expected",
    [
        ("修复完成：0 卷修复，2 卷复用", None),  # every volume reused: no restore
        ("修复完成：1 卷修复，1 卷复用", "applied"),
        (None, "applied"),  # a note this build does not parse: a restore
    ],
)
def test_an_all_reused_restore_counts_as_no_restore(world, note, expected):
    rt, s = world
    gpu = s.worker("Alpha")
    tries = standard(gpu)
    tries[0].note = note
    tries[0].energy = (3.0, 20.0)
    s.film(plan=(1 * H, False), tries=tries)
    (film,) = read(rt)["films"]
    restore = (film["worker"], film["restore_s"], film["queue_s"])
    if expected is None:
        assert restore == (None, None, None)
    else:
        assert restore == (gpu, 14400, 3600)
    # what the attempt drew is still the machine's energy
    assert film["restore_wh"] == [[gpu, 3.0, 20.0]]


def test_absent_stages_are_null_never_zero(world):
    rt, s = world
    s.film(
        size=None,
        duration_ms=None,
        tries=[
            Try("asr", note="字幕来源：reused_zh 1"),
            Try("translate", note="无需翻译"),
        ],
        plan=(0, False),
    )
    (film,) = read(rt)["films"]
    assert (film["res"], film["duration_s"], film["worker"]) == (None, None, None)
    assert (film["restore_s"], film["queue_s"], film["scrape_s"]) == (None, None, None)
    assert (film["translated"], film["cues"]) == (False, None)
    assert film["subtitles"] == {"reused_zh": 1}
    assert film["restore_wh"] == [] and film["controller_wh"] is None
    assert film["energy_partial"] is True  # resolved attempts without energy


def test_energy_per_worker_includes_failed_attempts_and_marks_partial(world):
    rt, s = world
    a, b = s.worker("Alpha"), s.worker("Bravo")
    tries = [
        Try("restore", "failed", worker=a, start=0, end=H, energy=(100.0, 3600.0)),
        Try("restore", "failed", worker=b, start=H, end=2 * H, energy=(50.0, 1800.0)),
        Try("restore", worker=b, start=2 * H, end=3 * H, energy=(150.0, 3600.0)),
        Try("asr", start=3 * H, end=4 * H),  # crashed sampler: no energy
        Try(
            "translate",
            start=4 * H,
            end=5 * H,
            energy=(2.0, 60.0),
            note="翻译完成：1 卷",
        ),
    ]
    s.film(tries=tries)
    (film,) = read(rt)["films"]
    assert film["worker"] == b
    assert film["restore_wh"] == [[a, 100.0, 3600.0], [b, 200.0, 5400.0]]
    assert film["controller_wh"] == 2.0
    assert film["energy_partial"] is True


def test_a_running_attempt_is_not_partial(world):
    rt, s = world
    tries = [*standard(None), Try("scrape", "running", start=20 * H, end=None)]
    s.film(tries=tries)
    assert read(rt)["films"][0]["energy_partial"] is False


@pytest.mark.parametrize(
    "tries,expected",
    [
        ([Try("translate", note="翻译完成：2 卷")], True),
        ([Try("translate", note="无需翻译")], False),
        ([Try("translate", note=None)], None),  # a note this build does not know
        ([Try("translate", "failed", note="翻译完成：2 卷")], None),  # no success
        ([], None),
    ],
)
def test_translated_follows_the_translate_success_note(world, tries, expected):
    rt, s = world
    s.film(tries=tries)
    assert read(rt)["films"][0]["translated"] is expected


class _Library:
    """`LibraryStore.reading()` over an in-memory library with the two
    tables the facts read."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.execute("CREATE TABLE archive_tasks (operation_id, receipt_json)")
        self.conn.execute("CREATE TABLE performers (path, name)")
        self.reads = 0

    @contextmanager
    def reading(self):
        self.reads += 1
        yield self.conn


def test_target_kinds_actor_labels_and_the_second_target(world):
    rt, s = world
    library = _Library()
    library.conn.execute(
        "INSERT INTO performers VALUES (?,?)", (NAS + "\\Actor A\\", "示例演员")
    )
    cases = [  # scheme, export identity, relative path → kind, actor, second
        (
            "library_performer",
            NAS + "\\Actor A",
            "FAKE-0",
            "performer",
            "示例演员",
            True,
        ),
        (
            "export_object_id",
            NAS + "\\Actor B",
            "FAKE-1",
            "performer",
            "Actor B",
            False,
        ),
        (
            "library_new_actor",
            NAS + "\\New Face",
            "FAKE-2",
            "new_actor",
            "New Face",
            False,
        ),
        ("library_category", NAS + "\\Category", "FAKE-3", "category", None, False),
        ("manual_directory", "D:\\Mine", "FAKE-4", "manual", None, False),
    ]
    for n, (scheme, export, relative, *_) in enumerate(cases):
        s.film(
            code=f"FAKE-{n}",
            scheme=scheme,
            relative=relative,
            export=export,
            accepted=(40 + n) * H,
            tries=standard(None),
        )
    receipts = rt.store.analytics_receipts(rt.operator, 0, 50)
    library.conn.execute(  # the engine's own receipt names both target paths
        "INSERT INTO archive_tasks VALUES (?,?)",
        (
            receipts[0]["operation_id"],
            json.dumps(
                {
                    "target_path": NAS + "\\actor a\\FAKE-0",
                    "second": {"target_path": "x"},
                }
            ),
        ),
    )
    films = read(rt, library=library)["films"]
    assert [(f["target"], f["actor"], f["second"]) for f in films] == [
        (kind, actor, second) for *_, kind, actor, second in cases
    ]
    assert library.reads == 1  # one library read for the batch


def test_without_the_library_the_actor_comes_from_the_target(world):
    rt, s = world
    s.film(export=NAS + "\\Actor B", relative="FAKE-9", tries=standard(None))
    (film,) = read(rt, library=None)["films"]
    assert (film["actor"], film["second"]) == ("Actor B", False)


def test_a_removed_film_still_counts_and_since_filters_receipts(world):
    rt, s = world
    s.film(code="OLD-1", accepted=10 * H, tries=standard(None))
    s.film(code="GONE-1", accepted=50 * H, removed=True, tries=standard(None))
    body = read(rt, since=T0 + timedelta(seconds=20 * H))
    assert [f["code"] for f in body["films"]] == ["GONE-1"]
    assert body["since"] == z(20 * H)
    assert body["coverage"]["archives"] == z(10 * H)  # all history, not since
    assert [f["code"] for f in read(rt)["films"]] == ["OLD-1", "GONE-1"]


def test_a_film_not_archived_has_no_fact(world):
    rt, s = world
    s.film(accepted=None, tries=standard(None))
    assert read(rt)["films"] == []


def test_coverage_of_energy_and_cues(world):
    rt, s = world
    old = standard(None, start=0)
    for t in old:
        t.energy, t.cues = None, None
    s.film(code="A", accepted=20 * H, tries=old)
    s.film(code="B", accepted=60 * H, tries=standard(None, start=30 * H))
    coverage = read(rt)["coverage"]
    assert coverage["archives"] == z(20 * H)
    assert coverage["energy"] == z(34 * H)  # B's restore end
    assert coverage["cues"] == z(35 * H + 52)  # B's translate end
    assert coverage["devices"] is None and coverage["outages"] is None


# ── the per-receipt cache (§6.4, AC5) ───────────────────────────────────
class Spy:
    def __init__(self, rt, delay: float = 0.0):
        self.real = rt.store.analytics_receipts
        self.calls: list[tuple[int, int, int]] = []  # (after, limit, rows)
        self.delay = delay
        rt.store.analytics_receipts = self

    def __call__(self, auth, after, limit):
        if self.delay:
            time.sleep(self.delay)
        rows = self.real(auth, after, limit)
        self.calls.append((after, limit, len(rows)))
        return rows


def test_new_receipts_are_read_without_recomputing_old_facts(world):
    rt, s = world
    s.film(code="A", accepted=10 * H, tries=standard(None))
    spy, cache = Spy(rt), an.AnalyticsFacts()
    assert [f["code"] for f in read(rt, cache)["films"]] == ["A"]
    assert spy.calls == [(0, 200, 1)]
    read(rt, cache)
    after = spy.calls[0][0]
    assert spy.calls[1][0] > after and spy.calls[1][2] == 0  # no fact read
    s.film(code="B", accepted=20 * H, tries=standard(None))
    assert [f["code"] for f in read(rt, cache)["films"]] == ["A", "B"]
    assert spy.calls[-1][2] == 1  # only the new receipt


def test_the_cold_build_reads_at_most_200_receipts_per_locked_read(world):
    rt, s = world
    with s.bulk():
        for n in range(450):
            s.film(code=f"F-{n:03}", accepted=10 * H + n, tries=standard(None))
    spy = Spy(rt)
    body = read(rt)
    assert len(body["films"]) == 450
    assert [(limit, rows) for _, limit, rows in spy.calls] == [
        (200, 200),
        (200, 200),
        (200, 50),
    ]
    assert [f["code"] for f in body["films"]] == [f"F-{n:03}" for n in range(450)]


def test_concurrent_cold_requests_read_each_batch_once(world):
    rt, s = world
    with s.bulk():
        for n in range(250):
            s.film(code=f"F-{n:03}", accepted=10 * H + n, tries=standard(None))
    spy, cache = Spy(rt, delay=0.05), an.AnalyticsFacts()
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(len(read(rt, cache)["films"])))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert results == [250, 250, 250]
    fact_reads = [after for after, _, rows in spy.calls if rows]
    assert len(fact_reads) == len(set(fact_reads)) == 2


def test_a_batch_that_fails_to_build_is_read_again_whole(world, monkeypatch):
    rt, s = world
    for n in range(3):
        s.film(code=f"F{n}", accepted=(10 + n) * H, tries=standard(None))
    cache, real, calls = an.AnalyticsFacts(), an.build_fact, []

    def flaky(row, *args):
        calls.append(row["display_name"])
        if len(calls) == 2:
            raise ValueError("unexpected row")
        return real(row, *args)

    monkeypatch.setattr(an, "build_fact", flaky)
    with pytest.raises(ValueError):
        read(rt, cache)
    assert [f["code"] for f in read(rt, cache)["films"]] == ["F0", "F1", "F2"]


def test_the_cache_starts_over_for_another_store(world, tmp_path):
    rt, s = world
    s.film(code="A", tries=standard(None))
    cache = an.AnalyticsFacts()
    assert len(read(rt, cache)["films"]) == 1
    other = make_runtime(tmp_path / "other")
    try:
        assert read(other, cache)["films"] == []
    finally:
        other.close()


# ── subtitles and GPU labels ────────────────────────────────────────────
@pytest.mark.parametrize(
    "note,expected",
    [
        ("字幕来源：generated_ja 1", {"generated_ja": 1}),
        ("字幕来源：reused_zh 1", {"reused_zh": 1}),
        ("字幕来源：no_speech 1", {"no_speech": 1}),
        ("字幕来源：generated_ja 2，no_speech 1", {"generated_ja": 2, "no_speech": 1}),
        ("字幕来源：", {}),
        ("字幕来源：whisper 1", {}),
        ("字幕来源：generated_ja x", {}),
        ("something else", {}),
        (None, {}),
    ],
)
def test_subtitles_parse_the_asr_note(note, expected):
    assert an.subtitles(note) == expected


@pytest.mark.parametrize(
    "model,vram,expected",
    [
        ("NVIDIA GeForce RTX 5060 Ti", 16311, "RTX 5060 Ti 16GB"),
        ("NVIDIA GeForce RTX 5060", 8151, "RTX 5060 8GB"),
        ("NVIDIA GeForce RTX 5070 Ti", 16303, "RTX 5070 Ti 16GB"),
        ("Some GPU", None, "Some GPU"),
        (None, 8151, None),
        ("", 8151, None),
    ],
)
def test_gpu_label(model, vram, expected):
    assert an.gpu_label(model, vram) == expected


def test_the_outage_task_is_the_workflow_translator():
    from taskpaw_v3.workflow.exec_translate import default_translator

    translator = default_translator(SimpleNamespace(), Path("unused"))
    assert translator._name == an.TRANSLATE_TASK


# ── failures (§7.2, §7.3, AC6) ──────────────────────────────────────────
@pytest.mark.parametrize(
    "kind,stage,note,expected",
    [
        ("exec_interrupted", "restore", "上一次运行被中断", ("interrupted", "warn")),
        ("exec_failed:cancelled", "restore", "已停止", ("stopped", "warn")),
        (
            "exec_failed:transient",
            "translate",
            "翻译暂停：服务不可用",
            ("translate_paused", "warn"),
        ),
        ("exec_failed:transient", "translate", "翻译失败：超时", ("transient", "warn")),
        (
            "exec_failed:transient",
            "restore",
            "[WinError 4551] x",
            ("transient", "warn"),
        ),
        ("exec_failed:machine", "restore", "worker fault", ("machine_fault", "fail")),
        (
            "exec_failed:needs_attention",
            "restore",
            "[WinError 4551] 应用程序控制",
            ("machine_fault", "fail"),
        ),
        (
            "exec_failed:needs_attention",
            "restore",
            "缺少 LTX 模型文件：x",
            ("missing_model", "fail"),
        ),
        (
            "exec_failed:needs_attention",
            "restore",
            "找不到 ffprobe",
            ("missing_ffprobe", "fail"),
        ),
        (
            "exec_failed:needs_attention",
            "restore",
            "Jasna LTX 失败（退出码 1）",
            ("ltx_failed", "fail"),
        ),
        (
            "exec_failed:needs_attention",
            "scrape",
            "白名单以外的文件",
            ("scrape_whitelist", "fail"),
        ),
        (
            "exec_failed:needs_attention",
            "restore",
            "白名单以外的文件",
            ("other", "fail"),
        ),
        ("exec_failed:permission", "asr", "拒绝访问", ("other", "fail")),
        ("", "restore", "", ("other", "fail")),
    ],
)
def test_failure_types_by_last_kind_then_note(kind, stage, note, expected):
    assert an.failure_type(kind, stage, note) == expected


def _failures(rt, **kw) -> list[dict[str, Any]]:
    return read(rt, **kw)["failures"]


def test_failure_rows_and_worker_less_stages(world):
    rt, s = world
    gpu = s.worker("Alpha")
    s.film(
        code="FAIL-1",
        accepted=None,
        tries=[
            Try(
                "restore",
                "failed",
                worker=gpu,
                start=0,
                end=H,
                kind="exec_failed:machine",
                note="x" * 300,
            ),
            Try("scrape", "failed", start=2 * H, end=3 * H, note="白名单以外的文件"),
        ],
    )
    rows = _failures(rt)
    assert rows[0] == {
        "at": z(H),
        "code": "FAIL-1",
        "stage": "restore",
        "machine": gpu,
        "severity": "fail",
        "type": "machine_fault",
        "exhausted": False,
        "note": "x" * 200,
        "outcome": "needs_action",
    }
    assert rows[1]["machine"] == rt.identity.worker_id  # worker-less: controller
    assert rows[1]["type"] == "scrape_whitelist"


def test_since_filters_failures_by_end(world):
    rt, s = world
    s.film(
        accepted=None,
        tries=[
            Try("restore", "failed", start=0, end=H, note="a"),
            Try("restore", "failed", start=2 * H, end=3 * H, note="b"),
        ],
    )
    rows = _failures(rt, since=T0 + timedelta(seconds=2 * H))
    assert [r["note"] for r in rows] == ["b"]


def test_failures_are_read_a_range_of_attempts_per_locked_read(world, monkeypatch):
    rt, s = world
    gpu = s.worker("Alpha")
    for n in range(3):  # each: a failure, then a success on another machine
        s.film(
            code=f"F{n}",
            accepted=None,
            tries=[
                Try(
                    "restore",
                    "failed",
                    worker=gpu,
                    start=n * H,
                    end=n * H + 60,
                    note=f"n{n}",
                ),
                Try("restore", start=n * H + 120, end=n * H + 180),
            ],
        )
    whole = _failures(rt)
    assert [(r["note"], r["outcome"]) for r in whole] == [
        ("n0", "reassigned"),
        ("n1", "reassigned"),
        ("n2", "reassigned"),
    ]
    calls: list[tuple[int, int, int, bool]] = []
    real = rt.store.analytics_attempts

    def spy(auth, after, span, recheck=()):
        rows, reached, done = real(auth, after, span, recheck)
        calls.append((after, reached, len(rows), done))
        return rows, reached, done

    monkeypatch.setattr(rt.store, "analytics_attempts", spy)
    monkeypatch.setattr(an, "FAILURE_SPAN", 2)  # the success sits in the next range
    assert _failures(rt) == whole  # a fresh cache: read range by range
    assert calls == [(0, 2, 1, False), (2, 4, 1, False), (4, 6, 1, True)]


class AttemptSpy:
    def __init__(self, rt, monkeypatch):
        self.calls: list[tuple[int, list[int], int]] = []
        real = rt.store.analytics_attempts

        def spy(auth, after, span, recheck=()):
            rows, reached, done = real(auth, after, span, recheck)
            self.calls.append((after, list(recheck), len(rows)))
            return rows, reached, done

        monkeypatch.setattr(rt.store, "analytics_attempts", spy)


def _set_attempt(s: Synth, film_code: str, status: str, kind: str, end: float):
    """What the store does when an attempt resolves (`_set_attempt`)."""
    body = json.dumps({"evidence": [{"kind": kind, "summary": {"note": "late"}}]})
    s.conn.execute(
        "UPDATE attempts SET status=?,ended_at=?,body_json=? WHERE job_id IN "
        "(SELECT s.job_id FROM stages s JOIN films f ON f.film_id=s.film_id "
        "WHERE f.display_name=?) AND status='running'",
        (status, timestamp(T0 + timedelta(seconds=end)), body, film_code),
    )


def test_failures_are_kept_and_only_new_or_running_attempts_read_again(
    world, monkeypatch
):
    rt, s = world
    s.film(
        code="DONE",
        accepted=None,
        tries=[Try("restore", "failed", start=0, end=60, note="first")],
    )
    s.film(
        code="RUN",
        accepted=None,
        tries=[Try("restore", "running", start=0, end=None)],
        stages={"restore": "running"},
    )
    spy, cache = AttemptSpy(rt, monkeypatch), an.AnalyticsFacts()
    assert [r["code"] for r in read(rt, cache)["failures"]] == ["DONE"]
    assert spy.calls == [(0, [], 2)]  # the failure and the running attempt
    read(rt, cache)
    assert spy.calls[-1] == (2, [2], 1)  # nothing new; the running one again
    # the running attempt fails in place: picked up by its rowid
    _set_attempt(s, "RUN", "failed", "exec_interrupted", 2 * H)
    rows = read(rt, cache)["failures"]
    assert [(r["code"], r["type"], r["at"]) for r in rows] == [
        ("DONE", "other", z(60)),
        ("RUN", "interrupted", z(2 * H)),
    ]
    read(rt, cache)
    assert spy.calls[-1] == (2, [], 0)  # settled: nothing is read again


def test_a_cached_failure_shows_its_outcome_as_it_is_now(world):
    rt, s = world
    s.film(
        code="F",
        accepted=None,
        tries=[
            Try("restore", "failed", start=0, end=60),
            Try("restore", "running", start=120, end=None),
        ],
        stages={"restore": "running"},
    )
    cache = an.AnalyticsFacts()
    assert [r["outcome"] for r in read(rt, cache)["failures"]] == ["retrying"]
    _set_attempt(s, "F", "succeeded", "exec_succeeded", 600)
    s.conn.execute("UPDATE stages SET status='succeeded' WHERE stage='restore'")
    assert [r["outcome"] for r in read(rt, cache)["failures"]] == ["retried_ok"]


@pytest.mark.parametrize(
    "tries,stage_status,expected",
    [
        # the stage later succeeded on the same machine / another one
        ([("failed", "A"), ("succeeded", "A")], None, ["retried_ok"]),
        ([("failed", "A"), ("succeeded", "B")], None, ["reassigned"]),
        ([("failed", None), ("succeeded", None)], None, ["retried_ok"]),
        # still going: retrying; needing the operator: needs_action
        ([("failed", "A")], "pending", ["retrying"]),
        ([("failed", "A"), ("running", "B")], "running", ["retrying"]),
        ([("failed", "A")], "blocked", ["needs_action"]),
        ([("failed", "A")], "unknown", ["needs_action"]),
        # R3-D1: an earlier attempt of a failed stage needs the operator too
        (
            [("failed", "A"), ("failed", "B")],
            "failed",
            ["needs_action", "needs_action"],
        ),
    ],
)
def test_outcome_of_a_failure(world, tries, stage_status, expected):
    rt, s = world
    workers = {"A": s.worker("Alpha"), "B": s.worker("Bravo"), None: None}
    s.film(
        accepted=None,
        tries=[
            Try(
                "restore",
                status,
                worker=workers[w],
                start=n * H,
                end=None if status == "running" else n * H + 60,
            )
            for n, (status, w) in enumerate(tries)
        ],
        stages={"restore": stage_status} if stage_status else {},
    )
    assert [r["outcome"] for r in _failures(rt)] == expected


def _transients(note: str, n: int, *, last_ok: bool | None) -> list[Try]:
    tries = [
        Try(
            "translate",
            "failed",
            start=k * H,
            end=k * H + 60,
            kind="exec_failed:transient",
            note=note,
        )
        for k in range(n)
    ]
    if last_ok is None:
        tries.append(Try("translate", "running", start=n * H, end=None))
    elif last_ok:
        tries.append(
            Try("translate", start=n * H, end=n * H + 60, note="翻译完成：1 卷")
        )
    return tries


@pytest.mark.parametrize(
    "note,type_",
    [("翻译失败：超时", "transient"), ("翻译暂停：服务不可用", "translate_paused")],
)
def test_three_transient_attempts_exhaust_the_retries(tmp_path, note, type_):
    def rows_for(tries, stage_status=None):
        rt = make_runtime(tmp_path / _id())
        s = Synth(rt)
        try:
            s.film(
                accepted=None,
                tries=tries,
                stages={"translate": stage_status} if stage_status else {},
            )
            return [
                (r["type"], r["severity"], r["exhausted"], r["outcome"])
                for r in _failures(rt)
            ]
        finally:
            s.close()
            rt.close()

    # while the third try runs, the first two are retrying warnings
    assert (
        rows_for(_transients(note, 2, last_ok=None), "running")
        == [(type_, "warn", False, "retrying")] * 2
    )
    # the third failed too and the stage is left failed: retries used up
    assert rows_for(_transients(note, 3, last_ok=False)) == [
        (type_, "warn", False, "needs_action"),
        (type_, "warn", False, "needs_action"),
        (type_, "fail", True, "needs_action"),
    ]
    # recovered: all three stay warnings
    assert (
        rows_for(_transients(note, 3, last_ok=True))
        == [(type_, "warn", False, "retried_ok")] * 3
    )


def test_a_retried_transient_followed_by_a_real_failure(world):
    """R3-D1: transient auto-retried, the retry fails needing attention."""
    rt, s = world
    s.film(
        accepted=None,
        tries=[
            Try(
                "asr",
                "failed",
                start=0,
                end=60,
                kind="exec_failed:transient",
                note="held",
            ),
            Try("asr", "failed", start=H, end=H + 60, note="ASR 失败"),
        ],
    )
    assert [
        (r["type"], r["severity"], r["exhausted"], r["outcome"]) for r in _failures(rt)
    ] == [
        ("transient", "warn", False, "needs_action"),
        ("other", "fail", False, "needs_action"),
    ]


def test_warnings_of_interrupted_and_stopped_attempts(world):
    rt, s = world
    s.film(
        accepted=None,
        tries=[
            Try(
                "restore",
                "failed",
                start=0,
                end=60,
                kind="exec_interrupted",
                note="上一次运行被中断",
            ),
            Try(
                "restore",
                "failed",
                start=H,
                end=H + 60,
                kind="exec_failed:cancelled",
                note="已停止",
            ),
        ],
        stages={"restore": "failed"},
    )
    assert [(r["type"], r["severity"], r["exhausted"]) for r in _failures(rt)] == [
        ("interrupted", "warn", False),
        ("stopped", "warn", False),
    ]


# ── machines (AC7) ──────────────────────────────────────────────────────
def test_machines_models_first_seen_retired_and_state_now(world):
    rt, s = world
    a = s.worker(
        "Alpha",
        boots=(5 * H, 2 * H, 9 * H),
        probes=(("NVIDIA GeForce RTX 3060", 12288),),
    )
    b = s.worker("Bravo", model="NVIDIA GeForce RTX 5070 Ti", vram=16303)
    old = s.worker("Old", retired=True)
    s.worker("Gone", retired=True)  # never referenced: left out
    s.worker("Fresh", model=None)
    s.film(tries=standard(old))  # the retired machine restored this one
    body = read(rt)
    by_name = {m["name"]: m for m in body["machines"]}
    assert set(by_name) == {"本机", "Alpha", "Bravo", "Old", "Fresh"}
    assert by_name["本机"]["kind"] == "controller"
    assert by_name["本机"]["id"] == rt.identity.worker_id
    assert by_name["本机"]["model"] == "RTX 5060 8GB"  # the runtime's own probe
    assert by_name["Alpha"] == {
        "id": a,
        "name": "Alpha",
        "kind": "gpu",
        "model": "RTX 5060 Ti 16GB",  # the latest probe, not the older one
        "first_seen": z(2 * H),
        "retired": False,
        "state_now": "off",  # remote workers off: no FleetLive
        "state_reason": "",
        "state_since": None,
    }
    assert by_name["Bravo"]["model"] == "RTX 5070 Ti 16GB"
    assert by_name["Bravo"]["first_seen"] is None
    assert by_name["Old"]["retired"] is True and by_name["Old"]["id"] == old
    assert by_name["Fresh"]["model"] is None
    assert by_name["本机"]["state_now"] == "online"
    paused = {m["name"]: m for m in read(rt, dispatcher_paused=True)["machines"]}
    assert (paused["本机"]["state_now"], paused["本机"]["state_reason"]) == (
        "paused",
        "global",
    )
    assert b in {m["id"] for m in body["machines"]}


def test_machines_referenced_by_a_failure_or_span(world):
    rt, s = world
    by_failure = s.worker("ByFailure", retired=True)
    by_span = s.worker("BySpan", retired=True)
    s.film(accepted=None, tries=[Try("restore", "failed", worker=by_failure, end=60)])
    spans = [Span(by_span, "offline", "", z(0), z(60), False)]
    ids = {m["id"] for m in read(rt, device_history=Recorder(spans))["machines"]}
    assert {by_failure, by_span} <= ids


class Recorder:
    """`DeviceHistory`'s read surface (C1)."""

    def __init__(self, spans=(), *, available=True, reason=""):
        self._spans, self.available, self.reason = list(spans), available, reason
        self.since: list[Any] = []

    def spans(self, since):
        self.since.append(since)
        return list(self._spans)

    def coverage_start(self):
        return min((sp.start for sp in self._spans), default=None)


def test_devices_come_from_the_recorder(world):
    rt, s = world
    ctrl = rt.identity.worker_id
    since = T0 + timedelta(seconds=H)
    recorder = Recorder(
        [
            Span(ctrl, "online", "", z(0), z(2 * H), False),
            Span(ctrl, "paused", "global", z(2 * H), z(3 * H), True),
        ]
    )
    body = read(rt, device_history=recorder, since=since)
    assert recorder.since == [since]
    assert body["devices"] == {
        "available": True,
        "reason": "",
        "spans": [
            {
                "machine": ctrl,
                "state": "online",
                "reason": "",
                "start": z(0),
                "end": z(2 * H),
                "open": False,
            },
            {
                "machine": ctrl,
                "state": "paused",
                "reason": "global",
                "start": z(2 * H),
                "end": z(3 * H),
                "open": True,
            },
        ],
    }
    assert body["coverage"]["devices"] == z(0)
    ctrl_row = next(m for m in body["machines"] if m["id"] == ctrl)
    assert ctrl_row["state_since"] == z(2 * H)  # the open span's start


def test_devices_unavailable_carry_the_reason(world):
    rt, _ = world
    off = read(rt, device_history=Recorder(available=False, reason="已损坏"))
    assert off["devices"] == {"available": False, "reason": "已损坏", "spans": []}
    assert off["coverage"]["devices"] is None
    none = read(rt, device_history=None)["devices"]
    assert none == {"available": False, "reason": an.NO_RECORDER, "spans": []}


# ── INBOX (AC9) ─────────────────────────────────────────────────────────
def test_inbox_pending_bytes_and_the_last_seven_days(world, tmp_path, monkeypatch):
    rt, s = world
    now = rt.clock.now()
    back = (now - T0).total_seconds()
    s.film(
        code="OLD",
        accepted=back - 8 * 86400,
        main_bytes=(9_000,),
        tries=standard(None),
        stages={"cleanup_input": "succeeded"},
    )
    s.film(
        code="WEEK-1",
        accepted=back - 86400,
        main_bytes=(7_000,),
        tries=standard(None),
        stages={"cleanup_input": "succeeded"},
    )
    s.film(
        code="WEEK-2",
        accepted=back - 3600,
        main_bytes=(14_000,),
        tries=standard(None),
        stages={"cleanup_input": "pending"},
    )
    s.film(
        code="WAITING", accepted=None, main_bytes=(1_000,), other_bytes=(10,), tries=[]
    )
    s.film(code="REMOVED", accepted=None, removed=True, main_bytes=(5,), tries=[])
    usage = SimpleNamespace(total=1_000_000, used=600_000, free=400_000)
    monkeypatch.setattr(an.shutil, "disk_usage", lambda root: usage)
    inbox = read(rt, inbox_root=str(tmp_path))["inbox"]
    assert inbox == {
        "total_bytes": 1_000_000,
        "free_bytes": 400_000,
        "used_bytes": 600_000,
        "pending_bytes": 14_000 + 2_000 + 1_000 + 10,  # WEEK-2 and WAITING
        "pending_films": 2,
        "freed_bytes_per_day_7d": 21_000 // 7,
        "avg_input_bytes_7d": 10_500,
    }
    assert read(rt, inbox_root=None)["inbox"] is None


def test_an_unreadable_inbox_disk_is_null(world, monkeypatch, caplog):
    rt, _ = world

    def missing(root):
        raise FileNotFoundError(root)

    monkeypatch.setattr(an.shutil, "disk_usage", missing)
    assert read(rt, inbox_root="X:\\nowhere")["inbox"] is None
    assert "INBOX disk usage unreadable" in caplog.text


# ── outages (D7, AC8) ───────────────────────────────────────────────────
class LogClock:
    def __init__(self):
        self.now = datetime(2026, 10, 30, 22, 0).astimezone()

    def __call__(self):
        return self.now


def _event(log: TaskLog, kind: str, model: str = "m1", task: str = an.TRANSLATE_TASK):
    log.record(task, kind, task_type="workflow", data={"model": model})


def test_outages_pair_provider_events_and_skip_other_tasks(world, tmp_path):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    start = clock.now
    _event(log, an.DOWN, "m1")
    clock.now += timedelta(minutes=5)
    _event(log, an.DOWN, "m2")
    _event(log, an.PAUSED)
    _event(log, an.DOWN, "x", task="subs-other")  # the stand-alone monitor
    clock.now += timedelta(minutes=5)
    _event(log, an.UP, "m1")  # m2 still down
    clock.now += timedelta(minutes=5)
    _event(log, an.UP, "m2")
    end = clock.now
    clock.now += timedelta(minutes=5)
    _event(log, an.DOWN, "m1")
    open_start = clock.now
    clock.now += timedelta(minutes=1)
    log.record("", "agent.started", task_type="agent")  # a restart ends it
    restarted = clock.now
    clock.now += timedelta(minutes=1)
    _event(log, an.DOWN, "m1")
    still = clock.now
    body = read(rt, task_log=log)
    assert body["outages"] == [
        {"start": _s(start), "end": _s(end), "paused": True},
        {"start": _s(open_start), "end": _s(restarted), "paused": False},
        {"start": _s(still), "end": None, "paused": False},
    ]
    assert body["coverage"]["outages"] == an._day_start("20261030")
    later = read(rt, task_log=log, since=end + timedelta(minutes=2))["outages"]
    assert [o["start"] for o in later] == [_s(open_start), _s(still)]


def _s(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(clock: LogClock, minutes: int) -> datetime:
    clock.now += timedelta(minutes=minutes)
    return clock.now


def test_a_translate_success_after_a_pause_closes_the_outage(world, tmp_path):
    """Each translate attempt has its own translator, so after a pause the
    provider_up of the instance that saw it down never comes (C2-F2)."""
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    start = clock.now
    _event(log, an.DOWN, "m1")
    _at(clock, 1)
    _event(log, an.PAUSED)
    _at(clock, 30)
    _event(log, "translate.started")  # the next attempt, a new instance
    _event(log, an.FINISHED, task="subs-other")  # another task: not ours
    end = _at(clock, 5)
    _event(log, an.FINISHED)
    _at(clock, 5)
    _event(log, an.DOWN, "m1")  # m1 down again: a new outage
    again = clock.now
    assert read(rt, task_log=log)["outages"] == [
        {"start": _s(start), "end": _s(end), "paused": True},
        {"start": _s(again), "end": None, "paused": False},
    ]


def test_a_paused_retry_does_not_close_the_outage(world, tmp_path):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    start = clock.now
    _event(log, an.DOWN, "m1")
    _at(clock, 1)
    _event(log, an.PAUSED)
    _at(clock, 30)
    _event(log, "translate.started")
    _at(clock, 1)
    _event(log, an.PAUSED)  # the retry paused as well
    assert read(rt, task_log=log)["outages"] == [
        {"start": _s(start), "end": None, "paused": True}
    ]


def test_an_outage_closes_when_no_model_is_down_and_forgets_them(world, tmp_path):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    start = clock.now
    _event(log, an.DOWN, "m1")
    _event(log, an.DOWN, "m2")
    _at(clock, 5)
    _event(log, an.UP, "m1")  # m2 still down
    end = _at(clock, 5)
    _event(log, an.UP, "m2")
    _at(clock, 5)
    _event(log, an.DOWN, "m3")
    second = clock.now
    end2 = _at(clock, 5)
    _event(log, an.FINISHED)  # closes, forgetting m3
    _at(clock, 5)
    _event(log, an.DOWN, "m1")
    third = clock.now
    end3 = _at(clock, 5)
    _event(log, an.UP, "m1")  # m3 is not still counted as down
    assert read(rt, task_log=log)["outages"] == [
        {"start": _s(start), "end": _s(end), "paused": False},
        {"start": _s(second), "end": _s(end2), "paused": False},
        {"start": _s(third), "end": _s(end3), "paused": False},
    ]


def test_outage_days_closed_cached_today_incremental_and_rollover(
    world, tmp_path, monkeypatch
):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    _event(log, an.DOWN)
    parsed: list[str] = []
    real = tl._parsed

    def counting(line, day):
        parsed.append(day)
        return real(line, day)

    monkeypatch.setattr(tl, "_parsed", counting)
    cache = an.AnalyticsFacts()
    read(rt, cache, task_log=log)
    assert parsed == ["20261030"]
    parsed.clear()
    read(rt, cache, task_log=log)
    assert parsed == []  # nothing appended: nothing parsed
    _event(log, an.UP)
    read(rt, cache, task_log=log)
    assert parsed == ["20261030"]  # only the appended line
    # midnight: yesterday's last line is drained before the day closes
    clock.now += timedelta(hours=3)
    _event(log, an.DOWN)  # first line of the new day (rollover)
    path = tmp_path / "agent/logs/tasklog-20261030.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        row.update(
            id="20261030-99",
            kind=an.DOWN,
            ts=(clock.now - timedelta(hours=2, minutes=1)).isoformat(),
        )
        stream.write(json.dumps(row) + "\n")
    parsed.clear()
    body = read(rt, cache, task_log=log)
    assert sorted(parsed) == ["20261030", "20261031"]
    assert len(body["outages"]) == 2  # 22:00 down and up, then 22:59 down (open)
    parsed.clear()
    read(rt, cache, task_log=log)
    assert parsed == []  # the closed day is not read again


def test_outages_include_unflushed_memory_rows_once(world, tmp_path, monkeypatch):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    _event(log, an.DOWN)  # on disk and in memory: counted once

    def fail(row):
        raise OSError("locked")

    monkeypatch.setattr(log, "_append", fail)
    clock.now += timedelta(minutes=3)
    _event(log, an.UP)  # memory only
    assert read(rt, task_log=log)["outages"] == [
        {
            "start": _s(clock.now - timedelta(minutes=3)),
            "end": _s(clock.now),
            "paused": False,
        }
    ]


def test_a_pruned_day_drops_out_and_coverage_is_the_oldest_scanned(world, tmp_path):
    rt, _ = world
    clock = LogClock()
    clock.now -= timedelta(days=2)
    log = TaskLog(tmp_path / "agent", clock=clock)
    _event(log, an.DOWN)
    clock.now += timedelta(days=2)
    _event(log, an.UP)
    cache = an.AnalyticsFacts()
    body = read(rt, cache, task_log=log)
    assert body["coverage"]["outages"] == an._day_start("20261028")
    assert len(body["outages"]) == 1
    (tmp_path / "agent/logs/tasklog-20261028.jsonl").unlink()
    log._ring.clear()  # long gone from memory too, as an old day is
    body = read(rt, cache, task_log=log)
    assert body["coverage"]["outages"] == an._day_start("20261030")
    assert body["outages"] == []  # an up without its down opens nothing


def test_days_older_than_the_retention_window_are_never_scanned(world, tmp_path):
    rt, _ = world
    clock = LogClock()
    log = TaskLog(tmp_path / "agent", clock=clock)
    folder = tmp_path / "agent/logs"
    folder.mkdir(parents=True)
    old = folder / "tasklog-20260901.jsonl"
    old.write_text(
        json.dumps(
            {
                "v": 1,
                "id": "20260901-1",
                "ts": "2026-09-01T10:00:00+09:00",
                "task": an.TRANSLATE_TASK,
                "task_type": "workflow",
                "kind": an.DOWN,
                "severity": "warn",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    body = read(rt, task_log=log)
    assert body["outages"] == [] and body["coverage"]["outages"] is None


# ── the whole body ──────────────────────────────────────────────────────
def test_the_body_has_every_section_and_is_strict_json(world):
    rt, s = world
    s.film(tries=standard(s.worker("Alpha")))
    body = read(rt, since=T0)
    assert list(body) == [
        "server_time",
        "since",
        "coverage",
        "machines",
        "films",
        "failures",
        "devices",
        "outages",
        "inbox",
    ]
    assert list(body["coverage"]) == [
        "archives",
        "energy",
        "cues",
        "devices",
        "outages",
    ]
    text = json.dumps(body, allow_nan=False)  # plain values only
    assert json.loads(text) == body
    assert body["server_time"].endswith("Z") and len(body["server_time"]) == 20
