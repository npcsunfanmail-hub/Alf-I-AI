import os
import json
import requests
from bs4 import BeautifulSoup
from http.server import BaseHTTPRequestHandler


SYSTEM_PROMPT = """You are a helpful AI assistant. You can browse the web to fetch information. Be concise and helpful."""

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
    }
]


def browse_web(url: str) -> str:
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
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


def call_llm(messages):
    from openai import OpenAI
    client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL if LLM_BASE_URL else None)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    return resp.choices[0].message


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"AI Assistant API is running")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))

        messages = body.get("messages", [])
        if not messages:
            self._respond(400, {"error": "messages required"})
            return

        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        msgs.extend(messages)

        try:
            msg = call_llm(msgs)
            content = msg.content or ""
            tool_calls = getattr(msg, "tool_calls", None) or []

            if tool_calls:
                msgs.append({"role": "assistant", "content": content})
                for tc in tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    if name == "browse_web":
                        result = browse_web(**args)
                    else:
                        result = f"Unknown tool: {name}"
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                msg2 = call_llm(msgs)
                content = msg2.content or ""

            self._respond(200, {"content": content.strip()})

        except Exception as e:
            self._respond(500, {"error": str(e)})

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _respond(self, status, data):
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
