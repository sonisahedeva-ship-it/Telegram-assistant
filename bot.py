import os, json, time, re, tempfile, requests, anthropic, pytz
from datetime import datetime, timedelta
from urllib.parse import quote
from apscheduler.schedulers.background import BackgroundScheduler

# ── Config ──────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")  # optional, for voice

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
IST = pytz.timezone("Asia/Kolkata")

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ── Scheduler (reminders) ───────────────────────────────────────────────────────
scheduler = BackgroundScheduler(timezone=IST)
scheduler.start()

# ── Memory (persists across messages, resets on redeploy) ───────────────────────
MEMORY_FILE = "/app/memory.json"

def load_memory():
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except:
        return {"facts": {}}

def save_memory(data):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(data, f)
    except:
        pass

memory = load_memory()
conversation_history = []

# ── System prompt ───────────────────────────────────────────────────────────────
def build_system_prompt():
    facts_str = ""
    if memory.get("facts"):
        facts_str = "\n\nThings I know about the user:\n" + \
            "\n".join(f"- {k}: {v}" for k, v in memory["facts"].items())

    return f"""You are a sharp personal assistant. Not a generic chatbot.

You have web search access. Use it proactively for:
- Finding doctors, restaurants, any local service near Ahmedabad
- Current news, prices, flights
- Anything requiring live data

Rules:
- Complete tasks, don't just chat
- Be concise, use bullet points
- User is in Ahmedabad, India (IST timezone, UTC+5:30)
- Never ask unnecessary questions{facts_str}

SPECIAL TASK FORMATS — when you detect these, respond ONLY with the JSON block shown:

1. REMINDER REQUEST (e.g. "remind me at 6pm to call John"):
Respond with ONLY this JSON (nothing else):
REMINDER_JSON:{{"time": "HH:MM", "date": "YYYY-MM-DD or 'today' or 'tomorrow'", "message": "reminder text"}}

2. CALENDAR EVENT (e.g. "add meeting tomorrow 3pm with client"):
Respond with ONLY this JSON:
CALENDAR_JSON:{{"title": "event title", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "description": ""}}

3. REMEMBER THIS (e.g. "remember that my wife's name is Priya"):
Respond with ONLY this JSON:
MEMORY_JSON:{{"key": "short key", "value": "value to remember"}}

4. FORGET THIS (e.g. "forget my wife's name"):
Respond with ONLY this JSON:
FORGET_JSON:{{"key": "key to forget"}}

For everything else, respond normally."""

TOOLS = [{"type": "web_search_20250305", "name": "web_search"}]
WEATHER_KEYWORDS = ["weather", "temperature", "temp", "degrees", "rain", "humid", "forecast", "hot outside", "cold outside"]

# ── Weather ─────────────────────────────────────────────────────────────────────
def get_live_weather(city="Ahmedabad"):
    try:
        resp = requests.get(f"https://wttr.in/{city}?format=j1", timeout=10)
        d = resp.json()["current_condition"][0]
        return (f"LIVE WEATHER for {city}: {d['temp_C']}C, feels like {d['FeelsLikeC']}C, "
                f"humidity {d['humidity']}%, {d['weatherDesc'][0]['value']}")
    except:
        return None

def is_weather_query(text):
    return any(kw in text.lower() for kw in WEATHER_KEYWORDS)

# ── Telegram helpers ─────────────────────────────────────────────────────────────
def send_message(chat_id, text, parse_mode="Markdown"):
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        requests.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id, "text": chunk, "parse_mode": parse_mode,
            "disable_web_page_preview": True
        })

def send_typing(chat_id):
    requests.post(f"{TELEGRAM_API}/sendChatAction", json={"chat_id": chat_id, "action": "typing"})

def get_updates(offset=None):
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    return requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35).json().get("result", [])

def get_file_url(file_id):
    resp = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = resp["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

# ── Voice transcription ──────────────────────────────────────────────────────────
def transcribe_voice(file_id):
    if not OPENAI_API_KEY:
        return None
    try:
        url = get_file_url(file_id)
        audio_data = requests.get(url).content
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(audio_data)
            f.flush()
            with open(f.name, "rb") as audio_file:
                resp = requests.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    data={"model": "whisper-1"},
                    files={"file": ("voice.ogg", audio_file, "audio/ogg")}
                )
            os.unlink(f.name)
        return resp.json().get("text", "")
    except Exception as e:
        print(f"Voice error: {e}")
        return None

# ── Reminder ─────────────────────────────────────────────────────────────────────
def send_reminder(chat_id, message):
    send_message(chat_id, f"Reminder: {message}")

def schedule_reminder(chat_id, time_str, date_str, message):
    try:
        now = datetime.now(IST)
        hour, minute = map(int, time_str.split(":"))
        if date_str == "today":
            run_date = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        elif date_str == "tomorrow":
            run_date = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        else:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            run_date = IST.localize(d.replace(hour=hour, minute=minute, second=0, microsecond=0))

        if run_date < now:
            return False, "That time has already passed."

        scheduler.add_job(send_reminder, "date", run_date=run_date,
                          args=[chat_id, message], id=f"reminder_{int(run_date.timestamp())}")
        return True, run_date.strftime("%d %b at %I:%M %p")
    except Exception as e:
        return False, str(e)

# ── Google Calendar link ──────────────────────────────────────────────────────────
def make_calendar_link(title, date_str, start_time, end_time, description=""):
    try:
        start = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
        end   = datetime.strptime(f"{date_str} {end_time}",   "%Y-%m-%d %H:%M")
        fmt   = "%Y%m%dT%H%M%S"
        url = (
            f"https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}"
            f"&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
            f"&details={quote(description)}"
            f"&ctz=Asia/Kolkata"
        )
        return url
    except:
        return None

# ── Ask Claude ────────────────────────────────────────────────────────────────────
def ask_claude(user_message, chat_id):
    global memory, conversation_history

    enriched = user_message
    if is_weather_query(user_message):
        weather = get_live_weather()
        if weather:
            enriched = f"{user_message}\n\n{weather}"

    conversation_history.append({"role": "user", "content": enriched})
    trimmed = conversation_history[-20:]

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=build_system_prompt(),
        tools=TOOLS,
        messages=trimmed,
        tool_choice={"type": "auto"}
    )

    # Handle tool use loop
    msgs = list(trimmed)
    current = response
    while current.stop_reason == "tool_use":
        tool_uses = [b for b in current.content if b.type == "tool_use"]
        results = [{"type": "tool_result", "tool_use_id": t.id, "content": "done"} for t in tool_uses]
        msgs.append({"role": "assistant", "content": current.content})
        msgs.append({"role": "user", "content": results})
        current = client.messages.create(
            model="claude-opus-4-5", max_tokens=1024,
            system=build_system_prompt(), tools=TOOLS, messages=msgs
        )

    reply = "".join(b.text for b in current.content if hasattr(b, "text"))
    if not reply:
        reply = "Could not find an answer. Try rephrasing?"

    # ── Handle special JSON responses ──────────────────────────────────────────
    # Reminder
    m = re.search(r'REMINDER_JSON:(\{.*?\})', reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            ok, info = schedule_reminder(chat_id, data["time"], data["date"], data["message"])
            reply = f"Reminder set for {info}: {data['message']}" if ok else f"Could not set reminder: {info}"
        except:
            reply = "Could not parse reminder. Try: 'remind me at 6pm to call John'"

    # Calendar
    m = re.search(r'CALENDAR_JSON:(\{.*?\})', reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            link = make_calendar_link(data["title"], data["date"], data["start_time"], data["end_time"], data.get("description",""))
            reply = f"Tap to add to Google Calendar:\n{data['title']} on {data['date']} at {data['start_time']}\n\n{link}"
        except:
            reply = "Could not create calendar link."

    # Memory save
    m = re.search(r'MEMORY_JSON:(\{.*?\})', reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            memory["facts"][data["key"]] = data["value"]
            save_memory(memory)
            reply = f"Got it. I'll remember that {data['key']} is {data['value']}."
        except:
            reply = "Could not save to memory."

    # Memory forget
    m = re.search(r'FORGET_JSON:(\{.*?\})', reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if data["key"] in memory["facts"]:
                del memory["facts"][data["key"]]
                save_memory(memory)
                reply = f"Forgotten: {data['key']}."
            else:
                reply = "I don't have that in memory."
        except:
            reply = "Could not forget that."

    conversation_history.append({"role": "assistant", "content": reply})
    return reply

# ── Commands ──────────────────────────────────────────────────────────────────────
def handle_command(cmd, chat_id):
    if cmd == "/start":
        send_message(chat_id, "Assistant ready. What do you need?")
    elif cmd == "/clear":
        global conversation_history
        conversation_history = []
        send_message(chat_id, "Conversation cleared.")
    elif cmd == "/memory":
        if memory.get("facts"):
            msg = "What I remember:\n" + "\n".join(f"- {k}: {v}" for k, v in memory["facts"].items())
        else:
            msg = "Nothing stored in memory yet."
        send_message(chat_id, msg)
    elif cmd == "/reminders":
        jobs = scheduler.get_jobs()
        if jobs:
            msg = "Upcoming reminders:\n" + "\n".join(
                f"- {j.next_run_time.strftime('%d %b %I:%M %p')}: {j.args[1]}" for j in jobs
            )
        else:
            msg = "No reminders set."
        send_message(chat_id, msg)
    elif cmd == "/help":
        send_message(chat_id,
            "What I can do:\n"
            "- Live weather\n"
            "- Web search for real info\n"
            "- Find doctors, restaurants near Ahmedabad\n"
            "- Set reminders: 'remind me at 6pm to call John'\n"
            "- Add calendar events: 'add meeting tomorrow 3pm'\n"
            "- Remember things: 'remember that my car is a Swift'\n"
            "- Draft emails, summarise text\n\n"
            "Commands:\n"
            "/memory - See what I remember\n"
            "/reminders - See upcoming reminders\n"
            "/clear - Reset conversation\n"
            "/help - This message"
        )

# ── Main loop ─────────────────────────────────────────────────────────────────────
def main():
    print("Bot running: memory + reminders + voice + calendar...")
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

                if user_id != ALLOWED_USER_ID:
                    send_message(chat_id, "Unauthorized.")
                    continue

                # Voice message
                if message.get("voice"):
                    send_typing(chat_id)
                    if not OPENAI_API_KEY:
                        send_message(chat_id, "Voice not set up. Add OPENAI_API_KEY in Railway to enable.")
                        continue
                    transcript = transcribe_voice(message["voice"]["file_id"])
                    if not transcript:
                        send_message(chat_id, "Could not transcribe voice. Try again.")
                        continue
                    send_message(chat_id, f"_Heard: {transcript}_")
                    reply = ask_claude(transcript, chat_id)
                    send_message(chat_id, reply)
                    continue

                text = message.get("text", "").strip()
                if not text:
                    send_message(chat_id, "Send me a text or voice message.")
                    continue

                if text.startswith("/"):
                    handle_command(text.split()[0], chat_id)
                    continue

                send_typing(chat_id)
                reply = ask_claude(text, chat_id)
                send_message(chat_id, reply)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
