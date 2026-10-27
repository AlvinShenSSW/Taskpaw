# Dependency security decisions

R15 repaired known advisories at the 2026-10-01 UTC baseline. The authoritative
inputs are `uv.lock`, `taskpaw_v3/ui/package-lock.json`,
`taskpaw_v3/src-tauri/Cargo.lock` and their declarations. The dedicated scanner
that produced the baseline below was removed in 3.10.x (#335); this page keeps
the dependency decisions and the rebuild boundaries.

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
| Compatible npm transitives | Both brace-expansion lines, baseline-browser-mapping, browserslist, js-yaml 4.3.2, nanoid and PostCSS update within existing parent families. These are lint/build tooling; advisory severity does not establish an exposed application sink. |
| Vitest 4.1.11 separately | Dev runner migration from 2.x; Node 22 and existing Vite 6.4.3 peers supported. 4.1.11 fixes redirect mocks; 2/3 do not receive that fix. npm's suggested 5.x is unnecessary for these advisories and adds migration/default changes. Keep React 19, TypeScript 5.9.3, jsdom 25 and Vite/plugin majors. [Maintainer advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9), [v4 migration](https://github.com/vitest-dev/vitest/blob/v4.1.11/docs/guide/migration.md). |
| Pillow 12.3.0 | Optional `tray` stays optional; Python >=3.10 supported. Its only consumer was the V2 tray icon (removed in 3.10.x), which drew fixed pixels with Image.new/ellipse, not untrusted codecs/fonts. [Release notes](https://pillow.readthedocs.io/en/stable/releasenotes/12.3.0.html). |
| pytest 9.0.3, bounded <9.1 | Dev/CI runner; fixes Unix temporary-directory ownership handling. Python >=3.10 and existing ini config supported. Whole suite must pass; no assertion weakening. [Fix](https://github.com/pytest-dev/pytest/pull/14343). |
| setuptools 83.0.0 | PyInstaller build transitive, no TaskPaw sdist (`uv.package=false`). Reviewed advisory identifies 83.0.0; maintainer advisory's patched-version field lagged the release/fixing commit at review time. [Advisory](https://github.com/advisories/GHSA-h35f-9h28-mq5c), [fix](https://github.com/pypa/setuptools/commit/dd9f436a36486b4cb8a4c70a2321548b0be09b8f). |
| plist 1.10.0 / quick-xml 0.41.0 | Existing plist family; MSRV 1.88 fits Rust 1.96. Inspected plist uses plain Reader rather than advisory sinks; still repair the locked dependency. [RUSTSEC-2026-0194](https://rustsec.org/advisories/RUSTSEC-2026-0194.html), [0195](https://rustsec.org/advisories/RUSTSEC-2026-0195.html). |
| tauri-utils 2.10.1 / urlpattern 0.6 | Compatible Tauri-utils minor removes five UNIC maintenance warnings; MSRV 1.90 fits Rust 1.96. Includes other transitive changes requiring clean locked build tests. Direct Tauri 2.11.3/build 2.6.3 pins remain. |

## Rebuild and package boundaries

Validate changed locks before release: `uv lock --check`, clean dev sync at
Python 3.10 and 3.12, canonical whole `uv run pytest` plus existing coverage,
ruff/format/mypy gates; clean Node 22 `npm ci`, full UI test collection,
lint and TS/Vite build; Rust 1.96 `cargo metadata --locked` and `cargo test
--locked` after clean backend sidecar creation. Existing CI retains
Linux/macOS/Windows Python and Linux shell/bundle gates.

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
V01 later validates Windows/macOS installers and external media tools on
operator-owned machines. These checks do not perform deployment, signing, real
service/data/config access or claim completed manual acceptance.
