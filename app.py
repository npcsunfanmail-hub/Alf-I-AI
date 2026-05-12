import os
import json
import traceback
import tempfile
import sqlite3
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from openai import OpenAI

import tv_control

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
    "Your name is Alf-I. You are an AI assistant. You were created by Logan Robinson. "
    "You can control the TV using the tv_power_on and tv_power_off tools. "
    "When the user asks you to turn the TV on or off, use the appropriate tool."
)

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


TV_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tv_power_on",
            "description": "Turn the TV on using Wake-on-LAN. Requires TV_MAC to be configured.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tv_power_off",
            "description": "Turn the TV off via network command (Samsung WebSocket, LG HTTP, etc). Requires TV_IP and TV_TYPE to be configured.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_MAP = {
    "tv_power_on": tv_control.power_on,
    "tv_power_off": tv_control.power_off,
}


def call_llm(messages):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=TV_TOOLS,
        tool_choice="auto",
    )
    return resp.choices[0].message


@app.route("/api/tv/config")
def tv_config():
    return jsonify({
        "ip": tv_control.TV_IP,
        "type": tv_control.TV_TYPE,
    })


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
        tv_command = None

        if msg.tool_calls:
            msgs.append(msg)
            for tc in msg.tool_calls:
                if tc.function.name in ("tv_power_on", "tv_power_off"):
                    tv_command = tc.function.name
                fn = TOOL_MAP.get(tc.function.name)
                if fn:
                    success, result = fn()
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"success": success, "message": result}),
                    })
                else:
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps({"success": False, "message": f"Unknown tool: {tc.function.name}"}),
                    })
            msg2 = call_llm(msgs)
            return jsonify({
                "content": (msg2.content or "").strip(),
                "tv_command": tv_command,
            })

        return jsonify({"content": (msg.content or "").strip()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": repr(e)}), 500
