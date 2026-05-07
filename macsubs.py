"""
MacSubs - 自动翻译流水线 (微服务 WebAPI 版 & 自动重命名归档 & 独立收纳)
监控 TODO 文件夹中的视频文件，通过 MLX Whisper 提取日语字幕并放入专门的 JA_Subs 文件夹，
通过 OpenRouter API (Grok) 翻译成中文，并将成品和中文字幕加上 "-C" 后缀保留在 OUTPUTS。

架构升级：在 5679 端口暴露出 HTTP API，供 TaskPaw Hub 直接轮询。
"""

import os
import time
import requests
import mlx_whisper
import re
import concurrent.futures
import shutil
import json
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

_status_lock = threading.Lock()

# ================= 核心目录配置区 =================
BASE_DIR = os.path.expanduser("~/Documents/MacSubs")
TODO_DIR = os.path.join(BASE_DIR, "TODO")             # 收件箱
OUTPUT_DIR = os.path.join(BASE_DIR, "OUTPUTS")        # 发件箱 (成品与中文字幕)
JA_SUBS_DIR = os.path.join(OUTPUT_DIR, "JA_Subs")     # 专属日文字幕收纳盒
CACHE_DIR = os.path.join(BASE_DIR, ".progress_cache") # 断点续传缓存区

# 启动时确保这些核心文件夹都存在
for d in [TODO_DIR, OUTPUT_DIR, JA_SUBS_DIR, CACHE_DIR]:
    os.makedirs(d, exist_ok=True)

# ================= API 配置区 =================
# 填入你的 OpenRouter API Key
API_KEY = "sk-or-v1-请在这里粘贴你的API密钥"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_NAME = "x-ai/grok-4.1-fast"

# ================= TaskPaw 微服务 API 配置 =================
TASKPAW_PORT = 5679
MACHINE_NAME = "MacSubsMini"
# Optional bearer-token auth: empty = no auth (current behavior); set
# the env var MACSUBS_API_TOKEN to require Authorization: Bearer <token>
# on /status and /events. /ping stays open for trivial reachability
# probes. Same model as taskpaw.py's api_token, configurable per machine.
API_TOKEN = os.environ.get("MACSUBS_API_TOKEN", "").strip()

_current_status = {
    "timestamp": "",
    "stage": "idle",
    "current_file": "",
    "progress": "Starting...",
    "detail": "",
    "todo_count": 0,
    "done_count": 0,
    "cpu_pct": "?",
    "ram_used_gb": 0,
    "ram_total_gb": 0,
}

# ================= Event queue for Hub integration =================
#
# The Hub (taskpaw_hub.py) persists last_event_ids per server to its
# SQLite. If we reset _next_event_id to 1 every time macsubs restarts,
# Hub filters out our new events as "already seen" until our counter
# climbs past the previously persisted max — meaning post-restart
# subtitle completions are silently lost. Persist next id to a small
# state file so it monotonically grows across restarts.

STATE_FILE = os.path.join(BASE_DIR, ".event_state.json")

_event_lock = threading.Lock()
_events_queue = []
_next_event_id = 1


def _load_event_state():
    """Load _next_event_id from disk on startup."""
    global _next_event_id
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _next_event_id = max(1, int(data.get("next_event_id", 1)))
    except Exception as e:
        print(f"[WARN] Failed to load event state, starting at 1: {e}")
        _next_event_id = 1


def _save_event_state():
    """Persist _next_event_id atomically (tempfile + rename)."""
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"next_event_id": _next_event_id}, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[DEBUG] Failed to persist event state: {e}")


def add_event(stage, current_file, message):
    """Add an event to the queue for Hub polling."""
    global _next_event_id
    with _event_lock:
        event = {
            "id": _next_event_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "stage": stage,
            "current_file": current_file,
            "message": message,
        }
        _events_queue.append(event)
        _next_event_id += 1
        # Keep only last 100 events
        while len(_events_queue) > 100:
            _events_queue.pop(0)
    # Persist outside the lock to keep the critical section short.
    _save_event_state()


def get_and_clear_events():
    """Get all events and clear the queue."""
    with _event_lock:
        events = list(_events_queue)
        _events_queue.clear()
        return events


# Load persisted next_event_id at import time.
_load_event_state()

# ================= TaskPaw HTTP API 核心服务 =================

class TaskPawHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _check_auth(self) -> bool:
        """Validate Authorization header against API_TOKEN.

        Empty token = auth disabled. Failed auth returns 401 and does NOT
        clear the events queue, so an attacker can't drain pending
        notifications by spamming unauthenticated polls.
        """
        if not API_TOKEN:
            return True
        sent = self.headers.get("Authorization", "")
        if sent == f"Bearer {API_TOKEN}":
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", 'Bearer realm="MacSubs"')
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Unauthorized"}).encode("utf-8"))
        return False

    def do_GET(self):
        if self.path == "/ping":
            self._send_json({"status": "ok", "machine": MACHINE_NAME})

        elif self.path == "/status":
            if not self._check_auth():
                return
            with _status_lock:
                status = _current_status.copy()
            stage = status.get("stage", "idle")
            stage_labels = {
                "idle": "Idle",
                "extracting": "Extracting (Whisper)",
                "translating": "Translating (API)",
                "moving": "Moving files",
                "error": "Error",
            }

            parts = []
            parts.append(stage_labels.get(stage, stage.title()))

            current_file = status.get("current_file", "")
            if current_file:
                parts.append(current_file)

            progress = status.get("progress", "")
            if progress:
                parts.append(progress)

            todo_count = status.get("todo_count", 0)
            done_count = status.get("done_count", 0)
            if todo_count > 0 or done_count > 0:
                parts.append(f"Queue: {done_count} done, {todo_count} todo")

            cpu_pct = status.get("cpu_pct", "?")
            parts.append(f"CPU {cpu_pct}%")

            ram_used = status.get("ram_used_gb", 0)
            ram_total = status.get("ram_total_gb", 0)
            if ram_total > 0:
                parts.append(f"RAM {ram_used}/{ram_total}GB")

            detail = status.get("detail", "")
            if detail:
                parts.append(detail)

            status_str = " | ".join(parts)

            response = {
                "machine": MACHINE_NAME,
                "monitors": [
                    {
                        "name": "MacSubs",
                        "type": "custom",
                        "status": status_str,
                        "enabled": True,
                    }
                ],
            }
            self._send_json(response)

        elif self.path == "/events":
            if not self._check_auth():
                return
            events = get_and_clear_events()
            self._send_json({"events": events})

        else:
            self._send_json({"error": "Not found"}, 404)

def start_http_server():
    server = HTTPServer(("0.0.0.0", TASKPAW_PORT), TaskPawHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"🌐 HTTP API 服务已启动: http://0.0.0.0:{TASKPAW_PORT}")
    return server

def get_mac_system_info() -> dict:
    info = {"cpu_pct": "?", "ram_used_gb": 0, "ram_total_gb": 0}
    try:
        result = subprocess.run(
            ["top", "-l", "1", "-n", "0", "-stats", "cpu"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.split("\n"):
            if "CPU usage" in line:
                parts = line.split(",")
                for p in parts:
                    if "idle" in p:
                        idle = float(p.strip().split("%")[0])
                        info["cpu_pct"] = f"{100 - idle:.0f}"
                break

        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5,
        )
        total_bytes = int(result.stdout.strip())
        info["ram_total_gb"] = round(total_bytes / (1024 ** 3), 1)

        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5,
        )
        page_size = 16384
        free_pages = inactive_pages = speculative_pages = 0
        for line in result.stdout.split("\n"):
            if "page size of" in line:
                page_size = int(line.split("page size of")[1].strip().split()[0])
            if "Pages free" in line:
                free_pages = int(line.split(":")[1].strip().rstrip("."))
            if "Pages inactive" in line:
                inactive_pages = int(line.split(":")[1].strip().rstrip("."))
            if "Pages speculative" in line:
                speculative_pages = int(line.split(":")[1].strip().rstrip("."))

        free_bytes = (free_pages + inactive_pages + speculative_pages) * page_size
        info["ram_used_gb"] = round((total_bytes - free_bytes) / (1024 ** 3), 1)
    except Exception as e:
        print(f"[DEBUG] get_mac_system_info failed: {e}")
    return info

def update_status(stage="idle", current_file="", progress="", detail="",
                  todo_count=0, done_count=0):
    global _current_status
    try:
        sys_info = get_mac_system_info()
        with _status_lock:
            _current_status = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "stage": stage,
                "current_file": current_file,
                "progress": progress,
                "detail": detail,
                "todo_count": todo_count,
                "done_count": done_count,
                "cpu_pct": sys_info["cpu_pct"],
                "ram_used_gb": sys_info["ram_used_gb"],
                "ram_total_gb": sys_info["ram_total_gb"],
            }
    except Exception as e:
        print(f"[DEBUG] update_status failed: {e}")

def count_files():
    exts = (".mp4", ".mov", ".mkv")
    todo = sum(1 for f in os.listdir(TODO_DIR) if f.lower().endswith(exts))
    done = sum(1 for f in os.listdir(OUTPUT_DIR) if f.lower().endswith(exts))
    return todo, done

# ================= 核心 AI 处理逻辑区 =================

def is_hallucination(text):
    text = text.strip()
    if not text:
        return True
    if re.search(r'(.{1,15})\1{3,}', text):
        return True
    noise_pattern = (
        r'^([あいうえおぁぃぅぇぉはひふへほハヒフヘホんンあっっー〜、。！？'
        r'\s\.\-—啊嗯哦哎呀哈呼嘶]){1,}$'
    )
    if re.match(noise_pattern, text):
        return True
    clean_text = re.sub(r'[^\w\u3040-\u30ff\u4e00-\u9fff]', '', text)
    if not clean_text:
        return True
    total_chars = len(clean_text)
    unique_chars = len(set(clean_text))
    if total_chars >= 5 and unique_chars <= 2:
        return True
    if total_chars >= 10 and unique_chars <= 4:
        return True
    if total_chars >= 20 and unique_chars <= 6:
        return True
    return False

def translate_single_chunk(chunk_data):
    chunk_index, chunk_text, total_chunks, cache_file = chunk_data

    if os.path.exists(cache_file):
        print(f"[{time.strftime('%H:%M:%S')}] Chunk {chunk_index}/{total_chunks} cached, skipping")
        with open(cache_file, 'r', encoding='utf-8') as f:
            return chunk_index, f.read()

    system_prompt = """你是一个专业的字幕组翻译，深谙各种语境（含成人/深夜档内容）。
请将提供的 SRT 格式日文字幕翻译成流畅的中文。
1. 绝对保留原有的 SRT 序号和时间轴。
2. 只翻译日语对话，不加任何解释。
3. 直接输出完整的 SRT 内容。
4. 成人内容翻译更加露骨。"""

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chunk_text}
        ],
        "temperature": 0.3
    }

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(API_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            response_data = response.json()
            if 'choices' not in response_data or not response_data['choices'][0]['message'].get('content'):
                raise ValueError("API returned empty data")
            result_text = response_data['choices'][0]['message']['content']
            result_text = result_text.replace("```srt", "").replace("```", "").strip()
            with open(cache_file, 'w', encoding='utf-8') as f:
                f.write(result_text)
            print(f"[{time.strftime('%H:%M:%S')}] Chunk {chunk_index}/{total_chunks} translated")
            return chunk_index, result_text
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Chunk {chunk_index} failed ({e}), retry {attempt+1}")
            time.sleep(3)

    print(f"[{time.strftime('%H:%M:%S')}] Chunk {chunk_index} failed after retries, keeping original")
    return chunk_index, chunk_text

def translate_srt_concurrent(ja_srt_path, zh_srt_path, video_name):
    print(f"\n[{time.strftime('%H:%M:%S')}] Starting translation: {os.path.basename(ja_srt_path)}")
    video_id = os.path.basename(ja_srt_path).replace(".ja.srt", "")

    with open(ja_srt_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    sub_blocks = content.split('\n\n')
    valid_blocks = [b for b in sub_blocks if b.strip()]
    if not valid_blocks:
        return False

    chunk_size = 100
    chunks_to_process = []
    total_chunks = (len(valid_blocks) + chunk_size - 1) // chunk_size

    for i in range(0, len(valid_blocks), chunk_size):
        chunk = valid_blocks[i:i + chunk_size]
        chunk_index = (i // chunk_size) + 1
        cache_file = os.path.join(CACHE_DIR, f"{video_id}_chunk_{chunk_index}.txt")
        chunks_to_process.append((chunk_index, "\n\n".join(chunk), total_chunks, cache_file))

    translated_results = []
    todo, done = count_files()

    print(f"[{time.strftime('%H:%M:%S')}] {total_chunks} chunks, 5 threads...")
    update_status("translating", video_name,
                  progress=f"0/{total_chunks} chunks",
                  detail=f"Model: {MODEL_NAME}",
                  todo_count=todo, done_count=done)

    completed_chunks = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(translate_single_chunk, c): c for c in chunks_to_process}
        for future in concurrent.futures.as_completed(futures):
            index, text = future.result()
            translated_results.append((index, text))
            completed_chunks += 1
            update_status("translating", video_name,
                          progress=f"{completed_chunks}/{total_chunks} chunks",
                          detail=f"Model: {MODEL_NAME}",
                          todo_count=todo, done_count=done)

    translated_results.sort(key=lambda x: x[0])
    final_zh_srt = "\n\n".join(text for _, text in translated_results)

    with open(zh_srt_path, 'w', encoding='utf-8') as f:
        f.write(final_zh_srt)

    print(f"\n[{time.strftime('%H:%M:%S')}] Translation complete!")
    return True

def cleanup_original_video(video_path, target_filename):
    try:
        target_path = os.path.join(OUTPUT_DIR, target_filename)
        shutil.move(video_path, target_path)
        print(f"[{time.strftime('%H:%M:%S')}] Video renamed to {target_filename} and moved to OUTPUTS")
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Failed to move video: {e}")

def format_timestamp(seconds: float):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def process_video(video_path):
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    video_ext = os.path.splitext(video_path)[1]
    
    new_video_name = f"{video_name}-C"
    
    # 【核心修改点】日语源文件放入独立的 JA_Subs 文件夹
    ja_srt_path = os.path.join(JA_SUBS_DIR, f"{new_video_name}.ja.srt")
    
    # 中文字幕依然放在 OUTPUTS 根目录
    zh_srt_path = os.path.join(OUTPUT_DIR, f"{new_video_name}.zh.srt")

    todo, done = count_files()

    # 1. 提取日语字幕并归档到子文件夹
    if not os.path.exists(zh_srt_path) and not os.path.exists(ja_srt_path):
        print(f"\n[{time.strftime('%H:%M:%S')}] New video found, starting MLX extraction...")
        update_status("extracting", video_name,
                      progress="Starting Whisper",
                      detail="MLX Whisper large-v3-turbo",
                      todo_count=todo, done_count=done)
        try:
            result = mlx_whisper.transcribe(
                video_path,
                path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
                language="ja",
                verbose=True,
                condition_on_previous_text=False
            )

            valid_index = 1
            with open(ja_srt_path, "w", encoding="utf-8") as f:
                for segment in result["segments"]:
                    if segment["start"] >= segment["end"]:
                        continue
                    text = segment["text"].strip()
                    if is_hallucination(text):
                        continue
                    start = format_timestamp(segment["start"])
                    end = format_timestamp(segment["end"])
                    f.write(f"{valid_index}\n{start} --> {end}\n{text}\n\n")
                    valid_index += 1

            print(f"[{time.strftime('%H:%M:%S')}] MLX extraction complete")
            update_status("extracting", video_name,
                          progress="Extraction complete",
                          detail=f"{valid_index - 1} subtitle segments",
                          todo_count=todo, done_count=done)
        except Exception as e:
            print(f"Extraction failed: {e}")
            add_event("error", video_name, f"Extraction failed: {e}")
            update_status("error", video_name,
                          progress=f"Extraction failed: {e}",
                          todo_count=todo, done_count=done)
            return

    # 2. 翻译中文字幕
    if os.path.exists(ja_srt_path) and not os.path.exists(zh_srt_path):
        success = translate_srt_concurrent(ja_srt_path, zh_srt_path, video_name)
        if success:
            add_event("translating", video_name, f"Translation complete: {video_name}")

    # 3. 打扫战场，把原片改名 (-C) 并移走
    if os.path.exists(zh_srt_path) and os.path.exists(video_path):
        update_status("moving", video_name,
                      progress="Moving to OUTPUTS",
                      todo_count=todo, done_count=done)
        cleanup_original_video(video_path, f"{new_video_name}{video_ext}")
        add_event("moving", video_name, f"Completed and moved to OUTPUTS: {new_video_name}{video_ext}")

    todo, done = count_files()
    update_status("idle", "", progress="Waiting for new files",
                  todo_count=todo, done_count=done)

def monitor():
    print("=======================================")
    print(" MacSubs Auto Translation Pipeline")
    print(f" Input:  {TODO_DIR}")
    print(f" Output: {OUTPUT_DIR}")
    start_http_server()  
    print("=======================================\n")

    todo, done = count_files()
    update_status("idle", "", progress="Waiting for new files",
                  todo_count=todo, done_count=done)

    while True:
        try:
            for filename in os.listdir(TODO_DIR):
                if filename.lower().endswith((".mp4", ".mov", ".mkv")):
                    process_video(os.path.join(TODO_DIR, filename))
        except Exception as e:
            # Previously: bare except: pass — the user would see status
            # stuck on "idle" with no clue that scanning failed (perms
            # error, network volume vanished, broken video, OpenRouter
            # outage, etc.). Now we log it, surface it to /status, and
            # emit an event so OpenClaw notices.
            err_msg = f"Monitor loop error: {e}"
            print(f"[ERROR] {time.strftime('%H:%M:%S')} {err_msg}")
            try:
                update_status("error", "", progress="ERROR (see logs)",
                              detail=str(e)[:200])
            except Exception as ue:
                print(f"[DEBUG] Failed to update status after error: {ue}")
            try:
                add_event("error", "", err_msg)
            except Exception as ee:
                print(f"[DEBUG] Failed to add error event: {ee}")
            # Brief sleep to avoid hot loop if the same error fires every cycle
            time.sleep(2)

        todo, done = count_files()
        update_status("idle", "", progress="Waiting for new files",
                      todo_count=todo, done_count=done)
        time.sleep(10)

if __name__ == "__main__":
    monitor()