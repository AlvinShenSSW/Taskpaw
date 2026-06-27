#!/usr/bin/env node
// codex-gate.mjs — cross-platform "外门 (outer gate)" Codex review wrapper.
//
// Runs `codex exec review` headless against a PR/branch diff and prints ONLY
// Codex's final review message on stdout (full transcript goes to a log file).
// Used by the /codex-review slash command and the standing PR workflow step.
// In default /afk this is the 外门 — it runs FIRST, before the Kimi 终审 (both
// run). Not used under /afk codex (Codex is the implementer there; Claude is 外门).
//
// Per-OS behavior:
//   - Windows: Codex's OS sandbox cannot launch under a normal user token
//     (CreateProcessAsUserW -> ACCESS_DENIED), so we pass
//     --dangerously-bypass-approvals-and-sandbox. Safe because `review` is
//     read-only and operates on your own git-tracked repo.
//   - macOS (Seatbelt) / Linux (Landlock): native sandbox works, so we do
//     NOT bypass — the review runs read-only under the OS sandbox.
//
// Usage:
//   node codex-gate.mjs                 # review current branch vs default base
//   node codex-gate.mjs --base master   # review vs an explicit base branch
//   node codex-gate.mjs --commit <sha>  # review one commit
//   node codex-gate.mjs --uncommitted   # review staged/unstaged/untracked
//   (any extra flags are passed through to `codex exec review`)
//
// Review SCOPE: relies on Codex's built-in `review` (already a structural code
// review). codex-cli 0.137.0+ made the diff selectors (--base/--commit/
// --uncommitted) mutually exclusive with a custom PROMPT, so the gate no longer
// injects a focus prompt via stdin (see #405).
//
// Exit code mirrors codex; 127 if the codex binary cannot be found.

import { spawnSync } from 'node:child_process';
import { existsSync, openSync, readFileSync, mkdtempSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const isWin = process.platform === 'win32';

function emitSkip(reason) {
  // A skip is NOT a failure — the gate is optional. Emit the marker block so the
  // caller (Claude / the workflow step) sees a clean SKIPPED result and
  // continues, and exit 0.
  process.stderr.write(`[codex-gate] skipped: ${reason}\n`);
  process.stdout.write('===== CODEX REVIEW (final message) =====\n');
  process.stdout.write(`SKIPPED: ${reason}\n`);
  process.stdout.write('===== END CODEX REVIEW =====\n');
  process.exit(0);
}

// Explicit opt-out — for users without a Codex subscription, or who simply do
// not want the gate. Set CODEX_REVIEW_GATE to off/0/false/no/disabled.
const gateFlag = (process.env.CODEX_REVIEW_GATE || '').trim().toLowerCase();
if (['off', '0', 'false', 'no', 'disabled'].includes(gateFlag)) {
  emitSkip('Codex gate disabled via CODEX_REVIEW_GATE.');
}

// Review scope is Codex's built-in `review` — a structural code review. No custom
// focus PROMPT is injected: codex-cli 0.137.0+ rejects a PROMPT alongside a diff
// selector (--base/--commit/--uncommitted), and the gate always selects a target.
// See #405.

function resolveCodex() {
  // Prefer PATH (works on macOS/Linux and Windows-with-PATH). On Windows also
  // fall back to the npm global shim, which isn't always on a child's PATH.
  if (isWin && process.env.APPDATA) {
    const shim = join(process.env.APPDATA, 'npm', 'codex.cmd');
    if (existsSync(shim)) return shim;
  }
  return 'codex';
}

function detectBase() {
  // origin/HEAD -> the repo's default branch (main/master/...); fall back sanely.
  const r = spawnSync('git', ['rev-parse', '--abbrev-ref', 'origin/HEAD'], {
    encoding: 'utf8',
  });
  if (r.status === 0 && r.stdout.trim()) {
    return r.stdout.trim().replace(/^origin\//, '');
  }
  for (const b of ['main', 'master']) {
    const v = spawnSync('git', ['rev-parse', '--verify', b], { encoding: 'utf8' });
    if (v.status === 0) return b;
  }
  return 'main';
}

const userArgs = process.argv.slice(2);
const hasTarget = userArgs.some((a) =>
  ['--base', '--commit', '--uncommitted'].includes(a),
);

const work = mkdtempSync(join(tmpdir(), 'codex-gate-'));
const finalFile = join(work, 'review.txt');
const logFile = join(work, 'codex.log');

// Lean context (token/cost control), per-run config overrides (`-c`, scoped to THIS
// call only — the operator's interactive Codex config is untouched):
//   - model_reasoning_effort=medium (default): a structural diff review does not need
//     the global high/xhigh. Clean win, no quality cost. Override CODEX_REVIEW_REASONING.
//   - project_doc_max_bytes: Codex loads AGENTS.md / AGENTS.override.md into every
//     review turn. Capping/zeroing it saves tokens — BUT this repo's AGENTS.md is small
//     (~5 KB) AND carries review-relevant constraints (the PyQt-in-core ban, fixture
//     rules), so stripping it loses more than it saves. So we do NOT override it by
//     default — the reviewer keeps the 禁区. Opt in (e.g. `=0` for max lean, or `=16384`
//     to cap) via CODEX_REVIEW_PROJECT_DOC_MAX_BYTES, mainly useful where AGENTS.md is large.
const reasoning = (process.env.CODEX_REVIEW_REASONING || 'medium').trim();
const projectDocMaxBytes = (process.env.CODEX_REVIEW_PROJECT_DOC_MAX_BYTES || '').trim();

const reviewArgs = ['exec', 'review'];
reviewArgs.push('-c', `model_reasoning_effort=${reasoning}`);
if (projectDocMaxBytes) reviewArgs.push('-c', `project_doc_max_bytes=${projectDocMaxBytes}`);
if (!hasTarget) reviewArgs.push('--base', detectBase());
reviewArgs.push(...userArgs);
reviewArgs.push('-o', finalFile);
if (isWin) reviewArgs.push('--dangerously-bypass-approvals-and-sandbox');
// Do NOT append a positional PROMPT. codex-cli 0.137.0+ errors out with
// "the argument '--base <BRANCH>' cannot be used with '[PROMPT]'" when a diff
// selector and a PROMPT are combined; the gate always selects a target, so it
// uses Codex's built-in structural review with no custom prompt (#405).

const codex = resolveCodex();

// Availability + auth pre-check (local only — reads ~/.codex/auth.json, NO model
// call / no metered cost). Skip cleanly if Codex is missing or not logged in
// (e.g. no subscription) instead of erroring — this is what makes the gate safe
// to leave always-on in the workflow.
const auth = spawnSync(codex, ['login', 'status'], { encoding: 'utf8', shell: isWin });
if (auth.error && auth.error.code === 'ENOENT') {
  emitSkip('Codex CLI not installed (run: npm i -g @openai/codex && codex login).');
}
const authOut = `${auth.stdout || ''}${auth.stderr || ''}`;
if (/not logged in/i.test(authOut) || !/logged in/i.test(authOut)) {
  emitSkip('Codex not authenticated — run `codex login`, or set CODEX_REVIEW_GATE=off to disable this gate.');
}

process.stderr.write(`[codex-gate] ${codex} ${reviewArgs.join(' ')}\n`);
process.stderr.write(`[codex-gate] transcript -> ${logFile}\n`);

// Send Codex's verbose transcript to the log file; keep our stdout clean so the
// caller (Claude) reads only the final verdict.
const fd = openSync(logFile, 'w');
const res = spawnSync(codex, reviewArgs, {
  stdio: ['ignore', fd, fd], // no stdin (no custom prompt); stdout/stderr -> log file
  shell: isWin, // needed to launch the .cmd shim on Windows
});

if (res.error) {
  if (res.error.code === 'ENOENT') {
    process.stderr.write(
      '[codex-gate] codex CLI not found. Install with: npm i -g @openai/codex (then `codex login`).\n',
    );
    process.exit(127);
  }
  process.stderr.write(`[codex-gate] failed to launch codex: ${res.error.message}\n`);
  process.exit(1);
}

if (existsSync(finalFile)) {
  const review = readFileSync(finalFile, 'utf8').trim();
  process.stdout.write('===== CODEX REVIEW (final message) =====\n');
  process.stdout.write(review + '\n');
  process.stdout.write('===== END CODEX REVIEW =====\n');
} else {
  process.stderr.write(
    `[codex-gate] No final message produced (exit ${res.status}). See ${logFile}\n`,
  );
}

process.exit(res.status ?? 1);
