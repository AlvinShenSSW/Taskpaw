# Dependency security scans

R15 repairs known advisories at the 2026-10-01 UTC baseline and adds read-only
weekly, manual and dependency-path PR scans. The authoritative inputs are
`uv.lock`, `taskpaw_v3/ui/package-lock.json`, `taskpaw_v3/src-tauri/Cargo.lock`,
their declarations, and `docs/security/dependency-exceptions.json`.

Python application support remains >=3.10. Scanner tooling runs separately on
Python 3.12; UI validation uses Node 22 and locked Rust validation uses 1.96.0.
The scanner does not sync the app environment, import/start TaskPaw, run package
lifecycle/build scripts, change a lock, invoke fixes or access app data/secrets.
Its temporary manifest-only installs validate lock consistency, not app startup.

## Run locally or in Actions

Install the pinned audit tool in a dedicated tooling directory, then scan:

```sh
CARGO_HOME="$PWD/build/dependency-tool-cargo-home" cargo +1.96.0 install \
  cargo-audit --locked --version 0.22.2 --root build/dependency-tools
uv run --no-project --python 3.12 python scripts/dependency_scan.py \
  --cargo-audit build/dependency-tools/bin/cargo-audit \
  --output-dir build/dependency-scan
```

Use Node 22 on PATH. The wrapper runs `pip-audit==2.10.1` in an isolated Python
3.12 tool environment; tool setup failure is an error. Cargo 1.96.0 must be
installed through rustup. Windows uses the corresponding `.exe` tool path.
Output must be a dedicated artifact directory; inside the checkout only
`build/dependency-scan` and its subdirectories are accepted. Do not point it at
source files, user data or an unrelated nonempty directory.

`.github/workflows/dependency-scan.yml` provides `workflow_dispatch`, Monday
03:17 UTC scans and dependency-path PR scans. It uses `contents: read`, checkout
without persisted credentials, no app/signing secrets and a 30-minute deadline.
It retains sanitized reports for 30 days even when a scan fails. It does not
post messages, open issues/PRs, upload SARIF, remediate, publish or deploy.

Read `summary.md` / `summary.json` first:

| Exit | Meaning | Action |
| --- | --- | --- |
| 0 | Complete `clean` or `accepted_exceptions`; the latter lists exact IDs, owners and expiry. | Review visible exceptions and final package boundaries. |
| 1 | Complete scan with actionable unexcepted findings, including dev tooling and yanked crates. | Research the official advisory, make a targeted compatible update, rebuild/test and rerun. |
| 2 | Scan, configuration, coverage, lock consistency or exception error. | Resolve the reported prerequisite/error and rerun; no exception can waive it. |

An audit CLI exit 1 can mean findings; it becomes a completed result only when
its supported JSON and exact inventory agree. Empty/malformed JSON, skipped
identities, failed network/database fetch, timeout, unexpected tool exit,
unsupported source/schema or input mutation fail closed. Available results from
other ecosystems survive an error. Command output/deadline caps prevent a
stalled scan from hanging indefinitely. Diagnostics retain bounded byte counts
and hashes without publishing tokens, auth headers, user configuration or raw
potentially sensitive stderr.

## Coverage and policy

Python audits every registry name/version in the universal lock, including
optional and platform-dependent packages, without evaluating away markers.
Multiple versions of one normalized name are partitioned into exact-pin batches;
required and audited identity sets must match. Roles can overlap: base, optional
v3/tray runtime candidates, build tooling and development. Unknown sources or
unresolved graph edges are errors, not exclusions.

npm audits the complete lock and separately `--omit=dev`, from an owned directory
with explicit public npm registry and empty user/global npm configuration.
`npm ci --ignore-scripts --no-audit` validates the copied manifest/lock. Reports
separate affected summaries, concrete installation paths and unique GHSA IDs.
Dev findings remain actionable. Production dependency labels identify candidate
bundle dependencies, not a finished asset SBOM.

Rust audits the full lock across targets, including normal/build/Linux paths.
The owned cwd `.cargo/audit.toml` and child `CARGO_HOME` require empty ignores,
all informational categories, no severity/architecture/OS filter, mandatory
fresh public RustSec fetch, enabled yank checking and index update. Exact
emitted report settings, unchanged policy/lock hashes and actual
origin/FETCH_HEAD/HEAD/report revision are validated. Ambient project/user audit
policy, Cargo source replacements, Git credential/config injection and proxies
are not scanner policy inputs. The two project exceptions are never passed to
cargo-audit's native ignore list.

[cargo-audit 0.22.2's implementation](https://github.com/rustsec/rustsec/blob/cargo-audit/v0.22.2/cargo-audit/src/auditor.rs)
can silently omit native yank checking in JSON mode after index errors. Its JSON
cannot certify that hidden pass succeeded. A small independent checker therefore
fetches the [public crates.io sparse index](https://doc.rust-lang.org/cargo/reference/registry-index.html)
for every locked source/name/exact-version/checksum, requires exact set equality
and records boolean yank status and HTTP/digest/time provenance. It uses verified
TLS, fixed public host, no redirects/auth/proxies/cached fallback, eight requests
maximum, 10-second socket / 15-second request bounds, 8 MiB per file / 256 MiB
aggregate and a 300-second parent worker deadline. Missing, partial, mismatched
or failed records are exit 2 even if only accepted GTK warnings remain. A true
yank is actionable and is not waived by those advisory exceptions. This checks
registry metadata; it does not download or execute crate code.

Artifacts contain input/tool/command hashes and versions, raw supported audit
JSON, exact inventories, Python and Rust coverage, Rust policy/database proof,
normalized findings and UTC time. Counts changing after a fresh advisory update
are possible; investigate unexplained deltas. Scans establish absence of
*reported known advisories at that time*, not absence of product vulnerabilities.

## Targeted R15 decisions and exposure

The dated npm baseline had 9 affected package summaries, 10 installation paths
(two brace-expansion copies) and 25 distinct GHSA IDs. The omit-dev scan left
fast-uri with eight advisories. Python had 48 registry identities, 40 raw records
and 22 distinct GHSA IDs across AnyIO/Pillow/pytest/setuptools. Rust's advisory
scan inventoried 433 records, two quick-xml vulnerabilities and seven warnings;
that earlier cargo-audit result did not certify independent yank coverage.

| Repair | Exposure / compatibility decision |
| --- | --- |
| AnyIO 4.14.2 | FastAPI/Starlette and explicitly collected PyInstaller backend dependency. Current networking/process call sites use stdlib APIs; lack of direct vulnerable helper calls limits exposure evidence, not shipped inventory. [Maintainer advisories](https://github.com/agronholm/anyio/security/advisories). |
| fast-uri 3.1.8 | RJSF AJV browser validator, observed in baseline built source maps. This does not prove server SSRF; it is not TaskPaw's backend HTTP allowlist. Compatible existing parent range. [Advisory](https://github.com/advisories/GHSA-hrr3-gc8f-f4qj). |
| Compatible npm transitives | Both brace-expansion lines, baseline-browser-mapping, browserslist, js-yaml 4.3.2, nanoid and PostCSS update within existing parent families. These are lint/build tooling; scanner severity does not establish an exposed application sink. |
| Vitest 4.1.11 separately | Dev runner migration from 2.x; Node 22 and existing Vite 6.4.3 peers supported. 4.1.11 fixes redirect mocks; 2/3 do not receive that fix. npm's suggested 5.x is unnecessary for these advisories and adds migration/default changes. Keep React 19, TypeScript 5.9.3, jsdom 25 and Vite/plugin majors. [Maintainer advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9), [v4 migration](https://github.com/vitest-dev/vitest/blob/v4.1.11/docs/guide/migration.md). |
| Pillow 12.3.0 | Optional `tray` stays optional; Python >=3.10 supported. Frozen V2 tray uses Image.new/ellipse with fixed pixels, not untrusted codecs/fonts. Dedicated compatibility test executes only that method with real Pillow and fake tray/thread objects, without Tk/app startup. [Release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html). |
| pytest 9.0.3, bounded <9.1 | Dev/CI runner; fixes Unix temporary-directory ownership handling. Python >=3.10 and existing ini config supported. Whole suite must pass; no assertion weakening. [Fix](https://github.com/pytest-dev/pytest/pull/14343). |
| setuptools 83.0.0 | PyInstaller build transitive, no TaskPaw sdist (`uv.package=false`). Reviewed advisory identifies 83.0.0; maintainer advisory's patched-version field lagged the release/fixing commit at review time. [Advisory](https://github.com/advisories/GHSA-h35f-9h28-mq5c), [fix](https://github.com/pypa/setuptools/commit/dd9f436a36486b4cb8a4c70a2321548b0be09b8f). |
| plist 1.10.0 / quick-xml 0.41.0 | Existing plist family; MSRV 1.88 fits Rust 1.96. Inspected plist uses plain Reader rather than advisory sinks; still repair the locked dependency. [RUSTSEC-2026-0194](https://rustsec.org/advisories/RUSTSEC-2026-0194.html), [0195](https://rustsec.org/advisories/RUSTSEC-2026-0195.html). |
| tauri-utils 2.10.1 / urlpattern 0.6 | Compatible Tauri-utils minor removes five UNIC maintenance warnings; MSRV 1.90 fits Rust 1.96. Includes other transitive changes requiring clean locked build tests. Direct Tauri 2.11.3/build 2.6.3 pins remain. |

## Exact temporary exceptions

Only `docs/security/dependency-exceptions.json` authorizes post-scan matches.
Current entries are glib **0.18.5 / RUSTSEC-2024-0429** (`unsound`, potential Linux
runtime exposure) and proc-macro-error **1.0.4 / RUSTSEC-2024-0370** (`unmaintained`,
Linux GTK macro build dependency). Patched glib >=0.20 cannot satisfy current GTK
0.18; proc-macro-error has no patch. GTK framework migration is outside this
narrow repair. These limits do not establish non-exploitability.

Both are owned by **@AlvinShenSSW**, reviewed **2026-10-01 UTC**, and expire
**2026-10-31 UTC exclusively** (at the start of that date). Maximum interval is
30 days. Earlier removal conditions are part of each entry. Exact package,
version, advisory, aliases and classification must match; future review dates,
expired/unused/duplicate/ambiguous entries or wildcards fail validation. A new
advisory on the same package stays actionable. Re-review requires a concrete
reviewed change, not automatic renewal. Scan errors and yanks cannot be waived.

## Rebuild and package boundaries

Validate changed locks before release: `uv lock --check`, clean dev sync at
Python 3.10 and 3.12, canonical whole `uv run pytest` plus existing coverage,
syntax/ruff/format/mypy gates; clean Node 22 `npm ci`, full UI test collection,
lint and TS/Vite build; Rust 1.96 `cargo metadata --locked` and `cargo test
--locked` after clean backend sidecar creation. Dedicated Pillow compatibility
must actually run in an isolated Pillow 12.3.0 environment; a default no-tray
skip is not that verification. Existing CI retains Linux/macOS/Windows Python
and Linux shell/bundle gates.

Clean `uv sync --frozen --extra build --extra v3` and
`uv run python scripts/build.py --skip-tauri` verify the PyInstaller archive
without launching it. The existing Linux `.deb` bundle smoke must rerun at final
lock hashes. A local Mac check does not substitute for Linux/Windows CI or real
installer acceptance. Never launch the normal packaged backend on a shared Mac
as an automated dependency test: its existing reclaim behavior can affect a
real app. Test-owned shell fixtures and archive inspection remain permitted.

Declared dependency groups and metadata roles are not proof of frozen contents:
the release sync can install default dev tooling and PyInstaller collects whole
modules. R13/V01 must inspect actual sidecar TOC/archive and final UI source maps.
V01 later validates Windows/macOS installers, external media tools and optional
V2 tray behavior on operator-owned machines. Scans do not perform deployment,
signing, real service/data/config access or claim completed manual acceptance.
