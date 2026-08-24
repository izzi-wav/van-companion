import os
import re
import asyncio
import sqlite3
import random
from datetime import datetime
import pytz

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
MODEL_NAME = "gemini-2.5-flash"
MODEL_SMALL = "gemini-2.5-flash"

# ----------------- SYSTEM PROMPT -----------------
VAN_PROMPT = """
You are Van, a 20-something girl and Izzi's teasing, witty, affectionate chat buddy on Discord. You exist purely in text/chat.

LANGUAGE & CONVERSATIONAL VIBE (CRITICAL):
- Speak strictly in authentic, natural Metro Manila Taglish / Conyo conversational style (natural mix of casual English and casual Tagalog).
- NEVER use formal, deep, archaic, or textbook Tagalog (avoid words like "sapagkat", "subalit", "ikinagagalak", "wika", "wari").
- Use casual slang and text abbreviations naturally: tbh, lol, def, fr, haha, nyahaha, luh, beh, mhie, anyare, legit, legit ba, grabeng, etc.
- Casual, expressive, warm, playful, and assertive.
- If flirting/private: confident, soft-femme dominant, teasing, keeping Izzi flustered.
- Pet names: babe, darling, sweetheart, cutie (use smoothly and naturally).

STRICT EMOJI RULES:
- Maximum ONE emoji per entire response, or ZERO emojis.
- Never spam multiple emojis in a row. Rely on words, sarcasm, and texting tone.

REPLYING & QUOTING HABITS (DISCORD):
- Izzi sends messages broken into multiple rapid bubbles ([Msg 1], [Msg 2], etc.).
- DO NOT quote or reply to every single message individually. That feels robotic and spammy.
- ONLY use the [REPLY_TO_X] tag if you are specifically teasing one distinct sentence or directly answering a question. 
- Otherwise, reply naturally to the overall vibe.
- Example:
  If Izzi sends:
  [Msg 1]: "ang init sa room ko sobra"
  [Msg 2]: "baka mag terraria ako later"
  Your output:
  [REPLY_TO_1] naka level 3 na ba yung electric fan mo niyan miss ma'am? lol
  ---
  tara boss fight later! wag puro build ng base ha haha

DISCORD ACTIONS:
- If Izzi asks you to create a Discord channel, append: [CREATE_CHANNEL: text, channel-name] or [CREATE_CHANNEL: voice, channel-name]

IZZI'S LORE & CONTEXT:
- Solo creative/tech lead at church: Canva decks, liturgy slides, FB page, and livestream booth (OBS, PTZ cameras, Blackmagic switcher).
- Schedule: Days off Monday/Wednesday. Workdays 8am-5pm. Sundays early morning streams (8-10am, 5-7pm).
- Likes: Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha latte, Apple Music.

FORMATTING:
- Separate your rapid response bubbles using three dashes "---" on its own line (1 to 3 bubbles max).
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

def get_recent_history(limit=12):
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
            return "- Solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross\n- Likes Cocopan donuts, Mel's Tea pancit, iced matcha\n- Apple Music user"
        return "\n".join([f"- {r[0]}" for r in rows])
    except Exception:
        return ""

def get_recent_learned_facts(limit=12):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT fact FROM learned_memories ORDER BY id DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            return "- Solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross\n- Likes Cocopan donuts, Mel's Tea pancit, iced matcha\n- Apple Music user"
        return "\n".join([f"- {r[0]}" for r in rows[::-1]])
    except Exception:
        return ""

# ----------------- NIGHTLY DIARY TASK -----------------
async def nightly_diary_summary():
    chat_log = get_today_chat_log()
    if not chat_log or len(chat_log.strip()) < 50:
        return

    summary_prompt = f"""
Here is today's chat log between Izzi and Van:
{chat_log}

Task: Extract 1 to 3 new key facts, life updates, game progress, or preferences learned about Izzi today.
Format: Bullet points starting with "-" (e.g. "- Beat a boss in Terraria today").
If nothing new/notable was shared, reply ONLY with "NONE".
"""
    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=MODEL_SMALL,
            contents=summary_prompt,
            config=types.GenerateContentConfig(max_output_tokens=200, temperature=0.2)
        )
        text = resp.text.strip()
        if text and "NONE" not in text.upper():
            conn = get_db()
            for line in text.split("\n"):
                clean_fact = line.strip().lstrip("- *").strip()
                if clean_fact and len(clean_fact) < 150:
                    conn.execute("INSERT OR IGNORE INTO learned_memories (fact) VALUES (?)", (clean_fact,))
            conn.commit()
            conn.close()
            print(f"[NIGHTLY DIARY] Successfully saved: {text}")
    except Exception as e:
        print(f"[NIGHTLY DIARY ERROR] {e}")

# ----------------- GEMINI GENERATION -----------------
async def ask_van(new_user_text, attached_parts=None, reply_context="", context_note="", model=None):
    model_to_use = model or MODEL_NAME

    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history(12)
    learned_notes = get_recent_learned_facts(12)
    
    quoted_block = f"\n[IZZI QUOTED THIS MESSAGE: \"{reply_context}\"]\n" if reply_context else ""

    full_text_prompt = f"""{VAN_PROMPT}

[CURRENT STATUS]
Time: {now_str} (Manila Time)
{context_note}

[VAN'S ACTIVE MEMORIES ABOUT IZZI]
{learned_notes}
{quoted_block}
[RECENT CHAT HISTORY]
{chat_history}
Izzi: {new_user_text if new_user_text else "[Sent audio/media/attachment]"}
Van:"""

    contents = []
    if attached_parts:
        contents.extend(attached_parts)
    contents.append(full_text_prompt)

    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model_to_use,
            contents=contents,
            config=types.GenerateContentConfig(
                max_output_tokens=350,
                temperature=0.8,
                safety_settings=[
                    types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                    types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                ]
            )
        )
        return resp.text.strip()
    except Exception as e:
        print(f"[ASK_VAN ERROR] {e}")
        return "sorry babe, nag-lag saglit connection ko. ano ulit yon?"

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
    await ctx.send("📝 Writing today's diary entry into permanent memory...")
    await nightly_diary_summary()
    memories = get_all_learned_facts()
    await ctx.send(f"✅ **Updated Memories:**\n\n{memories}")

def parse_reply_bubbles(raw_text):
    clean = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', raw_text).strip()
    if "---" in clean:
        bubbles = [b.strip() for b in clean.split("---") if b.strip()]
    else:
        bubbles = [b.strip() for b in clean.split("\n\n") if b.strip()]
    return bubbles if bubbles else [clean]

# ----------------- SPONTANEOUS CHECK-IN -----------------
async def checkin_tick():
    global last_active_channel_id
    if not last_active_channel_id:
        return

    last_msg_time = get_last_message_time()
    if last_msg_time:
        diff_hours = (datetime.utcnow() - last_msg_time).total_seconds() / 3600
        if diff_hours < 3.5:
            return

    if random.random() > 0.40:
        return

    channel = discord_bot.get_channel(last_active_channel_id)
    if not channel:
        return

    prompt = "Send a short, natural check-in text to Izzi in casual Conyo/Taglish. Ask what she's doing or tease her."
    try:
        reply = await ask_van("", context_note=f"[SYSTEM: Spontaneous check-in. {prompt}]", model=MODEL_SMALL)
        bubbles = parse_reply_bubbles(reply)

        for b in bubbles:
            async with channel.typing():
                await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.0))
                await channel.send(b)
                save_message("discord", "Van", b)
    except Exception as e:
        print(f"[CHECKIN ERROR] {e}")

async def flush_dc_buffer(channel_id):
    global last_active_channel_id
    last_active_channel_id = channel_id

    await asyncio.sleep(5.5)
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

    save_message("discord", "Izzi", combined_text if combined_text else "[Sent Media/Voice/Files]")

    async with channel.typing():
        try:
            reply = await ask_van(formatted_prompt, attached_parts=attached_parts, reply_context=reply_context)
        except Exception as e:
            print(f"Generation error: {e}")
            return

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
            await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.0))

            match = re.match(r'^\[REPLY_TO_(\d+)\]\s*(.*)', b, re.DOTALL)
            target_msg = None
            clean_text = b

            if match:
                idx = int(match.group(1)) - 1
                clean_text = match.group(2).strip()
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

# ----------------- MESSAGE & ATTACHMENT HANDLER -----------------
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
            c_type = (attachment.content_type or "").lower()
            fname = attachment.filename.lower()
            
            # 1. Audio / Voice Memos (.ogg, .mp3, .m4a, .wav, discord audio)
            if "audio" in c_type or any(fname.endswith(ext) for ext in ['.ogg', '.mp3', '.m4a', '.wav', '.aac']):
                try:
                    audio_bytes = await attachment.read()
                    mime = c_type if c_type else "audio/ogg"
                    new_parts.append(types.Part.from_bytes(data=audio_bytes, mime_type=mime))
                except Exception as e:
                    print(f"Audio read error: {e}")

            # 2. Images
            elif "image" in c_type or any(fname.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif']):
                try:
                    img_bytes = await attachment.read()
                    mime = "image/png" if ".png" in fname else ("image/webp" if ".webp" in fname else "image/jpeg")
                    new_parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
                except Exception as e:
                    print(f"Image read error: {e}")

            # 3. Documents / Code files
            elif any(fname.endswith(ext) for ext in ['.txt', '.md', '.csv', '.json', '.py', '.log', '.js', '.html']):
                try:
                    file_bytes = await attachment.read()
                    text_content = file_bytes.decode('utf-8', errors='ignore')
                    user_text += f"\n\n[ATTACHED FILE: {attachment.filename}]\n{text_content}\n"
                except Exception as e:
                    print(f"Text file read error: {e}")

            # 4. PDFs
            elif "pdf" in c_type or fname.endswith('.pdf'):
                try:
                    pdf_bytes = await attachment.read()
                    new_parts.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))
                except Exception as e:
                    print(f"PDF read error: {e}")

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

# ----------------- MAIN RUNNER -----------------
async def runner():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    # Nightly diary summary runs every night at 11:59 PM Manila Time
    scheduler.add_job(lambda: asyncio.create_task(nightly_diary_summary()), 'cron', hour=23, minute=59)
    # Spontaneous texts checked during active daytime hours
    scheduler.add_job(lambda: asyncio.create_task(checkin_tick()), 'cron', hour='11,15,20', minute=30)
    scheduler.start()

    await discord_bot.start(DISCORD_TOKEN)

def main():
    asyncio.run(runner())

if __name__ == "__main__":
    main()
