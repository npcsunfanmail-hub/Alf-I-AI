import os
import json
import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify
from openai import OpenAI

app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)

SYSTEM_PROMPT = "You are Alf-I, a helpful AI assistant. You talk with a friendly and slightly playful personality. You can browse the web to fetch information. Be concise."

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "browse_web",
            "description": "Fetch and read the content of a webpage.",
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

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    try:
        msg = call_llm(msgs)
        content = msg.content or ""
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            msgs.append({"role": "assistant", "content": content})
            for tc in tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                result = browse_web(**args) if name == "browse_web" else f"Unknown tool: {name}"
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            msg2 = call_llm(msgs)
            content = msg2.content or ""

        return jsonify({"content": content.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
