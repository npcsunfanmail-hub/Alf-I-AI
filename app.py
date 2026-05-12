import os
import json
import traceback
import tempfile
import sqlite3
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
    print(f"Saved {len(history)} messages for user {user_id[:16]}... DB: {DB_PATH}")


init_db()
print(f"DB path: {DB_PATH}")

SYSTEM_PROMPT = (
    "Your name is Alf-I. You are an AI assistant. You were created by Logan Robinson."
    "\n\nYou can control TVs and home entertainment devices. "
    "You have knowledge of universal remote protocols: "
    "IR (NEC 32-bit, Sony SIRC, Philips RC-5), "
    "IP/network control (Roku HTTP on port 8060, Samsung WebSocket on 8001/8002, "
    "LG WebSocket on 3000/3001, Sony REST API), "
    "HDMI-CEC, Bluetooth/BLE HID, Wake-on-LAN (magic packet on UDP 9), "
    "and cloud services (SmartThings, HomeKit, Google Home)."
    "\nWhen the user asks to control their TV, use the tv_control function. "
    "Never mention the function calling mechanism to the user."
)

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TV_FUNCTIONS = [
    {
        "type": "function",
        "function": {
            "name": "tv_control",
            "description": "Send a control command to a TV. Use this when the user asks to control their TV.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The TV command to execute",
                        "enum": [
                            "power_on", "power_off", "power_toggle",
                            "volume_up", "volume_down", "mute_toggle",
                            "input_hdmi1", "input_hdmi2", "input_hdmi3",
                            "input_av", "input_tv",
                            "channel_up", "channel_down",
                            "home", "back", "ok",
                            "up", "down", "left", "right",
                            "launch_netflix", "launch_youtube",
                            "launch_prime_video", "launch_disney_plus",
                            "launch_hulu", "launch_app",
                            "settings", "source", "guide", "info"
                        ]
                    },
                    "value": {
                        "type": "string",
                        "description": "Optional extra data (e.g., app ID for launch_app)"
                    }
                },
                "required": ["command"]
            }
        }
    }
]


def call_llm(messages):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL, messages=messages,
            tools=TV_FUNCTIONS, tool_choice="auto"
        )
    except Exception:
        resp = client.chat.completions.create(
            model=LLM_MODEL, messages=messages
        )
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
        msg = call_llm(msgs)

        if msg.tool_calls:
            tc = msg.tool_calls[0]
            if tc.function.name == "tv_control":
                args = json.loads(tc.function.arguments)
                return jsonify({
                    "type": "tv_action",
                    "command": args.get("command", ""),
                    "value": args.get("value", ""),
                    "content": msg.content or ""
                })

        return jsonify({"content": (msg.content or "").strip()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": repr(e)}), 500
