# Personal Telegram Assistant (Claude-powered)

A private Telegram bot that acts as your personal assistant — built on Claude AI.

---

## Deployment on Railway (Step-by-Step)

### Step 1 — Put this code on GitHub
1. Go to github.com → New repository → Name it `telegram-assistant`
2. Upload these 3 files: `bot.py`, `requirements.txt`, `Procfile`

### Step 2 — Deploy on Railway
1. Go to railway.app → Login with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select your `telegram-assistant` repo
4. Railway will detect Python automatically

### Step 3 — Add Environment Variables (YOUR SECRETS GO HERE)
In Railway dashboard → your project → **Variables** tab, add these 3:

| Variable Name    | Value                          |
|-----------------|--------------------------------|
| TELEGRAM_TOKEN  | Token from @BotFather          |
| CLAUDE_API_KEY  | Key from console.anthropic.com |
| ALLOWED_USER_ID | Your numeric Telegram user ID  |

### Step 4 — Deploy
Click **Deploy**. Railway will install dependencies and start the bot.
Check the **Logs** tab — you should see: `✅ Bot is running...`

---

## Commands

| Command  | What it does               |
|----------|----------------------------|
| /start   | Wake up the bot            |
| /clear   | Reset conversation memory  |
| /help    | Show available commands     |

---

## How to Use

Just message it like a real assistant:

- "Find a good cardiologist near Satellite, Ahmedabad"
- "Draft a follow-up email to a client who went silent for 2 weeks"
- "Summarise this" *(then paste any text)*
- "What should I say to a recruiter at Finastra?"
- "Explain SPICED framework in 5 bullet points"

---

## Cost Estimate

- **Railway:** Free ($5 credit/month — more than enough for personal use)
- **Claude API:** ~$0.01–0.05 per day for normal personal usage
- **Total:** Essentially free for personal use
