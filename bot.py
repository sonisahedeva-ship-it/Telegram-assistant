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

CRITICAL RULE: You MUST use the web_search tool before answering ANY of these:
- Weather or temperature questions (ALWAYS search, never guess)
- Finding doctors, clinics, restaurants, any local business
- Flight prices or availability
- Current news or recent events
- Prices of anything
- Anything that changes day to day

NEVER answer from memory for the above topics. Always search first, then answer.

For tasks like drafting emails or explaining concepts, you can answer directly.

Rules:
- Return specific results: names, addresses, phone numbers, links
- Be concise, use bullet points
- User is based in Ahmedabad, India. Use this for all location searches.
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
        messages=trimmed,
        tool_choice={"type": "auto"}
    )

    # Handle tool use loop - Claude may search multiple times before final answer
    messages = list(trimmed)
    current_response = response

    while current_response.stop_reason == "tool_use":
        tool_uses = [b for b in current_response.content if b.type == "tool_use"]
        tool_results = []

        for tool_use in tool_uses:
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use.id,
                "content": "Search executed."
            })

        messages.append({"role": "assistant", "content": current_response.content})
        messages.append({"role": "user", "content": tool_results})

        current_response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

    # Extract final text reply
    reply = ""
    for block in current_response.content:
        if hasattr(block, "text"):
            reply += block.text

    if not reply:
        reply = "Could not find a clear answer. Try rephrasing?"

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
            "- Current weather, flights, news\n"
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
