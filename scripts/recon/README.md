# Recon scripts (V3 #0 / issue #13)

Read-only probes that gather the facts the V3 design needs before implementation.
They **only read** — never start/stop/modify a service.

## `moomoo_probe.sh`

Run on the **moomoo trading machine** to confirm the four life-signs' config
(process-manager + orchestrator job name, heartbeat path + grace, OpenD port,
pm2 daemon liveness). See V3 design §5.1.

```bash
bash scripts/recon/moomoo_probe.sh [MQT_HOME]
```

`MQT_HOME` is optional — pass the MQT runtime root (e.g. `~/mqt`) if the script
can't auto-locate it. Copy the **entire output** back into GitHub issue #13.

## Not needed

- **Windows GPU**: already solved in V2 (`taskpaw.py` `_get_gpu_info()` via
  `nvidia-smi`). The V3 `host_metrics` plugin reuses it — no recon required.
- **macOS GPU**: ignored by operator decision (field = `n/a`).
