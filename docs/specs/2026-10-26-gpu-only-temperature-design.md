# GPU-only temperature display

## Scope

For the current homelab, the power page should show GPU temperature only on the
local machine and every remote machine. CPU and motherboard/chassis temperature
placeholders add no useful information while their readers are unavailable.

## Change

Render only `gpu` in the shared `TemperatureStrip`, using a single-column layout.
Keep its measured value, source, readable GPU coverage, stale sample handling,
missing-data message and sensor error handling. Hide CPU/board entries even if a
peer supplies values. Do not change temperature sampling, wire fields, power
estimates, history, task handling or cross-machine upgrade rules.

## Validation and delivery

Update the existing UI tests first to require GPU-only local and peer strips,
including payloads with actual CPU/board readings. Preserve existing GPU freshness,
invalid-reading, partial-coverage and older-peer cases. Run the relevant tests,
UI lint/build and version consistency check. Bump the six V3 version files to
3.10.6; V2 stays frozen. No installation or service restart is part of delivery.
