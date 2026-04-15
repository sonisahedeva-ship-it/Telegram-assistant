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

# ── Config ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
CLAUDE_API_KEY  = os.environ["CLAUDE_API_KEY"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_KEY    = os.environ["SUPABASE_KEY"]

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
IST = ZoneInfo("Asia/Kolkata")
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
scheduler.start()

# ── Supabase helpers ─────────────────────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table, filters=""):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    r = requests.get(url, headers=SB_HEADERS, timeout=10)
    return r.json() if r.ok else []

def sb_insert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=SB_HEADERS, json=data, timeout=10)
    return r.ok

def sb_upsert(table, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    r = requests.post(url, headers=headers, json=data, timeout=10)
    return r.ok

def sb_update(table, filters, data):
    url = f"{SUPABASE_URL}/rest/v1/{table}?{filters}"
    r = requests.patch(url, headers=SB_HEADERS, json=data, timeout=10)
    return r.ok

# ── Memory (Supabase) ────────────────────────────────────────────────────────────
def mem_set(key, value):
    return sb_upsert("memory", {"key": key, "value": str(value)})

def mem_get(key):
    rows = sb_get("memory", f"key=eq.{key}&select=value")
    return rows[0]["value"] if rows else None

def mem_get_all():
    rows = sb_get("memory", "select=key,value")
    return {r["key"]: r["value"] for r in rows} if rows else {}

def mem_delete(key):
    url = f"{SUPABASE_URL}/rest/v1/memory?key=eq.{key}"
    r = requests.delete(url, headers=SB_HEADERS, timeout=10)
    return r.ok

def log_entry(entry_type, content, metadata=None):
    sb_insert("logs", {
        "type": entry_type,
        "content": content,
        "metadata": metadata or {}
    })

def get_logs(entry_type, limit=10):
    return sb_get("logs", f"type=eq.{entry_type}&order=created_at.desc&limit={limit}&select=content,metadata,created_at")

# ── Reminders (Supabase) ─────────────────────────────────────────────────────────
def save_reminder_db(chat_id, message, remind_at):
    return sb_insert("reminders", {
        "chat_id": chat_id,
        "message": message,
        "remind_at": remind_at.strftime("%Y-%m-%dT%H:%M:%S"),
        "done": False
    })

def mark_reminder_done(reminder_id):
    sb_update("reminders", f"id=eq.{reminder_id}", {"done": True})

def load_pending_reminders():
    now_str = datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S")
    return sb_get("reminders", f"done=eq.false&remind_at=gt.{now_str}&select=id,chat_id,message,remind_at")

conversation_history = []

# ── System Prompt ────────────────────────────────────────────────────────────────
def build_system_prompt():
    now = datetime.now(IST)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    facts = mem_get_all()
    facts_str = ""
    if facts:
        facts_str = "\n\nThings I know about the user:\n" + \
            "\n".join(f"- {k}: {v}" for k, v in facts.items())

    return f"""You are a sharp, caring personal assistant and life coach. Not a generic chatbot.

Current date and time in Ahmedabad (IST): {now_str}
{facts_str}

You have web_search access. Use it for: doctors, restaurants, news, flights, prices, anything live.

MODES:
- Normal: complete tasks efficiently, be concise, use bullet points
- Therapist mode (triggered by "therapist mode" or detecting distress): Use CBT-style questions. Be warm, non-judgmental. Ask one question at a time. Help user reframe negative thoughts. Never give empty positivity.
- Vent mode (triggered by "vent mode"): Just listen and validate. No advice unless asked.

You CANNOT control the phone, set phone alarms, or access apps. Say so clearly if asked.
User is in Ahmedabad, India.

SPECIAL COMMANDS - detect the intent and reply ONLY with the JSON, nothing else:

1. REMINDER intent:
Calculate exact datetime from {now_str}.
"in X mins" = add X mins. "in X hours" = add X hours. "at HH:MM" = today at that time. "tomorrow at HH:MM" = tomorrow.
Reply ONLY: REMINDER_JSON:{{"datetime": "YYYY-MM-DD HH:MM", "message": "reminder text"}}

2. CALENDAR EVENT intent:
Reply ONLY: CALENDAR_JSON:{{"title": "title", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "description": ""}}

3. REMEMBER intent ("remember that X"):
Reply ONLY: MEMORY_JSON:{{"key": "short key", "value": "value"}}

4. FORGET intent ("forget my X"):
Reply ONLY: FORGET_JSON:{{"key": "key"}}

5. LOG IDEA intent ("idea: X", "log idea X"):
Reply ONLY: IDEA_JSON:{{"idea": "the idea", "category": "business/personal/product/other"}}

6. LOG DECISION intent ("log decision: X"):
Reply ONLY: DECISION_JSON:{{"decision": "what you decided", "reason": "why"}}

7. GRATITUDE intent ("grateful for X", "gratitude: X", "log gratitude X"):
Reply ONLY: GRATITUDE_JSON:{{"entry": "what they are grateful for"}}

8. MOOD/CHECKIN intent ("mood: X/10", "energy: X", "checkin", "how am I doing"):
Reply ONLY: CHECKIN_JSON:{{"mood": 0, "energy": 0, "sleep": 0, "note": ""}}
Use 0 for any score not mentioned.

9. STANDUP intent ("standup", "daily standup", "my 3 priorities"):
Reply ONLY: STANDUP_JSON:{{"priorities": ["priority 1", "priority 2", "priority 3"]}}

For everything else respond normally."""

TOOLS = [{"type": "web_search_20250305", "name": "web_search"}]
WEATHER_KEYWORDS = ["weather", "temperature", "temp", "degrees", "rain", "humid", "forecast"]

# ── Weather ──────────────────────────────────────────────────────────────────────
def get_live_weather(city="Ahmedabad"):
    try:
        resp = requests.get(f"https://wttr.in/{city}?format=j1", timeout=10)
        d = resp.json()["current_condition"][0]
        return (f"LIVE WEATHER for {city}: {d['temp_C']}C, "
                f"feels like {d['FeelsLikeC']}C, "
                f"humidity {d['humidity']}%, "
                f"{d['weatherDesc'][0]['value']}")
    except:
        return None

def is_weather_query(text):
    return any(kw in text.lower() for kw in WEATHER_KEYWORDS)

# ── Telegram helpers ─────────────────────────────────────────────────────────────
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

# ── Reminder scheduling ──────────────────────────────────────────────────────────
def fire_reminder(chat_id, message, reminder_id=None):
    send_message(chat_id, f"Reminder: {message}")
    if reminder_id:
        mark_reminder_done(reminder_id)

def schedule_reminder(chat_id, dt_str, message):
    try:
        now = datetime.now(IST)
        run_date = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
        if run_date <= now:
            run_date = now + timedelta(minutes=1)
        job_id = f"r_{int(run_date.timestamp())}"
        scheduler.add_job(fire_reminder, "date", run_date=run_date,
                          args=[chat_id, message], id=job_id, replace_existing=True)
        save_reminder_db(chat_id, message, run_date)
        return True, run_date.strftime("%d %b at %I:%M %p")
    except Exception as e:
        return False, str(e)

# ── Google Calendar link ──────────────────────────────────────────────────────────
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

# ── Scheduled daily tasks ────────────────────────────────────────────────────────
def morning_standup(chat_id):
    send_message(chat_id,
        "Good morning! Time for your daily standup.\n\n"
        "What are your *3 priorities* for today?\n"
        "Reply with: standup: priority 1, priority 2, priority 3"
    )

def morning_learning(chat_id):
    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=300,
            messages=[{"role": "user", "content":
                "Give me one sharp, specific, actionable insight about productivity, "
                "sales, RevOps, or personal growth. Make it practical and under 100 words. "
                "Format: bold insight title, then the insight."}]
        )
        insight = response.content[0].text
        send_message(chat_id, f"Your morning insight:\n\n{insight}")
    except Exception as e:
        print(f"Morning learning error: {e}")

def evening_review(chat_id):
    send_message(chat_id,
        "Evening check-in time.\n\n"
        "1. Did you complete your priorities today?\n"
        "2. What was your win of the day?\n"
        "3. What are you *grateful* for today?\n\n"
        "Just reply naturally and I will log it."
    )

def setup_daily_schedules(chat_id):
    wake_time = mem_get("wake_time") or "07:00"
    sleep_time = mem_get("sleep_time") or "22:30"
    wake_h, wake_m = map(int, wake_time.split(":"))
    sleep_h, sleep_m = map(int, sleep_time.split(":"))

    # Morning standup
    scheduler.add_job(morning_standup, "cron",
                      hour=wake_h, minute=wake_m + 15,
                      args=[chat_id], id="daily_standup", replace_existing=True)
    # Morning insight
    scheduler.add_job(morning_learning, "cron",
                      hour=wake_h, minute=wake_m,
                      args=[chat_id], id="daily_learning", replace_existing=True)
    # Evening review
    scheduler.add_job(evening_review, "cron",
                      hour=sleep_h - 1, minute=sleep_m,
                      args=[chat_id], id="evening_review", replace_existing=True)

# ── Ask Claude ────────────────────────────────────────────────────────────────────
def ask_claude(user_message, chat_id):
    global conversation_history

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

    # ── Handle special JSON responses ────────────────────────────────────────────

    # Reminder
    m = re.search(r'REMINDER_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            ok, info = schedule_reminder(chat_id, data["datetime"], data["message"])
            reply = f"Reminder set for *{info}*\n_{data['message']}_" if ok else f"Could not set reminder: {info}"
        except Exception as e:
            reply = f"Reminder error: {e}"

    # Calendar
    m = re.search(r'CALENDAR_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            link = make_calendar_link(data["title"], data["date"], data["start_time"], data["end_time"], data.get("description", ""))
            reply = f"Tap to add to Google Calendar:\n*{data['title']}* - {data['date']} at {data['start_time']}\n\n{link}"
        except Exception as e:
            reply = f"Calendar error: {e}"

    # Memory
    m = re.search(r'MEMORY_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            mem_set(data["key"], data["value"])
            reply = f"Got it. Remembered: *{data['key']}* = {data['value']}"
        except Exception as e:
            reply = f"Memory error: {e}"

    # Forget
    m = re.search(r'FORGET_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            mem_delete(data["key"])
            reply = f"Forgotten: {data['key']}"
        except Exception as e:
            reply = f"Forget error: {e}"

    # Idea
    m = re.search(r'IDEA_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("idea", data["idea"], {"category": data.get("category", "other")})
            reply = f"Idea logged under *{data.get('category', 'other')}*:\n_{data['idea']}_"
        except Exception as e:
            reply = f"Idea log error: {e}"

    # Decision
    m = re.search(r'DECISION_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("decision", data["decision"], {"reason": data.get("reason", "")})
            reply = f"Decision logged:\n*{data['decision']}*\nReason: _{data.get('reason', '')}_"
        except Exception as e:
            reply = f"Decision log error: {e}"

    # Gratitude
    m = re.search(r'GRATITUDE_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("gratitude", data["entry"])
            reply = f"Gratitude logged: _{data['entry']}_"
        except Exception as e:
            reply = f"Gratitude error: {e}"

    # Checkin
    m = re.search(r'CHECKIN_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("checkin", f"mood:{data.get('mood',0)} energy:{data.get('energy',0)} sleep:{data.get('sleep',0)}", data)
            parts = []
            if data.get("mood"): parts.append(f"Mood: {data['mood']}/10")
            if data.get("energy"): parts.append(f"Energy: {data['energy']}/10")
            if data.get("sleep"): parts.append(f"Sleep: {data['sleep']}/10")
            reply = "Check-in logged:\n" + "\n".join(parts)
        except Exception as e:
            reply = f"Checkin error: {e}"

    # Standup
    m = re.search(r'STANDUP_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            priorities = data.get("priorities", [])
            content = " | ".join(priorities)
            log_entry("standup", content, {"priorities": priorities})
            reply = "Standup logged. Your priorities today:\n" + \
                "\n".join(f"{i+1}. {p}" for i, p in enumerate(priorities))
        except Exception as e:
            reply = f"Standup error: {e}"

    conversation_history.append({"role": "assistant", "content": reply})
    return reply

# ── Commands ──────────────────────────────────────────────────────────────────────
def handle_command(cmd, chat_id):
    global conversation_history

    if cmd == "/start":
        wake = mem_get("wake_time")
        if not wake:
            send_message(chat_id,
                "Assistant ready. Let us personalise it first.\n\n"
                "What time do you usually wake up? (e.g. 7:00)\n"
                "Reply: wake 07:00"
            )
        else:
            setup_daily_schedules(chat_id)
            send_message(chat_id, "Assistant ready. What do you need?")

    elif cmd == "/clear":
        conversation_history = []
        send_message(chat_id, "Conversation cleared.")

    elif cmd == "/memory":
        facts = mem_get_all()
        if facts:
            msg = "*What I remember:*\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items())
        else:
            msg = "Nothing stored yet."
        send_message(chat_id, msg)

    elif cmd == "/reminders":
        jobs = [j for j in scheduler.get_jobs() if j.id.startswith("r_")]
        if jobs:
            lines = [f"- {j.next_run_time.strftime('%d %b %I:%M %p')}: {j.args[1]}" for j in jobs]
            send_message(chat_id, "*Upcoming reminders:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No reminders set.")

    elif cmd == "/ideas":
        ideas = get_logs("idea", 10)
        if ideas:
            lines = [f"- [{r['metadata'].get('category','?')}] {r['content']}" for r in ideas]
            send_message(chat_id, "*Your recent ideas:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No ideas logged yet. Say 'idea: ...' to log one.")

    elif cmd == "/decisions":
        decisions = get_logs("decision", 5)
        if decisions:
            lines = [f"- {r['content']}\n  _{r['metadata'].get('reason','')}_" for r in decisions]
            send_message(chat_id, "*Recent decisions:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No decisions logged yet.")

    elif cmd == "/gratitude":
        entries = get_logs("gratitude", 7)
        if entries:
            lines = [f"- {r['content']}" for r in entries]
            send_message(chat_id, "*Recent gratitude entries:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No gratitude entries yet.")

    elif cmd == "/checkin":
        entries = get_logs("checkin", 7)
        if entries:
            lines = [f"- {r['content']}" for r in entries]
            send_message(chat_id, "*Recent check-ins:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No check-ins yet. Say 'mood: 7/10' to log one.")

    elif cmd == "/help":
        send_message(chat_id,
            "*What I can do:*\n\n"
            "*Daily Structure:*\n"
            "- Morning standup + insight (auto, daily)\n"
            "- Evening review (auto, daily)\n"
            "- Reminders: 'remind me in 10 mins to...'\n"
            "- Calendar: 'add meeting tomorrow 3pm'\n\n"
            "*Logging:*\n"
            "- Ideas: 'idea: what if I...'\n"
            "- Decisions: 'log decision: I chose X because...'\n"
            "- Gratitude: 'grateful for...'\n"
            "- Mood: 'mood: 8/10 energy: 6/10'\n"
            "- Standup: 'standup: task1, task2, task3'\n\n"
            "*Support:*\n"
            "- Say 'therapist mode' for structured thinking help\n"
            "- Say 'vent mode' to just be heard\n\n"
            "*Search:*\n"
            "- Live weather, doctors, restaurants, news, flights\n\n"
            "*Commands:*\n"
            "/memory /reminders /ideas /decisions /gratitude /checkin /clear /help"
        )

# ── Handle wake/sleep time setup ─────────────────────────────────────────────────
def handle_setup(text, chat_id):
    text_lower = text.lower().strip()
    if text_lower.startswith("wake "):
        t = text_lower.replace("wake ", "").strip()
        mem_set("wake_time", t)
        send_message(chat_id, f"Wake time set to {t}. What time do you sleep? Reply: sleep 22:30")
        return True
    if text_lower.startswith("sleep "):
        t = text_lower.replace("sleep ", "").strip()
        mem_set("sleep_time", t)
        setup_daily_schedules(chat_id)
        send_message(chat_id,
            f"Sleep time set to {t}.\n\n"
            "All set! Your daily schedule is active.\n"
            "Type /help to see everything I can do.")
        return True
    return False

# ── Main loop ─────────────────────────────────────────────────────────────────────
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

                if handle_setup(text, chat_id):
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
