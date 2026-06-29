# Windows code-signing (#112)

The `release` workflow builds the Windows `.exe` (NSIS) and `.msi` installers via
`tauri build`. With the secrets below configured, a post-build step
**Authenticode-signs** them (SHA-256 + an RFC-3161 timestamp), so installing has
no "unknown publisher" SmartScreen / UAC warning. Without the secrets the build
still succeeds but the installers are **unsigned** (the prior behaviour) — parity
with the unsigned-macOS path, so a fork / PR / secret-less run never fails.

Signing needs an **Authenticode code-signing certificate** (from a CA such as
DigiCert / Sectigo, or an org-internal CA). That's why it's operator-provided.

## Repo secrets to add

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | What it is | How to get it |
|--------|------------|---------------|
| `WINDOWS_CERTIFICATE` | base64 of your code-signing cert as a password-protected `.pfx` (PKCS#12, cert + private key) | export from `certmgr`/your CA as `cert.pfx`, then `base64 -w0 cert.pfx` (Linux) or `[Convert]::ToBase64String([IO.File]::ReadAllBytes("cert.pfx"))` (PowerShell) |
| `WINDOWS_CERTIFICATE_PASSWORD` | the password set on that `.pfx` | — |

## How it works

The `Sign Windows installers (gated on secrets)` step (Windows job only):

1. Skips cleanly if `WINDOWS_CERTIFICATE` is empty (unsigned build).
2. Resolves the newest `signtool.exe` from the Windows SDK on the runner.
3. Writes the `.pfx` from the base64 secret to `$RUNNER_TEMP` (deleted in a
   `finally`).
4. For every `.exe`/`.msi` under `taskpaw_v3/src-tauri/target/release/bundle`:
   `signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256`, then
   `signtool verify /pa`.

Verify a downloaded installer locally: right-click → Properties → **Digital
Signatures**, or `signtool verify /pa /v <installer>`.

## Notes / limits

- This signs the **installer artifacts** (what users download and run — the
  SmartScreen-relevant surface). Signing the inner `taskpaw.exe` *inside* the
  bundle via Tauri's native `bundle.windows.certificateThumbprint` is a possible
  follow-up.
- A brand-new certificate still accrues SmartScreen reputation over time; an EV
  certificate gets reputation immediately but needs a hardware token / a cloud
  signing service (e.g. Azure Trusted Signing) — out of scope here.
- The timestamp server keeps signatures valid after the cert expires; swap the
  URL if your CA recommends a different one.
