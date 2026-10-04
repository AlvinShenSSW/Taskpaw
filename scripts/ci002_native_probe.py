"""Bounded Windows diagnostic driver. No remediation or CI acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

HEAD = "1b8549368152d8f165d2c02992856d8f439c0b54"
TEST = "taskpaw_v3/tests/test_activity_setup.py"
FUNCTION = "test_reinstall_after_external_edit_revokes_whole_file_restore"
P = argparse.ArgumentParser(description=__doc__)
P.add_argument("--product", type=Path, required=True)
P.add_argument("--output", type=Path, required=True)
P.add_argument("--rounds", type=int, choices=range(1, 5), default=4)
A = P.parse_args()
assert sys.platform == "win32" and sys.version_info[:2] == (3, 12)
product, output = A.product.resolve(), A.output.resolve()
output.mkdir(parents=True, exist_ok=True)
sha = lambda value: hashlib.sha256(value).hexdigest()

def git(*args):
    return subprocess.check_output(["git", "-C", str(product), *args])

def binding():
    assert git("rev-parse", "HEAD").decode().strip() == HEAD
    assert not git("status", "--porcelain", "--untracked-files=no").strip()
    paths = ["taskpaw_v3/integrations/activity_setup.py", "taskpaw_v3/integrations/activity_writer.py",
             TEST, "taskpaw_v3/tests/conftest.py", "pyproject.toml", "uv.lock"]
    return {"head": HEAD, "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
            "tracked_clean": True,
            "files": {p: {"git_blob_sha256": sha(git("show", HEAD + ":" + p)),
                          "checkout_sha256": sha((product / p).read_bytes())} for p in paths}}

initial = binding()
records = []
state = {"schema": 1, "source": initial, "python": platform.python_version(),
         "platform": platform.system(), "architecture": platform.machine(),
         "max_rounds": A.rounds, "rounds": records, "acceptance": False,
         "diagnosis": "pending", "source_unchanged": False,
         "limitations": "Bounded diagnosis only. A passing invocation neither repairs nor overrides required CI RED. No raw pytest stdout/stderr is retained."}

def save():
    (output / "summary.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

save()
exitcode = 0
try:
    for number in range(1, A.rounds + 1):
        receipt = output / f"round-{number}.json"
        assert not receipt.exists(), "Fresh diagnostic output is required"
        env = dict(os.environ)
        env.pop("PYTEST_ADDOPTS", None)
        env["PYTHONPATH"] = os.pathsep.join([str(Path(__file__).resolve().parent), str(product)])
        env["TASKPAW_CI002_PRODUCT"] = str(product)
        env["TASKPAW_CI002_RECEIPT"] = str(receipt)
        env["TASKPAW_CI002_ROUND"] = str(number)
        command = [sys.executable, "-m", "pytest", "-p", "ci002_trace_plugin", "-p", "no:cov",
                   "-o", "addopts=", "-q", "-x", "--tb=no", "--show-capture=no",
                   TEST + "::" + FUNCTION, "-k", "writer"]
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=product, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=300, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124  # Diagnostic harness ceiling, not a product deadline finding.
        item = {"round": number, "exitcode": code, "elapsed_s": round(time.monotonic() - started, 3),
                "receipt_present": receipt.exists()}
        if receipt.exists():
            data = json.loads(receipt.read_text(encoding="utf-8"))
            item["receipt_sha256"] = sha(receipt.read_bytes())
            item["test_failures"] = [r for r in data["reports"] if r["outcome"] == "failed"]
            item["third_call_failure"] = any(c["third_call_seen"] and c["third_result"] == 1 for c in data["cases"])
            item["trace_valid"] = bool(data["cases"]) and all(c["third_call_seen"] and c["prior_trace_present"] is False and c["trace_restored"] and c["main_restored"] for c in data["cases"])
            if code == 0:
                item["trace_valid"] = item["trace_valid"] and len(data["cases"]) == 4 and sum(r["phase"] == "call" and r["outcome"] == "passed" for r in data["reports"]) == 4
        else:
            item["trace_valid"] = False
        records.append(item)
        assert binding() == initial
        print(json.dumps({"round": number, "exitcode": code, "trace_valid": item["trace_valid"]}))
        save()
        if code != 0 or not item["trace_valid"]:
            state["diagnosis"] = "failure_captured_requires_causal_review" if item.get("third_call_failure") and item["trace_valid"] else "diagnostic_incomplete_or_other_failure"
            exitcode = code or 2
            break
    else:
        state["diagnosis"] = "not_reproduced_cause_unresolved"
finally:
    try:
        state["source_unchanged"] = binding() == initial
    finally:
        save()
raise SystemExit(exitcode)
