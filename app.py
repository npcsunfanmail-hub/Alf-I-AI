import os
import json
import traceback
import tempfile
import sqlite3
import socket
import re
import time
import urllib.request
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(tempfile.gettempdir(), "alfi_conversations.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            user_id TEXT PRIMARY KEY,
            history TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def load_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT history FROM conversations WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


def save_history(user_id, history):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO conversations (user_id, history, updated_at) VALUES (?, ?, datetime('now'))",
        (user_id, json.dumps(history)),
    )
    conn.commit()
    conn.close()


init_db()

SYSTEM_PROMPT = """Your name is Alf-I. You are an AI assistant created by Logan Robinson.

You have the ability to control smart TVs on the local network using the control_tv function. When the user asks you to control a TV:
- If you don't know the TV's IP address or brand, ask the user to add a TV in the remote panel
- Use control_tv with the correct brand (roku, samsung, lg), IP address, action (keypress), and key name
- Common actions: power_on, power_off, volume_up, volume_down, mute, home, back, menu, exit, up, down, left, right, select, play, pause, netflix, youtube, prime_video, input
- After sending a command, tell the user what you did
- The user has a remote control panel they can use directly for day-to-day TV control
- You can also help them find their TV's IP by suggesting they check their router or use the scan feature in the remote panel"""

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TV_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "connect_tv",
            "description": "Open the TV remote panel so the user can select or add a TV. The user can also control the TV directly from the remote panel.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "disconnect_tv",
            "description": "Deselect the currently selected TV in the remote panel.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tv_status",
            "description": "Check if a TV is currently selected and connected in the remote panel.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_tv",
            "description": "Send a remote control command to a smart TV. The user also has a remote panel with buttons for direct control.",
            "parameters": {
            "parameters": {
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "enum": ["roku", "samsung", "lg"],
                        "description": "TV brand (required for network TV control)"
                    },
                    "ip": {
                        "type": "string",
                        "description": "TV IP address on the local network (required for network TV control)"
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "power_on", "power_off", "toggle_power",
                            "volume_up", "volume_down", "mute", "unmute",
                            "channel_up", "channel_down",
                            "home", "back", "menu", "exit",
                            "up", "down", "left", "right", "select",
                            "play", "pause", "stop", "rewind", "fast_forward", "record",
                            "netflix", "youtube", "prime_video", "disney_plus",
                            "hdmi1", "hdmi2", "hdmi3", "av", "tv_mode", "input"
                        ],
                        "description": "The remote control action to perform"
                    },
                    "key": {
                        "type": "string",
                        "description": "Raw remote key name (PowerOn, PowerOff, VolumeUp, etc.)"
                    }
                },
                "required": ["action"]
            }
        }
    }
]


def discover_tvs_ssdp(timeout=3):
    tvs = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    msg = "\r\n".join([
        'M-SEARCH * HTTP/1.1',
        'HOST: 239.255.255.250:1900',
        'MAN: "ssdp:discover"',
        'ST: roku:ecp',
        '', ''
    ])
    try:
        sock.sendto(msg.encode(), ("239.255.255.250", 1900))
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, addr = sock.recvfrom(1024)
                text = data.decode("utf-8", errors="replace")
                loc = re.search(r'Location: https?://([^:/]+):?(\d+)?/', text, re.I)
                name = re.search(r'friendlyName:\s*(.+?)[\r\n]', text, re.I)
                if loc:
                    ip = loc.group(1)
                    tv = {
                        "ip": ip,
                        "brand": "roku",
                        "name": name.group(1).strip() if name else f"Roku ({ip})"
                    }
                    if tv not in tvs:
                        tvs.append(tv)
            except socket.timeout:
                break
    except Exception:
        pass
    finally:
        sock.close()
    return tvs


def control_roku(ip, key):
    url = f"http://{ip}:8060/keypress/{key}"
    try:
        req = urllib.request.Request(url, method="POST")
        urllib.request.urlopen(req, timeout=3)
        return {"success": True, "message": f"Sent {key} to Roku at {ip}"}
    except Exception as e:
        return {"success": False, "message": str(e), "pending": True}


def execute_tv_command(brand, ip, action, key):
    brand = brand.lower()
    if brand == "roku":
        return control_roku(ip, key)
    return {"success": True, "message": "Command forwarded to client", "pending": True}


def call_llm(messages, tools=None):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    kwargs = {"model": LLM_MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message


@app.route("/")
def serve():
    return open(os.path.join(BASE_DIR, "index.html")).read(), 200, {"Content-Type": "text/html"}


@app.route("/api/sync", methods=["GET", "POST"])
def sync():
    if request.method == "POST":
        data = request.get_json()
        uid = data.get("user_id")
        hist = data.get("history", [])
        if not uid:
            return jsonify({"error": "user_id required"}), 400
        save_history(uid, hist)
        return jsonify({"ok": True})
    uid = request.args.get("user_id")
    if not uid:
        return jsonify({"error": "user_id required"}), 400
    hist = load_history(uid)
    return jsonify({"history": hist or []})


@app.route("/api/tv/discover", methods=["POST"])
def tv_discover():
    try:
        tvs = discover_tvs_ssdp(timeout=3)
        return jsonify({"tvs": tvs})
    except Exception as e:
        return jsonify({"tvs": [], "error": str(e)})


@app.route("/api/tv/command", methods=["POST"])
def tv_command():
    data = request.get_json()
    brand = data.get("brand", "").lower()
    ip = data.get("ip", "")
    action = data.get("action", "keypress")
    key = data.get("key", "")
    if not brand or not ip or not key:
        return jsonify({"success": False, "message": "brand, ip, and key required"}), 400
    result = execute_tv_command(brand, ip, action, key)
    return jsonify(result)


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages required"}), 400

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d, %Y at %I:%M %p UTC")
    sys_msg = SYSTEM_PROMPT + f"\n\n[SYSTEM: Today's date is {date_str}.]"
    msgs = [{"role": "system", "content": sys_msg}] + messages

    try:
        msg = call_llm(msgs, tools=TV_TOOLS)

        if msg.tool_calls:
            tool_calls_data = []
            for tc in msg.tool_calls:
                tool_calls_data.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })
            return jsonify({
                "type": "tool_call",
                "content": msg.content or "",
                "tool_calls": tool_calls_data,
                "message": {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": tool_calls_data
                }
            })

        content = (msg.content or "").strip()
        return jsonify({"type": "final", "content": content})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": repr(e)}), 500
