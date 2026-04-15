import os
import json
import time
import re
import requests
import anthropic
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
from apscheduler.schedulers.background import BackgroundScheduler

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
IST = ZoneInfo("Asia/Kolkata")
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.start()

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

def build_system_prompt():
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

    facts_str = ""
    if memory.get("facts"):
        facts_str = "\n\nThings I know about the user:\n" + \
            "\n".join(f"- {k}: {v}" for k, v in memory["facts"].items())

    return f"""You are a sharp personal assistant. Not a generic chatbot.

Current date and time in Ahmedabad (IST): {now_str}
Tomorrow's date: {tomorrow_str}
{facts_str}

You have web_search access. Use it for: doctors, restaurants, news, flights, prices, anything live.

Rules:
- Complete tasks, do not just chat
- Be concise, use bullet points
- User is in Ahmedabad, India
- You CANNOT control the phone, set phone alarms, or access apps. Say so clearly if asked.

SPECIAL COMMANDS - detect the intent and reply ONLY with the JSON, nothing else:

1. REMINDER intent (remind me in X mins, at 6pm, tomorrow at 9am, etc):
Calculate exact future datetime from current time {now_str}.
Examples: "in 10 mins" = {(now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")}, "in 2 hours" = {(now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")}
Reply ONLY:
REMINDER_JSON:{{"datetime": "YYYY-MM-DD HH:MM", "message": "reminder text"}}

2. CALENDAR EVENT intent (add meeting, schedule call, etc):
Reply ONLY:
CALENDAR_JSON:{{"title": "event title", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "description": ""}}

3. REMEMBER intent (remember that X is Y):
Reply ONLY:
MEMORY_JSON:{{"key": "short key", "value": "value"}}

4. FORGET intent (forget my X):
Reply ONLY:
FORGET_JSON:{{"key": "key to delete"}}

For everything else respond normally."""

TOOLS = [{"type": "web_search_20250305", "name": "web_search"}]
WEATHER_KEYWORDS = ["weather", "temperature", "temp", "degrees", "rain", "humid", "forecast"]

def get_live_weather(city="Ahmedabad"):
    try:
        resp = requests.get(f"https://wttr.in/{city}?format=j1", timeout=10)
        d = resp.json()["current_condition"][0]
        return (
            f"LIVE WEATHER for {city}: "
            f"{d['temp_C']}C, feels like {d['FeelsLikeC']}C, "
            f"humidity {d['humidity']}%, {d['weatherDesc'][0]['value']}"
        )
    except:
        return None

def is_weather_query(text):
    return any(kw in text.lower() for kw in WEATHER_KEYWORDS)

def send_message(chat_id, text):
    try:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }, timeout=10)
    except Exception as e:
        print(f"send_message error: {e}")

def send_typing(chat_id):
    try:
        requests.post(f"{TELEGRAM_API}/sendChatAction",
                      json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except:
        pass

def get_updates(offset=None):
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    resp = requests.get(f"{TELEGRAM_API}/getUpdates", params=params, timeout=35)
    return resp.json().get("result", [])

def fire_reminder(chat_id, message):
    send_message(chat_id, f"Reminder: {message}")

def schedule_reminder(chat_id, dt_str, message):
    try:
        now = datetime.now(IST)
        run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        if run_date <= now:
            run_date = now + timedelta(minutes=1)
        job_id = f"r_{int(run_date.timestamp())}"
        scheduler.add_job(fire_reminder, "date", run_date=run_date,
                          args=[chat_id, message], id=job_id, replace_existing=True)
        return True, run_date.strftime("%d %b at %I:%M %p")
    except Exception as e:
        return False, str(e)

def make_calendar_link(title, date_str, start_time, end_time, description=""):
    try:
        start = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
        end   = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
        fmt   = "%Y%m%dT%H%M%S"
        return (
            "https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}"
            f"&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
            f"&details={quote(description)}"
            "&ctz=Asia/Kolkata"
        )
    except:
        return None

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
        messages=trimmed
    )

    msgs = list(trimmed)
    current = response
    loop_count = 0
    while current.stop_reason == "tool_use" and loop_count < 5:
        loop_count += 1
        tool_uses = [b for b in current.content if b.type == "tool_use"]
        results = [{"type": "tool_result", "tool_use_id": t.id, "content": "done"} for t in tool_uses]
        msgs.append({"role": "assistant", "content": current.content})
        msgs.append({"role": "user", "content": results})
        current = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1024,
            system=build_system_prompt(),
            tools=TOOLS,
            messages=msgs
        )

    reply = "".join(b.text for b in current.content if hasattr(b, "text")).strip()
    if not reply:
        reply = "Could not get a response. Try rephrasing?"

    m = re.search(r'REMINDER_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            ok, info = schedule_reminder(chat_id, data["datetime"], data["message"])
            reply = f"Reminder set for *{info}*\n_{data['message']}_" if ok else f"Could not set reminder: {info}"
        except Exception as e:
            reply = f"Reminder error: {e}"

    m = re.search(r'CALENDAR_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            link = make_calendar_link(data["title"], data["date"], data["start_time"], data["end_time"], data.get("description", ""))
            reply = f"Tap to add to Google Calendar:\n*{data['title']}* - {data['date']} at {data['start_time']}\n\n{link}"
        except Exception as e:
            reply = f"Calendar error: {e}"

    m = re.search(r'MEMORY_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            memory["facts"][data["key"]] = data["value"]
            save_memory(memory)
            reply = f"Got it. Remembered: *{data['key']}* = {data['value']}"
        except Exception as e:
            reply = f"Memory error: {e}"

    m = re.search(r'FORGET_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            key = data["key"]
            if key in memory["facts"]:
                del memory["facts"][key]
                save_memory(memory)
                reply = f"Forgotten: {key}"
            else:
                reply = f"Nothing stored under: {key}"
        except Exception as e:
            reply = f"Forget error: {e}"

    conversation_history.append({"role": "assistant", "content": reply})
    return reply

def handle_command(cmd, chat_id):
    global conversation_history
    if cmd == "/start":
        send_message(chat_id, "Assistant ready. What do you need?")
    elif cmd == "/clear":
        conversation_history = []
        send_message(chat_id, "Conversation cleared.")
    elif cmd == "/memory":
        if memory.get("facts"):
            msg = "*What I remember:*\n" + "\n".join(f"- {k}: {v}" for k, v in memory["facts"].items())
        else:
            msg = "Nothing stored yet. Say 'remember that...' to save something."
        send_message(chat_id, msg)
    elif cmd == "/reminders":
        jobs = scheduler.get_jobs()
        if jobs:
            lines = [f"- {j.next_run_time.strftime('%d %b %I:%M %p')}: {j.args[1]}" for j in jobs]
            send_message(chat_id, "*Upcoming reminders:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No reminders set.")
    elif cmd == "/help":
        send_message(chat_id,
            "*What I can do:*\n"
            "- Live weather for any city\n"
            "- Web search: doctors, restaurants, flights, news\n"
            "- Reminders: 'remind me in 10 mins to call John'\n"
            "- Calendar: 'add meeting tomorrow 3pm' (tap link to save)\n"
            "- Memory: 'remember that my doctor is Dr Shah'\n"
            "- Draft emails, messages, content\n"
            "- Summarise anything you paste\n\n"
            "*What I cannot do:*\n"
            "- Control your phone or set phone alarms\n"
            "- Access your contacts or apps\n\n"
            "*Commands:*\n"
            "/memory - See stored facts\n"
            "/reminders - See upcoming reminders\n"
            "/clear - Reset conversation\n"
            "/help - This message"
        )

def main():
    print("Bot is running...")
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
                text = message.get("text", "").strip()
                if not text:
                    continue
                if text.startswith("/"):
                    handle_command(text.split()[0], chat_id)
                    continue
                send_typing(chat_id)
                try:
                    reply = ask_claude(text, chat_id)
                    send_message(chat_id, reply)
                except Exception as e:
                    err = str(e)[:300]
                    print(f"Claude error: {err}")
                    send_message(chat_id, f"Error: {err}")
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
