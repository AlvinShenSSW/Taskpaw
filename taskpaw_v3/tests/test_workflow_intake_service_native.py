"""Actual discovery-to-helper-to-publication on generated tiny local videos."""

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from taskpaw_v3.tests import test_workflow_intake_runner as f
from taskpaw_v3.tests.test_workflow_media_facts import profile
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow.intake import IntakeService
from taskpaw_v3.workflow.models import WorkflowError

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Native source/helper/store"
)
package, ready = f.package, f.ready


@pytest.mark.parametrize("fail_second", [False, True])
def test_two_actual_films_publish_together_and_second_insert_failure_rolls_back_all(
    ready, monkeypatch, fail_second
):
    h, adapter, observation, runner = ready
    config = h.store.get_intake_configuration(adapter, observation.root_id)
    directory = Path(config.private_path) / "Actor" / "Film"
    (directory / "movie.mp4").rename(directory / "AAA-001.mp4")
    (directory / "movie.zh.srt").rename(directory / "AAA-001.zh.srt")
    shutil.copyfile(directory / "AAA-001.mp4", directory / "BBB-002.mp4")
    config = config.model_copy(
        update={
            "expected_version": 1,
            "profiles": (profile(),),
            "policy": config.policy.model_copy(
                update={"stable_seconds": 1, "rescan_seconds": 1}
            ),
        }
    )
    h.store.configure_intake_root(h.operator, h.meta(), config)
    service = IntakeService(
        h.store,
        adapter,
        root_id=config.root_id,
        meta=h.meta,
        owner_boot_id=runner.owner_boot_id,
        ffprobe=runner.ffprobe,
        controller=True,
        enabled=True,
        clock=h.clock.now,
    )
    captured = []
    finish = h.store.finish_intake_snapshot

    def checked(auth, meta, command):
        captured.append(command)
        assert command.snapshot is not None and len(command.snapshot.manifests) == 2
        if fail_second:
            insert = h.store._put_input_sql
            calls = []

            def failed(value, now):
                calls.append(value)
                if len(calls) == 2:
                    raise sqlite3.OperationalError("injected second film failure")
                return insert(value, now)

            with monkeypatch.context() as patch:
                patch.setattr(h.store, "_put_input_sql", failed)
                with pytest.raises(WorkflowError, match="authority_unavailable"):
                    finish(auth, meta, command)
            assert len(calls) == 2
            for manifest in command.snapshot.manifests:
                with pytest.raises(WorkflowError, match="not_found"):
                    h.store.get_film(h.operator, manifest.film_id)
            assert (
                h.store.get_intake_operation(adapter, command.operation_id).state
                == "running"
            )
        result = finish(auth, meta, command)
        assert (
            finish(auth, meta, command) == result
        )  # reply replay is exact, no extra film
        return result

    monkeypatch.setattr(h.store, "finish_intake_snapshot", checked)
    try:
        assert service.scan_once()
        time.sleep(
            1.05
        )  # actual live stability under an explicit one-second test policy
        assert service.scan_once()
        result = service.run_ready_once()
        assert result is not None and result.state == "completed"
        assert result.exit_proof.state == "stopped"
        assert len(captured) == 1
        snapshot = result.result.snapshot
        assert snapshot.eligibility.decision == "eligible"
        for manifest in snapshot.manifests:
            assert (
                h.store.get_film(adapter, manifest.film_id).film.current_input_revision
                == 1
            )
            assert h.store.get_input_manifest(adapter, manifest.film_id, 1) == manifest
        (item,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
        assert item.state == "operational_ready"
        assert all(probe.raw_facts.ffprobe_version for probe in snapshot.media_probes)
        assert (directory / "AAA-001.mp4").read_bytes() == (
            directory / "BBB-002.mp4"
        ).read_bytes()
    finally:
        assert service.stop(timeout=5)
