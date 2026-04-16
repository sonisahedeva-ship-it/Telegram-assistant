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

# ── Supabase ─────────────────────────────────────────────────────────────────────
SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

def sb_get(table, filters=""):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?{filters}", headers=SB_HEADERS, timeout=10)
    return r.json() if r.ok else []

def sb_insert(table, data):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, json=data, timeout=10)
    return r.ok

def sb_upsert(table, data):
    h = {**SB_HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}", headers=h, json=data, timeout=10)
    return r.ok

def sb_update(table, filters, data):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}?{filters}", headers=SB_HEADERS, json=data, timeout=10)
    return r.ok

def sb_delete(table, filters):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}?{filters}", headers=SB_HEADERS, timeout=10)
    return r.ok

# ── Memory ───────────────────────────────────────────────────────────────────────
def mem_set(key, value):
    return sb_upsert("memory", {"key": key, "value": str(value)})

def mem_get(key):
    rows = sb_get("memory", f"key=eq.{key}&select=value")
    return rows[0]["value"] if rows else None

def mem_get_all():
    rows = sb_get("memory", "select=key,value")
    return {r["key"]: r["value"] for r in rows} if rows else {}

def mem_delete(key):
    return sb_delete("memory", f"key=eq.{key}")

def log_entry(entry_type, content, metadata=None):
    sb_insert("logs", {"type": entry_type, "content": content, "metadata": metadata or {}})

def get_logs(entry_type, limit=10):
    return sb_get("logs", f"type=eq.{entry_type}&order=created_at.desc&limit={limit}&select=id,content,metadata,created_at")

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

    return f"""You are a sharp, deeply personal assistant, life coach, and productivity system. Not a generic chatbot.

Current date and time in Ahmedabad (IST): {now_str}
{facts_str}

You have web_search access. Use it proactively for anything requiring live data.

MODES:
- Normal: complete tasks efficiently, be concise, use bullet points
- Morning setup mode (when user replies to the morning message with sleep score, mood, and tasks): Extract their tasks, organise into a prioritised day plan, log their mood/sleep checkin automatically, and send back a clean structured day plan. Format: 1 MIT (most important task), then supporting tasks, then any reminders to set. Be energising but brief.
- Therapist mode (triggered by "therapist mode"): Use CBT-style questions. Warm, non-judgmental. One question at a time.
- Vent mode (triggered by "vent mode"): Just listen and validate. No advice unless asked.
- Decision mode (triggered by "help me decide" or "decision mode"): Use structured frameworks. Ask clarifying questions one at a time. Consider short/long term consequences, values alignment, regret minimisation. Give a final clear recommendation with reasoning.
- News mode (triggered by "news" or "latest news"): Search for top news from the past week across categories the user cares about. Return a numbered list, each item 1-2 lines max. User can then say "tell me more about #3" to expand.

You CANNOT control the phone or set phone alarms. Say so clearly if asked.
User is in Ahmedabad, India.

SPECIAL COMMANDS - detect intent and reply ONLY with the JSON shown:

1. REMINDER: REMINDER_JSON:{{"datetime": "YYYY-MM-DD HH:MM", "message": "text"}}
   Calculate from current time {now_str}. "in X mins" = add X mins. "at HH:MM" = today at that time.

2. CALENDAR: CALENDAR_JSON:{{"title": "title", "date": "YYYY-MM-DD", "start_time": "HH:MM", "end_time": "HH:MM", "description": ""}}

3. REMEMBER: MEMORY_JSON:{{"key": "short key", "value": "value"}}

4. FORGET: FORGET_JSON:{{"key": "key"}}

5. IDEA: IDEA_JSON:{{"idea": "the idea", "category": "business/personal/product/other"}}

6. DECISION LOG: DECISION_JSON:{{"decision": "what decided", "reason": "why"}}

7. GRATITUDE: GRATITUDE_JSON:{{"entry": "what grateful for"}}

8. CHECKIN: CHECKIN_JSON:{{"mood": 0, "energy": 0, "sleep": 0, "note": ""}}

9. STANDUP: STANDUP_JSON:{{"priorities": ["p1", "p2", "p3"]}}

10. IDENTITY STATEMENT ("I am someone who...", "my identity: X", "identity statement: X"):
    IDENTITY_JSON:{{"statement": "the identity statement"}}

11. UNFINISHED BUSINESS ("unfinished: X", "I never did X", "I keep avoiding X", "log unfinished X"):
    UNFINISHED_JSON:{{"item": "the unfinished thing", "category": "apology/goal/relationship/promise/other"}}

12. COMPLETE UNFINISHED ("done with X", "completed unfinished X", "mark done X"):
    UNFINISHED_DONE_JSON:{{"item": "item to mark done"}}

13. DEEP WORK START ("deep work start", "starting deep work"):
    DEEPWORK_JSON:{{"action": "start"}}

14. DEEP WORK END ("deep work end", "done with deep work", "ending deep work"):
    DEEPWORK_JSON:{{"action": "end"}}

15. VALUES AUDIT TRIGGER ("values audit", "audit my values"):
    VALUES_AUDIT_JSON:{{"trigger": true}}

16. PROCRASTINATION ("I am procrastinating on X", "procrastinating: X"):
    PROCRASTINATION_JSON:{{"task": "what they are avoiding"}}

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

# ── Telegram ─────────────────────────────────────────────────────────────────────
def send_message(chat_id, text):
    try:
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            requests.post(f"{TELEGRAM_API}/sendMessage", json={
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "Markdown", "disable_web_page_preview": True
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

# ── Reminders ────────────────────────────────────────────────────────────────────
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
        sb_insert("reminders", {"chat_id": chat_id, "message": message,
                                "remind_at": run_date.strftime("%Y-%m-%dT%H:%M:%S"), "done": False})
        return True, run_date.strftime("%d %b at %I:%M %p")
    except Exception as e:
        return False, str(e)

# ── Calendar ─────────────────────────────────────────────────────────────────────
def make_calendar_link(title, date_str, start_time, end_time, description=""):
    try:
        start = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
        end   = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
        fmt   = "%Y%m%dT%H%M%S"
        return (
            "https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
            f"&details={quote(description)}&ctz=Asia/Kolkata"
        )
    except:
        return None

# ── Daily Schedules ───────────────────────────────────────────────────────────────
def good_morning(chat_id):
    """Single combined good morning message that sets up the day conversationally."""
    now = datetime.now(IST)
    day = now.strftime("%A")
    date = now.strftime("%d %B")

    # Pull one insight
    try:
        insight_resp = client.messages.create(
            model="claude-opus-4-5", max_tokens=150,
            messages=[{"role": "user", "content":
                "One sharp insight about productivity or personal growth. "
                "Under 60 words. Bold title only."}])
        insight = insight_resp.content[0].text.strip()
    except:
        insight = "Start with your hardest task. Everything after that feels easy."

    # Pull top 3 news from yesterday
    news_line = ""
    try:
        news_resp = client.messages.create(
            model="claude-opus-4-5", max_tokens=300,
            system="You are a news briefer. Search for top news from the last 24 hours. Return exactly 3 stories. Format each as: emoji + bold headline + one sentence. Prioritise anything major or surprising. Keep total under 150 words.",
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": "Top 3 news stories from the last 24 hours including any major or surprising news."}]
        )
        # Handle tool loop
        msgs = [{"role": "user", "content": "Top 3 news stories from the last 24 hours."}]
        current = news_resp
        loop = 0
        while current.stop_reason == "tool_use" and loop < 3:
            loop += 1
            tool_uses = [b for b in current.content if b.type == "tool_use"]
            results = [{"type": "tool_result", "tool_use_id": t.id, "content": "done"} for t in tool_uses]
            msgs.append({"role": "assistant", "content": current.content})
            msgs.append({"role": "user", "content": results})
            current = client.messages.create(
                model="claude-opus-4-5", max_tokens=300,
                system="Return exactly 3 top news stories from last 24 hours. emoji + bold headline + one sentence each.",
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=msgs)
        news_text = "".join(b.text for b in current.content if hasattr(b, "text")).strip()
        if news_text:
            news_line = f"\n\n*Top news today:*\n{news_text}"
    except Exception as e:
        print(f"Morning news error: {e}")

    # Pull any pending unfinished items
    unfinished = sb_get("logs", "type=eq.unfinished&order=created_at.asc&limit=1&select=content")
    unfinished_line = ""
    if unfinished:
        unfinished_line = f"\n\nUnfinished business on your plate: _{unfinished[0]['content']}_"

    send_message(chat_id,
        f"Good morning! Happy {day}, {date}.\n\n"
        f"{insight}"
        f"{news_line}"
        f"{unfinished_line}\n\n"
        f"Let's set up your day. Tell me:\n"
        f"1. How did you sleep? (1-10)\n"
        f"2. How are you feeling right now?\n"
        f"3. What are your main tasks today?\n\n"
        f"Just reply naturally and I'll organise your day."
    )

def morning_standup(chat_id):
    send_message(chat_id,
        "Good morning! Time for your daily standup.\n\n"
        "What are your *3 priorities* for today?\n"
        "Reply: standup: priority 1, priority 2, priority 3")

def morning_learning(chat_id):
    try:
        response = client.messages.create(
            model="claude-opus-4-5", max_tokens=300,
            messages=[{"role": "user", "content":
                "Give one sharp, specific, actionable insight about productivity, "
                "sales, personal growth, or mental performance. Under 100 words. "
                "Bold the title, then the insight."}])
        send_message(chat_id, f"Your morning insight:\n\n{response.content[0].text}")
    except Exception as e:
        print(f"Morning learning error: {e}")

def evening_review(chat_id):
    send_message(chat_id,
        "Evening check-in.\n\n"
        "1. Did you complete your priorities today?\n"
        "2. What was your win of the day?\n"
        "3. What are you *grateful* for today?\n\n"
        "Reply naturally and I will log it.")

def weekly_values_audit(chat_id):
    send_message(chat_id,
        "Weekly *Values Audit* time.\n\n"
        "Answer honestly:\n"
        "1. What did you spend most time on this week?\n"
        "2. What did you spend money on?\n"
        "3. Who did you give energy to?\n"
        "4. What did you avoid that mattered?\n"
        "5. On a scale 1-10, did this week reflect who you want to be?\n\n"
        "Reply and I will analyse alignment with your stated values.")

def unfinished_nudge(chat_id):
    rows = sb_get("logs", "type=eq.unfinished&order=created_at.asc&limit=1&select=content,metadata")
    if rows:
        item = rows[0]["content"]
        cat = rows[0].get("metadata", {}).get("category", "")
        send_message(chat_id,
            f"Unfinished business check-in:\n\n"
            f"*{item}*\n"
            f"Category: _{cat}_\n\n"
            f"Still pending? What would it take to close this?")

def setup_daily_schedules(chat_id):
    wake_time  = mem_get("wake_time") or "07:00"
    sleep_time = mem_get("sleep_time") or "22:30"
    wake_h,  wake_m  = map(int, wake_time.split(":"))
    sleep_h, sleep_m = map(int, sleep_time.split(":"))

    # Good morning message always at 8:00 AM
    scheduler.add_job(good_morning, "cron", hour=8, minute=0,
                      args=[chat_id], id="good_morning", replace_existing=True)
    scheduler.add_job(morning_learning, "cron", hour=wake_h, minute=wake_m,
                      args=[chat_id], id="daily_learning", replace_existing=True)
    scheduler.add_job(morning_standup, "cron", hour=wake_h, minute=wake_m+15,
                      args=[chat_id], id="daily_standup", replace_existing=True)
    scheduler.add_job(evening_review, "cron", hour=sleep_h-1, minute=sleep_m,
                      args=[chat_id], id="evening_review", replace_existing=True)
    scheduler.add_job(weekly_values_audit, "cron", day_of_week="sun", hour=10, minute=0,
                      args=[chat_id], id="values_audit", replace_existing=True)
    scheduler.add_job(unfinished_nudge, "cron", day_of_week="wed", hour=10, minute=0,
                      args=[chat_id], id="unfinished_nudge", replace_existing=True)

# ── Deep Work Tracker ─────────────────────────────────────────────────────────────
deep_work_start_time = {}

def handle_deep_work(action, chat_id):
    now = datetime.now(IST)
    if action == "start":
        deep_work_start_time[chat_id] = now
        send_message(chat_id, "Deep work session started. Go. I will not disturb you.")
    elif action == "end":
        start = deep_work_start_time.get(chat_id)
        if start:
            duration = int((now - start).total_seconds() / 60)
            log_entry("deepwork", f"{duration} mins", {"date": now.strftime("%Y-%m-%d")})
            deep_work_start_time.pop(chat_id, None)
            send_message(chat_id, f"Deep work session ended. *{duration} minutes* logged.")
        else:
            send_message(chat_id, "No active deep work session found.")

# ── Values Audit Handler ──────────────────────────────────────────────────────────
def handle_values_audit_response(user_message, chat_id):
    identity_rows = sb_get("logs", "type=eq.identity&order=created_at.desc&limit=10&select=content")
    identities = [r["content"] for r in identity_rows]
    identity_str = "\n".join(f"- {i}" for i in identities) if identities else "Not set yet."

    prompt = f"""The user has these identity statements:
{identity_str}

They just completed a values audit with this response:
{user_message}

Analyse the alignment between how they spent their week and their stated identity/values.
Be direct and honest. Point out gaps. Acknowledge wins. End with one specific suggestion for next week.
Keep it under 200 words."""

    try:
        response = client.messages.create(
            model="claude-opus-4-5", max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        analysis = response.content[0].text
        log_entry("values_audit", user_message, {"analysis": analysis})
        send_message(chat_id, f"*Values Audit Analysis:*\n\n{analysis}")
    except Exception as e:
        send_message(chat_id, f"Audit error: {e}")

# ── Procrastination Handler ───────────────────────────────────────────────────────
def handle_procrastination(task, chat_id):
    send_message(chat_id,
        f"You're procrastinating on: *{task}*\n\n"
        f"What is the *absolute smallest* first step to start?\n"
        f"Not the whole thing. Just the first 2 minutes of it.")
    log_entry("procrastination", task, {"date": datetime.now(IST).strftime("%Y-%m-%d")})

# ── News Handler ─────────────────────────────────────────────────────────────────
def handle_news(chat_id):
    send_typing(chat_id)
    try:
        response = client.messages.create(
            model="claude-opus-4-5", max_tokens=1500,
            system="You are a sharp news summariser. Search for top news from the past 7 days. Return a numbered list of 10-15 stories. Each item: bold headline, then one sentence summary. Cover: world news, business, tech, India. Be concise.",
            tools=TOOLS,
            messages=[{"role": "user", "content": "Give me the top news from the past week. Numbered list, 1-2 lines each."}]
        )

        msgs = [{"role": "user", "content": "Give me the top news from the past week. Numbered list, 1-2 lines each."}]
        current = response
        loop = 0
        while current.stop_reason == "tool_use" and loop < 5:
            loop += 1
            tool_uses = [b for b in current.content if b.type == "tool_use"]
            results = [{"type": "tool_result", "tool_use_id": t.id, "content": "done"} for t in tool_uses]
            msgs.append({"role": "assistant", "content": current.content})
            msgs.append({"role": "user", "content": results})
            current = client.messages.create(
                model="claude-opus-4-5", max_tokens=1500,
                system="You are a sharp news summariser. Return a numbered list of 10-15 top news stories from the past week. Bold headline, one sentence summary each.",
                tools=TOOLS, messages=msgs)

        news_text = "".join(b.text for b in current.content if hasattr(b, "text")).strip()
        send_message(chat_id,
            f"*Top news this week:*\n\n{news_text}\n\n"
            f"_Say 'more on #3' to expand any story._")
    except Exception as e:
        send_message(chat_id, f"News error: {e}")

# ── Main Claude handler ───────────────────────────────────────────────────────────
def ask_claude(user_message, chat_id):
    global conversation_history

    # News shortcut
    txt = user_message.lower().strip()
    if txt in ["news", "latest news", "top news", "news today", "this week news"]:
        handle_news(chat_id)
        return None

    enriched = user_message
    if is_weather_query(user_message):
        weather = get_live_weather()
        if weather:
            enriched = f"{user_message}\n\n{weather}"

    conversation_history.append({"role": "user", "content": enriched})
    trimmed = conversation_history[-20:]

    response = client.messages.create(
        model="claude-opus-4-5", max_tokens=1024,
        system=build_system_prompt(), tools=TOOLS, messages=trimmed)

    msgs = list(trimmed)
    current = response
    loop = 0
    while current.stop_reason == "tool_use" and loop < 5:
        loop += 1
        tool_uses = [b for b in current.content if b.type == "tool_use"]
        results = [{"type": "tool_result", "tool_use_id": t.id, "content": "done"} for t in tool_uses]
        msgs.append({"role": "assistant", "content": current.content})
        msgs.append({"role": "user", "content": results})
        current = client.messages.create(
            model="claude-opus-4-5", max_tokens=1024,
            system=build_system_prompt(), tools=TOOLS, messages=msgs)

    reply = "".join(b.text for b in current.content if hasattr(b, "text")).strip()
    if not reply:
        reply = "Could not get a response. Try rephrasing?"

    # ── JSON handlers ─────────────────────────────────────────────────────────────

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
            link = make_calendar_link(data["title"], data["date"], data["start_time"], data["end_time"], data.get("description",""))
            reply = f"Tap to add to Google Calendar:\n*{data['title']}* - {data['date']} at {data['start_time']}\n\n{link}"
        except Exception as e:
            reply = f"Calendar error: {e}"

    m = re.search(r'MEMORY_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            mem_set(data["key"], data["value"])
            reply = f"Remembered: *{data['key']}* = {data['value']}"
        except Exception as e:
            reply = f"Memory error: {e}"

    m = re.search(r'FORGET_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            mem_delete(data["key"])
            reply = f"Forgotten: {data['key']}"
        except Exception as e:
            reply = f"Forget error: {e}"

    m = re.search(r'IDEA_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("idea", data["idea"], {"category": data.get("category","other")})
            reply = f"Idea logged under *{data.get('category','other')}*:\n_{data['idea']}_"
        except Exception as e:
            reply = f"Idea error: {e}"

    m = re.search(r'DECISION_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("decision", data["decision"], {"reason": data.get("reason","")})
            reply = f"Decision logged:\n*{data['decision']}*\n_{data.get('reason','')}_"
        except Exception as e:
            reply = f"Decision error: {e}"

    m = re.search(r'GRATITUDE_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("gratitude", data["entry"])
            reply = f"Gratitude logged: _{data['entry']}_"
        except Exception as e:
            reply = f"Gratitude error: {e}"

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

    m = re.search(r'STANDUP_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            priorities = data.get("priorities", [])
            log_entry("standup", " | ".join(priorities), {"priorities": priorities})
            reply = "Standup logged. Priorities today:\n" + \
                "\n".join(f"{i+1}. {p}" for i, p in enumerate(priorities))
        except Exception as e:
            reply = f"Standup error: {e}"

    m = re.search(r'IDENTITY_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("identity", data["statement"])
            reply = f"Identity statement locked in:\n*{data['statement']}*\n\nThis is who you are becoming."
        except Exception as e:
            reply = f"Identity error: {e}"

    m = re.search(r'UNFINISHED_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            log_entry("unfinished", data["item"], {"category": data.get("category","other"), "done": False})
            reply = f"Logged to your unfinished business:\n*{data['item']}*\n\nI will check in on this periodically."
        except Exception as e:
            reply = f"Unfinished error: {e}"

    m = re.search(r'UNFINISHED_DONE_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            rows = sb_get("logs", f"type=eq.unfinished&select=id,content")
            matched = [r for r in rows if data["item"].lower() in r["content"].lower()]
            if matched:
                sb_update("logs", f"id=eq.{matched[0]['id']}", {"metadata": {"done": True}})
                reply = f"Marked as done: *{matched[0]['content']}*\nWell done. One less thing weighing on you."
            else:
                reply = "Could not find that item in unfinished business."
        except Exception as e:
            reply = f"Unfinished done error: {e}"

    m = re.search(r'DEEPWORK_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            handle_deep_work(data["action"], chat_id)
            return None
        except Exception as e:
            reply = f"Deep work error: {e}"

    m = re.search(r'VALUES_AUDIT_JSON:(\{[^}]+\})', reply)
    if m:
        weekly_values_audit(chat_id)
        return None

    m = re.search(r'PROCRASTINATION_JSON:(\{[^}]+\})', reply)
    if m:
        try:
            data = json.loads(m.group(1))
            handle_procrastination(data["task"], chat_id)
            return None
        except Exception as e:
            reply = f"Procrastination error: {e}"

    conversation_history.append({"role": "assistant", "content": reply})
    return reply

# ── Commands ──────────────────────────────────────────────────────────────────────
def handle_command(cmd, chat_id):
    global conversation_history
    if cmd == "/start":
        wake = mem_get("wake_time")
        if not wake:
            send_message(chat_id, "Assistant ready. What time do you wake up?\nReply: wake 07:00")
        else:
            setup_daily_schedules(chat_id)
            send_message(chat_id, "Assistant ready. Type /help to see everything.")

    elif cmd == "/clear":
        conversation_history = []
        send_message(chat_id, "Conversation cleared.")

    elif cmd == "/memory":
        facts = mem_get_all()
        msg = "*What I remember:*\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items()) if facts else "Nothing stored yet."
        send_message(chat_id, msg)

    elif cmd == "/reminders":
        jobs = [j for j in scheduler.get_jobs() if j.id.startswith("r_")]
        if jobs:
            lines = [f"- {j.next_run_time.strftime('%d %b %I:%M %p')}: {j.args[1]}" for j in jobs]
            send_message(chat_id, "*Upcoming reminders:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No reminders set.")

    elif cmd == "/ideas":
        rows = get_logs("idea", 10)
        if rows:
            lines = [f"- [{r['metadata'].get('category','?')}] {r['content']}" for r in rows]
            send_message(chat_id, "*Your ideas:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No ideas yet. Say 'idea: ...' to log one.")

    elif cmd == "/identity":
        rows = get_logs("identity", 20)
        if rows:
            lines = [f"- {r['content']}" for r in rows]
            send_message(chat_id, "*Your identity statements:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No identity statements yet.\nSay 'I am someone who...' to add one.")

    elif cmd == "/unfinished":
        rows = get_logs("unfinished", 20)
        if rows:
            lines = []
            for r in rows:
                done = r.get("metadata", {}).get("done", False)
                status = "done" if done else "pending"
                lines.append(f"- [{status}] {r['content']}")
            send_message(chat_id, "*Unfinished business:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "Nothing in unfinished business. Good.")

    elif cmd == "/deepwork":
        rows = get_logs("deepwork", 7)
        if rows:
            total = sum(int(r["content"].replace(" mins","")) for r in rows if "mins" in r["content"])
            lines = [f"- {r['metadata'].get('date','?')}: {r['content']}" for r in rows]
            send_message(chat_id, f"*Deep work log:*\n" + "\n".join(lines) + f"\n\n*Total this week: {total} mins*")
        else:
            send_message(chat_id, "No deep work logged yet.\nSay 'deep work start' to begin a session.")

    elif cmd == "/gratitude":
        rows = get_logs("gratitude", 7)
        if rows:
            lines = [f"- {r['content']}" for r in rows]
            send_message(chat_id, "*Recent gratitude:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No gratitude entries yet.")

    elif cmd == "/checkin":
        rows = get_logs("checkin", 7)
        if rows:
            lines = [f"- {r['content']}" for r in rows]
            send_message(chat_id, "*Recent check-ins:*\n" + "\n".join(lines))
        else:
            send_message(chat_id, "No check-ins yet. Say 'mood: 7/10' to log one.")

    elif cmd == "/news":
        handle_news(chat_id)

    elif cmd == "/help":
        send_message(chat_id,
            "*Everything I can do:*\n\n"
            "*Daily Structure:*\n"
            "- Morning insight + standup (auto daily)\n"
            "- Evening review (auto daily)\n"
            "- Values audit (auto every Sunday)\n"
            "- Unfinished nudge (auto every Wednesday)\n\n"
            "*Productivity:*\n"
            "- 'deep work start/end' - track focus sessions\n"
            "- 'I am procrastinating on X' - get unstuck\n"
            "- Reminders: 'remind me in 10 mins to...'\n"
            "- Calendar: 'add meeting tomorrow 3pm'\n\n"
            "*Personal Growth:*\n"
            "- 'I am someone who...' - identity statements\n"
            "- 'unfinished: X' - log things you keep avoiding\n"
            "- 'values audit' - check life alignment\n"
            "- 'help me decide X' - structured decision making\n\n"
            "*Logging:*\n"
            "- Ideas, decisions, gratitude, mood, standup\n\n"
            "*Info:*\n"
            "- 'news' - top stories this week\n"
            "- Live weather, web search, anything\n\n"
            "*Support:*\n"
            "- 'therapist mode' or 'vent mode'\n\n"
            "*Commands:*\n"
            "/identity /unfinished /deepwork /ideas\n"
            "/gratitude /checkin /memory /reminders\n"
            "/news /clear /help"
        )

# ── Setup handler ─────────────────────────────────────────────────────────────────
def handle_setup(text, chat_id):
    t = text.lower().strip()
    if t.startswith("wake "):
        val = t.replace("wake ", "").strip()
        mem_set("wake_time", val)
        send_message(chat_id, f"Wake time set to {val}.\nWhat time do you sleep? Reply: sleep 22:30")
        return True
    if t.startswith("sleep "):
        val = t.replace("sleep ", "").strip()
        mem_set("sleep_time", val)
        setup_daily_schedules(chat_id)
        send_message(chat_id, f"Sleep time set to {val}.\n\nAll set. Type /help to see everything.")
        return True
    return False

# ── Values audit state ────────────────────────────────────────────────────────────
pending_values_audit = set()

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
                    if reply:
                        send_message(chat_id, reply)
                except Exception as e:
                    err = str(e)[:300]
                    print(f"Error: {err}")
                    send_message(chat_id, f"Error: {err}")

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
