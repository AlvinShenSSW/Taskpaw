# macOS code-signing + notarization (#49)

The `release` workflow builds the macOS `.app`/`.dmg` via `tauri build`. With the
secrets below configured, it **Developer ID–signs and notarizes** them so
Gatekeeper opens them on a double-click. Without the secrets the build still
succeeds but is **unsigned** (open via right-click → Open, or
`xattr -dr com.apple.quarantine <app>`).

Signing requires an **Apple Developer account** ($99/yr) — it can't be done
without one, which is why this is operator-provided.

## Repo secrets to add

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | What it is | How to get it |
|--------|------------|---------------|
| `APPLE_CERTIFICATE` | base64 of your **Developer ID Application** cert as a `.p12` | In Keychain Access export the cert+key to `cert.p12`, then `base64 -i cert.p12 \| pbcopy` |
| `APPLE_CERTIFICATE_PASSWORD` | the password you set on that `.p12` | — |
| `APPLE_SIGNING_IDENTITY` | the identity string | e.g. `Developer ID Application: Your Name (TEAMID)` — `security find-identity -v -p codesigning` |
| `APPLE_ID` | your Apple ID email | — |
| `APPLE_PASSWORD` | an **app-specific password** for notarization | appleid.apple.com → Sign-In and Security → App-Specific Passwords |
| `APPLE_TEAM_ID` | your 10-char Team ID | developer.apple.com → Membership |

> Alternative to `APPLE_ID`/`APPLE_PASSWORD`: an App Store Connect **API key**
> (`APPLE_API_KEY` / `APPLE_API_ISSUER` / a `.p8` key file). The Apple-ID path is
> wired here because it needs no key file in CI.

## How it works

`tauri build` reads the `APPLE_*` env vars (the release step maps them from the
secrets). When `APPLE_SIGNING_IDENTITY` is set it imports `APPLE_CERTIFICATE` into
a temporary keychain, signs the app + the bundled `taskpaw-backend` sidecar with
the hardened runtime, then notarizes with the Apple-ID credentials and staples
the ticket. Empty vars ⇒ no signing.

## Verifying a build (on a Mac)

```bash
codesign -dv --verbose=4 "TaskPaw Agent.app"     # signed + hardened runtime
spctl -a -vvv -t install "TaskPaw Agent.app"     # "accepted" (Gatekeeper)
xcrun stapler validate "TaskPaw Agent.dmg"       # notarization ticket stapled
```

If notarization rejects an **unsigned nested binary**, confirm the
`taskpaw-backend` sidecar got signed (it's signed as part of the `.app`); a custom
entitlements file may be needed if the app later gains privileged capabilities.
