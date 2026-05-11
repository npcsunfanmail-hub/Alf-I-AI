import os
import json
import re
import traceback
import tempfile
import sqlite3
from datetime import datetime, timezone
import requests
import urllib.parse
from bs4 import BeautifulSoup
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
    "Your name is Alf-I. You are an AI assistant. You were created by Logan Robinson.\n\n"
    "You have access to the internet. When you need current information, use one of these commands "
    "exactly as shown (on its own line, start of line):\n"
    "- To search: [SEARCH: your search query]\n"
    "- To visit a page: [BROWSE: full URL]\n"
    "After the command, I will show you the results. Then continue your response."
)

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")


def search_web(query: str) -> str:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for r in soup.select(".result__body"):
            title_el = r.select_one(".result__title a")
            snippet_el = r.select_one(".result__snippet")
            if title_el:
                title = title_el.get_text(strip=True)
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                results.append(f"{title}\n{snippet}")
            if len(results) >= 5:
                break
        if results:
            return "\n\n".join(results)[:3000]
        return f"No results found for '{query}'."
    except Exception as e:
        return f"Search error: {e}"


def browse_web(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"}
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [l for l in text.splitlines() if l.strip()]
        return "\n".join(lines[:200])
    except Exception as e:
        return f"Error: {e}"


def call_llm(messages):
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL or None)
    resp = client.chat.completions.create(model=LLM_MODEL, messages=messages)
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


TOOL_RE = r'\[(SEARCH|BROWSE):\s*(.+?)\]'

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages required"}), 400

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d, %Y at %I:%M %p UTC")
    sys_msg = SYSTEM_PROMPT + f"\n\n[SYSTEM: Today's date is {date_str}. You already know the current date - do not search for it.]"
    msgs = [{"role": "system", "content": sys_msg}] + messages

    try:
        for _ in range(5):
            msg = call_llm(msgs)
            content = msg.content or ""
            m = re.search(TOOL_RE, content)
            if not m:
                return jsonify({"content": content.strip()})
            action, arg = m.group(1), m.group(2).strip()
            if action == "SEARCH":
                result = search_web(arg)
            elif action == "BROWSE":
                result = browse_web(arg)
            else:
                result = f"Unknown action: {action}"
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": f"[TOOL RESULT for {action}: {arg}]\n{result}"})

        return jsonify({"content": (msg.content or "").strip()})
    except Exception as e:
        traceback.print_exc()
        body = getattr(e, "body", None) or getattr(e, "response", None) or ""
        return jsonify({"error": repr(e), "detail": str(body)[:500]}), 500
