# #108 Close-confirm dialog follows the app language — design (2026-06-29)

The window-close confirmation (#52) is bilingual (中 + EN in one dialog) because
the native Rust dialog can't read the webview's chosen language. Make it follow
the app's language setting (Settings → 中文 / English) instead. Issue:
[#108](https://github.com/AlvinShenSSW/Taskpaw/issues/108).

## Problem

`close_confirm_text(role)` in `src-tauri/src/main.rs` returns a bilingual title +
body, and the dialog buttons are `"关闭 / Close"` / `"取消 / Cancel"`. The text is
computed once at `setup` and captured into the close-event closure. The language
choice lives in the webview's `localStorage["taskpaw.lang"]` (`ui/src/i18n.ts`),
which the shell never sees.

## Design — frontend pushes the language into shell state

1. **Rust managed state `UiLang(Mutex<String>)`**, default `"zh-CN"` (matches the
   i18n default). `app.manage(...)` in `setup`.
2. **Custom command `set_ui_lang(lang)`** — validates `lang ∈ {"zh-CN","en"}`
   (ignore anything else, keep prior), stores it in `UiLang`. Registered via
   `invoke_handler(generate_handler![set_ui_lang])`. (Tauri v2 app commands are
   callable by the app's own window without a capability entry; the ACL gates
   plugin/core commands only.)
3. **Compute the dialog text at close time, per-language.** Replace
   `close_confirm_text(role) -> (title, msg)` with
   `close_confirm_text(role, lang) -> (title, msg, ok, cancel)` returning a single
   language. The close-event closure reads the current lang from `UiLang` state
   and builds title/msg/buttons then (so a mid-session language switch is honored
   without restart).
4. **Frontend syncs on init + on change** (`ui/src/i18n.ts`): a small
   `syncLangToShell(lang)` that, only inside the Tauri shell (`window.__TASKPAW__`
   present), dynamically imports `@tauri-apps/api/core` `invoke` and calls
   `set_ui_lang` (fire-and-forget, errors swallowed — same dynamic-import + guard
   pattern as `PathWidget.tsx`). Called once for the initial language and from
   `setLang()`.

The window is only closeable after the page has loaded (the user has to interact),
by which point the initial `set_ui_lang` has run — so the dialog always reflects
the active language. If the frontend never calls it (older UI), the state stays
`"zh-CN"` (the default), i.e. Chinese — no longer bilingual, still sensible.

## Why not read localStorage from Rust

The webview's localStorage isn't accessible from the Rust side; pushing the value
via a command is the idiomatic Tauri path and makes the dialog reactive to
in-session language changes.

## Test plan

- **Rust unit tests** (`cargo test`, in `main.rs`'s `mod tests`): `close_confirm_text`
  returns Chinese-only for `"zh-CN"`, English-only for `"en"` (no `/` bilingual
  separator), for both `agent` and `hub` roles; an unknown lang falls back to the
  default language. Role tailoring (hub mentions aggregation; agent mentions this
  machine) preserved.
- **Frontend** (`vitest`): `setLang` still updates i18n + `document.documentElement.lang`
  (existing behavior intact); `syncLangToShell` is a no-op outside Tauri (no
  `window.__TASKPAW__`) — guards the browser/dev path.
- **Manual / smoke**: rebuild the exe; switch language; the close dialog shows one
  language. (Runtime IPC path isn't covered by unit tests, so verify in the app.)

## Out of scope

Localizing OS-provided dialog chrome beyond the title/body/buttons we set.
