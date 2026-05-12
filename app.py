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
    "You can fully control the TV via Bluetooth. Available tools:\n"
    "- tv_power_on / tv_power_off: turn TV on/off\n"
    "- tv_send_key(key): send any remote key command. "
    "Valid keys: KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_ENTER (navigation); "
    "KEY_PLAY, KEY_PAUSE, KEY_STOP, KEY_FF, KEY_REW (playback); "
    "KEY_VOLUP, KEY_VOLDOWN, KEY_MUTE (volume); "
    "KEY_HOME, KEY_BACK, KEY_EXIT, KEY_MENU, KEY_SOURCE, KEY_GUIDE, KEY_INFO (system); "
    "KEY_CHUP, KEY_CHDOWN (channel); "
    "KEY_NETFLIX, KEY_YOUTUBE, KEY_PRIME, KEY_DISNEY_PLUS, KEY_SPOTIFY (apps); "
    "KEY_0 through KEY_9 (numeric)\n"
    "- tv_open_app(app): open a streaming app (netflix, youtube, prime video, disney+, spotify, apple tv, plex)\n"
    "- tv_discover: scan for nearby Bluetooth TVs\n"
    "When the user asks to control the TV, use the appropriate tool."
)

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


TV_TOOLS = [
    {"type": "function", "function": {"name": "tv_power_on", "description": "Turn the TV on", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "tv_power_off", "description": "Turn the TV off", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "tv_send_key", "description": "Send a remote control key to the TV (works with Samsung, LG, Roku, Philips, Sony, Apple TV)", "parameters": {"type": "object", "properties": {"key": {"type": "string", "description": "Key code like KEY_PLAY, KEY_PAUSE, KEY_VOLUP, KEY_VOLDOWN, KEY_MUTE, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_ENTER, KEY_HOME, KEY_BACK, KEY_EXIT, KEY_MENU, KEY_SOURCE, KEY_CHUP, KEY_CHDOWN, KEY_NETFLIX, KEY_YOUTUBE, KEY_PRIME, KEY_DISNEY_PLUS, KEY_SPOTIFY, KEY_0..KEY_9"}}, "required": ["key"]}}},
    {"type": "function", "function": {"name": "tv_open_app", "description": "Open a streaming app on the TV", "parameters": {"type": "object", "properties": {"app": {"type": "string", "description": "App name: netflix, youtube, prime video, disney+, spotify, apple tv, plex"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "tv_discover", "description": "Scan the local network to discover TVs and their supported brands", "parameters": {"type": "object", "properties": {}, "required": []}}},
]

def _tv_send_key_wrapper(args=None):
    key = (args or {}).get("key", "")
    if key:
        return tv_control.send_key(key)
    return False, "No key specified"

def _tv_open_app_wrapper(args=None):
    return True, "App launch command sent (executed client-side)"

def _tv_discover_wrapper(args=None):
    return tv_control.discover_tvs()

TOOL_MAP = {
    "tv_power_on": lambda _: tv_control.power_on(),
    "tv_power_off": lambda _: tv_control.power_off(),
    "tv_send_key": _tv_send_key_wrapper,
    "tv_open_app": _tv_open_app_wrapper,
    "tv_discover": _tv_discover_wrapper,
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
                fn = TOOL_MAP.get(tc.function.name)
                args = {}
                if tc.function.arguments:
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        pass
                if tc.function.name == "tv_power_on":
                    tv_command = {"action": "power_on"}
                elif tc.function.name == "tv_power_off":
                    tv_command = {"action": "power_off"}
                elif tc.function.name == "tv_send_key":
                    tv_command = {"action": "send_key", "key": args.get("key", "")}
                elif tc.function.name == "tv_open_app":
                    tv_command = {"action": "open_app", "app": args.get("app", "")}
                elif tc.function.name == "tv_discover":
                    tv_command = {"action": "discover"}
                if fn:
                    success, result = fn(args)
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
