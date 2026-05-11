# AI Assistant - Web Version

Deploy your own AI assistant with voice input/output, web browsing, and file access.

## Deploy to Vercel

1. Push this folder to a GitHub repo
2. Go to https://vercel.com and import the repo
3. In Settings > Environment Variables, add:

| Variable | Value | Example |
|---|---|---|
| `LLM_API_KEY` | Your API key | `gsk_xxx` or `AIzaxxx` |
| `LLM_BASE_URL` | API endpoint | `https://api.openai.com/v1` |
| `LLM_MODEL` | Model name | `gpt-4o-mini` |

4. Deploy

## Free API Providers (no credit card)

- **Groq**: https://console.groq.com — 14,400 req/day free
- **Google AI Studio**: https://aistudio.google.com/apikey — 1,500 req/day free
- **OpenRouter**: https://openrouter.ai — multiple free models

## Features

- Text chat
- Voice input (mic button) — Chrome/Edge/Safari
- Voice output (speaker toggle)
- Web browsing tool
