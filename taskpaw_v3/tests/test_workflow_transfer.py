"""P2-2 (#293): the transfer matrix over the real worker API (§2, §4, §7).

Download: envelope, ranged work copies, 404/416/429. Upload: plan, streaming
PUT with offset CAS, resume at random offsets (also across a controller
restart), whole-file mismatch, oversize, path rules, 507, 429 and C7. The
finish-ref check (R16) is unit-tested here; it is not wired yet.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import uuid

import pytest

from taskpaw_v3.tests.test_workflow_films import input_manifest
from taskpaw_v3.tests.test_workflow_local_dispatch import STAGES, plan_film, runtime
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import (
    Host,
    StepClock,
    boot_id,
    client,
    enrol,
)
from taskpaw_v3.tests.workflow_route_helpers import (
    Remote,
    boot,
    claim_remote,
    resolve_open,
    route,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import transfer as tr
from taskpaw_v3.workflow.stage_results import (
    FileRef,
    RestoreFacts,
    StageResult,
    VideoResult,
)
from taskpaw_v3.workflow.transfer import (
    CONTROLLER_RESERVE_BYTES,
    PlanFile,
    TransferError,
    Transfers,
    live_attempts,
    validate_finish_refs,
)

P = "/worker/v1"
GB = 10**9
VIDEO = bytes(random.Random(7).randrange(256) for _ in range(300_000))
SUBS = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n".encode()


class Free:
    def __init__(self) -> None:
        self.value = 2_000 * GB

    def __call__(self, _path) -> int:
        return self.value


@pytest.fixture
def clock():
    return StepClock()


@pytest.fixture
def rt(tmp_path, clock):
    value = runtime(tmp_path / "wf", clock=clock.now)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def free():
    return Free()


def transfers(rt, free) -> Transfers:
    """The controller's `Transfers` as the host builds it (IR5: store-backed)."""
    return Transfers(rt.work_dir, free_bytes=free, live=lambda: live_attempts(rt))


@pytest.fixture
def host(rt, free):
    return Host(rt, transfers=transfers(rt, free))


@pytest.fixture
def api(host):
    return client(host)


def real_film(rt) -> tuple[str, m.InputManifest]:
    """A planned film whose work copies exist with these exact bytes."""
    folder = f"intake/{identity()}"
    contents = {"movie.mkv": VIDEO, "movie.ja.srt": SUBS}

    def change(data):
        for item in (*data["items"], *data["readiness"]["workcopy"]["items"]):
            name = item["relative_path"].rsplit("/", 1)[-1]
            raw = contents[name]
            item["size_bytes"] = len(raw)
            item["sha256"] = hashlib.sha256(raw).hexdigest()
        for copy in data["readiness"]["workcopy"]["items"]:
            copy["relative_path"] = f"{folder}/{copy['relative_path']}"

    _, auth = rt.grant_intake_adapter()
    film_id = identity()
    rt.store.create_film(
        auth,
        rt.meta(),
        m.FilmCreate(
            film_id=film_id,
            intake_session_id=identity(),
            display_name="Real film",
            policy=m.FilmPolicy(
                policy_revision=1,
                required_stages=STAGES,
                subtitle_requirement="chinese",
                required_image_roles=("poster",),
                folder_cover_required=False,
            ),
        ),
    )
    manifest = input_manifest(film_id, change=change)
    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(film_id=film_id, expected_version=1, manifest=manifest),
    )
    for copy in manifest.readiness.workcopy.items:
        path = rt.work_dir / copy.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[copy.relative_path.rsplit("/", 1)[-1]])
    plan_film(rt, film_id)
    return film_id, manifest


@pytest.fixture
def world(api, rt):
    joined, booted = enrol(api, rt)
    film_id, manifest = real_film(rt)
    route(rt, film_id, joined.worker_id)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    remote = Remote(
        joined.worker_id,
        "5060 Ti-1",
        joined.auth(rt),
        booted,
        worker.physical_binding.resource_id,
    )
    return joined, remote, film_id, manifest


def video_copy(manifest):
    return next(
        c for c in manifest.readiness.workcopy.items if c.relative_path.endswith(".mkv")
    )


# ── R11: the envelope ───────────────────────────────────────────────────
def test_the_envelope_answers_only_a_routed_film(api, rt, world):
    joined, _, film_id, manifest = world
    data = api.get(f"{P}/films/{film_id}/1", headers=joined.headers)
    assert data.status_code == 200
    body = data.json()
    assert (body["film"], body["rev"], body["ltx_model"]) == (film_id, 1, "standard")
    assert body["route"]["id"] == rt.store.film_route(rt.operator, film_id, 1).route_id
    assert {c["item_id"] for c in body["workcopy"]} == {
        c.source_item_id for c in manifest.readiness.workcopy.items
    }
    assert m.InputManifest.model_validate(body["manifest"]) == manifest
    assert body["ledgers"] == {}
    other, _ = real_film(rt)
    missing = api.get(f"{P}/films/{other}/1", headers=joined.headers)
    assert missing.status_code == 404 and missing.json() == {"code": "not_routed"}
    assert api.get(f"{P}/films/{film_id}/2", headers=joined.headers).status_code == 404


def test_another_workers_film_is_404(api, rt, world):
    _, _, film_id, _ = world
    second, _ = enrol(api, rt, "5060 Ti-2")
    response = api.get(f"{P}/films/{film_id}/1", headers=second.headers)
    assert response.status_code == 404


def test_the_envelope_carries_a_succeeded_stages_ledger(api, rt, world):
    joined, remote, film_id, manifest = world
    claimed, command = claim_remote(rt, remote, film_id, "restore")
    copy = video_copy(manifest)
    result = StageResult(
        film_id=film_id,
        input_revision=1,
        stage="restore",
        attempt_id=command.attempt_id,
        videos=(
            VideoResult(
                video_item_id=copy.source_item_id,
                volume_ordinal=None,
                restore=RestoreFacts(
                    decision="reused",
                    file=FileRef(
                        path=copy.relative_path,
                        sha256=copy.sha256,
                        size=copy.size_bytes,
                    ),
                    width=1920,
                    height=1080,
                    duration_ms=1000,
                    video_streams=1,
                    audio_streams=1,
                ),
            ),
        ),
    )
    raw = json.dumps(result.model_dump(mode="json")).encode()
    (rt.work_dir / command.attempt_id).mkdir(parents=True)
    (rt.work_dir / command.attempt_id / "stage-result.json").write_bytes(raw)
    current = next(
        o.assignment
        for o in rt.store.list_open_assignments(rt.operator)
        if o.assignment.assignment_id == claimed.assignment_id
    )
    rt.store.resolve_assignment(
        rt.operator,
        rt.meta(),
        m.AssignmentResolution(
            assignment_id=current.assignment_id,
            attempt_id=current.attempt_id,
            expected_version=current.version,
            reason="远程完成",
            operation_id=command.operation_id,
            local_owner=m.LocalOwnerEvidence(
                owner_id=identity(),
                generation=1,
                boot_id=current.boot_id,
                process_identity="remote-synthetic",
                state="stopped",
                evidence=rt.evidence("native_owner_stopped"),
            ),
            evidence=(
                rt.evidence(
                    "exec_succeeded", payload_digest=hashlib.sha256(raw).hexdigest()
                ),
            ),
            outcome="succeeded",
        ),
    )
    # OD7 R4: the route closed with the restore; an in-flight ASR claimed
    # before the upgrade still holds one, which the envelope serves
    route(rt, film_id, remote.worker_id)
    body = api.get(f"{P}/films/{film_id}/1", headers=joined.headers).json()
    ledger = body["ledgers"]["restore"]
    assert ledger["attempt_id"] == command.attempt_id
    assert ledger["payload_digest"] == hashlib.sha256(raw).hexdigest()
    assert ledger["text"].encode() == raw


# ── R12: ranged GET of work copies ──────────────────────────────────────
def test_a_whole_item_and_random_ranges(api, world):
    joined, _, film_id, manifest = world
    copy = video_copy(manifest)
    url = f"{P}/films/{film_id}/1/items/{copy.source_item_id}"
    whole = api.get(url, headers=joined.headers)
    assert whole.status_code == 200 and whole.content == VIDEO
    assert whole.headers["x-sha256"] == copy.sha256
    assert whole.headers["x-size"] == str(len(VIDEO))
    rnd = random.Random(11)
    for _ in range(5):
        offset = rnd.randrange(1, len(VIDEO))
        part = api.get(url, headers={**joined.headers, "Range": f"bytes={offset}-"})
        assert part.status_code == 206 and part.content == VIDEO[offset:]
    beyond = api.get(url, headers={**joined.headers, "Range": f"bytes={len(VIDEO)}-"})
    assert beyond.status_code == 416
    unknown = api.get(
        f"{P}/films/{film_id}/1/items/{identity()}", headers=joined.headers
    )
    assert unknown.status_code == 404


def test_a_second_download_stream_is_429(api, host, world):
    joined, _, film_id, manifest = world
    copy = video_copy(manifest)
    host.transfers.acquire(joined.worker_id, "down")  # a stream already running
    try:
        busy = api.get(
            f"{P}/films/{film_id}/1/items/{copy.source_item_id}", headers=joined.headers
        )
        assert busy.status_code == 429 and busy.headers["retry-after"] == "5"
    finally:
        host.transfers.release(joined.worker_id, "down")
    ok = api.get(
        f"{P}/films/{film_id}/1/items/{copy.source_item_id}", headers=joined.headers
    )
    assert ok.status_code == 200
    again = api.get(
        f"{P}/films/{film_id}/1/items/{copy.source_item_id}", headers=joined.headers
    )
    assert again.status_code == 200  # the slot frees after each stream


# ── R13/R14: upload ─────────────────────────────────────────────────────
OUTPUT = bytes(random.Random(3).randrange(256) for _ in range(200_000))


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def attempt(rt, world):
    _, remote, film_id, _ = world
    claimed, command = claim_remote(rt, remote, film_id, "restore")
    return command.attempt_id


def plan(api, joined, attempt, files):
    return api.post(
        f"{P}/attempts/{attempt}/outputs", json=files, headers=joined.headers
    )


def put(api, joined, attempt, path, body, offset, total):
    return api.put(
        f"{P}/attempts/{attempt}/files/{path}",
        content=body,
        headers={
            **joined.headers,
            "Content-Range": f"bytes {offset}-{total - 1}/{total}",
        },
    )


def test_upload_resumes_at_random_offsets_and_commits(api, rt, world, attempt):
    joined = world[0]
    path = f"{attempt}/movie-破解.mp4"
    planned = plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    assert planned.status_code == 200
    assert planned.json()["files"][0] | {} == {
        "path": path,
        "size": len(OUTPUT),
        "sha256": sha(OUTPUT),
        "received": 0,
        "done": False,
        "mismatches": 0,
    }
    rnd = random.Random(5)
    received = 0
    while received < len(OUTPUT):
        cut = min(len(OUTPUT), received + rnd.randrange(1, 60_000))
        response = put(
            api, joined, attempt, path, OUTPUT[received:cut], received, len(OUTPUT)
        )
        if cut < len(OUTPUT):  # an interrupted stream keeps what arrived
            assert response.status_code == 409
            assert response.json() == {"code": "offset_mismatch", "received": cut}
        else:
            assert response.status_code == 200
            assert response.json() == {
                "path": path,
                "received": len(OUTPUT),
                "done": True,
            }
        received = cut
    target = rt.work_dir / path
    assert target.read_bytes() == OUTPUT
    view = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers).json()
    assert view["files"][0]["done"] is True and view["files"][0]["received"] == len(
        OUTPUT
    )
    record = json.loads((rt.work_dir / ".incoming" / attempt / "plan.json").read_text())
    assert record["committed"][path] == {
        "sha256": sha(OUTPUT),
        "size": len(OUTPUT),
        "mtime_ns": target.stat().st_mtime_ns,
    }
    done = put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT))
    assert done.status_code == 409 and done.json()["received"] == len(OUTPUT)


def test_identical_repost_returns_offsets_and_a_changed_plan_conflicts(
    api, world, attempt
):
    joined = world[0]
    files = [{"path": f"{attempt}/a.srt", "size": len(SUBS), "sha256": sha(SUBS)}]
    plan(api, joined, attempt, files)
    put(api, joined, attempt, files[0]["path"], SUBS[:10], 0, len(SUBS))
    again = plan(api, joined, attempt, files)
    assert again.status_code == 200 and again.json()["files"][0]["received"] == 10
    changed = plan(api, joined, attempt, [dict(files[0], size=len(SUBS) + 1)])
    assert changed.status_code == 409 and changed.json() == {"code": "plan_conflict"}


def test_offset_cas(api, world, attempt):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    put(api, joined, attempt, path, OUTPUT[:1000], 0, len(OUTPUT))
    wrong = put(api, joined, attempt, path, OUTPUT[500:], 500, len(OUTPUT))
    assert wrong.status_code == 409
    assert wrong.json() == {"code": "offset_mismatch", "received": 1000}
    size = put(api, joined, attempt, path, OUTPUT[1000:], 1000, len(OUTPUT) + 1)
    assert size.status_code == 422 and size.json() == {"code": "invalid_range"}
    bad = api.put(
        f"{P}/attempts/{attempt}/files/{path}",
        content=b"x",
        headers={**joined.headers, "Content-Range": "bytes */9"},
    )
    assert bad.status_code == 422


def test_a_whole_file_mismatch_resets_and_counts(api, rt, world, attempt):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    corrupt = bytearray(OUTPUT)
    corrupt[1234] ^= 0xFF
    first = put(api, joined, attempt, path, bytes(corrupt), 0, len(OUTPUT))
    assert first.status_code == 422
    assert first.json() == {"code": "hash_mismatch", "mismatches": 1}
    view = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers).json()
    assert view["files"][0]["received"] == 0 and view["files"][0]["mismatches"] == 1
    assert not (rt.work_dir / path).exists()
    ok = put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT))
    assert ok.status_code == 200


def test_an_oversize_body_is_413_and_truncated_back(api, world, attempt):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    put(api, joined, attempt, path, OUTPUT[:100], 0, len(OUTPUT))

    def stream():
        yield OUTPUT[100:]
        yield b"overflow"

    response = api.put(
        f"{P}/attempts/{attempt}/files/{path}",
        content=stream(),
        headers={
            **joined.headers,
            "Content-Range": f"bytes 100-{len(OUTPUT) - 1}/{len(OUTPUT)}",
        },
    )
    assert response.status_code == 413
    view = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers).json()
    assert view["files"][0]["received"] == 100
    declared = api.put(
        f"{P}/attempts/{attempt}/files/{path}",
        content=OUTPUT[100:] + b"x",
        headers={
            **joined.headers,
            "Content-Range": f"bytes 100-{len(OUTPUT) - 1}/{len(OUTPUT)}",
        },
    )
    assert declared.status_code == 413


def test_a_controller_restart_mid_upload_keeps_offsets_and_rebuilds_the_hash(
    api, host, rt, free, world, attempt
):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    put(api, joined, attempt, path, OUTPUT[:77_777], 0, len(OUTPUT))
    host.transfers = transfers(rt, free)  # a new process: no hash state yet
    cold = put(api, joined, attempt, path, OUTPUT[77_777:], 77_777, len(OUTPUT))
    assert cold.status_code == 409  # IR3: a PUT never re-hashes a prefix
    assert cold.json() == {"code": "prefix_unverified", "received": 77_777}
    view = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers).json()
    assert view["files"][0]["received"] == 77_777  # GET rebuilt the hash
    done = put(api, joined, attempt, path, OUTPUT[77_777:], 77_777, len(OUTPUT))
    assert done.status_code == 200 and (rt.work_dir / path).read_bytes() == OUTPUT


def test_a_commit_interrupted_before_its_record_is_settled(
    api, host, rt, world, attempt
):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    (rt.work_dir / attempt).mkdir(parents=True, exist_ok=True)
    (rt.work_dir / path).write_bytes(OUTPUT)  # renamed in, the record lost
    view = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers).json()
    assert view["files"][0]["done"] is True


@pytest.mark.parametrize(
    "path",
    [
        "{a}/../escape.mp4",
        "{a}/x/../../escape.mp4",
        "{a}\\x.mp4",
        "{a}/x.mp4:stream",
        "/abs/x.mp4",
        "C:/x.mp4",
        "{a}/CON.srt",
        "{a}/nul",
        "{a}/stage-result.json",
        "{a}/Stage-Result.JSON",
        "other/x.mp4",
        "{a}",
        "{a}/x.mp4 ",
        "{a}/cafe\u0301.mp4",
    ],
)
def test_unsafe_paths_are_refused(api, world, attempt, path):
    joined = world[0]
    value = path.replace("{a}", attempt)
    response = plan(
        api, joined, attempt, [{"path": value, "size": 1, "sha256": "0" * 64}]
    )
    assert response.status_code == 422, value
    assert response.json() == {"code": "invalid_path"}


@pytest.mark.parametrize(
    "path", ["{a}/%2E%2E/escape.mp4", "{a}/x%5Cy.mp4", "{a}/x%3Ay.mp4"]
)
def test_unsafe_put_urls_are_refused(api, world, attempt, path):
    joined = world[0]
    plan(
        api,
        joined,
        attempt,
        [{"path": f"{attempt}/ok.mp4", "size": 1, "sha256": sha(b"x")}],
    )
    response = put(api, joined, attempt, path.replace("{a}", attempt), b"x", 0, 1)
    assert response.status_code in (404, 422), path
    assert response.status_code != 200


def test_the_disk_gate_counts_other_plans(api, rt, free, world, attempt):
    joined, remote, film_id, _ = world
    size = 10 * GB
    free.value = CONTROLLER_RESERVE_BYTES + size + 2 * GB
    first = plan(
        api,
        joined,
        attempt,
        [{"path": f"{attempt}/a.mp4", "size": size, "sha256": "a" * 64}],
    )
    assert first.status_code == 200
    # another attempt's remaining plan counts against the free space
    other, _ = real_film(rt)
    second_joined, second_boot = enrol(api, rt, "5060 Ti-2")
    route(rt, other, second_joined.worker_id)
    worker = rt.store.get_worker(rt.operator, second_joined.worker_id).worker
    second = Remote(
        second_joined.worker_id,
        "5060 Ti-2",
        second_joined.auth(rt),
        second_boot,
        worker.physical_binding.resource_id,
    )
    _, command = claim_remote(rt, second, other, "restore")
    b = command.attempt_id
    refused = plan(
        api,
        second_joined,
        b,
        [{"path": f"{b}/b.mp4", "size": 5 * GB, "sha256": "b" * 64}],
    )
    assert refused.status_code == 507 and refused.json() == {
        "code": "controller_disk_full"
    }
    small = plan(
        api, second_joined, b, [{"path": f"{b}/b.mp4", "size": GB, "sha256": "b" * 64}]
    )
    assert small.status_code == 200


def test_a_second_upload_stream_is_429(api, host, world, attempt):
    joined = world[0]
    path = f"{attempt}/a.mp4"
    plan(
        api,
        joined,
        attempt,
        [{"path": path, "size": len(OUTPUT), "sha256": sha(OUTPUT)}],
    )
    host.transfers.acquire(joined.worker_id, "up")
    try:
        busy = put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT))
        assert busy.status_code == 429 and busy.headers["retry-after"] == "5"
    finally:
        host.transfers.release(joined.worker_id, "up")
    assert put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT)).status_code == 200


# ── C7 ──────────────────────────────────────────────────────────────────
def test_a_resolved_foreign_or_unknown_attempt(api, rt, world):
    joined, remote, film_id, _ = world
    claimed, command = claim_remote(rt, remote, film_id, "restore")
    a = command.attempt_id
    files = [{"path": f"{a}/a.srt", "size": len(SUBS), "sha256": sha(SUBS)}]
    second, _ = enrol(api, rt, "5060 Ti-2")
    foreign = plan(api, second, a, files)
    assert foreign.status_code == 409 and foreign.json() == {"code": "stale_attempt"}
    assert (
        api.get(f"{P}/attempts/{a}/outputs", headers=second.headers).status_code == 409
    )
    nobody = plan(api, joined, str(uuid.uuid4()), files)
    assert nobody.status_code == 409
    # a new boot isolates the attempt to `unknown`; still current, so accepted
    boot(rt, remote, boot_id())
    unknown = plan(api, joined, a, files)
    assert unknown.status_code == 200
    resolve_open(rt, claimed.assignment_id, "failed")
    stale = plan(api, joined, a, files)
    assert stale.status_code == 409 and stale.json() == {"code": "stale_attempt"}
    assert put(api, joined, a, files[0]["path"], SUBS, 0, len(SUBS)).status_code == 409


def test_a_file_that_is_also_a_folder_or_a_case_twin_is_refused(api, world, attempt):
    joined = world[0]
    sha_x = sha(b"x")
    for files in (
        [f"{attempt}/x", f"{attempt}/x/y.srt"],
        [f"{attempt}/a.srt", f"{attempt}/A.SRT"],
    ):
        body = [{"path": p, "size": 1, "sha256": sha_x} for p in files]
        response = plan(api, joined, attempt, body)
        assert response.status_code == 422 and response.json() == {
            "code": "invalid_plan"
        }


def test_an_empty_output_is_complete_at_once(api, rt, world, attempt):
    joined = world[0]
    path = f"{attempt}/empty.srt"
    done = plan(api, joined, attempt, [{"path": path, "size": 0, "sha256": sha(b"")}])
    assert done.json()["files"][0]["done"] is True
    assert (rt.work_dir / path).read_bytes() == b""


# ── R16: finish refs (unit) ─────────────────────────────────────────────
def _ledger(film_id, attempt, refs, *, stage="restore", revision=1):
    videos = tuple(
        VideoResult(
            video_item_id=identity(),
            volume_ordinal=None,
            restore=RestoreFacts(
                decision="applied",
                file=ref,
                width=1920,
                height=1080,
                duration_ms=1000,
                video_streams=1,
                audio_streams=1,
            ),
        )
        for ref in refs
    )
    result = StageResult(
        film_id=film_id,
        input_revision=revision,
        stage=stage,
        attempt_id=attempt,
        videos=videos,
    )
    return json.dumps(result.model_dump(mode="json")).encode()


def test_finish_refs_accept_both_kinds_and_refuse_changes(tmp_path):
    film_id, attempt = identity(), identity()
    manifest = input_manifest(film_id)
    copy = manifest.readiness.workcopy.items[0]
    reused = FileRef(path=copy.relative_path, sha256=copy.sha256, size=copy.size_bytes)
    out = tmp_path / attempt / "movie.mp4"
    out.parent.mkdir()
    out.write_bytes(OUTPUT)
    uploaded = FileRef(
        path=f"{attempt}/movie.mp4", sha256=sha(OUTPUT), size=len(OUTPUT)
    )
    record = {
        uploaded.path: {
            "sha256": sha(OUTPUT),
            "size": len(OUTPUT),
            "mtime_ns": out.stat().st_mtime_ns,
        }
    }
    args = dict(
        attempt_id=attempt,
        film_id=film_id,
        input_revision=1,
        stage="restore",
        manifest=manifest,
    )
    raw = _ledger(film_id, attempt, [reused, uploaded])
    result = validate_finish_refs(tmp_path, raw, committed=record, **args)
    assert len(result.videos) == 2

    def refused(raw, committed=record, **change):
        with pytest.raises(TransferError) as exc:
            validate_finish_refs(
                tmp_path, raw, committed=committed, **{**args, **change}
            )
        return exc.value.code

    assert refused(raw, stage="asr") == "ledger_mismatch"
    assert refused(raw, film_id=identity()) == "ledger_mismatch"
    assert refused(raw, attempt_id=identity()) == "ledger_mismatch"
    assert (
        refused(_ledger(film_id, attempt, [uploaded], revision=2)) == "ledger_mismatch"
    )
    assert refused(b"{") == "invalid_ledger"
    # not committed, or another attempt's path
    assert refused(raw, committed={}) == "unverified_output"
    other = identity()
    foreign = FileRef(path=f"{other}/movie.mp4", sha256=sha(OUTPUT), size=len(OUTPUT))
    assert (
        refused(
            _ledger(film_id, attempt, [foreign]),
            committed={foreign.path: record[uploaded.path]},
        )
        == "unverified_output"
    )
    # a wrong reused sha is not a work copy
    fake = FileRef(path=copy.relative_path, sha256="f" * 64, size=copy.size_bytes)
    assert refused(_ledger(film_id, attempt, [fake])) == "unverified_output"
    # changed mtime, then changed size
    os.utime(out, ns=(out.stat().st_atime_ns, out.stat().st_mtime_ns + 10**9))
    assert refused(raw) == "unverified_output"
    out.write_bytes(OUTPUT + b"x")
    assert refused(raw) == "unverified_output"


# ── review fixes (IR2, IR3, IR5, IR13) ──────────────────────────────────
def _one(api, joined, attempt, raw=OUTPUT, name="a.mp4"):
    path = f"{attempt}/{name}"
    planned = plan(
        api, joined, attempt, [{"path": path, "size": len(raw), "sha256": sha(raw)}]
    )
    assert planned.status_code == 200, planned.text
    return path


def _view(api, joined, attempt):
    response = api.get(f"{P}/attempts/{attempt}/outputs", headers=joined.headers)
    assert response.status_code == 200, response.text
    return response.json()["files"][0]


def test_a_complete_uncommitted_part_is_committed_on_get(
    api, rt, host, free, world, attempt
):
    """IR2: every byte arrived but the rename never ran (a crash)."""
    joined = world[0]
    path = _one(api, joined, attempt)
    part = host.transfers.part_path(attempt, path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(OUTPUT)
    host.transfers = transfers(rt, free)  # and a restart
    entry = _view(api, joined, attempt)
    assert (entry["done"], entry["received"]) == (True, len(OUTPUT))
    assert (rt.work_dir / path).read_bytes() == OUTPUT and not part.exists()


def test_a_complete_part_with_a_wrong_hash_is_reset_on_get(api, host, world, attempt):
    joined = world[0]
    path = _one(api, joined, attempt)
    part = host.transfers.part_path(attempt, path)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(bytes(len(OUTPUT)))
    entry = _view(api, joined, attempt)
    assert (entry["done"], entry["received"], entry["mismatches"]) == (False, 0, 1)
    assert put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT)).status_code == 200


def test_a_failed_commit_is_retried_by_the_next_request(
    api, rt, world, attempt, monkeypatch
):
    """IR2: `os.replace` refused (a Windows sharing violation) leaves a
    verified part, answered 503 `commit_pending`; the next GET commits it."""
    joined = world[0]
    path = _one(api, joined, attempt)
    replace = os.replace

    def refusing(src, dst):
        if str(src).endswith(".part"):
            raise PermissionError(13, "in use")
        return replace(src, dst)

    monkeypatch.setattr(tr.os, "replace", refusing)
    refused = put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT))
    assert refused.status_code == 503 and refused.json() == {"code": "commit_pending"}
    assert refused.headers["retry-after"] == "5"
    entry = _view(api, joined, attempt)
    assert (entry["done"], entry["received"]) == (False, len(OUTPUT))
    again = put(api, joined, attempt, path, OUTPUT, 0, len(OUTPUT))
    assert again.json() == {"code": "offset_mismatch", "received": len(OUTPUT)}
    monkeypatch.setattr(tr.os, "replace", replace)
    calls = []
    monkeypatch.setattr(tr, "hash_prefix", lambda *a: calls.append(a))
    entry = _view(api, joined, attempt)
    assert entry["done"] is True and calls == []  # verified once, not re-hashed
    assert (rt.work_dir / path).read_bytes() == OUTPUT


def test_a_resumed_put_reuses_the_hash_state_of_the_cut_stream(
    api, rt, world, attempt, monkeypatch
):
    """IR3: the prefix hash survives in memory; the resumed PUT streams at
    once and never re-reads the prefix."""
    joined = world[0]
    path = _one(api, joined, attempt)
    cut = put(api, joined, attempt, path, OUTPUT[:150_000], 0, len(OUTPUT))
    assert cut.status_code == 409
    calls = []
    monkeypatch.setattr(tr, "hash_prefix", lambda *a: calls.append(a))
    done = put(api, joined, attempt, path, OUTPUT[150_000:], 150_000, len(OUTPUT))
    assert done.status_code == 200 and calls == []
    assert (rt.work_dir / path).read_bytes() == OUTPUT


def test_an_oversize_body_keeps_the_hash_state_at_its_offset(api, world, attempt):
    joined = world[0]
    path = _one(api, joined, attempt)
    put(api, joined, attempt, path, OUTPUT[:100], 0, len(OUTPUT))

    def stream():
        yield OUTPUT[100:150]
        yield OUTPUT[150:] + b"overflow"

    response = api.put(
        f"{P}/attempts/{attempt}/files/{path}",
        content=stream(),
        headers={
            **joined.headers,
            "Content-Range": f"bytes 100-{len(OUTPUT) - 1}/{len(OUTPUT)}",
        },
    )
    assert response.status_code == 413
    done = put(api, joined, attempt, path, OUTPUT[100:], 100, len(OUTPUT))
    assert done.status_code == 200, done.text


def test_plans_of_settled_attempts_are_swept_and_stop_counting(
    api, rt, free, world, attempt
):
    """IR5: a resolved, superseded or unknown attempt's plan and parts are
    deleted and no longer reserve disk."""
    joined, remote, film_id, _ = world
    size = 10 * GB
    free.value = CONTROLLER_RESERVE_BYTES + size + 2 * GB
    big = [{"path": f"{attempt}/a.mp4", "size": size, "sha256": "a" * 64}]
    assert plan(api, joined, attempt, big).status_code == 200
    put(api, joined, attempt, big[0]["path"], b"x" * 1000, 0, size)
    stray = rt.work_dir / ".incoming" / str(uuid.uuid4())
    stray.mkdir(parents=True)
    (stray / "plan.json").write_text("{}", encoding="utf-8")
    boot(rt, remote, boot_id())  # isolated to `unknown`, then resolved
    resolve_open(
        rt,
        next(
            o.assignment.assignment_id
            for o in rt.store.list_open_assignments(rt.operator)
            if o.assignment.attempt_id == attempt
        ),
        "failed",
    )
    other, _ = real_film(rt)
    second_joined, second_boot = enrol(api, rt, "5060 Ti-2")
    route(rt, other, second_joined.worker_id)
    worker = rt.store.get_worker(rt.operator, second_joined.worker_id).worker
    second = Remote(
        second_joined.worker_id,
        "5060 Ti-2",
        second_joined.auth(rt),
        second_boot,
        worker.physical_binding.resource_id,
    )
    b = claim_remote(rt, second, other, "restore")[1].attempt_id
    again = [{"path": f"{b}/a.mp4", "size": size, "sha256": "a" * 64}]
    assert plan(api, second_joined, b, again).status_code == 200  # a is gone
    assert not (rt.work_dir / ".incoming" / attempt).exists()
    assert not stray.exists()
    assert (rt.work_dir / ".incoming" / b / "plan.json").is_file()


def test_the_sweep_runs_at_start(rt, free, world, attempt):
    stray = rt.work_dir / ".incoming" / str(uuid.uuid4())
    stray.mkdir(parents=True)
    kept = rt.work_dir / ".incoming" / attempt
    kept.mkdir(parents=True)
    transfers(rt, free).sweep()
    assert kept.is_dir() and not stray.exists()


def test_a_part_name_cannot_shadow_a_folder(api, world, attempt):
    """IR13: `x`'s part is `x.part`, so `x.part/` cannot be a folder too."""
    joined = world[0]
    body = [
        {"path": p, "size": 1, "sha256": sha(b"x")}
        for p in (f"{attempt}/x", f"{attempt}/X.part/y.srt")
    ]
    response = plan(api, joined, attempt, body)
    assert response.status_code == 422 and response.json() == {"code": "invalid_plan"}


def test_unit_plans_count_only_live_attempts(tmp_path):
    a = str(uuid.uuid4())
    t = Transfers(tmp_path, free_bytes=lambda p: 10**15, live=lambda: {a})
    t.put_plan(a, [PlanFile(f"{a}/x.mp4", 10, "a" * 64)])
    assert len(t._plans(frozenset({a}))) == 1
    assert t._plans(frozenset()) == []


# ── P2-3 R1 / CX15: the sweep re-checks each folder before it deletes it ──
class _Racing:
    """A `live` source whose first read (the sweep's snapshot) misses the
    attempt; `during` runs inside a later read, as if a claim, a plan or an
    upload had landed between the snapshot and the deletion."""

    def __init__(self, attempt_id: str, *, fresh: set[str], during=None) -> None:
        self.attempt_id, self.fresh, self.during = attempt_id, fresh, during
        self.reads = 0

    def __call__(self) -> set[str]:
        self.reads += 1
        if self.reads == 1:
            return set()
        if self.during is not None:
            during, self.during = self.during, None
            during()
        return set(self.fresh)


def _stale_plan(t: Transfers, a: str) -> None:
    folder = t.incoming(a)
    folder.mkdir(parents=True, exist_ok=True)
    plan = {
        "attempt_id": a,
        "files": [{"path": f"{a}/x.mp4", "size": 4, "sha256": sha(b"abcd")}],
        "committed": {},
        "mismatches": {},
    }
    # written by an earlier process: only the store's liveness speaks for it
    (folder / tr.PLAN_NAME).write_text(json.dumps(plan), encoding="utf-8")


def test_a_sweep_keeps_an_attempt_that_became_live_after_its_snapshot(tmp_path):
    a = str(uuid.uuid4())
    live = _Racing(a, fresh={a})
    t = Transfers(tmp_path, free_bytes=lambda p: 10**15, live=live)
    _stale_plan(t, a)
    t.sweep()
    assert (t.incoming(a) / tr.PLAN_NAME).is_file()
    assert live.reads >= 2  # re-read under the attempt lock


def test_a_sweep_keeps_a_plan_written_after_its_snapshot(tmp_path):
    a = str(uuid.uuid4())
    t = Transfers(tmp_path, free_bytes=lambda p: 10**15, live=lambda: set())
    live = _Racing(a, fresh=set(), during=lambda: _write_plan_now(t, a))
    t._live = live
    t.incoming(a).mkdir(parents=True)
    t.sweep()
    assert (t.incoming(a) / tr.PLAN_NAME).is_file()
    t._live = lambda: set()
    t.sweep()  # a later sweep, with the attempt still not live, deletes it
    assert not t.incoming(a).exists()


def _write_plan_now(t: Transfers, a: str) -> None:
    """A plan written as `put_plan` writes it (the attempt's lock held)."""
    with t._attempt(a):
        t._write(a, {"attempt_id": a, "files": [], "committed": {}, "mismatches": {}})


def test_a_sweep_keeps_an_attempt_an_upload_opened_meanwhile(tmp_path):
    a = str(uuid.uuid4())
    t = Transfers(tmp_path, free_bytes=lambda p: 10**15, live=lambda: {a})
    t.put_plan(a, [PlanFile(f"{a}/x.mp4", 4, sha(b"abcd"))])
    opened = []
    t._live = _Racing(
        a,
        fresh=set(),
        during=lambda: opened.append(t.open_upload(a, f"{a}/x.mp4", 0, 4)),
    )
    t.sweep()
    assert opened and (t.incoming(a) / tr.PLAN_NAME).is_file()
    opened[0].write(b"abcd")
    assert opened[0].finish()["done"] is True


def test_a_concurrent_plan_survives_a_sweep(tmp_path):
    """CX15 with real threads: a plan posted while a sweep runs is never
    lost — the sweep's snapshot is taken before the attempt is live."""
    a = str(uuid.uuid4())
    live_ids: set[str] = set()
    snapshot_taken, plan_posted = threading.Event(), threading.Event()

    def live() -> set[str]:
        value = set(live_ids)
        if not snapshot_taken.is_set():
            snapshot_taken.set()
            assert plan_posted.wait(10)  # the plan lands after the snapshot
        return value

    t = Transfers(tmp_path, free_bytes=lambda p: 10**15, live=live)
    t.incoming(a).mkdir(parents=True)  # the folder exists at the scan
    sweeper = threading.Thread(target=t.sweep)
    sweeper.start()
    assert snapshot_taken.wait(10)
    live_ids.add(a)  # the claim committed
    _write_plan_now(t, a)
    plan_posted.set()
    sweeper.join(10)
    assert not sweeper.is_alive()
    assert (t.incoming(a) / tr.PLAN_NAME).is_file()
