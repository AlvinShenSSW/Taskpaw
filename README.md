# 🐾 TaskPaw - Local AI Workflow Monitor

TaskPaw is a lightweight Windows desktop app for monitoring AI tasks (Lada, ComfyUI, downloads, etc.) running on multiple machines in your local network, and automatically notifying your OpenClaw assistant when tasks complete.

## Features

- **Lada Process Monitor** - Detects `lada-cli` process from running to exited, auto-notifies
- **ComfyUI Queue Monitor** - Polls the `/queue` API, notifies when all tasks complete
- **Folder Monitor** - Watches download directories, notifies when files stabilize (great for BT/HTTP downloads)
- **Generic Process Monitor** - Monitors any process by name
- **Custom Command** - Runs commands on a schedule, determines status by exit code
- **System Tray** - Minimizes to background when window is closed
- **OpenClaw Webhook** - Sends HTTP POST notifications directly to your AI assistant

## Installation & Running

### Option 1: Run Python script directly

```bash
# 1. Ensure Python 3.10+ is installed
python --version

# 2. Install optional dependencies (system tray support)
pip install pystray Pillow

# 3. Run
python taskpaw.py
```

### Option 2: Package as .exe

```bash
# 1. Install packaging tools
pip install pyinstaller pystray Pillow

# 2. Run the build script
build.bat

# 3. The generated exe is at dist/TaskPaw.exe
```

## Quick Start

1. **Launch TaskPaw** -> Go to the "⚙️ OpenClaw Settings" tab
2. **Enter your OpenClaw address and Token** -> Click "Test Connection" to verify
3. **Switch to "📡 Monitors"** -> Click "+ Add Monitor"
4. **Choose a monitor type**, fill in parameters, and save
5. **Click "Start"** -> You'll be notified via OpenClaw when tasks complete

## Monitor Types

### Lada (Process Monitor)
Watches for the `lada-cli` or `lada-cli.exe` process. When you start Lada to process a video from the command line, TaskPaw detects the running process; when processing finishes and the process exits, it automatically notifies OpenClaw.

Parameters:
- **Process Name**: Default `lada-cli`, on Windows you can use `lada-cli.exe`
- **Poll Interval**: Check frequency, default 10 seconds

### ComfyUI (Queue Monitor)
Polls ComfyUI's HTTP API to check queue status. When the queue transitions from having tasks to empty (confirmed multiple consecutive times), it is considered complete.

Parameters:
- **ComfyUI Address**: Use `127.0.0.1` if on the same machine, or the LAN IP for other machines
- **Port**: Default `8188`
- **Idle Confirm Count**: Number of consecutive idle detections before notifying, prevents false positives between tasks
- **Poll Interval**: Default 10 seconds

### Folder Monitor (Downloads etc.)
Monitors a specified folder for new files. When a file's size remains unchanged for the configured stable time, it is considered download/write complete.

Parameters:
- **Watch Directory**: Path to the download folder
- **File Stable Time**: How long the file size must remain unchanged, default 30 seconds
- **File Extensions**: Comma-separated, e.g. `mp4,mkv,zip`, leave empty to monitor all files
- **Poll Interval**: Default 5 seconds

### Generic Process Monitor
Same principle as the Lada monitor, but you can specify any process name, such as `ffmpeg`, `yt-dlp`, etc.

### Custom Command
Runs a command on a schedule and determines status by exit code:
- exit 0 -> idle/complete
- non-zero -> busy/incomplete

Great for custom detection logic, such as querying remote machines via SSH, calling custom APIs, etc.

## OpenClaw Configuration

Make sure your Mac Mini's OpenClaw has webhooks enabled:

```yaml
# ~/.openclaw/config.yaml
hooks:
  enabled: true
  token: "your-secure-token-here"  # Must match the token in TaskPaw
```

And ensure the OpenClaw Gateway is bound to a LAN-accessible address (not 127.0.0.1).

## Config File Location

- Config file: `%APPDATA%\TaskPaw\config.json`
- Log file: `%APPDATA%\TaskPaw\taskpaw.log`

## Network Architecture

```
Windows Machine (TaskPaw runs here)
  ├── Monitors local Lada process
  ├── Monitors local or LAN ComfyUI queue
  ├── Monitors local download directory
  └── HTTP POST ──→ Mac Mini (OpenClaw :18789)
                        └── Notifies via Telegram / Discord / WhatsApp
```

TaskPaw can run on multiple Windows machines simultaneously, each monitoring its own tasks, all sending notifications to the same OpenClaw instance.

## Development

```bash
# Run directly
python taskpaw.py

# Code structure
taskpaw.py          # Main program (GUI + all Watcher logic)
requirements.txt    # Dependencies
build.bat          # Build script
README.md          # This file
```

## License

MIT
