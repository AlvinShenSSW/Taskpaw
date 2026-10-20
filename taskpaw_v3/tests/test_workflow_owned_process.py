"""Workflow children live and die with their attempt's Job (T03-P1 C3, D02, D13, D15)."""

import subprocess
import sys
import threading
import time

import pytest

from taskpaw_v3.workflow.owned_process import OwnedJob, own_identity, process_alive

native = pytest.mark.skipif(sys.platform != "win32", reason="Native Job ownership")

PARENT = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print("grandchild", child.pid, flush=True)
time.sleep(60)
"""


def _sink():
    seen = []
    return seen, lambda name, text: seen.append((name, text))


def _grandchild(seen, deadline=15.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        for _, text in list(seen):
            if text.startswith("grandchild "):
                return int(text.split()[1])
        time.sleep(0.05)
    raise AssertionError("grandchild pid not reported")


def _alive(pid):
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return str(pid) in out


@native
def test_terminate_ends_the_whole_tree_and_settles(tmp_path):
    seen, sink = _sink()
    launched = []
    job = OwnedJob(on_launch=launched.append)
    child = job.launch([sys.executable, "-c", PARENT], cwd=str(tmp_path), on_line=sink)
    grandchild = _grandchild(seen)
    assert launched == [child.identity]
    assert child.poll() is None and process_alive(child.identity)
    assert job.active() >= 2  # launchers may add layers; the count is never relied on
    job.terminate()
    assert job.settle(timeout=15)
    assert not _alive(grandchild)
    assert not process_alive(child.identity)


@native
def test_sequential_children_share_one_job_with_cwd_and_heavy_output(tmp_path):
    seen, sink = _sink()
    job = OwnedJob()
    script = "import os; print(os.getcwd()); [print('x' * 200) for _ in range(20000)]"
    first = job.launch([sys.executable, "-c", script], cwd=str(tmp_path), on_line=sink)
    assert first.wait() == 0
    second = job.launch(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=str(tmp_path)
    )
    assert second.wait() == 3
    assert job.settle(timeout=30)
    assert seen[0] == ("stdout", str(tmp_path))
    assert sum(1 for _, text in seen if text.startswith("x")) == 20000
    assert (first.poll(), second.poll()) == (0, 3)


@native
def test_wait_returns_none_when_cancelled(tmp_path):
    job = OwnedJob()
    child = job.launch(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=str(tmp_path)
    )
    cancel = threading.Event()
    cancel.set()
    assert child.wait(cancel) is None
    job.terminate()
    assert job.settle(timeout=15)


def test_unknown_or_reused_identity_is_not_alive():
    assert not process_alive("not-an-identity")
    assert not process_alive("999999999:1")
    assert process_alive(own_identity())
