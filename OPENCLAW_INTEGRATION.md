# TaskPaw Hub — OpenClaw integration

The current read contract and examples are in
[OpenClaw integration — reading TaskPaw fleet status](docs/guides/openclaw-integration.md).

Read `~/.taskpaw-hub/hub.db` in SQLite read-only mode or the accompanying
`status.md`, using the configured Hub data directory if different. Check the
latest attempt, last success and age before reporting current health. A latest
historical `status_log` row or an old `ONLINE` line does not prove the service is
currently healthy.

The media processing center and its Jasna fleet are in top-level `workflow`;
legacy manually configured tasks remain in `monitors`. Empty `monitors` does not
mean an empty media queue. The current guide explains controller registration,
queue/dispatcher fields, list limits, mixed-version behavior and unavailable data.

OpenClaw must not poll or acknowledge Agent `/events`; the monitoring Hub is its
only consumer. The Mac is read-only: all execution and media operations stay on
the Windows controller/workers.
