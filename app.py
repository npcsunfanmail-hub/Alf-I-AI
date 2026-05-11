import os
import json
from datetime import datetime, timezone
import requests
import urllib.parse
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)

SYSTEM_PROMPT = "You are Alf-I, an AI assistant created by Logan Robinson. You have a friendly, slightly playful Australian personality. You can search the web and browse webpages to get current information. When asked about anything time-sensitive or recent, always search the web first. Never claim to lack current knowledge without searching. Be concise."

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "Search the web for current information, news, and recent events. Use this for anything time-sensitive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browse_web",
            "description": "Fetch and read the full content of a specific webpage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to visit"}
                },
                "required": ["url"],
            },
        },
    }
]


def search_web(query: str) -> str:
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&skip_disambig=1"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        parts = []
        if data.get("Heading"):
            parts.append(f"Topic: {data['Heading']}")
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        if data.get("Infobox") and data["Infobox"].get("content"):
            for item in data["Infobox"]["content"]:
                if item.get("label") and item.get("value"):
                    parts.append(f"{item['label']}: {item['value']}")
        if data.get("RelatedTopics"):
            for t in data["RelatedTopics"][:8]:
                if isinstance(t, dict) and t.get("Text"):
                    parts.append(t["Text"])
        if data.get("Results"):
            for r in data["Results"][:5]:
                if r.get("Text"):
                    parts.append(r["Text"])
        if parts:
            return "\n".join(parts)[:3000]
        text_url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
        tr = requests.get(text_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(tr.text, "html.parser")
        links = soup.find_all("a", class_="result-link")
        if links:
            return "\n".join([a.get_text(strip=True) for a in links[:10]])
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
    resp = client.chat.completions.create(model=LLM_MODEL, messages=messages, tools=TOOLS, tool_choice="auto")
    return resp.choices[0].message


@app.route("/")
def serve():
    return open(os.path.join(BASE_DIR, "index.html")).read(), 200, {"Content-Type": "text/html"}


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "messages required"}), 400

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %B %d, %Y at %I:%M %p UTC")
    sys_msg = SYSTEM_PROMPT + f"\n\n[SYSTEM: Today's date is {date_str}. You already know the current date — do not search for it.]"
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
                if name == "search_web":
                    result = search_web(**args)
                elif name == "browse_web":
                    result = browse_web(**args)
                else:
                    result = f"Unknown tool: {name}"
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            msg2 = call_llm(msgs)
            content = msg2.content or ""

        return jsonify({"content": content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
