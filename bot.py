import os
import anthropic
import requests
import time
import json

# ── Config from environment variables ──────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]       # from BotFather
CLAUDE_API_KEY   = os.environ["CLAUDE_API_KEY"]        # from console.anthropic.com
ALLOWED_USER_ID  = int(os.environ["ALLOWED_USER_ID"])  # your Telegram user ID (number)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ── Conversation memory (per session) ──────────────────────────────────────────
conversation_history = []

SYSTEM_PROMPT = """You are a sharp, efficient personal assistant. You are NOT a generic chatbot.

Your job is to complete tasks, not have conversations. When given a task:
- Find real, specific answers (not generic advice)
- Be concise — bullet points over paragraphs
- If you don't know something current, say so clearly
- Always give actionable output: names, numbers, addresses, links

The user is based in Ahmedabad, India. Keep this in mind for location-based tasks.

Examples of how you respond:
- "Find a dentist near Satellite" → Return 3 specific clinics with address + phone number
- "Draft a follow-up email" → Return the email, ready to copy-paste
- "Remind me at 6pm" → Acknowledge and confirm the reminder

Never ask unnecessary follow-up questions. Make reasonable assumptions and complete the task.
If you truly need clarification, ask ONE question only."""

# ── Telegram helpers ────────────────────────────────────────────────────────────
def send_message(chat_id: int, text: str):
    """Send a message back to the user on Telegram."""
    # Telegram has a 4096 char limit — split if needed
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown"
        })

def send_typing(chat_id: int):
    """Show 'typing...' indicator while Claude thinks."""
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={
        "chat_id": chat_id,
        "action": "typing"
    })

def get_updates(offset: int = None):
    """Long-poll Telegram for new messages."""
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    return resp.json().get("result", [])

# ── Claude ─────────────────────────────────────────────────────────────────────
def ask_claude(user_message: str) -> str:
    """Send message to Claude, maintaining conversation history."""
    global conversation_history

    conversation_history.append({"role": "user", "content": user_message})

    # Keep last 20 messages to avoid token bloat
    trimmed = conversation_history[-20:]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=trimmed
    )

    reply = response.content[0].text
    conversation_history.append({"role": "assistant", "content": reply})

    return reply

# ── Commands ───────────────────────────────────────────────────────────────────
def handle_command(cmd: str, chat_id: int):
    if cmd == "/start":
        send_message(chat_id, "👋 *Assistant ready.* Just tell me what you need.")
    elif cmd == "/clear":
        global conversation_history
        conversation_history = []
        send_message(chat_id, "🧹 Memory cleared. Fresh start.")
    elif cmd == "/help":
        send_message(chat_id, (
            "*What I can do:*\n"
            "• Answer questions with real info\n"
            "• Draft emails, messages, content\n"
            "• Summarise text you paste\n"
            "• Help with work tasks on the go\n\n"
            "*Commands:*\n"
            "/clear — Reset conversation memory\n"
            "/help — Show this message"
        ))

# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    print("✅ Bot is running...")
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
                text    = message.get("text", "").strip()

                # Security: only respond to your account
                if user_id != ALLOWED_USER_ID:
                    send_message(chat_id, "⛔ Unauthorized.")
                    continue

                if not text:
                    send_message(chat_id, "Send me a text message.")
                    continue

                # Handle commands
                if text.startswith("/"):
                    handle_command(text.split()[0], chat_id)
                    continue

                # Normal message → Claude
                send_typing(chat_id)
                reply = ask_claude(text)
                send_message(chat_id, reply)

        except requests.exceptions.Timeout:
            pass  # Normal for long-polling, just continue
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
