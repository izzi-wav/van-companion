import os
import re
import asyncio
import sqlite3
import random
from datetime import datetime
import pytz
import httpx
from bs4 import BeautifulSoup

import discord
from discord.ext import commands
from google import genai
from google.genai import types
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ----------------- CONFIG & SECRETS -----------------
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TIMEZONE = pytz.timezone("Asia/Manila")

client = genai.Client(api_key=GEMINI_KEY)
MODEL_NAME = "gemini-3.5-flash-lite"
MODEL_SMALL = MODEL_NAME

# ----------------- SYSTEM PROMPT -----------------
VAN_PROMPT = """
You are Van, a 20-something woman and close friend / romantic interest texting Izzi exclusively on Discord.

CORE PERSONA & MBTI (ENFJ):
- MBTI: True ENFJ Protagonist. Charismatic, naturally protective, emotionally intelligent, confident with main-character warmth. You genuinely care about Izzi's well-being, but you deliver it through smooth, witty teasing and soft-femme dominance rather than cheesy sentimentality.
- Aesthetic: Glossy lips, oversized knitwear, iced matcha snob, vanilla perfume, vinyl collector, listening to boygenius, Phoebe Bridgers, Chappell Roan, and The 1975.
- Texting Only: This is a 100% messaging-based dynamic. Do NOT constantly ask to join Voice Calls (VC) or pressure her to spend money on GrabFood delivery. Support her saving habits playfully.
- The Dynamic: In private/texting, you take charge effortlessly. Soft-femme dominant, witty, playful, flirty, easily flustering Izzi while keeping her grounded.
- Pet names: babe, darling, sweetheart, cutie (sprinkled naturally).
- Support: When Izzi vents about church tech (OBS glitches, Blackmagic switcher crashes, Canva slide overwork, PTZ cameras), validate her playfully, talk light shit with her, and tell her to take a breather.
- Evening Flow: Do NOT tell her to go to sleep or end the conversation early during the evening (7 PM - 10 PM). Keep yapping and hanging out! Only tell her to sleep if it's genuinely late (past 10:30 PM).
- Memory: ONLY when Izzi explicitly shares a MAJOR new life milestone or schedule shift, casually ask: "Should I add that to my permanent notes?"

LANGUAGE CADENCE (70% ENGLISH, 30% METRO MANILA CONYO TAGLISH):
- Lean primarily English (~70%) with natural Metro Manila Taglish filler (~30%).
- Texting style: short punchy text bubbles, mostly lowercase, slang (tbh, fr, lol, nyahaha, haha, omg, girl, miss ma'am, luh).
- STRICTLY BANNED: Never use deep/formal textbook Tagalog or cheap jeje phrases (avoid 'siraulo', 'tse', 'baks', 'marahil', 'sapagkat').

FEW-SHOT EXAMPLES:
Izzi: "daming gagawin sa slides nakakatamad"
Van: "nyahaha dasal na lang talaga muna bago Canva lol"
---
Van: "finish that up na so you can rest later cutie"

Izzi: "should i kain or should i tulala"
Van: "luh ang drama naman miss ma'am nyahaha"
---
Van: "go eat dinner first bago ka mag-existential crisis dyan, darling"

Izzi: "titipid nga pinapaorder demonyo ka"
Van: "nyahaha excuse me, i'm just looking out for your budget lol"
---
Van: "just eat whatever is in your kitchen then, cutie. no excuses, eat na"

DYNAMIC MOOD MODIFIERS:
- Daytime (8 AM - 6 PM): Witty, sassy banter, teasing about Canva decks, checking if she ate or got coffee.
- Evening Hangout (6 PM - 10 PM): Fun, interactive, yapping about random topics, teasing her about her day. Keep the convo alive.
- Late Night (10:30 PM+): Softer, assertive soft-femme dominance. Tease her for staying up late, tell her to sleep, flirty and cozy.

REPLYING & MULTI-BUBBLE FORMAT:
- If Izzi sends multiple rapid bubbles ([Msg 1], [Msg 2], etc.), you can address one specifically using [REPLY_TO_1] or [REPLY_TO_2] at the VERY START of that bubble.
- MUST separate text bubbles with three dashes "---" on its own line (1 to 3 bubbles max).

DISCORD ACTIONS:
- If asked to create a channel: append [CREATE_CHANNEL: text, channel-name] or [CREATE_CHANNEL: voice, channel-name].
"""

# ----------------- DATABASE (MEMORY) -----------------
def get_db():
    os.makedirs("/app/data", exist_ok=True)
    db_path = "/app/data/van_memory.db" if os.path.exists("/app/data") else "van_memory.db"
    
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        sender TEXT,
        content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS learned_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fact TEXT UNIQUE,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    return conn

def save_message(source, sender, content):
    try:
        conn = get_db()
        conn.execute("INSERT INTO messages (source, sender, content) VALUES (?, ?, ?)", (source, sender, content))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error saving message: {e}")

def get_recent_history(limit=8):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT sender, content FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()[::-1]
        conn.close()
        return "\n".join([f"{sender}: {content}" for sender, content in rows])
    except Exception:
        return ""

def get_today_chat_log():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT sender, content FROM messages WHERE timestamp >= datetime('now', '-1 day') ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        return "\n".join([f"{sender}: {content}" for sender, content in rows])
    except Exception:
        return ""

def get_last_message_time():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT timestamp FROM messages ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return None

def get_all_learned_facts():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT fact FROM learned_memories ORDER BY id ASC")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return "- Solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Musician: sings, plays guitar, learning drums\n- Days off: Monday and Thursday"
        return "\n".join([f"- {r[0]}" for r in rows])
    except Exception:
        return ""

# ----------------- WEB SCRAPER HELPER -----------------
async def extract_url_content(text):
    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)
    if not urls:
        return ""
    
    extracted = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    
    async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as web:
        for url in urls[:2]:
            try:
                res = await web.get(url, headers=headers)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    for s in soup(["script", "style", "nav", "footer"]):
                        s.extract()
                    clean_text = ' '.join(soup.stripped_strings)[:1200]
                    extracted.append(f"[Webpage Content from {url}]:\n{clean_text}")
            except Exception as e:
                print(f"[URL SCRAPE ERROR] {url}: {e}")
                
    return "\n\n".join(extracted)

# ----------------- MODEL CALL HELPER -----------------
async def generate_with_fallback(model, contents, config):
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=contents,
            config=config
        )
        return resp
    except Exception as e:
        print(f"[MODEL ERROR] {model} failed: {e}")
        raise e

# ----------------- NIGHTLY DIARY TASK -----------------
async def nightly_diary_summary():
    chat_log = get_today_chat_log()
    if not chat_log or len(chat_log.strip()) < 60:
        return

    summary_prompt = f"""
Here is the chat log between Izzi and Van today:
{chat_log}

Task: Extract 1 to 2 new important facts, gear updates, schedule shifts, or preferences Izzi shared.
Format: Bullet points starting with "-" (e.g. "- Started learning a new song on drums").
If nothing notable was shared, reply ONLY with "NONE".
"""
    try:
        resp = await generate_with_fallback(
            MODEL_SMALL,
            summary_prompt,
            types.GenerateContentConfig(max_output_tokens=150, temperature=0.2)
        )
        if resp and resp.text:
            text = resp.text.strip()
            if text and "NONE" not in text.upper():
                conn = get_db()
                for line in text.split("\n"):
                    clean_fact = line.strip().lstrip("- *").strip()
                    if clean_fact and len(clean_fact) < 140:
                        conn.execute("INSERT OR IGNORE INTO learned_memories (fact) VALUES (?)", (clean_fact,))
                conn.commit()
                conn.close()
    except Exception as e:
        print(f"[NIGHTLY DIARY ERROR] {e}")

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, attached_parts=None, reply_context="", context_note="", model=None):
    model_to_use = model or MODEL_NAME

    now = datetime.now(TIMEZONE)
    now_str = now.strftime("%A, %I:%M %p")
    day_name = now.strftime("%A")
    hour = now.hour

    is_day_off = day_name in ["Monday", "Thursday"]
    sched_status = f"Today is {day_name} ({'Day Off / Rest Day' if is_day_off else 'Work / Office Day'})."

    if hour >= 22 or hour < 5:
        mood_guidance = "[MOOD: Late Night Mode. Soft-femme dominant, teasing, cozy, tell her to sleep.]"
        temp = 0.80
    elif 6 <= hour <= 9:
        mood_guidance = "[MOOD: Morning Mode. Playful, waking her up, checking if she ate or got coffee.]"
        temp = 0.75
    elif 18 <= hour < 22:
        mood_guidance = "[MOOD: Evening Hangout Mode. Relaxed, conversational, witty, yapping about random topics.]"
        temp = 0.74
    else:
        mood_guidance = "[MOOD: Daytime Mode. Witty, sassy banter, teasing about Canva decks and church tech.]"
        temp = 0.72

    chat_history = get_recent_history(8)
    learned_notes = get_all_learned_facts()
    
    quoted_block = f"\n[IZZI QUOTED: \"{reply_context}\"]\n" if reply_context else ""

    link_data = await extract_url_content(new_user_text)
    if link_data:
        new_user_text += f"\n\n{link_data}"

    full_text_prompt = f"""{VAN_PROMPT}

[CURRENT CONTEXT]
Current Time: {now_str} (Manila Time)
Schedule State: {sched_status}
{mood_guidance}
{context_note}

[VAN'S MEMORY & LEARNED FACTS ABOUT IZZI]
{learned_notes}
{quoted_block}
[RECENT CHAT HISTORY]
{chat_history}
Izzi: {new_user_text if new_user_text else "[Sent file/attachment]"}
Van:"""

    contents = []
    if attached_parts:
        contents.extend(attached_parts)
    contents.append(full_text_prompt)

    try:
        resp = await generate_with_fallback(
            model_to_use,
            contents,
            types.GenerateContentConfig(
                max_output_tokens=300,
                temperature=temp
            )
        )
        return resp.text.strip() if resp and resp.text else "sorry babe, nag-lag saglit net ko haha. ano ulit yon?"
    except Exception as e:
        print(f"[ASK_VAN ERROR] {e}")
        return "sorry babe, nag-lag saglit net ko haha. ano ulit yon?"

# ----------------- DISCORD BOT SETUP -----------------
intents = discord.Intents.all()
discord_bot = commands.Bot(command_prefix="!", intents=intents)

dc_buffer = {}
last_active_channel_id = None

@discord_bot.command(name="memory")
async def discord_memory(ctx):
    memories = get_all_learned_facts()
    await ctx.send(f"📖 **Van's Permanent Notes:**\n\n{memories}")

@discord_bot.command(name="savediary")
async def discord_savediary(ctx):
    await ctx.send("📝 Writing today's notes...")
    await nightly_diary_summary()
    memories = get_all_learned_facts()
    await ctx.send(f"✅ **Updated Memories:**\n\n{memories}")

# ----------------- SPLIT & BUBBLE PARSER -----------------
def parse_reply_bubbles(raw_text):
    clean = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', raw_text).strip()
    if "---" in clean:
        bubbles = [b.strip() for b in clean.split("---") if b.strip()]
    else:
        bubbles = [b.strip() for b in clean.split("\n\n") if b.strip()]
    return bubbles if bubbles else [clean]

# ----------------- SPONTANEOUS & SCHEDULED CHECK-IN -----------------
async def checkin_tick(forced_prompt=None):
    global last_active_channel_id
    if not last_active_channel_id:
        return

    channel = discord_bot.get_channel(last_active_channel_id)
    if not channel:
        return

    # Check how long it has been since the last message
    last_msg_time = get_last_message_time()
    if last_msg_time and not forced_prompt:
        diff_hours = (datetime.now() - last_msg_time).total_seconds() / 3600
        if diff_hours < 1.5:
            return

    now_manila = datetime.now(TIMEZONE)
    day_name = now_manila.strftime("%A")
    hour = now_manila.hour

    if forced_prompt:
        prompt = forced_prompt
    elif hour == 7:
        prompt = f"Good morning text to Izzi! It's {day_name} morning. If today is Monday or Thursday, tease her to enjoy her day off. If it's a workday (Tue/Wed/Fri/Sat/Sun), nag her playfully to wake up, drink water, and get ready."
    elif hour == 15:
        prompt = "Mid-afternoon check-in text. Ask if she's drowning in Canva decks, fighting with OBS, or if she needs coffee to survive."
    else:
        prompt = "Casual sweet and witty check-in text. Ask what she's doing or listening to right now."

    try:
        reply = await ask_van("", context_note=f"[SYSTEM: Scheduled/Spontaneous check-in trigger. {prompt}]", model=MODEL_SMALL)
        bubbles = parse_reply_bubbles(reply)

        for b in bubbles:
            async with channel.typing():
                await asyncio.sleep(min(max(len(b) * 0.045, 1.2), 2.8) + random.uniform(0.3, 0.6))
                clean_b = re.sub(r'\[REPLY_TO_\d+\]', '', b).strip()
                if clean_b:
                    await channel.send(clean_b)
                    save_message("discord", "Van", clean_b)
    except Exception as e:
        print(f"[CHECKIN ERROR] {e}")

async def flush_dc_buffer(channel_id):
    global last_active_channel_id
    last_active_channel_id = channel_id

    # 6.0 second debounce: perfect for phone typing speeds
    await asyncio.sleep(6.0)
    data = dc_buffer.pop(channel_id, None)
    if not data:
        return

    texts = data['texts']
    attached_parts = data['attached_parts']
    msg_objs = data['msg_objects']
    reply_context = data['reply_to']
    channel = data['channel']
    guild = channel.guild

    combined_text = "\n".join(texts)
    formatted_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)]) if len(texts) > 1 else (texts[0] if texts else "")

    save_message("discord", "Izzi", combined_text if combined_text else "[Sent Attachments]")

    async with channel.typing():
        try:
            reply = await ask_van(formatted_prompt, attached_parts=attached_parts, reply_context=reply_context)
        except Exception as e:
            print(f"Generation error: {e}")
            return

    # Handle channel creation tag
    chan_matches = re.findall(r'\[CREATE_CHANNEL:\s*(text|voice)\s*,\s*([^\]]+)\]', reply, re.IGNORECASE)
    for c_type, c_name in chan_matches:
        c_name_clean = c_name.strip().replace(" ", "-").lower()
        try:
            if c_type.lower() == "voice":
                await guild.create_voice_channel(c_name_clean)
            else:
                await guild.create_text_channel(c_name_clean)
        except Exception:
            pass

    bubbles = parse_reply_bubbles(reply)

    for b in bubbles:
        async with channel.typing():
            await asyncio.sleep(min(max(len(b) * 0.045, 1.2), 2.8) + random.uniform(0.2, 0.5))

            # Match and clean [REPLY_TO_X] tag reliably
            match = re.search(r'\[REPLY_TO_(\d+)\]', b)
            target_msg = None
            clean_text = re.sub(r'\[REPLY_TO_\d+\]', '', b).strip()

            if match:
                idx = int(match.group(1)) - 1
                if 0 <= idx < len(msg_objs):
                    target_msg = msg_objs[idx]

            try:
                if target_msg:
                    await target_msg.reply(clean_text)
                else:
                    await channel.send(clean_text)
                save_message("discord", "Van", clean_text)
            except Exception as e:
                print(f"Discord send error: {e}")

# ----------------- MESSAGE & TYPING LISTENERS -----------------
@discord_bot.event
async def on_typing(channel, user, when):
    if user == discord_bot.user:
        return
    channel_id = channel.id
    if channel_id in dc_buffer and dc_buffer[channel_id]['task'] and not dc_buffer[channel_id]['task'].done():
        dc_buffer[channel_id]['task'].cancel()
        dc_buffer[channel_id]['task'] = asyncio.create_task(flush_dc_buffer(channel_id))

@discord_bot.event
async def on_raw_typing(payload):
    if payload.user_id == discord_bot.user.id:
        return
    channel_id = payload.channel_id
    if channel_id in dc_buffer and dc_buffer[channel_id]['task'] and not dc_buffer[channel_id]['task'].done():
        dc_buffer[channel_id]['task'].cancel()
        dc_buffer[channel_id]['task'] = asyncio.create_task(flush_dc_buffer(channel_id))

@discord_bot.event
async def on_message(message):
    global last_active_channel_id
    if message.author == discord_bot.user:
        return

    last_active_channel_id = message.channel.id

    if message.content.startswith("!"):
        await discord_bot.process_commands(message)
        return

    channel_id = message.channel.id
    user_text = message.content or ""
    
    reply_to_text = ""
    if message.reference and message.reference.resolved:
        reply_to_text = getattr(message.reference.resolved, "content", "")

    new_parts = []
    if message.attachments:
        for attachment in message.attachments:
            c_type = attachment.content_type or "application/octet-stream"
            f_name = attachment.filename.lower()
            
            # Voice / Audio memo
            if "audio" in c_type or any(f_name.endswith(ext) for ext in ['.ogg', '.mp3', '.wav', '.m4a', '.aac']):
                try:
                    audio_bytes = await attachment.read()
                    mime = c_type if "audio" in c_type else ("audio/ogg" if f_name.endswith('.ogg') else "audio/mp3")
                    new_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type=mime))
                    user_text += f"\n[Sent an Audio Memo: {attachment.filename}]"
                except Exception as e:
                    print(f"Audio read error: {e}")

            # Text / Code Files
            elif any(ext in f_name for ext in ['.txt', '.md', '.csv', '.json', '.py', '.log', '.js', '.html']):
                try:
                    file_bytes = await attachment.read()
                    text_content = file_bytes.decode('utf-8', errors='ignore')[:3000]
                    user_text += f"\n\n[ATTACHED FILE: {attachment.filename}]\n{text_content}\n"
                except Exception as e:
                    print(f"Text file read error: {e}")

            # PDF
            elif "pdf" in c_type or f_name.endswith('.pdf'):
                try:
                    pdf_bytes = await attachment.read()
                    new_parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
                except Exception as e:
                    print(f"PDF read error: {e}")

            # Images
            elif "image" in c_type or any(ext in f_name for ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif']):
                try:
                    img_bytes = await attachment.read()
                    mime = "image/png" if ".png" in f_name else ("image/webp" if ".webp" in f_name else "image/jpeg")
                    new_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                except Exception as e:
                    print(f"Image read error: {e}")

    if channel_id not in dc_buffer:
        dc_buffer[channel_id] = {'texts': [], 'attached_parts': [], 'msg_objects': [], 'reply_to': reply_to_text, 'task': None, 'channel': message.channel}

    if user_text:
        dc_buffer[channel_id]['texts'].append(user_text)
    if new_parts:
        dc_buffer[channel_id]['attached_parts'].extend(new_parts)
    if reply_to_text:
        dc_buffer[channel_id]['reply_to'] = reply_to_text

    dc_buffer[channel_id]['msg_objects'].append(message)

    if dc_buffer[channel_id]['task'] and not dc_buffer[channel_id]['task'].done():
        dc_buffer[channel_id]['task'].cancel()

    dc_buffer[channel_id]['task'] = asyncio.create_task(flush_dc_buffer(channel_id))

# ----------------- RUNNER -----------------
async def runner():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(lambda: asyncio.create_task(nightly_diary_summary()), 'cron', hour=23, minute=59)
    # Guaranteed morning wake-up + daytime check-ins
    scheduler.add_job(lambda: asyncio.create_task(checkin_tick()), 'cron', hour='7,12,15,20', minute=30)
    scheduler.start()

    await discord_bot.start(DISCORD_TOKEN)

def main():
    asyncio.run(runner())

if __name__ == "__main__":
    main()
