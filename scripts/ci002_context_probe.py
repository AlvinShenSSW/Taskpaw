"""One immutable full-suite coverage-context diagnosis; never CI acceptance."""
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
PATHS = ("taskpaw_v3/integrations/activity_setup.py", "taskpaw_v3/integrations/activity_writer.py",
         "taskpaw_v3/tests/test_activity_setup.py", "taskpaw_v3/tests/conftest.py",
         "pyproject.toml", "uv.lock", ".github/workflows/ci.yml")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    assert sys.platform == "win32" and sys.version_info[:2] == (3, 12)
    product, output = args.product.resolve(), args.output.resolve()
    helper = Path(__file__).resolve().parent
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / "context.json"
    summary = output / "summary.json"
    assert not receipt.exists() and not summary.exists(), "fresh_output_required"

    def git(*parts):
        return subprocess.check_output(["git", "-C", str(product), *parts], stderr=subprocess.DEVNULL)

    def binding():
        assert git("rev-parse", "HEAD").decode().strip() == HEAD
        assert not git("status", "--porcelain", "--untracked-files=no").strip()
        files = {}
        for path in PATHS:
            blob, checkout = git("show", HEAD + ":" + path), (product / path).read_bytes()
            assert checkout.replace(b"\r\n", b"\n") == blob.replace(b"\r\n", b"\n")
            files[path] = {"git_blob_sha256": sha(blob), "checkout_sha256": sha(checkout)}
        return {"head": HEAD, "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
                "tracked_clean": True, "files": files}

    helper_head = subprocess.check_output(["git", "-C", str(helper), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    assert helper_head == os.environ["GITHUB_SHA"]
    assert not subprocess.check_output(["git", "-C", str(helper), "status", "--porcelain", "--untracked-files=no"], stderr=subprocess.DEVNULL).strip()
    initial = binding()
    state = {"schema": 2, "source": initial, "helper_head": helper_head,
        "helper_files": {name: sha((helper / name).read_bytes()) for name in
            ("ci002_context_probe.py", "ci002_context_plugin.py")},
        "python": platform.python_version(), "platform": platform.system(),
        "architecture": platform.machine(), "watchdog_s": 1500, "processes": 1,
        "acceptance": False, "source_unchanged": False, "observer_valid": False,
        "diagnosis": "pending", "exitcode": None,
        "limitations": "Diagnostic only; old CI remains RED. Raw pytest output is discarded. No automatic retry."}

    def save():
        summary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    save()
    exitcode = 2
    try:
        env = dict(os.environ)
        # Preserve original environment/addopts; only add the observer's import path/receipt.
        prior_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([str(helper), str(product)] + ([prior_path] if prior_path else []))
        env["TASKPAW_CI002_PRODUCT"] = str(product)
        env["TASKPAW_CI002_RECEIPT"] = str(receipt)
        command = [sys.executable, "-m", "pytest", "--cov=taskpaw_v3", "--cov-report=term-missing",
                   "-p", "ci002_context_plugin", "-x"]
        started = time.monotonic()
        try:
            result = subprocess.run(command, cwd=product, env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=1500, check=False)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        state.update({"exitcode": code, "elapsed_s": round(time.monotonic() - started, 3),
                      "receipt_present": receipt.exists()})
        if receipt.exists():
            raw = receipt.read_bytes()
            assert len(raw) <= 2 * 1024 * 1024
            data = json.loads(raw)
            state["receipt_sha256"] = sha(raw)
            cases = data.get("cases", [])
            reports = data.get("reports", [])
            collection = data.get("collection", {})
            state["observer_valid"] = bool(cases) and collection.get("count_matches") is True and collection.get("targets_match") is True and all(
                c["third_call_seen"] and c["observer_valid"] and c["observer_errors"] == 0 and
                c["dropped_records"] == 0 and c["truncated_events"] == 0 and c["bindings_restored"] and
                c["print_restored"] and c["tracing_unchanged"] and c["main_restored"] and
                c["coverage"]["version"] == "7.14.3" and c["coverage"]["core"] in ("CTracer", "PyTracer") and
                c["coverage"]["branch"] is True for c in cases)
            state["target_failure"] = any(r["case"] == "codex-new" and r["phase"] == "call" and r["outcome"] == "failed" for r in reports)
            state["third_call_failure"] = any(c["case"] == "codex-new" and c["third_result"] == 1 for c in cases)
            if code == 0:
                state["observer_valid"] &= len(cases) == 4 and sum(r["phase"] == "call" and r["outcome"] == "passed" for r in reports) == 4
            if code == 0 and state["observer_valid"]:
                state["diagnosis"] = "not_reproduced_cause_unresolved"
            elif code != 124 and state["target_failure"] and state["third_call_failure"] and state["observer_valid"]:
                state["diagnosis"] = "failure_captured_requires_causal_review"
            else:
                state["diagnosis"] = "diagnostic_incomplete_or_other_failure"
        else:
            state["diagnosis"] = "diagnostic_incomplete_or_other_failure"
        exitcode = code or (0 if state["observer_valid"] else 2)
    except Exception:
        state["diagnosis"] = "diagnostic_harness_invalid"
        exitcode = 2
    finally:
        try:
            state["source_unchanged"] = binding() == initial
        except Exception:
            state["source_unchanged"] = False
        if not state["source_unchanged"]:
            state["diagnosis"] = "diagnostic_source_binding_invalid"
            exitcode = 2
        save()
    print(json.dumps({key: state[key] for key in ("exitcode", "observer_valid", "source_unchanged", "diagnosis")}))
    return exitcode


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        print(json.dumps({"diagnosis": "diagnostic_preflight_invalid", "acceptance": False}))
        code = 2
    raise SystemExit(code)
