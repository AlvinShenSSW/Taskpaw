# TaskPaw Hub - OpenClaw Integration Guide

## Overview

TaskPaw Hub monitors 4 Windows AI servers (running Lada, ComfyUI, etc.) from the Mac Mini. It stores all status data locally. OpenClaw can read this data to answer questions about server status.

## Data Sources

Both files are on the same Mac Mini, updated every 60 seconds by TaskPaw Hub.

### Option 1: Markdown Status File (simplest)

**Path:** `~/.taskpaw-hub/status.md`

This file contains a human-readable snapshot of all servers. It is overwritten every poll cycle (every 60 seconds). Example content:

```
# TaskPaw Hub Status

Last updated: 2026-03-17 17:30:00

## BlackSilverPig: ONLINE
- Lada Monitor: Listening
- ComfyUI: Processing (1 running, 17 pending)

## SnowLeopard: OFFLINE (last seen 17:15:00)

## DarkbluePig: ONLINE
- Lada Monitor: Listening

## BlackGoldPig: ONLINE
- Lada Monitor: Listening
```

**How to use:** Read the file contents. The status is always current (updated every minute).

### Option 2: SQLite Database (full history)

**Path:** `~/.taskpaw-hub/hub.db`

This database contains full historical data. Open it read-only to avoid locking conflicts with the Hub.

**Connection string:** `sqlite:///Users/youruser/.taskpaw-hub/hub.db`

#### Tables

**servers** - Registered Windows servers

| Column     | Type    | Description                    |
|------------|---------|--------------------------------|
| id         | INTEGER | Primary key                    |
| name       | TEXT    | Server name (e.g. BlackSilverPig) |
| ip         | TEXT    | Server IP address              |
| port       | INTEGER | API port (default 5678)        |
| enabled    | INTEGER | 1 = active, 0 = disabled       |
| created_at | TIMESTAMP | When the server was added     |

**status_log** - Historical status snapshots (pruned after 7 days)

| Column      | Type    | Description                      |
|-------------|---------|----------------------------------|
| id          | INTEGER | Primary key                      |
| server_id   | INTEGER | Foreign key to servers.id        |
| timestamp   | TIMESTAMP | When this status was recorded  |
| status_json | TEXT    | Full JSON status from the server |

The `status_json` field contains the raw response from the Windows server's API, for example:

```json
{
  "machine": "BlackSilverPig",
  "uptime_seconds": 3600,
  "api_version": "2.0.0",
  "monitors": [
    {"name": "Lada Monitor", "type": "lada", "status": "Listening", "enabled": true},
    {"name": "ComfyUI", "type": "comfyui", "status": "Processing (1 running, 17 pending)", "enabled": true}
  ]
}
```

**events** - Notable events (monitor completions, errors, etc.)

| Column    | Type    | Description                          |
|-----------|---------|--------------------------------------|
| id        | INTEGER | Primary key                          |
| server_id | INTEGER | Foreign key to servers.id            |
| timestamp | TIMESTAMP | When the event occurred             |
| machine   | TEXT    | Server name                          |
| monitor   | TEXT    | Monitor name that triggered it       |
| message   | TEXT    | Event description                    |

**config** - Hub configuration key-value store

| Column | Type | Description           |
|--------|------|-----------------------|
| key    | TEXT | Config key            |
| value  | TEXT | Config value          |

#### Useful Queries

**Get current status of all servers (latest poll per server):**

```sql
SELECT s.name, sl.timestamp, sl.status_json
FROM servers s
LEFT JOIN status_log sl ON s.id = sl.server_id
  AND sl.id = (SELECT MAX(id) FROM status_log WHERE server_id = s.id)
WHERE s.enabled = 1
ORDER BY s.name;
```

**Get recent events (last 24 hours):**

```sql
SELECT e.timestamp, s.name AS server, e.monitor, e.message
FROM events e
JOIN servers s ON e.server_id = s.id
WHERE e.timestamp >= datetime('now', '-1 day')
ORDER BY e.timestamp DESC;
```

**Check if any server is offline (no status in last 5 minutes):**

```sql
SELECT s.name, MAX(sl.timestamp) AS last_seen
FROM servers s
LEFT JOIN status_log sl ON s.id = sl.server_id
WHERE s.enabled = 1
GROUP BY s.id
HAVING last_seen IS NULL OR last_seen < datetime('now', '-5 minutes');
```

**Get ComfyUI processing history for a specific server:**

```sql
SELECT sl.timestamp, json_extract(value, '$.status') AS status
FROM status_log sl, json_each(sl.status_json, '$.monitors')
WHERE sl.server_id = (SELECT id FROM servers WHERE name = 'SnowLeopard')
  AND json_extract(value, '$.type') = 'comfyui'
ORDER BY sl.timestamp DESC
LIMIT 20;
```

## Server Names

| Name           | Role                          |
|----------------|-------------------------------|
| BlackSilverPig | AI workstation                |
| BlackGoldPig   | AI workstation                |
| DarkbluePig    | AI workstation                |
| SnowLeopard    | AI workstation (ComfyUI)      |

## Monitor Types

| Type     | Description                                    |
|----------|------------------------------------------------|
| lada     | Monitors Lada AI workflow status               |
| comfyui  | Monitors ComfyUI queue (running + pending)     |
| folder   | Watches a folder for new/changed files         |
| process  | Monitors a system process                      |
| custom   | Runs a custom command to check status          |

## Notes

- The Hub polls all servers every 60 seconds
- Status log is automatically pruned after 7 days
- The SQLite database should be opened in read-only mode to avoid locking issues with the running Hub
- All timestamps are in local time
