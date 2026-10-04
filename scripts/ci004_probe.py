"""Bounded Windows reproduction sampling, never product acceptance."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HEAD = "3247dccdce661c25f34e891464099659b3845e6b"
TREE = "f9ddc85973ab561180690d1eb61f8cef293b3786"
PINS = {
    "taskpaw_v3/integrations/activity_writer.py": "67c7a98f9d6f1c7c7f7888769a7069ff12b0319b6bc208ea65184febb075140d",
    "taskpaw_v3/tests/test_activity.py": "b75a9dc1557f530c3012f950c58b99583b7ed71fd27a0b362f81ac3a039815fc",
}
HELPERS = (
    "scripts/ci004_probe.py",
    "scripts/ci004_plugin.py",
    "scripts/ci004_child/sitecustomize.py",
    ".github/workflows/release.yml",
)
TARGET = "taskpaw_v3/tests/test_activity.py::test_i216_copied_absolute_writer_concurrent_sessions"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def receipt_valid(data, trace, code):
    """Validate completeness separately from the reproduced test outcome."""
    if (
        data.get("errors") != 0
        or data.get("collection_exact") is not True
        or data.get("restored") is not True
        or data.get("exitcode") != code
    ):
        return False
    reports = data.get("reports", [])
    if len(reports) != 3 or [(r["phase"], r["outcome"]) for r in reports] not in (
        [("setup", "passed"), ("call", "passed"), ("teardown", "passed")],
        [("setup", "passed"), ("call", "failed"), ("teardown", "passed")],
    ):
        return False
    children = data.get("children", [])
    indices = [r["index"] for r in children]
    if indices != list(range(13)) and not (
        indices == [0] and children[0]["returncode"] != 0
    ):
        return False
    if any(
        r["exception"] is not None or type(r["returncode"]) is not int for r in children
    ):
        return False
    if trace and any(
        not r["trace"]
        or r["trace"]["valid"] is not True
        or r["trace"]["errors"] != 0
        or r["trace"]["dropped"] != 0
        for r in children
    ):
        return False
    return code in (0, 1) and ((reports[1]["outcome"] == "passed") == (code == 0))


def main():
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    product, output = args.product.resolve(), args.output.resolve()
    helper = Path(__file__).resolve().parents[1]
    if sys.platform != "win32" or sys.version_info[:2] != (3, 12):
        raise ValueError("native_windows_python312_required")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("fresh_output_required")
    summary = {
        "schema": 1,
        "acceptance": False,
        "historical_cause_proven": False,
        "diagnosis": "preflight_pending",
        "watchdog_s": 240,
        "pytest_timeout_s": 30,
        "trace_round_limit": 8,
        "rounds": [],
        "source_unchanged": False,
        "helper_unchanged": False,
        "original_target_failed": False,
        "limitations": "Target-only reproduction sampling. Observer overhead changes scheduling; all-pass leaves cause unresolved. Original Windows CI remains failed. Raw pytest and child output discarded.",
    }

    def remaining():
        return 240 - (time.monotonic() - started)

    def git(root, *parts):
        if remaining() <= 0:
            raise TimeoutError("watchdog")
        return subprocess.check_output(
            ["git", "-C", str(root), *parts],
            stderr=subprocess.DEVNULL,
            timeout=min(5, remaining()),
        )

    def binding(root, expected_head, expected_tree=None):
        head = git(root, "rev-parse", "HEAD").decode().strip()
        tree = git(root, "rev-parse", "HEAD^{tree}").decode().strip()
        if (
            head != expected_head
            or (expected_tree and tree != expected_tree)
            or git(root, "status", "--porcelain", "--untracked-files=normal").strip()
        ):
            raise ValueError("source_binding_invalid")
        names = git(root, "ls-files", "-z").decode().split("\0")
        names = [
            p
            for p in names
            if p.endswith(".py")
            or p in ("pyproject.toml", "uv.lock")
            or p.startswith(".github/workflows/")
        ]
        # The immutable tree pins git blobs; checkout-byte hashes additionally
        # expose native checkout newline conversion without copying source.
        files = {name: sha((root / name).read_bytes()) for name in names}
        if root == product:
            for name, pinned in PINS.items():
                if sha(git(root, "show", HEAD + ":" + name)) != pinned:
                    raise ValueError("source_pin_invalid")
        return {"head": head, "tree": tree, "clean": True, "checkout_sha256": files}

    def save():
        destination = output / "summary.json"
        temporary = output / "summary.tmp"
        temporary.write_text(
            json.dumps(summary, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        os.replace(temporary, destination)

    save()
    initial = helper_initial = None
    code = 2
    try:
        initial = binding(product, HEAD, TREE)
        helper_initial = binding(helper, os.environ["GITHUB_SHA"])
        summary.update(
            source_before=initial,
            helper_head=os.environ["GITHUB_SHA"],
            helper_files={name: sha((helper / name).read_bytes()) for name in HELPERS},
            helper_clean_before=True,
            python=list(sys.version_info[:3]),
            native_windows=True,
            product_blob_pins=PINS,
        )
        writer_sha = initial["checkout_sha256"][
            "taskpaw_v3/integrations/activity_writer.py"
        ]
        for number in range(9):
            tracing = number != 0
            budget = min(30, remaining() - 10)
            if budget <= 0:
                summary["diagnosis"] = "watchdog_budget_exhausted"
                break
            receipt = output / ("round-" + str(number) + ".json")
            env = dict(os.environ)
            env["TASKPAW_CI004_HAD_PYTHONPATH"] = "1" if "PYTHONPATH" in env else "0"
            env["TASKPAW_CI004_ORIGINAL_PYTHONPATH"] = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = str(helper / "scripts") + (
                os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
            )
            env.update(
                TASKPAW_CI004_RECEIPT=str(receipt),
                TASKPAW_CI004_TRACE="1" if tracing else "0",
                TASKPAW_CI004_WRITER_SHA=writer_sha,
            )
            command = [
                sys.executable,
                "-m",
                "pytest",
                "--cov=taskpaw_v3",
                "--cov-report=term-missing",
                "-p",
                "ci004_plugin",
                TARGET,
            ]
            process = subprocess.Popen(
                command,
                cwd=product,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            timed_out = False
            try:
                result = process.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                timed_out = True
                # Kill the owned process tree while pytest still exists; avoid
                # leaving its independently launched writers after a watchdog.
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                finally:
                    process.kill()
                    process.wait(timeout=2)
                result = 124
            round_data = {
                "number": number,
                "trace": tracing,
                "exitcode": result,
                "timed_out": timed_out,
                "observer_valid": False,
            }
            summary["rounds"].append(round_data)
            if receipt.exists() and receipt.stat().st_size <= 400000:
                data = json.loads(receipt.read_text(encoding="utf-8"))
                round_data["observer_valid"] = receipt_valid(data, tracing, result)
                round_data["receipt_sha256"] = sha(receipt.read_bytes())
                round_data["nonzero_children"] = [
                    row["index"]
                    for row in data["children"]
                    if row["returncode"] not in (None, 0)
                ]
            if timed_out or not round_data["observer_valid"]:
                summary["diagnosis"] = "diagnostic_incomplete_or_invalid"
                break
            if not tracing:
                summary["original_target_failed"] = result != 0
            if tracing and result != 0:
                summary["diagnosis"] = (
                    "failure_captured_requires_causal_review"
                    if round_data["nonzero_children"]
                    else "target_assertion_failure_without_child_failure"
                )
                break  # Never retry after any failed trace round.
            summary["diagnosis"] = (
                "original_failure_without_traced_reproduction_cause_unresolved"
                if summary["original_target_failed"]
                else "not_reproduced_cause_unresolved"
            )
            save()
        code = 0 if summary["diagnosis"] == "not_reproduced_cause_unresolved" else 1
    except Exception:
        summary["diagnosis"] = "diagnostic_harness_invalid"
        code = 2
    finally:
        try:
            summary["source_unchanged"] = binding(product, HEAD, TREE) == initial
            summary["helper_unchanged"] = (
                binding(helper, os.environ["GITHUB_SHA"]) == helper_initial
            )
            summary["source_clean_after"] = summary["source_unchanged"]
            summary["helper_clean_after"] = summary["helper_unchanged"]
        except Exception:
            summary["source_unchanged"] = summary["helper_unchanged"] = False
        if not summary["source_unchanged"] or not summary["helper_unchanged"]:
            summary["diagnosis"] = "diagnostic_binding_invalid"
            code = 2
        summary["elapsed_s"] = round(time.monotonic() - started, 3)
        summary["exitcode"] = code
        save()
    print(
        json.dumps(
            {
                key: summary[key]
                for key in (
                    "diagnosis",
                    "acceptance",
                    "source_unchanged",
                    "helper_unchanged",
                )
            }
        )
    )
    return code


if __name__ == "__main__":
    try:
        status = main()
    except Exception:
        print(
            json.dumps(
                {"diagnosis": "diagnostic_preflight_invalid", "acceptance": False}
            )
        )
        status = 2
    raise SystemExit(status)
