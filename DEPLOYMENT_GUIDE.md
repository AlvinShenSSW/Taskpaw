# TaskPaw V2.1 Deployment Guide

## Architecture Overview

V2 uses a **pull model**: each Windows server runs TaskPaw with a built-in HTTP API. The Mac Mini runs TaskPaw Hub, which polls all servers every minute, collects status and events, stores them in SQLite, and writes a status.md file for OpenClaw to read.

```
Windows Servers (TaskPaw V2.1)          Mac Mini (TaskPaw Hub)
+-----------------------+              +------------------------+
|  HTTP API :5678       |<--- poll ----|  Polls every 60s       |
|  GET /status          |              |  SQLite database       |
|  GET /events          |              |  ~/.taskpaw-hub/       |
|  GET /ping            |              |    hub.db (history)    |
|  Local monitors       |              |    status.md (live)    |
|  (ComfyUI, Lada...)  |              |    hub.log             |
+-----------------------+              +------------------------+
                                              |
                                              v
                                       OpenClaw (Sara)
                                       Reads status.md or hub.db
                                              |
                                              v
                                         Telegram
```

No SSH tunnels, no persistent connections. Simple HTTP polls over LAN.

---

## Part 1: Deploy TaskPaw V2.1 on Windows Servers

### Step 1: Copy Files

Copy these files to each Windows server (e.g., `C:\Tools\TaskPaw\`):

- `taskpaw.py` — main application
- `build.bat` — build script
- `lada-wrapper.bat` — Lada CLI wrapper for progress capture (optional)

### Step 2: Install Dependencies

```powershell
pip install pystray Pillow
```

### Step 3: Launch and Configure

```powershell
python taskpaw.py
```

In the Settings tab:

- **Machine Alias**: Set a unique name (e.g., `BlackSilverPig`)
- **API Port**: `5678` (default, change if needed)
- Click **Save All Settings**

The HTTP API server starts automatically. You should see "API: Running" in the top right corner.

### Step 4: Configure Monitors

Click **+ Add Monitor** to set up your monitors:

#### Lada Monitor (v2.1 Enhanced)

TaskPaw v2.1 provides detailed Lada monitoring with GPU tracking, file queue counting, and CLI progress parsing.

- **Type**: lada
- **Process Name**: `lada-cli` (or whatever the process is called on your server)
- **Input Folder**: Path where videos are queued for Lada (e.g., `D:\TODO`)
- **Output Folder**: Path where Lada writes processed videos (e.g., `D:\Outputs`)
- **Monitor GPU usage**: Check to show GPU utilization and VRAM via nvidia-smi
- **Poll Interval**: `10` seconds recommended

The Lada monitor shows a detailed status line like:

```
Processing 45% @ 18.5fps | GPU 95% | VRAM 8.2/12.0GB | Queue: 9/32 done (23 left) | 12m elapsed
```

When idle, it shows the queue status:

```
Listening | Queue: 9/32 done (23 left)
```

**Using the Lada CLI Wrapper (optional, for progress percentage):**

To capture lada-cli's progress percentage and fps, use the wrapper script instead of running lada-cli directly:

```
lada-wrapper.bat "C:\path\to\lada-cli.exe" --input "D:\TODO" --output "D:\Outputs"
```

Find lada-cli's full path with:

```powershell
where lada-cli
```

The wrapper writes progress to a temp file that TaskPaw reads automatically.

**What works without the wrapper:**

- Process running/not running detection (automatic)
- GPU usage and VRAM (automatic, via nvidia-smi)
- File queue counting (requires Input and Output folders configured)
- Elapsed time tracking (automatic)

**What requires the wrapper:**

- Progress percentage (e.g., "Processing 45%")
- Processing speed (e.g., "@ 18.5fps")

#### ComfyUI Monitor

- **Type**: comfyui
- **Host**: `127.0.0.1` (or the ComfyUI host)
- **Port**: `8188` (or your custom port, e.g., `32341` for SnowLeopard)
- **Idle Confirms**: `3` (number of consecutive empty-queue checks before marking complete)

Shows detailed status: `Processing (1 running, 17 pending)`

#### Folder Monitor

- **Type**: folder
- **Folder Path**: Path to watch for new/changed files
- **File Extensions**: Comma-separated filter (leave empty for all)
- **Stable Seconds**: `30` (file considered complete after no size change)

#### Process Monitor

- **Type**: process
- **Process Name**: Name of the process to monitor

#### Custom Command Monitor

- **Type**: custom
- **Command**: Any command that returns status text

### Step 5: Open Firewall Port

TaskPaw listens on port 5678 for incoming HTTP requests from the Mac Mini. Allow this through Windows Firewall:

```powershell
netsh advfirewall firewall add rule name="TaskPaw API" dir=in action=allow protocol=TCP localport=5678
```

### Step 6: Test the API

From any machine on the LAN, verify the API is reachable:

```bash
curl http://<server-ip>:5678/ping
```

Expected response: `{"ok": true, "machine": "BlackSilverPig"}`

### Step 7: Build Standalone .exe

Build a standalone exe so you don't need Python installed to run TaskPaw:

```powershell
cd C:\Tools\TaskPaw
.\build.bat
```

Or manually:

```powershell
pip install pyinstaller pystray Pillow
python -m PyInstaller --onefile --windowed --name "TaskPaw" --hidden-import pystray --hidden-import pystray._win32 --hidden-import PIL --hidden-import PIL._tkinter_finder taskpaw.py
```

Output: `dist\TaskPaw.exe` — a single standalone file. Copy it to any Windows server and double-click to run. No Python required.

**Note:** Windows Smart App Control may block unsigned exe files. If this happens, right-click the exe, select Properties, and click "Unblock". Or go to Windows Security > App & browser control > Smart App Control and set it to Off.

### Step 8: Auto-Start on Boot

Press `Win + R`, type `shell:startup`, create a shortcut to `TaskPaw.exe` in that folder. TaskPaw will launch automatically every time the server boots.

---

## Part 2: Deploy TaskPaw Hub on Mac Mini

### Prerequisites

Install Homebrew Python with tkinter support (required for GUI):

```bash
brew install python python-tk
```

### Step 1: Copy Files and Build Standalone App

Copy `taskpaw_hub.py` and `build_hub.sh` to your Mac Mini:

```bash
mkdir -p ~/Documents/Taskpaw
# Copy taskpaw_hub.py and build_hub.sh to ~/Documents/Taskpaw/
```

Build the standalone app:

```bash
cd ~/Documents/Taskpaw
chmod +x build_hub.sh
./build_hub.sh
```

The build script automatically uses Homebrew Python, creates a virtual environment, installs PyInstaller, and builds `dist/TaskPawHub`.

### Step 2: Launch Hub

```bash
./dist/TaskPawHub
```

A terminal window will stay open behind the GUI showing logs — this is normal and useful for debugging.

### Step 3: Add Your Servers

Go to the **Servers** tab and click **Add Server** for each Windows machine:

- **BlackSilverPig**: IP `192.168.1.xxx`, Port `5678`
- **BlackGoldPig**: IP `192.168.1.xxx`, Port `5678`
- **DarkbluePig**: IP `192.168.1.xxx`, Port `5678`
- **SnowLeopard**: IP `192.168.1.xxx`, Port `5678`

### Step 4: Configure Settings

Go to the **Settings** tab:

- **Poll Interval**: `60` seconds (how often to check each server)
- **Report Every N Polls**: `5` (sends summary to OpenClaw every 5 minutes)
- **OpenClaw Token**: Your hooks token (optional, webhook is no longer the primary method)
- **Enable OpenClaw**: Check the box if you want webhook notifications too

Click **Save Settings**.

### Step 5: Verify

Switch to the **Dashboard** tab. Within one poll cycle (60 seconds), you should see:

- Each server's status card turn green
- Monitor statuses populated (including Lada GPU info and queue counts)
- Events appearing as they come in

Verify the status file is being written:

```bash
cat ~/.taskpaw-hub/status.md
```

### Step 6: OpenClaw Integration

OpenClaw can read server status from two sources, both in the `~/.taskpaw-hub/` directory (hidden folder — press Cmd+Shift+G in Finder and type `~/.taskpaw-hub/`):

**Option A: Markdown Status File (simplest)**

- **Path**: `~/.taskpaw-hub/status.md`
- Updated every poll cycle (60 seconds)
- Human-readable current snapshot of all servers

**Option B: SQLite Database (full history)**

- **Path**: `~/.taskpaw-hub/hub.db`
- Contains tables: `servers`, `status_log`, `events`, `config`
- Status log pruned after 7 days
- Open read-only to avoid locking conflicts

See `OPENCLAW_INTEGRATION.md` for full schema details and SQL queries.

### Step 7: Auto-Start Hub on Boot (Optional)

Create a LaunchAgent to start Hub automatically:

```bash
cat > ~/Library/LaunchAgents/com.taskpaw.hub.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.taskpaw.hub</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/youruser/Documents/Taskpaw/dist/TaskPawHub</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.taskpaw.hub.plist
```

---

## Per-Server Checklist

For each Windows server:

- [ ] TaskPaw V2.1 files copied (`taskpaw.py`, `build.bat`, `lada-wrapper.bat`)
- [ ] Dependencies installed: `pip install pystray Pillow`
- [ ] Launched TaskPaw, set Machine Alias
- [ ] API Port 5678 confirmed running
- [ ] Firewall port opened
- [ ] Verified from Mac Mini: `curl http://<ip>:5678/ping`
- [ ] Monitors configured (Lada with input/output folders, ComfyUI with correct port)
- [ ] (Optional) Built standalone exe
- [ ] (Optional) Auto-start on boot configured

For Mac Mini:

- [ ] Homebrew Python installed: `brew install python python-tk`
- [ ] TaskPaw Hub built and launched
- [ ] All servers added in Servers tab
- [ ] Dashboard shows green status for all servers
- [ ] `~/.taskpaw-hub/status.md` is being updated
- [ ] OpenClaw configured to read status.md or hub.db
- [ ] (Optional) OpenClaw webhook token configured
- [ ] (Optional) LaunchAgent for auto-start

---

## Troubleshooting

**Server shows red/offline in Dashboard**

- Verify the Windows server is on and TaskPaw is running
- Verify the IP is correct: `ping <server-ip>` from Mac Mini
- Verify the API port is open: `curl http://<server-ip>:5678/ping`
- Check Windows Firewall allows port 5678

**Lada shows "Listening" but is actually processing**

- Verify the process name matches (default `lada-cli`, check with Task Manager)
- If using GUI version of Lada, the process name might be different (e.g., `lada.exe`)

**Lada queue shows 0 completed but output folder has files**

- Ensure the Output Folder path is correct in the monitor settings
- Lada output files must be video files (.mp4, .mkv, .avi, etc.)

**GPU info not showing**

- Verify nvidia-smi is available: run `nvidia-smi` in PowerShell
- If not on PATH, install NVIDIA drivers with the CLI tools included

**Hub database errors ("unable to open database file")**

- Delete corrupted database and restart: `rm ~/.taskpaw-hub/hub.db*`
- Re-add your servers after restart

**Windows Smart App Control blocks exe**

- Run: `Unblock-File -Path "C:\Tools\TaskPaw\dist\TaskPaw.exe"` in PowerShell
- Or: Windows Security > App & browser control > Smart App Control > Off

**Mac Hub crashes with "macOS 26 required"**

- Must use Homebrew Python, not system Python
- Rebuild: `rm -rf .venv build dist *.spec && ./build_hub.sh`

**status.md not being created**

- The file only appears after the Hub successfully polls at least one server
- Check: `ls -la ~/.taskpaw-hub/`
- Hidden folder — use Cmd+Shift+G in Finder to navigate to `~/.taskpaw-hub/`

**Dashboard not updating**

- Check poll interval in Settings (default 60 seconds)
- Check the Hub log: `cat ~/.taskpaw-hub/hub.log`
- Verify servers are reachable from the Mac Mini
