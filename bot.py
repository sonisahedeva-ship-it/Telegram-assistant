import os
import anthropic
import requests
import time

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

conversation_history = []

SYSTEM_PROMPT = """You are a sharp, efficient personal assistant. Not a generic chatbot.

You have web search access. Use it proactively for:
- Finding doctors, restaurants, services near Ahmedabad
- Current news, prices, weather
- Anything requiring up-to-date facts

Rules:
- Search the web first if the answer needs real-world data
- Return specific results: names, addresses, phone numbers, links
- Be concise, use bullet points
- Never give generic advice when you can give a real answer
- User is based in Ahmedabad, India
- Never ask unnecessary questions. Ask ONE question max if truly needed."""

TOOLS = [{"type": "web_search_20250305", "name": "web_search"}]

def send_message(chat_id, text):
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown"
        })

def send_typing(chat_id):
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })

def get_updates(offset=None):
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    return resp.json().get("result", [])

def ask_claude(user_message):
    global conversation_history
    conversation_history.append({"role": "user", "content": user_message})
    trimmed = conversation_history[-20:]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=trimmed
    )

    reply = ""
    for block in response.content:
        if hasattr(block, "text"):
            reply += block.text

    if not reply:
        reply = "I searched but could not find a clear answer. Try rephrasing?"

    conversation_history.append({"role": "assistant", "content": reply})
    return reply

def handle_command(cmd, chat_id):
    if cmd == "/start":
        send_message(chat_id, "Assistant ready. Just tell me what you need.")
    elif cmd == "/clear":
        global conversation_history
        conversation_history = []
        send_message(chat_id, "Memory cleared. Fresh start.")
    elif cmd == "/help":
        send_message(chat_id,
            "What I can do:\n"
            "- Search the web for live info\n"
            "- Find doctors, restaurants, services near Ahmedabad\n"
            "- Find flights, prices, current news\n"
            "- Draft emails, messages, content\n"
            "- Summarise text you paste\n\n"
            "Commands:\n"
            "/clear - Reset conversation memory\n"
            "/help - Show this message"
        )

def main():
    print("Bot is running with web search...")
    offset = None

    while True:
        try:
            updates = get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                user_id = message["from"]["id"]
                text = message.get("text", "").strip()

                if user_id != ALLOWED_USER_ID:
                    send_message(chat_id, "Unauthorized.")
                    continue

                if not text:
                    send_message(chat_id, "Send me a text message.")
                    continue

                if text.startswith("/"):
                    handle_command(text.split()[0], chat_id)
                    continue

                send_typing(chat_id)
                reply = ask_claude(text)
                send_message(chat_id, reply)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
