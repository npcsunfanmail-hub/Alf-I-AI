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

SYSTEM_PROMPT = "Your name is Alf-I. You are an AI assistant. You were created by Logan Robinson."

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TV_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "connect_tv",
            "description": "Scan for nearby Bluetooth TVs. Opens a device picker for the user to select their TV.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "disconnect_tv",
            "description": "Disconnect from the currently connected Bluetooth TV.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "tv_status",
            "description": "Check if a TV is currently connected via Bluetooth.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "control_tv",
            "description": "Send a remote control command to the connected Bluetooth TV. TV must be connected first.",
            "parameters": {
                "type": "object",
                "properties": {
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
                    }
                },
                "required": ["action"]
            }
        }
    }
]


def call_llm(messages):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=TV_TOOLS,
        tool_choice="auto"
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

        return jsonify({"type": "final", "content": (msg.content or "").strip()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": repr(e)}), 500
