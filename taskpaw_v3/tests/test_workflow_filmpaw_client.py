"""F02-P1: the Filmpaw client finds the sidecar through workflow-endpoint.json and never guesses."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from taskpaw_v3.tests.workflow_fake_filmpaw import FakeFilmpaw
from taskpaw_v3.workflow.filmpaw_client import (
    FilmpawClient,
    FilmpawRefused,
    FilmpawUnavailable,
)


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    target = tmp_path / "nas" / "Actor"
    target.mkdir(parents=True)
    return target


def _package(tmp_path: Path) -> tuple[Path, list[dict]]:
    root = tmp_path / "out" / "ABC-123"
    root.mkdir(parents=True)
    (root / "ABC-123.mp4").write_bytes(b"video" * 100)
    (root / "ABC-123.nfo").write_text("<movie/>", encoding="utf-8")
    files = [
        {
            "path": p.name,
            "size": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(root.iterdir())
    ]
    return root, files


def test_no_endpoint_file_means_unavailable(tmp_path):
    with pytest.raises(FilmpawUnavailable):
        FilmpawClient(tmp_path / "Filmpaw").lookup("op")


def test_stale_endpoint_file_is_not_trusted(tmp_path):
    data_dir = tmp_path / "Filmpaw"
    data_dir.mkdir()
    (data_dir / "workflow-endpoint.json").write_text(
        json.dumps({"port": 1, "pid": 2**22 + 7, "token": "x"}), encoding="utf-8"
    )
    with pytest.raises(FilmpawUnavailable):
        FilmpawClient(data_dir).lookup("op")


def test_target_archive_and_lookup_round_trip(tmp_path, nas):
    root, files = _package(tmp_path)
    with FakeFilmpaw(tmp_path / "Filmpaw", {"actor": nas}):
        client = FilmpawClient(tmp_path / "Filmpaw")
        proof = client.target("actor", "ABC-123")
        assert proof["absent"] is True
        assert client.lookup("op-1") is None
        task = client.archive("op-1", root, "actor", proof["expected_target"], files)
        assert task["status"] == "completed"
        assert client.lookup("op-1")["receipt"]["target_path"] == str(nas / "ABC-123")
        with pytest.raises(FilmpawRefused) as e:
            client.target("nobody", "ABC-123")
        assert e.value.status == 404


def test_lost_response_is_unavailable_and_lookup_finds_the_work(tmp_path, nas):
    root, files = _package(tmp_path)
    with FakeFilmpaw(tmp_path / "Filmpaw", {"actor": nas}) as fake:
        client = FilmpawClient(tmp_path / "Filmpaw")
        proof = client.target("actor", "ABC-123")
        fake.mode = "lose_response"
        with pytest.raises(FilmpawUnavailable):
            client.archive("op-1", root, "actor", proof["expected_target"], files)
        assert client.lookup("op-1")["status"] == "completed"
        assert fake.copies == 1


@pytest.mark.parametrize(
    "performer_id,target_dir", [(None, None), ("actor", "C:/library")]
)
def test_target_and_archive_require_exactly_one_destination(
    tmp_path, performer_id, target_dir
):
    client = FilmpawClient(tmp_path / "Filmpaw")
    with pytest.raises(ValueError, match="exactly one"):
        client.target(performer_id, "ABC-123", target_dir=target_dir)
    with pytest.raises(ValueError, match="exactly one"):
        client.archive("op", tmp_path, performer_id, {}, [], target_dir=target_dir)
