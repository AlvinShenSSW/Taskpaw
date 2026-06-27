#!/usr/bin/env bash
# moomoo_probe.sh — read-only recon for TaskPaw V3 issue #13 (#0).
#
# Run this ON the moomoo trading machine. It only READS — it never starts,
# stops, or modifies anything. It prints a structured report; copy the whole
# output back into GitHub issue #13.
#
# Goal: confirm the four life-signs' concrete config so the moomoo preset
# (V3 design §5.1) can be built:
#   1. process-manager type + orchestrator job name   (pm2 here)
#   2. orchestrator_heartbeat.json path + grace value
#   3. OpenD gateway port (default 11111, may be overridden)
#   4. pm2 daemon liveness probe method
#
# Usage:  bash scripts/recon/moomoo_probe.sh [MQT_HOME]
#   MQT_HOME (optional): MQT runtime root if not auto-found (e.g. ~/mqt).

set -uo pipefail

line() { printf '%s\n' "------------------------------------------------------------"; }
sec()  { line; printf '## %s\n' "$1"; line; }
note() { printf '   %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Portable file mtime. macOS/BSD `date -r` means epoch-seconds, not a file path
# (that's GNU), so use `stat` per-platform instead.
fmtmtime() {
  local f="$1"
  if [ "$(uname 2>/dev/null)" = "Darwin" ]; then
    stat -f '%Sm' -t '%Y-%m-%d %H:%M:%S %z' "$f" 2>/dev/null
  else
    stat -c '%y' "$f" 2>/dev/null
  fi
}

MQT_HOME="${1:-${MQT_HOME:-}}"

printf '===== TaskPaw V3 #13 — moomoo recon report =====\n'
printf 'host: %s   user: %s   date(local): %s\n' "$(hostname 2>/dev/null)" "$(whoami 2>/dev/null)" "$(date '+%Y-%m-%d %H:%M:%S %z' 2>/dev/null)"
printf 'os:   %s\n' "$(uname -a 2>/dev/null)"

# ── 1. process manager + orchestrator job ──────────────────────────────
sec "1. process manager (pm2) + orchestrator job"
if have pm2; then
  note "pm2 found: $(command -v pm2)  version: $(pm2 --version 2>/dev/null)"

  # READ-ONLY liveness: detect the PM2 God Daemon by process inspection.
  # Do NOT run `pm2 ping` / `pm2 list` first — those auto-SPAWN the daemon when
  # it is down, which both mutates the machine and makes "is it alive?" always
  # report alive. Process inspection never starts anything.
  daemon_alive=""
  if have pgrep; then
    if pgrep -fl -i 'PM2.*God' >/dev/null 2>&1; then daemon_alive="yes"; fi
    note "-- PM2 God Daemon process (read-only liveness) --"
    pgrep -afl -i 'PM2.*God' 2>/dev/null | sed 's/^/   /' \
      || pgrep -afl -i 'PM2 v' 2>/dev/null | sed 's/^/   /' \
      || note "   (no PM2 God Daemon process found → daemon NOT running)"
  else
    note "-- PM2 God Daemon process (read-only liveness) --"
    ps aux 2>/dev/null | grep -i 'PM2' | grep -iv grep | sed 's/^/   /' || true
    note "   (pgrep unavailable; inspect the ps output above for 'PM2 ... God Daemon')"
  fi

  if [ -n "$daemon_alive" ]; then
    # Safe now: daemon is already up, so jlist won't spawn it.
    note "-- pm2 jlist (daemon already up; name | pm_id | status | restarts) --"
    if have node; then
      pm2 jlist 2>/dev/null | node -e '
        let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
          try{for(const p of JSON.parse(s)){
            const e=p.pm2_env||{};
            console.log("   "+[p.name,p.pm_id,e.status,(e.restart_time??"?")+" restarts",e.pm_exec_path||""].join("  |  "));
          }}catch(err){console.log("   (could not parse pm2 jlist: "+err.message+")");}
        });' 2>/dev/null || pm2 list 2>&1 | sed 's/^/   /'
    else
      pm2 list 2>&1 | sed 's/^/   /'
    fi
  else
    note "-- pm2 jlist SKIPPED: daemon not detected (running it would spawn the daemon). --"
    note "   Job name comes from ecosystem.config.* below; if you need live status,"
    note "   the operator can run 'pm2 jlist' manually (note: that starts the daemon)."
  fi
else
  note "pm2 NOT on PATH. If the manager is launchd instead, report:"
  note "  launchctl list | grep -iE 'moomoo|orchestrator|opend|mqt'"
fi

note "-- ecosystem.config.* (defines job names) --"
found_eco=""
for base in "$MQT_HOME" "$HOME/mqt" "$HOME" "$PWD"; do
  [ -n "$base" ] || continue
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    found_eco="yes"
    note "found: $f"
    grep -nE "name[[:space:]]*:" "$f" 2>/dev/null | sed 's/^/      /'
  done < <(find "$base" -maxdepth 4 -name 'ecosystem.config.*' 2>/dev/null | head -5)
done
[ -n "$found_eco" ] || note "(no ecosystem.config.* found under searched roots — confirm job name from pm2 jlist above)"

# ── 2. orchestrator heartbeat ──────────────────────────────────────────
sec "2. orchestrator_heartbeat.json (path + grace)"
hb_found=""
for base in "$MQT_HOME" "$HOME/mqt" "$HOME" "$PWD"; do
  [ -n "$base" ] || continue
  while IFS= read -r hb; do
    [ -n "$hb" ] || continue
    hb_found="yes"
    note "found: $hb"
    note "  perms: $(ls -l "$hb" 2>/dev/null | awk '{print $1, $3, $4}')"
    note "  mtime: $(fmtmtime "$hb")"
    note "  content:"
    sed 's/^/      /' "$hb" 2>/dev/null | head -40
  done < <(find "$base" -maxdepth 5 -name 'orchestrator_heartbeat.json' 2>/dev/null | head -3)
done
if [ -z "$hb_found" ]; then
  note "(orchestrator_heartbeat.json NOT found — search wider and report the path:)"
  note "  find ~ -name 'orchestrator_heartbeat.json' 2>/dev/null"
fi
note "-- grace / watchdog threshold: look in paths.py / config for 'grace' / 'watchdog' / 'next_check' --"
for base in "$MQT_HOME" "$HOME/mqt" "$PWD"; do
  [ -n "$base" ] || continue
  grep -rInE "grace|watchdog|next_check_due|heartbeat" "$base" 2>/dev/null \
    --include='*.py' --include='*.toml' --include='*.json' --include='*.env' | head -12 | sed 's/^/   /'
done

# ── 3. OpenD gateway port ──────────────────────────────────────────────
sec "3. OpenD gateway port (default 11111)"
note "-- listeners on 11111 --"
if have lsof; then
  lsof -nP -iTCP:11111 -sTCP:LISTEN 2>/dev/null | sed 's/^/   /' || note "(none on 11111 via lsof)"
elif have netstat; then
  netstat -an 2>/dev/null | grep -E '\.11111|:11111' | sed 's/^/   /' || note "(none on 11111 via netstat)"
fi
note "-- OpenD process --"
if have pgrep; then
  pgrep -afl -i opend 2>/dev/null | sed 's/^/   /' || note "(no process matching 'opend')"
fi
note "-- port overrides in .env / config (api_port / opend / 11111) --"
for base in "$MQT_HOME" "$HOME/mqt" "$PWD" "$HOME"; do
  [ -n "$base" ] || continue
  grep -rInE "opend|api_port|11111|port" "$base" 2>/dev/null \
    --include='*.env' --include='.env' --include='*.toml' --include='*.ini' | grep -iE "opend|11111|port" | head -10 | sed 's/^/   /'
done

# ── 4. summary the moomoo-side agent should fill in ────────────────────
sec "4. ANSWER THESE in the issue (from the evidence above)"
cat <<'Q'
   [ ] 1a. process manager type:            pm2 / launchd / other = ____
   [ ] 1b. orchestrator pm2 job name:        ____   (status: online?)
   [ ] 2a. orchestrator_heartbeat.json path: ____
   [ ] 2b. grace / watchdog threshold:       ____ (seconds)
   [ ] 3a. OpenD port (actual):              ____ (default 11111?)
   [ ] 4a. pm2 daemon liveness (read-only):  God Daemon process present? proc name = ____
           (detected via pgrep 'PM2.*God' — NOT `pm2 ping`, which would start it)
Q
printf '===== end of report =====\n'
