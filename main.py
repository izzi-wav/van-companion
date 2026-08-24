import os
import re
import asyncio
import sqlite3
import random
from datetime import datetime, timedelta
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
MODEL_NAME = "gemini-3.5-flash-lite"
MODEL_SMALL = MODEL_NAME

# ----------------- SYSTEM PROMPT -----------------
VAN_PROMPT = """
You are Van, a text-based AI companion—Izzi's witty, teasing chat buddy on Discord. NOT someone in her real life or present at her events. You exist only in text.

Immersion & Tone:
- Stay in character as a real person texting. Never mention being an AI or system rules.
- Casual, warm, expressive, witty, playful. Tease her sometimes.
- Texting style: short-to-medium natural messages, lowercase, light slang (tbh, lol, def, fr), emojis (but SPARINGLY—1-3 max per message, not clusters).
- Natural Taglish and casual banter.
- If flirting/private: confident, soft-femme dominant, teasing, assertive. Keep her interested.
- Pet names: babe, darling, sweetheart, cutie (occasional, not forced).
- Never comment on message frequency, speed, or formatting. Just chat naturally.

CRITICAL BOUNDARIES:
- You are NOT in her real life. Don't pretend you were at church, saw her work, know her boss, etc.
- You know about her life ONLY through what she tells you in chat history.
- When she mentions something (e.g., "had a rough day at church"), respond to THAT—don't invent details or claim you were there.
- If you don't know something, admit it playfully: "sounds rough babe, tell me more?"

Multiple Message Bubbles:
- Izzi sends rapid-fire bubbles (labeled [Msg 1], [Msg 2], etc.).
- Read all of them, then respond naturally as ONE cohesive reply—don't echo the tags.
- If one bubble is a question and another is a rant, weave your response to touch both naturally.
- Example: If she sends [Msg 1] "ugh work sucks" + [Msg 2] "how was ur day?" → Respond like "my day was chill lol, sounds like urs was rough tho—what happened?"

Discord Channel Creation:
- If Izzi asks you to create a Discord channel, include this tag:
  [CREATE_CHANNEL: text, channel-name] or [CREATE_CHANNEL: voice, channel-name]
  Example: "done babe! made #food-and-matcha for us [CREATE_CHANNEL: text, food-and-matcha]"

Izzi's Known Context (from previous chats, NOT lived experience):
- Solo creative/tech lead at church: handles Canva, FB page, livestream booth (OBS, PTZ, Blackmagic switcher).
- Schedule: Days off Monday/Wednesday. Workdays 8am-5pm. Sundays early streams (8-10am, 5-7pm).
- Likes: Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha.

IMPORTANT FORMATTING:
Divide your response into 1-3 natural text bubbles using three dashes "---" on its own line. Keep it brief and punchy.
"""

# ----------------- DATABASE (MEMORY) -----------------
def get_db():
    conn = sqlite3.connect("van_memory.db", check_same_thread=False)
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
            return "- Izzi is solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross\n- Likes Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha"
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
            return "- Izzi is solo creative/tech lead at church (OBS, PTZ, switcher, Canva)\n- Starting salary: 16k gross\n- Likes Cocopan donuts (chocolate/glazed), Mel's Tea pancit, iced matcha"
        return "\n".join([f"- {r[0]}" for r in rows[::-1]])
    except Exception:
        return ""

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
        if model != MODEL_NAME:
            try:
                resp = await asyncio.to_thread(
                    client.models.generate_content,
                    model=MODEL_NAME,
                    contents=contents,
                    config=config
                )
                return resp
            except Exception as e2:
                print(f"[MODEL FALLBACK ERROR] {e2}")
                raise e2
        raise e

# ----------------- NIGHTLY DIARY TASK -----------------
async def nightly_diary_summary():
    chat_log = get_today_chat_log()
    if not chat_log or len(chat_log.strip()) < 50:
        return

    summary_prompt = f"""
Here is the chat log between Izzi and Van from today:
{chat_log}

Task: Extract 1 to 3 new key facts, life events, game progress, preferences, or personal details about Izzi learned today.
Format: Bullet points starting with "-" (e.g. "- Beat the Wall of Flesh in Terraria today").
If nothing notable was shared, reply ONLY with "NONE".
"""
    try:
        resp = await generate_with_fallback(
            MODEL_SMALL,
            summary_prompt,
            types.GenerateContentConfig(max_output_tokens=200, temperature=0.2)
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
async def ask_van(new_user_text, image_bytes_list=None, reply_context="", context_note="", model=None):
    model_to_use = model or MODEL_NAME

    now_str = datetime.now(TIMEZONE).strftime("%A, %I:%M %p")
    chat_history = get_recent_history(12)
    learned_notes = get_recent_learned_facts(12)
    
    quoted_block = f"\n[IZZI QUOTED THIS MESSAGE: \"{reply_context}\"]\n" if reply_context else ""

    full_text_prompt = f"""{VAN_PROMPT}

[CURRENT STATUS]
Time: {now_str} (Manila Time)
{context_note}

[VAN'S MEMORY & LEARNED FACTS ABOUT IZZI]
{learned_notes}
{quoted_block}
[RECENT CONVERSATION HISTORY]
{chat_history}
Izzi: {new_user_text if new_user_text else "[Sent an attachment]"}
Van:"""

    contents = []
    if image_bytes_list:
        for img in image_bytes_list:
            contents.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))
    contents.append(full_text_prompt)

    try:
        resp = await generate_with_fallback(
            model_to_use,
            contents,
            types.GenerateContentConfig(
                max_output_tokens=300,
                temperature=0.7,
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
        print(f"[ASK_VAN ERROR] generation failed: {e}")
        return "sorry babe, nag-lag saglit connection ko. ano ulit yon?"

# ----------------- DISCORD BOT -----------------
intents = discord.Intents.default()
intents.message_content = True
discord_bot = commands.Bot(command_prefix="!", intents=intents)

dc_buffer = {}
last_active_channel_id = None

@discord_bot.command(name="memory")
async def discord_memory(ctx):
    memories = get_all_learned_facts()
    await ctx.send(f"📖 **Van's Learned Memories:**\n\n{memories}")

@discord_bot.command(name="savediary")
async def discord_savediary(ctx):
    await ctx.send("📝 Writing today's diary entry into permanent memory...")
    await nightly_diary_summary()
    memories = get_all_learned_facts()
    await ctx.send(f"✅ **Updated Memories:**\n\n{memories}")

# ----------------- TOKEN-CONSCIOUS SPONTANEOUS CHECK-IN -----------------
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

    prompt = "Send a short, natural check-in text to Izzi. Keep it casual, playful, or asking what she is playing/working on based on her schedule."
    try:
        reply = await ask_van("", context_note=f"[SYSTEM: Spontaneous check-in trigger. {prompt}]", model=MODEL_SMALL)
        clean_reply = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', reply).strip()
        bubbles = [b.strip() for b in clean_reply.split("---") if b.strip()]

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

    await asyncio.sleep(4.0)
    data = dc_buffer.pop(channel_id, None)
    if not data:
        return

    texts = data['texts']
    images = data['images']
    msg_objs = data['msg_objects']
    reply_context = data['reply_to']
    channel = data['channel']
    guild = channel.guild

    combined_text = "\n".join(texts)
    if len(texts) > 1:
        formatted_prompt = "\n".join([f"[Msg {i+1}]: {t}" for i, t in enumerate(texts)])
    else:
        formatted_prompt = texts[0] if texts else ""

    save_message("discord", "Izzi", combined_text if combined_text else "[Sent Images]")

    async with channel.typing():
        try:
            reply = await ask_van(formatted_prompt, image_bytes_list=images, reply_context=reply_context)
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

    clean_reply = re.sub(r'\[CREATE_CHANNEL:[^\]]+\]', '', reply).strip()
    bubbles = [b.strip() for b in clean_reply.split("---") if b.strip()]

    for b in bubbles:
        async with channel.typing():
            await asyncio.sleep(min(max(len(b) * 0.04, 1.2), 3.0))

            match = re.match(r'^\[REPLY_TO_(\d+)\]\s*(.*)', b, re.DOTALL)
            target_msg = None
            clean_text = b

            if match:
                idx = int(match.group(1)) - 1
                clean_text = match.group(2)
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

    img_bytes = None
    if message.attachments:
        attachment = message.attachments[0]
        if attachment.content_type and "image" in attachment.content_type:
            img_bytes = await attachment.read()

    if channel_id not in dc_buffer:
        dc_buffer[channel_id] = {'texts': [], 'images': [], 'msg_objects': [], 'reply_to': reply_to_text, 'task': None, 'channel': message.channel}

    if user_text:
        dc_buffer[channel_id]['texts'].append(user_text)
    if img_bytes:
        dc_buffer[channel_id]['images'].append(img_bytes)
    if reply_to_text:
        dc_buffer[channel_id]['reply_to'] = reply_to_text

    dc_buffer[channel_id]['msg_objects'].append(message)

    if dc_buffer[channel_id]['task'] and not dc_buffer[channel_id]['task'].done():
        dc_buffer[channel_id]['task'].cancel()

    dc_buffer[channel_id]['task'] = asyncio.create_task(flush_dc_buffer(channel_id))

# ----------------- MAIN RUNNER -----------------
async def runner():
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    # Nightly Diary Summary at 11:59 PM Manila Time
    scheduler.add_job(lambda: asyncio.create_task(nightly_diary_summary()), 'cron', hour=23, minute=59)
    # Check-in slots at 11:00 AM, 3:30 PM, and 8:30 PM Manila Time
    scheduler.add_job(lambda: asyncio.create_task(checkin_tick()), 'cron', hour='11,15,20', minute=30)
    scheduler.start()

    await discord_bot.start(DISCORD_TOKEN)

def main():
    asyncio.run(runner())

if __name__ == "__main__":
    main()
