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

SYSTEM_PROMPT = "Your name is Alf-I. You are an AI assistant. You were created by Logan Robinson."

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browse_web",
            "description": "Fetch and read the content of a webpage. Returns the page text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to visit (must include https://)",
                    }
                },
                "required": ["url"],
            },
        },
    },
] + tv_control.TV_TOOLS


def browse_web(url: str) -> str:
    import requests
    from bs4 import BeautifulSoup
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [l for l in text.splitlines() if l.strip()]
        content = "\n".join(lines[:200])
        return f"--- Page: {url} ---\n{content}\n--- End ---"
    except Exception as e:
        return f"Error browsing {url}: {e}"


TOOL_DISPATCH = {
    "browse_web": lambda **kw: browse_web(**kw),
    "discover_tvs": lambda **kw: json.dumps(tv_control.discover_tvs(**kw)),
    "pair_tv": lambda **kw: json.dumps(tv_control.pair_tv(**kw)),
    "send_remote_command": lambda **kw: json.dumps(tv_control.send_remote_command(**kw)),
    "get_paired_tvs": lambda **kw: json.dumps(tv_control.get_paired_tvs(**kw)),
    "disconnect_tv": lambda **kw: json.dumps(tv_control.disconnect(**kw)),
}


def call_llm(messages):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    resp = client.chat.completions.create(model=LLM_MODEL, messages=messages, tools=TOOLS, tool_choice="auto")
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
def api_tv_discover():
    try:
        tvs = tv_control.discover_tvs()
        return jsonify({"tvs": tvs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tv/pair", methods=["POST"])
def api_tv_pair():
    data = request.get_json()
    ip = data.get("ip")
    tv_type = data.get("tv_type")
    code = data.get("pairing_code")
    if not ip or not tv_type or not code:
        return jsonify({"error": "ip, tv_type, and pairing_code required"}), 400
    result = tv_control.pair_tv(ip, tv_type, code)
    return jsonify(result)


@app.route("/api/tv/command", methods=["POST"])
def api_tv_command():
    data = request.get_json()
    ip = data.get("ip")
    command = data.get("command")
    if not ip or not command:
        return jsonify({"error": "ip and command required"}), 400
    result = tv_control.send_remote_command(ip, command)
    return jsonify(result)


@app.route("/api/tv/status", methods=["GET"])
def api_tv_status():
    return jsonify({"paired": tv_control.get_paired_tvs()})


@app.route("/api/tv/disconnect", methods=["POST"])
def api_tv_disconnect():
    data = request.get_json()
    ip = data.get("ip")
    if not ip:
        return jsonify({"error": "ip required"}), 400
    tv_control.disconnect(ip)
    return jsonify({"ok": True})


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
        content = msg.content or ""
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            msgs.append({"role": "assistant", "content": content})
            for tc in tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                fn = TOOL_DISPATCH.get(name)
                result = fn(**args) if fn else f"Unknown tool: {name}"
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            msg2 = call_llm(msgs)
            content = msg2.content or ""

        return jsonify({"content": content.strip()})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": repr(e)}), 500
