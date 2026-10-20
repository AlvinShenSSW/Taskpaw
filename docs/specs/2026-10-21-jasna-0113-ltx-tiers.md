# Jasna 0.11.3: four LTX quality tiers with the new Standard default (3.9.18)

- **Issue:** #283.
- **Base:** v3.9.17 (`885ead2`). Agent release 3.9.18.
- **Operator, 2026-10-21:** "帮我按照新的默认来配置" (configure it to the new defaults), and "在之前17版之上改版一个agent的18版" (make an agent 3.9.18 on top of 3.9.17).
- **Principle P0:** a single-user home tool. Make the smallest change that keeps Jasna 0.11.3 working with its new default.

## Upstream (verified against Jasna v0.11.3 source and the HF API)

- **`main.py` `CLI_RESTORATION_MODELS`:**
  - `ltx` → distilled2, the new default.
  - `ltx-4step` → distilled4.
  - `ltx-8step` → distill8, the old `ltx`.
  - `ltx-undistilled` → unchanged.
  - argparse uses `choices=`, so an unknown name fails visibly.
- **Flags:** every flag TaskPaw passes is unchanged:
  - `--ltx-fast`
  - `--ltx-large-canvas`
  - `--[no-]experimental-ltx-less-overlap`
  - `--ltx-auto-split`
  - `--working-directory`
- **Progress output:** unchanged. It is still `"{label} {n}/4 {stage}"` with the stages scan/encode/denoise/compose, plus `LTX speed test`. The only change is that the overall percent no longer goes backwards. `jasna_progress` needs no change.
- **Model files:** HF `Kruk2/ltx-restore`, revision `a6e596656f9374dd85536a9313c9a5511cbdd407`.
  - Each tier's model needs the model file plus the two shared VAE files.
  - "fast" means nvfp4 (`--ltx-fast`); otherwise int8.

  | Tier | int8 file / bytes / sha256 | nvfp4 file / bytes / sha256 |
  |---|---|---|
  | standard | `ltx-restore-alpha1-distill2-int8.safetensors.enc` 13368048749 `c23559f9d120df49266fc2dbe60ec384058d51834cf1a2da049e54b28aea51ad` | `ltx-restore-alpha1-distill2-nvfp4.safetensors.enc` 7733294997 `4aa08c146c8cc335e7f90d19aa293cc0dfe4f14520fd85efc257fe809510693b` |
  | good | `…-distill4-int8…` 13368048733 `1c0f1642232813f690d949b05fe90bdc02d3203e0092950de25e9cc961f30965` | `…-distill4-nvfp4…` 7733294981 `57098e622d02598467f903049f4d1a613852f832847a01c43da148f506876973` |
  | high | `…-distill8-int8…` 13368048773 `b006342a109c640a46ae91a5cd10e67186d968c63ef2f715c78bedf36936e057` | `…-distill8-nvfp4…` 7733295021 `54f2f4f6f7d3f5d60e18aca7bf6137da206a2b66f282e1a9078743c3173e62e2` |
  | top | `ltx-restore-alpha1-int8.safetensors.enc` 13368048821 `c72161b1afefe4d8cc0592fa57afef6c20466dcf912912f3628a451468780f83` | `ltx-restore-alpha1-nvfp4.safetensors.enc` 7733295061 `8301d5ba17660b577ebe906d343963208f7e5453e589ef2f7470087a3ba2ccb5` |

  The shared files are unchanged:
  - `vae.safetensors.enc`: 1472223815 bytes, `b9252d5a799748d1932d4eed60a2d77461949a19f6feb57f2238dbffe08f5e25`
  - `vae-decoder.safetensors.enc`: 38718609 bytes, `c60c3572871203bd364d6bb2390225fa4c347f7ea5c64fe468256c900caeea2d`

## Contract

- **C1. Setting.** `JasnaConfig.ltx_model: Literal["standard","good","high","top"]`, default `"standard"`. It replaces `ltx_best`.
  - CLI mapping: `standard→ltx`, `good→ltx-4step`, `high→ltx-8step`, `top→ltx-undistilled`.
  - `ltx_fast` and `fast_supported` are unchanged. `fast_supported` drops fast only when every known GPU is pre-Blackwell.
- **C2. Legacy configs keep loading.**
  - `BaseMonitorConfig` is `extra="forbid"`, and a rejected spec stops the whole agent at boot. So a `@model_validator(mode="before")` on `JasnaConfig` maps the legacy key:
    - `ltx_best` is removed.
    - If `ltx_model` is absent: `ltx_best: true` → `"top"`; otherwise `"standard"`.
    - If `ltx_model` is present, it wins.
  - This covers agent.yaml at boot, Hub-pushed specs, preset payloads and admin patches.
  - The editor must not downgrade a never-resaved `ltx_best: true` config to standard through RJSF defaults. Required behaviour:
    - opening it shows `top`;
    - saving it untouched keeps `top`;
    - choosing `standard` explicitly saves `standard`.
  - Use the smallest mechanism that gives this, for example the admin config view returning the legacy-migrated config so the form never sees `ltx_best`.
- **C3. Every caller follows the setting:**
  - `build_argv`
  - `_ltx_label` and `_ltx_mode` (these name the tier: `LTX standard|good|high|top` + fast; they are also the download `variant`)
  - `ltx_missing_files(exe_dir, model, fast)`, used by the monitor preflight and the download request
  - `jasna_presets` (`ltx_model="standard"`)
  - `ui_schema` `ui:order`
  - the UI catalog fixture
  - wizard tests
- **C4. Downloader.**
  - `jasna_download.REVISION` is the revision above.
  - `MODEL_FILES` is keyed by `(model, fast)` with all 8 files, and `FILES` holds sizes and sha256 for all 10 files exactly as in the table.
  - Sizes shown in the UI come from `FILES`.
  - The download worker protocol is unchanged.
- **C5. Labels (en / zh).** The backend schema renders the field as a select with titled options: `oneOf` of `const` + `title`, or the RJSF enum-names equivalent. The zh overlay in `schemaI18n` translates the option titles.

  | Value | English | Chinese |
  |---|---|---|
  | `standard` | Standard (default, fastest) | 标准画质（默认，最快） |
  | `good` | Good (~1.5x slower) | 良好画质（慢约 1.5 倍） |
  | `high` | High (~2.5x slower; the pre-0.11.3 default) | 高画质（慢约 2.5 倍，0.11.3 前的默认） |
  | `top` | Top (~5x slower; formerly "best") | 顶级画质（慢约 5 倍，原「最佳」） |

  - The field description states that the tiers need Jasna 0.11.3 or newer. On 0.11.2, `standard` runs the old distill8 model and `good` fails with Jasna's own CLI error.
- **C6. Stale text.** Update the following:
  - `ltx_1080p`: "Jasna 0.11.1 or newer" becomes 0.11.3.
  - `ltx_fast` help: drop the "standard LTX model does not fit an 8 GB card" wording that clashes with the new tier name; say int8.
  - `smallGpu` i18n: drop "Best".
  - `ltx_overlap` help: upstream wording ("when a mosaic stays on screen for more than 4 seconds").
  - The module docstrings of `jasna.py` and `jasna_download.py`.
- **C7. Release.**
  - Version 3.9.18 in `taskpaw_v3/__init__.py`, `src-tauri/Cargo.toml` (+ lock), `tauri.conf.json`, and `ui/package.json` (+ lock).
  - A CHANGELOG entry.
  - Windows agent installer, published like 3.9.17.

## Not in scope

- Jasna version detection (the requirement is stated in C5).
- Media workflow (EPIC #271) changes. The EPIC branch adopts this later.
- Editing the operator's agent.yaml. C2 migrates it on first load; any save rewrites it.

## Tests

- **Migration:**
  - `ltx_best` true → top; false or missing → standard; `ltx_model` present wins.
  - An agent boot / supervisor build from a yaml spec containing `ltx_best`.
  - The editor round-trip from C2, all three cases.
- **`build_argv`:** the model name per tier, with and without fast.
- **`ltx_missing_files`:** per `(model, fast)`.
- **`_ltx_mode` / label:** per tier.
- **Downloader manifest:** equals the table; the URL uses the new revision.
- **Presets:** build valid configs.
- **UI:** the select shows the four zh labels; the catalog fixture is regenerated.
- **Version:** test passes at 3.9.18.
