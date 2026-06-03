import discord
import asyncio
import random
import os
import re
import aiohttp
import time
import json
from collections import deque
from datetime import datetime
from groq import Groq

# ========== CONFIGURATION ==========
TOKEN = os.getenv("TOKEN") # Your main Discord user token
if not TOKEN:
    print(" TOKEN environment variable not set.")
    exit(1)
GROQ_API_KEY = "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"  # Replace with your Groq key

# ========== GLOBAL VARIABLES ==========
client = discord.Client(self_bot=True)
start_time = time.time()
tasks = {}                # channel_id -> scheduler task
typing_task = None
status_task = None
name_task = None
spam_tasks = []
afk_task = None
wordlists = {}            # name -> list of lines
autopaste_msgs = {}       # channel_id -> list of (delay, message)
stam_msgs = {}            # channel_id -> list of (delay, message)
count_tasks = {}          # channel_id -> asyncio.Task
react_task = None
stream_task = None
auto_reply_tasks = {}     # user_id -> asyncio.Task
gc_task = None
token_pool = []           # list of {"token": str, "client": discord.Client?, "user": object}
main_user_id = None
tool_channel_id = None
anti_target_channel = None
anti_user_history = {}    # user_id -> deque of (content, message)
anti_user_last_number = {}
anti_replied_instruction = set()
BEEF_WORDS = []            # loaded from beef.txt if exists
aball_tasks = {}          # alias -> asyncio.Task for beef workers
react_tasks = {}   # alias -> asyncio.Task for auto-reactions
mimic_tasks = {}   # alias -> asyncio.Task for message mimic
mimic_enabled = False   # global flag for mimic mode
reaction_emojis = []   # list of emojis to react with
ar_replied_ids = {}   # user_id -> set of message IDs already replied to
pending_import = {}   # user_id -> wordlist name
deleted_cache = {}
snipe_enabled = set()
spamall_tasks = {}          # alias -> asyncio.Task for spam workers
spamall_interval = 2        # seconds between messages (default, adjustable via command)

# ========== LOAD / SAVE HELPERS ==========
async def load_lines_async(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except:
        return []

def load_lines(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip()]
    except:
        return []

def save_wordlist(name, lines):
    with open(f"wordlist_{name}.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    wordlists[name] = lines

# ========== GROQ CLIENTS ==========
groq = Groq(api_key=GROQ_API_KEY)

# ========== ANTI AFK LOGIC ==========
EXTRACT_PROMPT = """Extract the secret answer from the message. Return ONLY the exact word/phrase, nothing else – no explanation, no extra words, no punctuation. If the message does NOT contain any request for a specific answer, return just: NONE
Examples:
- "afk check say pineapple" → pineapple
- "kw = strawberry" → strawberry
- "reply with hello" → hello
- "type: apple" → apple
- "||hidden|| answer is watermelon" → watermelon
- "**bold** keyword: orange" → orange
- "verify: cat" → cat
- "what is the secret word? banana" → banana
- "what is capital of russia? → moscow
- "what is date today?" → 26 jan
- "tell what is formula of Sodium Chloride" → NaCl
- "what is fastest animal?" → cheetah
- "what is 1+1*1/1+1?" → 1
- "say "im gay/ u are gay/ ur underage/ ur my son" → ?
Return ONLY the answer word/phrase, nothing else."""

NUMBERS_PATTERN = re.compile(r'^(\d+\s+)+\d+$')
ROMAN_PATTERN = re.compile(r'^([IVXLCDMivxlcdm]+\s+)+[IVXLCDMivxlcdm]+$')
WORD_NUM_PATTERN = re.compile(r'^([A-Za-z]+\d+\s+)+[A-Za-z]+\d+$')
ENGLISH_NUMBERS = r'(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)'
ENGLISH_PATTERN = re.compile(r'^(' + ENGLISH_NUMBERS + r'\s+)+' + ENGLISH_NUMBERS + r'$', re.IGNORECASE)

def roman_to_int(roman):
    roman = roman.upper().strip()
    values = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    total = 0
    prev = 0
    for ch in reversed(roman):
        val = values.get(ch)
        if not val: return None
        if val < prev: total -= val
        else: total += val
        prev = val
    return total if total <= 20 else None

def word_to_int(word):
    words = {"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10,
             "eleven":11,"twelve":12,"thirteen":13,"fourteen":14,"fifteen":15,"sixteen":16,"seventeen":17,
             "eighteen":18,"nineteen":19,"twenty":20}
    return words.get(word.lower())

def parse_count_number(text):
    text = text.strip()
    if text.isdigit(): return int(text)
    r = roman_to_int(text)
    if r is not None: return r
    return word_to_int(text)

def is_counting_message(text):
    text = text.strip()
    if not text: return False
    return (bool(NUMBERS_PATTERN.match(text)) or bool(ROMAN_PATTERN.match(text)) or
            bool(WORD_NUM_PATTERN.match(text)) or bool(ENGLISH_PATTERN.match(text)))

def simple_keyword_extract(text):
    patterns = [r'kw\s*=\s*(\w+)', r'say\s+(\w+)', r'reply with\s+(\w+)', r'type:\s*(\w+)',
                r'answer is\s+(\w+)', r'keyword:?\s*(\w+)', r'capital of\s+(\w+)', r'formula of\s+(\w+)']
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m: return m.group(1).strip()
    return None

def extract_answer(message_text):
    try:
        resp = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": EXTRACT_PROMPT},
                      {"role": "user", "content": message_text}],
            temperature=0.0, max_tokens=50
        )
        ans = resp.choices[0].message.content.strip()
        if ans == "NONE" or not ans:
            return simple_keyword_extract(message_text)
        return ans
    except:
        return simple_keyword_extract(message_text)

# ========== AI PACK GENERATOR ==========
def generate_ai_pack(pack_type: str, pack_size: int) -> str:
    system_prompt = """You are a Discord pack generator that creates swear and messages containing abuse for virtual "pack openings". 
The theme can be anything: rare, epic, joke, meme, roast (non‑personal), silly, etc. 
Keep the message to 4-5 number of sentences, use humour, puns, or absurdity.
your motive is to roast and humiliate the other person badly.
often include abuses like "bitch, whore, trash, dork, cunt, moron, fucking, shit, loser, pedophile, shitty, ass, fuck, slut, cuck, maggot" dont include them in every line.
dont add exclamation signs or fullstops or commas.
keep starting words,middle words, and last words unique in every pack.
dont add the words "just" and "pack" in the pack.
sentence should only be one.
Respond with only the pack message, no extra text."""
    user_prompt = f"Create a {pack_type} pack for roasting opening with {pack_size} items."
    try:
        resp = groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0.9, max_tokens=100
        )
        return resp.choices[0].message.content.strip()
    except:
        return f" You opened a {pack_type} pack and got {pack_size} mysterious items"

# ========== NUKE FUNCTION ==========
async def nuke_server(guild_id, new_name="captured by supreme", description="This server has been taken", channel_prefix="fucked-", channel_count=10):
    guild = client.get_guild(guild_id)
    if not guild:
        return "Server not found"
    try:
        await guild.edit(name=new_name, description=description)
        # Delete all channels
        for ch in guild.channels:
            try:
                await ch.delete()
                await asyncio.sleep(0.3)
            except: pass
        # Create new channels
        for i in range(channel_count):
            await guild.create_text_channel(f"{channel_prefix}{i+1}")
            await asyncio.sleep(0.5)
        return f"Nuked {guild.name}"
    except Exception as e:
        return f"Nuke failed: {e}"

# ========== COMMAND HANDLER ==========
@client.event
async def on_ready():
    global main_user_id
    main_user_id = client.user.id
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print("Type .menu to see all commands")

@client.event
async def on_message_delete(message):
    # only work if enabled in this channel
    if message.channel.id not in snipe_enabled:
        return

    data = deleted_cache.get(message.id)

    if not data:
        return

    try:
        await message.channel.send(
            f" **this guy deleted Message**\n"
            f" sender: {data['author']}\n"
            f" Sent at: {data['time']}\n"
            f" message: {data['content']}"
        )
    except:
        pass
      
@client.event
async def on_message(message):
    global anti_target_channel, anti_user_history, anti_user_last_number, tasks
    global status_task, name_task, spam_tasks, afk_task, autopaste_msgs, stam_msgs
    global count_tasks, react_task, stream_task, auto_reply_tasks, gc_task, token_pool
    global BEEF_WORDS, main_user_id, tool_channel_id, current_menu_page, reaction_emojis

        # store message info
    deleted_cache[message.id] = {
        "content": message.content,
        "author": f"{message.author} ({message.author.id})",
        "time": message.created_at.strftime("%Y-%m-%d %H:%M:%S")
    }

    authorized_ids = {client.user.id}   # always allow the main account
    for token_info in token_pool:
        if token_info.get('user_id'):
            authorized_ids.add(token_info['user_id'])
    
# ----- Anti AFK logic (works for ANY user in monitored channel) -----
    if anti_target_channel and message.channel.id == anti_target_channel:
        author_id = message.author.id
        content = message.content
        if author_id not in anti_user_history:
            anti_user_history[author_id] = deque(maxlen=10)
        anti_user_history[author_id].append((content, message))
        num = parse_count_number(content)
        if num is not None:
            last = anti_user_last_number.get(author_id, 0)
            if num == last + 1:
                anti_user_last_number[author_id] = num
                if num == 9:
                    history = list(anti_user_history.get(author_id, []))
                    answer = None
                    for prev_content, _ in reversed(history):
                        if prev_content == content: continue
                        ans = extract_answer(prev_content)
                        if ans:
                            answer = ans
                            break
                    if answer:
                        await message.channel.send(f"# {answer}")
                        print(f"Anti AFK replied: {answer}")
                    anti_user_last_number[author_id] = 0
            else:
                anti_user_last_number[author_id] = 0
        else:
            anti_user_last_number[author_id] = 0

        # ----- Auto-reaction (instant) -----
    if message.author == client.user and reaction_emojis:
        # React to your own messages immediately
        for emoji in reaction_emojis:
            try:
                await message.add_reaction(emoji)
                await asyncio.sleep(0.2)  # tiny delay between reactions to avoid rate limits
            except:
                pass

    # ----- Command processing only for authorized users -----
    if message.author.id not in authorized_ids:
        return

    # ----- Handle file uploads for pending imports -----
    if message.attachments and message.author.id in pending_import:
        name = pending_import.pop(message.author.id)
        attachment = message.attachments[0]
        if not attachment.filename.endswith('.txt'):
            await message.channel.send(f" Only `.txt` files are allowed for wordlists.")
            return
        try:
            # Download the file content
            async with aiohttp.ClientSession() as session:
                async with session.get(attachment.url) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        lines = [l.strip() for l in content.splitlines() if l.strip()]
                        if not lines:
                            await message.channel.send(f" File is empty.")
                            return
                        # Save as wordlist_<name>.txt
                        filename = f"{name}.txt"
                        with open(filename, "w", encoding="utf-8") as f:
                            f.write("\n".join(lines))
                        wordlists[name] = lines
                        await message.channel.send(f" Wordlist **{name}** imported with {len(lines)} lines.")
                    else:
                        await message.channel.send(f" Failed to download file (HTTP {resp.status}).")
        except Exception as e:
            await message.channel.send(f" Error importing wordlist: {e}")
        return  # Don't process the message as a command
    
    if not message.content.startswith("."):
        return

    parts = message.content.split()
    cmd = parts[0].lower()
    args = parts[1:]

    # ========== ORIGINAL COMMANDS ==========
    if cmd == ".ab" and len(args) == 3:
        try:
            ch_id = int(args[0]); delay = float(args[1]); fname = args[2]
            channel = client.get_channel(ch_id)
            if not channel:
                await message.channel.send("Invalid channel ID")
                return
            if ch_id in tasks:
                tasks[ch_id].cancel()
            async def sched():
                try:
                    while True:
                        if fname in wordlists:
                            lines = wordlists[fname]
                        else:
                            lines = await asyncio.to_thread(load_lines, fname)
                        await asyncio.sleep(0)   # cancellation point
                        if not lines:
                            await asyncio.sleep(5)
                            continue
                        random.shuffle(lines)
                        for line in lines:
                            # Check for cancellation before each send
                            if asyncio.current_task().cancelled():
                                return
                            try:
                                await channel.send(line)
                                await asyncio.sleep(delay)
                            except asyncio.CancelledError:
                                raise
                            except:
                                await asyncio.sleep(5)
                except asyncio.CancelledError:
                    return

            tasks[ch_id] = asyncio.create_task(sched())
            
            await message.channel.send(f"ab started in {ch_id} every {delay}s using {fname}")
        except:
            await message.channel.send("Usage: .ab <channel_id> <delay> <file.txt>")
    
    elif cmd == ".abstop":
        if not tasks:
            await message.channel.send("No active ab running")
            return
        count = 0
        for ch_id, task in list(tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        tasks.clear()
        await message.channel.send(f" Stopped {count} ab(s).")

    elif cmd == ".startnames" and len(args) >= 1:
        names = [n.strip() for n in message.content[12:].split(",") if n.strip()]
        if not names:
            await message.channel.send("Provide names: .startnames name1,name2,...")
            return
        async def cycle():
            count = 0
            while count < 500000:
                for name in names:
                    try:
                        await message.channel.edit(name=name)
                        await asyncio.sleep(1)
                        count += 1
                        if count >= 500000: break
                    except:
                        await asyncio.sleep(60)
        if name_task: name_task.cancel()
        name_task = asyncio.create_task(cycle())
        await message.channel.send("Name cycling started")

    elif cmd == ".stopnames":
        if name_task:
            name_task.cancel()
            name_task = None
            await message.channel.send("Name cycling stopped")

    elif cmd == ".spam":
        spam_msg = message.content[6:]
        if not spam_msg:
            await message.channel.send("Usage: .spam <message>")
            return
        async def sp():
            while True:
                try:
                    await message.channel.send(spam_msg)
                    await asyncio.sleep(6)
                except:
                    await asyncio.sleep(10)
        task = asyncio.create_task(sp())
        spam_tasks.append(task)
        await message.channel.send(" Spamming started")

    elif cmd == ".stopspam":
        if not spam_tasks:
            await message.channel.send("No active spam tasks.")
            return
        count = 0
        for task in spam_tasks:
            if not task.done():
                task.cancel()
                count += 1
        spam_tasks.clear()
        await message.channel.send(f" Stopped {count} spam task(s).")

    elif cmd == ".check" and len(args) >= 2 and message.mentions:
        user = message.mentions[0]
        try:
            limit = int(args[-1])
        except:
            await message.channel.send("Enter a valid number")
            return
        async def afk():
            for i in range(1, limit+1):
                try:
                    await message.channel.send(f"{user.mention} {i}")
                    await asyncio.sleep(2)
                except:
                    await asyncio.sleep(5)
        if afk_task: afk_task.cancel()
        afk_task = asyncio.create_task(afk())
        await message.channel.send(f"AFK check started for {user}")

    elif cmd == ".stopafk":
        if afk_task:
            afk_task.cancel()
            afk_task = None
            await message.channel.send("AFK check stopped")

    # ========== NEW COMMANDS ==========
    elif cmd == ".menu":
        # pagination will be handled by separate commands .n and .p
        await show_menu_page(message.channel, 0)

    elif cmd == ".n":
        await next_menu_page(message.channel)

    elif cmd == ".p":
        await prev_menu_page(message.channel)

    elif cmd == ".wordlist" and len(args) == 1:
        name = args[0]
        base = name if not name.endswith('.txt') else name[:-4]
        lines = load_lines(f"{base}.txt")
        if lines:
            wordlists[name] = lines
            await message.channel.send(f"Loaded wordlist '{name}' with {len(lines)} lines")
        else:
            await message.channel.send(f"Wordlist '{name}' not found")

    elif cmd == ".wordlists":
        txt_files = [f for f in os.listdir() if f.endswith('.txt') and os.path.isfile(f)]
        if txt_files:
            await message.channel.send(" .txt files in directory:\n" + "\n".join(txt_files))
        else:
            await message.channel.send("No .txt files found.")

    elif cmd == ".importwl" and len(args) == 1:
        name = args[0]
        pending_import[message.author.id] = name
        await message.channel.send(f" upload the `.txt` file for wordlist **{name}** now (Send only the file, no extra text)")

    elif cmd == ".autopaste" and len(args) >= 3:
        try:
            ch_id = int(args[0]); delay = float(args[1]); msg = " ".join(args[2:])
            if ch_id not in autopaste_msgs:
                autopaste_msgs[ch_id] = []
            autopaste_msgs[ch_id].append((delay, msg))
            # start background task if not already
            if ch_id not in tasks:
                async def auto_paste_loop():
                    while True:
                        if ch_id not in autopaste_msgs or not autopaste_msgs[ch_id]:
                            await asyncio.sleep(5)
                            continue
                        for d, m in autopaste_msgs[ch_id]:
                            try:
                                ch = client.get_channel(ch_id)
                                if ch: await ch.send(m)
                            except: pass
                            await asyncio.sleep(d)
                        await asyncio.sleep(1)
                tasks[ch_id] = asyncio.create_task(auto_paste_loop())
            await message.channel.send(f"Auto-paste added in {ch_id}")
        except:
            await message.channel.send("Usage: .autopaste <channel_id> <delay> <message>")

    elif cmd == ".autopastelist" and len(args) == 1:
        ch_id = int(args[0])
        if ch_id in autopaste_msgs:
            msgs = "\n".join([f"{i+1}. delay={d} msg={m[:30]}" for i,(d,m) in enumerate(autopaste_msgs[ch_id])])
            await message.channel.send(f"Auto-paste messages in {ch_id}:\n{msgs}")
        else:
            await message.channel.send("No auto-paste for that channel")

    elif cmd == ".autopasteremove" and len(args) == 2:
        ch_id = int(args[0]); idx = int(args[1]) - 1
        if ch_id in autopaste_msgs and 0 <= idx < len(autopaste_msgs[ch_id]):
            del autopaste_msgs[ch_id][idx]
            await message.channel.send(f"Removed entry {idx+1}")
        else:
            await message.channel.send("Invalid index")

    elif cmd == ".stopautopaste" and len(args) == 1:
        ch_id = int(args[0])
        if ch_id in tasks:
            tasks[ch_id].cancel()
            del tasks[ch_id]
        autopaste_msgs.pop(ch_id, None)
        await message.channel.send(f"Stopped auto-paste in {ch_id}")

    # .stam similar to autopaste but with different name
    elif cmd == ".stam" and len(args) >= 3:
        # same as autopaste but store in stam_msgs
        try:
            ch_id = int(args[0]); delay = float(args[1]); msg = " ".join(args[2:])
            if ch_id not in stam_msgs:
                stam_msgs[ch_id] = []
            stam_msgs[ch_id].append((delay, msg))
            # start background task if not already
            if f"stam_{ch_id}" not in tasks:
                async def stam_loop():
                    while True:
                        if ch_id not in stam_msgs or not stam_msgs[ch_id]:
                            await asyncio.sleep(5)
                            continue
                        for d, m in stam_msgs[ch_id]:
                            try:
                                ch = client.get_channel(ch_id)
                                if ch: await ch.send(m)
                            except: pass
                            await asyncio.sleep(d)
                        await asyncio.sleep(1)
                tasks[f"stam_{ch_id}"] = asyncio.create_task(stam_loop())
            await message.channel.send(f"Stam added in {ch_id}")
        except:
            await message.channel.send("Usage: .stam <channel_id> <delay> <message>")

    elif cmd == ".stamlist" and len(args) == 1:
        ch_id = int(args[0])
        if ch_id in stam_msgs:
            msgs = "\n".join([f"{i+1}. delay={d} msg={m[:30]}" for i,(d,m) in enumerate(stam_msgs[ch_id])])
            await message.channel.send(f"Stam messages in {ch_id}:\n{msgs}")
        else:
            await message.channel.send("No stam for that channel")

    elif cmd == ".stamremove" and len(args) == 2:
        ch_id = int(args[0]); idx = int(args[1]) - 1
        if ch_id in stam_msgs and 0 <= idx < len(stam_msgs[ch_id]):
            del stam_msgs[ch_id][idx]
            await message.channel.send(f"Removed stam entry {idx+1}")
        else:
            await message.channel.send("Invalid index")

    elif cmd == ".stopstam" and len(args) == 1:
        ch_id = int(args[0])
        if f"stam_{ch_id}" in tasks:
            tasks[f"stam_{ch_id}"].cancel()
            del tasks[f"stam_{ch_id}"]
        stam_msgs.pop(ch_id, None)
        await message.channel.send(f"Stopped stam in {ch_id}")

    elif cmd == ".autocount" and len(args) >= 2:
        try:
            ch_id = int(args[0]); start = int(args[1]); end = int(args[2]) if len(args) > 2 else None
            async def count_loop():
                i = start
                while True:
                    try:
                        ch = client.get_channel(ch_id)
                        if ch: await ch.send(str(i))
                        i += 1
                        if end and i > end: break
                        await asyncio.sleep(1)
                    except:
                        await asyncio.sleep(2)
            if ch_id in count_tasks: count_tasks[ch_id].cancel()
            count_tasks[ch_id] = asyncio.create_task(count_loop())
            await message.channel.send(f"Counting started in {ch_id} from {start}")
        except:
            await message.channel.send("Usage: .autocount <channel> <start> [end]")

    elif cmd == ".count" and len(args) == 2:
        ch_id = int(args[0]); start = int(args[1])
        async def cdown():
            for i in range(start, 0, -1):
                try:
                    ch = client.get_channel(ch_id)
                    if ch: await ch.send(str(i))
                    await asyncio.sleep(1)
                except:
                    await asyncio.sleep(2)
        if ch_id in count_tasks: count_tasks[ch_id].cancel()
        count_tasks[ch_id] = asyncio.create_task(cdown())
        await message.channel.send(f"Countdown started in {ch_id} from {start}")

    elif cmd == ".stopac":
        for tid in list(count_tasks.keys()):
            count_tasks[tid].cancel()
            del count_tasks[tid]
        await message.channel.send("All counting tasks stopped")

    elif cmd == ".react" and len(args) >= 1:
        reaction_emojis = args  # store the emojis
        await message.channel.send(f"Auto-react enabled")

    elif cmd == ".stopreact":
        reaction_emojis = []
        await message.channel.send(" Auto-react stopped")
            
    elif cmd == ".stream" and len(args) >= 1:
        if stream_task:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
        texts = " ".join(args).split(",")
        async def stream_loop():
            try:
                while True:
                    for t in texts:
                        await client.change_presence(activity=discord.Streaming(name=t.strip(), url="https://twitch.tv/yourchannel"))
                        await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Clear presence when cancelled
                await client.change_presence(activity=None)
                raise
        stream_task = asyncio.create_task(stream_loop())
        await message.channel.send(f"Stream rotation started: {texts}")
    
    elif cmd == ".streamend":
        if stream_task:
            stream_task.cancel()
            try:
                await stream_task
            except asyncio.CancelledError:
                pass
            stream_task = None
            await message.channel.send("Stream rotation stopped")
        else:
            await message.channel.send("No active stream task")
            
    elif cmd == ".ar" and len(args) >= 3 and message.mentions:
        user = message.mentions[0]
        try:
            channel_id = int(args[1])
        except ValueError:
            await message.channel.send(" Channel ID must be a number.")
            return
        reply_msg = " ".join(args[2:])
        if not reply_msg:
            await message.channel.send(" You must provide a reply message.")
            return
    
        # Cancel existing task for this user if any
        if user.id in auto_reply_tasks and not auto_reply_tasks[user.id].done():
            auto_reply_tasks[user.id].cancel()
            try:
                await auto_reply_tasks[user.id]
            except asyncio.CancelledError:
                pass
    
        # Track which messages we've already replied to
        if user.id not in ar_replied_ids:
            ar_replied_ids[user.id] = set()
    
        async def ar_loop():
            try:
                while True:
                    try:
                        ch = client.get_channel(channel_id)
                        if ch:
                            # Get the last 10 messages in that channel
                            async for msg in ch.history(limit=10):
                                if msg.author == user and msg.id not in ar_replied_ids[user.id]:
                                    # New message from target user – reply once
                                    await msg.reply(reply_msg)
                                    ar_replied_ids[user.id].add(msg.id)
                                    # Keep set size manageable (optional)
                                    if len(ar_replied_ids[user.id]) > 100:
                                        ar_replied_ids[user.id].clear()
                                    break  # Only reply to the newest one per check
                        await asyncio.sleep(2)
                    except asyncio.CancelledError:
                        break
                    except:
                        await asyncio.sleep(5)
            except asyncio.CancelledError:
                pass
            finally:
                # Clean up when task is stopped
                if user.id in ar_replied_ids:
                    ar_replied_ids[user.id].clear()
    
        task = asyncio.create_task(ar_loop())
        auto_reply_tasks[user.id] = task
        await message.channel.send(f" Auto-reply to {user} in <#{channel_id}>: \"{reply_msg[:50]}\"")

    elif cmd == ".sar":
        if not auto_reply_tasks:
            await message.channel.send(" No auto-reply tasks running.")
            return
        count = 0
        for uid, task in list(auto_reply_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        auto_reply_tasks.clear()
        ar_replied_ids.clear()
        await message.channel.send(f" Stopped {count} auto-reply task(s).")

    elif cmd == ".ar2" and len(args) >= 3 and message.mentions:
        user = message.mentions[0]
        lines = int(args[1])
        msg = " ".join(args[2:])
        for _ in range(lines):
            await message.channel.send(f"{user.mention} {msg}")
            await asyncio.sleep(0.5)
        await message.channel.send(f"Flood sent to {user}")

    elif cmd == ".gcname" and len(args) >= 3:
        ch_id = int(args[0]); delay = float(args[1]); name = " ".join(args[2:])
        async def gc_loop():
            while True:
                try:
                    ch = client.get_channel(ch_id)
                    if ch and isinstance(ch, discord.GroupChannel):
                        await ch.edit(name=name)
                        await asyncio.sleep(delay)
                    else:
                        break
                except:
                    await asyncio.sleep(10)
        if gc_task: gc_task.cancel()
        gc_task = asyncio.create_task(gc_loop())
        await message.channel.send(f"GC name changer started in {ch_id}")

    elif cmd == ".stopgc":
        if gc_task:
            gc_task.cancel()
            gc_task = None
            await message.channel.send("GC name changer stopped")

    elif cmd == ".lockgc" and len(args) == 2:
        ch_id = int(args[0]); name = args[1]
        ch = client.get_channel(ch_id)
        if ch and isinstance(ch, discord.GroupChannel):
            await ch.edit(name=name, reason="Locked")
            await message.channel.send(f"GC locked with name {name}")
        else:
            await message.channel.send("Invalid group channel")

    elif cmd == ".agct":
        await message.channel.send("Anti-GC settings: Not implemented")

    elif cmd == ".purge" and len(args) >= 1:
        amount = int(args[0])
        channel = message.channel
        if len(args) > 1:
            channel = client.get_channel(int(args[1]))
        if not channel:
            await message.channel.send("Invalid channel")
            return
        deleted = 0
        async for msg in channel.history(limit=amount):
            if msg.author == client.user:
                try:
                    await msg.delete()
                    deleted += 1
                    await asyncio.sleep(0.5)
                except:
                    pass
        await message.channel.send(f"Deleted {deleted} messages")

    elif cmd == ".aball":
        # Parse arguments: [channel_id] [wordlist]
        target_channel_id = None
        wordlist_name = None
        if len(args) >= 1:
            if args[0].isdigit():
                target_channel_id = int(args[0])
                if len(args) >= 2:
                    wordlist_name = args[1]
            else:
                wordlist_name = args[0]
        if target_channel_id is None:
            target_channel_id = message.channel.id
    
        if not token_pool:
            await message.channel.send("No tokens loaded. Use `.host <token>` first.")
            return
    
        # Load beef word list from specified wordlist or default
        if wordlist_name:
            if wordlist_name in wordlists:
                BEEF_WORDS = wordlists[wordlist_name]
            else:
                # Try to load from file (wordlist_<name>.txt) if not in memory
                lines = load_lines(f"wordlist_{wordlist_name}.txt")
                if lines:
                    wordlists[wordlist_name] = lines
                    BEEF_WORDS = lines
                else:
                    await message.channel.send(f" Wordlist `{wordlist_name}` not found. Use `.wordlist {wordlist_name}` first.")
                    return
        else:
            if not BEEF_WORDS:
                BEEF_WORDS = load_lines("beef.txt")
                if not BEEF_WORDS:
                    BEEF_WORDS = ["You got rekt", "L + ratio", "Get owned"]
    
        # Cancel any existing beef tasks
        for alias, task in list(aball_tasks.items()):
            if not task.done():
                task.cancel()
        aball_tasks.clear()
    
        async def beef_worker(token_info, channel_id, alias):
            token = token_info["token"]
            headers = {"Authorization": token, "Content-Type": "application/json"}
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
            try:
                async with aiohttp.ClientSession() as session:
                    # Verify token
                    async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                        if resp.status != 200:
                            print(f"[Beef] {alias} token invalid: HTTP {resp.status}")
                            return
                        user_data = await resp.json()
                        print(f"[Beef] {alias} authenticated as {user_data['username']}")
    
                    # Main loop
                    while True:
                        await asyncio.sleep(0)
                        word = random.choice(BEEF_WORDS)
                        payload = {"content": word}
                        async with session.post(url, json=payload, headers=headers) as resp:
                            if resp.status not in (200, 204):
                                print(f"[Beef] {alias} send failed: {resp.status}")
                            else:
                                print(f"[Beef] {alias} sent: {word}")
                        await asyncio.sleep(2)
    
            except asyncio.CancelledError:
                print(f"[Beef] {alias} task cancelled")
            except Exception as e:
                print(f"[Beef] {alias} error: {e}")
                await message.channel.send(f" **{alias}** error: {e}")
    
        for token_info in token_pool:
            alias = token_info.get("alias", "unknown")
            task = asyncio.create_task(beef_worker(token_info, target_channel_id, alias))
            aball_tasks[alias] = task
            await asyncio.sleep(1)
    
        wl_msg = f" using wordlist `{wordlist_name}`" if wordlist_name else " using default beef list"
        await message.channel.send(f" Auto-beef started with {len(token_pool)} token(s) in <#{target_channel_id}>{wl_msg}")
        
    elif cmd == ".aballstop":
        if not aball_tasks:
            await message.channel.send("No active beef tasks to stop.")
            return
        
        count = 0
        for alias, task in list(aball_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        aball_tasks.clear()
        
        await message.channel.send(f" Stopped {count} beef task(s).")        

    elif cmd == ".streamall" and len(args) >= 1:
        if not token_pool:
            await message.channel.send("No hosted tokens. Use `.host` first.")
            return
        texts = " ".join(args).split(",")
        if not texts:
            await message.channel.send("Usage: .streamall title1,title2,...")
            return

        async def stream_on_token(token_obj, channel_id):
            temp_client = discord.Client(self_bot=True)
            try:
                await temp_client.start(token_obj["token"])
                print(f"[Stream] Logged in as {temp_client.user}")
                while True:
                    for title in texts:
                        try:
                            await temp_client.change_presence(
                                activity=discord.Streaming(name=title.strip(), url="https://twitch.tv/yourchannel")
                            )
                            await asyncio.sleep(10)
                        except:
                            await asyncio.sleep(5)
            except Exception as e:
                print(f"[Stream] Error for {token_obj['alias']}: {e}")
            finally:
                await temp_client.close()

        for t in token_pool:
            asyncio.create_task(stream_on_token(t, message.channel.id))
        await message.channel.send(f"Stream rotation started on {len(token_pool)} tokens: {texts}")
    
    elif cmd == ".reactall" and len(args) >= 1:
        if not token_pool:
            await message.channel.send("No tokens loaded. Use `.host` first.")
            return
        emojis = " ".join(args).split()
        if not emojis:
            await message.channel.send("Usage: `.reactall 🎉 ✅ 😂`")
            return
    
        # Cancel any existing reaction tasks
        for alias, task in list(react_tasks.items()):
            if not task.done():
                task.cancel()
        react_tasks.clear()
    
        target_channel = message.channel
        target_guild = target_channel.guild
    
        async def react_worker(token_info, channel, alias, emoji_list):
            temp_client = discord.Client(self_bot=True)
            try:
                await temp_client.start(token_info["token"])
                # Wait for client to be fully ready
                await temp_client.wait_until_ready()
                print(f"[React] {alias} logged in as {temp_client.user}")
    
                # Verify the alt can see the target channel
                if target_guild:
                    guild = temp_client.get_guild(target_guild.id)
                    if not guild:
                        raise Exception(f"Alt {alias} is not in guild {target_guild.name}. Invite it first.")
                    channel = guild.get_channel(target_channel.id)
                    if not channel:
                        raise Exception(f"Alt {alias} cannot see channel #{target_channel.name}. Check permissions.")
                else:
                    # DM channel – try to fetch/create
                    channel = temp_client.get_channel(target_channel.id)
                    if not channel:
                        user = await temp_client.fetch_user(target_channel.recipient.id)
                        channel = await user.create_dm()
                        print(f"[React] {alias} created DM channel")
    
                # Send a test reaction to confirm connection (optional)
                # await channel.send(" React worker online")  # uncomment if you want a test message
    
                # Now listen for messages from this alt in this channel
                @temp_client.event
                async def on_message(msg):
                    if msg.channel.id != channel.id:
                        return
                    if msg.author == temp_client.user:
                        for e in emoji_list:
                            try:
                                await msg.add_reaction(e)
                                await asyncio.sleep(0.5)
                            except:
                                pass
    
                # Keep the client alive (the event loop runs automatically)
                # We just need to prevent the task from exiting. Use a long-lived await.
                await asyncio.Event().wait()  # wait forever (task will be cancelled on .reactallstop)
            except asyncio.CancelledError:
                print(f"[React] {alias} task cancelled")
            except Exception as e:
                print(f"[React] {alias} error: {e}")
                await message.channel.send(f" **{alias}** error: {e}")
            finally:
                await temp_client.close()
    
        for token_info in token_pool:
            alias = token_info.get("alias", "unknown")
            task = asyncio.create_task(react_worker(token_info, target_channel, alias, emojis))
            react_tasks[alias] = task
            await asyncio.sleep(1)  # small delay between starting workers
    
        await message.channel.send(f" Auto-reaction started for {len(token_pool)} token(s) with emojis: {' '.join(emojis)}")

    elif cmd == ".reactallstop":
        if not react_tasks:
            await message.channel.send("No active reaction tasks to stop.")
            return
        count = 0
        for alias, task in list(react_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        react_tasks.clear()
        await message.channel.send(f" Stopped {count} reaction task(s).")
        
    elif cmd == ".mimic" and len(args) == 1:
        global mimic_enabled
        if args[0].lower() == "on":
            if not token_pool:
                await message.channel.send("No tokens loaded. Use `.host` first.")
                return
            if mimic_enabled:
                await message.channel.send("Mimic already ON.")
                return
            mimic_enabled = True

            # Cancel any existing mimic tasks
            for alias, task in list(mimic_tasks.items()):
                if not task.done():
                    task.cancel()
            mimic_tasks.clear()

            async def mimic_worker(token_info, alias):
                temp_client = discord.Client(self_bot=True)
                try:
                    await temp_client.start(token_info["token"])
                    print(f"[Mimic] {alias} logged in as {temp_client.user}")
                    @temp_client.event
                    async def on_message(msg):
                        if not mimic_enabled:
                            return
                        # If the message author is the main bot (self-bot) and not from the mimic client itself
                        if msg.author.id == client.user.id and msg.author != temp_client.user:
                            try:
                                # Send the same message to the same channel
                                await msg.channel.send(msg.content)
                                print(f"[Mimic] {alias} echoed: {msg.content[:50]}")
                            except Exception as e:
                                print(f"[Mimic] {alias} failed: {e}")
                    while True:
                        await asyncio.sleep(5)
                except asyncio.CancelledError:
                    print(f"[Mimic] {alias} cancelled")
                    await temp_client.close()
                    raise
                except Exception as e:
                    print(f"[Mimic] {alias} error: {e}")
                finally:
                    await temp_client.close()

            for token_info in token_pool:
                alias = token_info.get("alias", "unknown")
                task = asyncio.create_task(mimic_worker(token_info, alias))
                mimic_tasks[alias] = task

            await message.channel.send(f" Mimic mode ON – {len(token_pool)} tokens will copy your messages.")

        elif args[0].lower() == "off":
            if not mimic_enabled:
                await message.channel.send("Mimic was not ON.")
                return
            mimic_enabled = False
            for alias, task in list(mimic_tasks.items()):
                if not task.done():
                    task.cancel()
            mimic_tasks.clear()
            await message.channel.send(" Mimic mode OFF. All mimic tasks stopped.")
        else:
            await message.channel.send("Usage: `.mimic on` or `.mimic off`")
        
    elif cmd == ".listtokens":
        if token_pool:
            names = [f"{t.get('alias','unknown')}" for t in token_pool]
            await message.channel.send(f"Loaded tokens: {', '.join(names)}")
        else:
            await message.channel.send("No tokens loaded")

    elif cmd == ".host" and len(args) == 1:
        new_token = args[0]
        headers = {"Authorization": new_token}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                    if resp.status == 200:
                        user_data = await resp.json()
                        token_pool.append({
                            "token": new_token,
                            "alias": user_data.get("username", f"token{len(token_pool)+1}"),
                            "user_id": int(user_data.get("id"))   # <-- convert to int
                        })
                        await message.channel.send(f"Hosted **{user_data.get('username')}**. Total: {len(token_pool)}")
                    else:
                        await message.channel.send(f" Invalid token (HTTP {resp.status})")
            except Exception as e:
                await message.channel.send(f" Error: {e}")
                
    elif cmd == ".anti" and len(args) == 1:
        anti_target_channel = int(args[0])
        await message.channel.send(f"Anti AFK monitoring channel {anti_target_channel}")

    elif cmd == ".offanti":
        anti_target_channel = None
        await message.channel.send(" disabled ")

    elif cmd == ".spamall":
        # Parse arguments: [channel_id] [message] or [message] (current channel)
        target_channel_id = None
        msg_start = 0
        if len(args) >= 1 and args[0].isdigit():
            target_channel_id = int(args[0])
            msg_start = 1
        else:
            target_channel_id = message.channel.id
            msg_start = 0
        spam_msg = " ".join(args[msg_start:])
        if not spam_msg:
            await message.channel.send("Usage: `.spamall <message>` or `.spamall <channel_id> <message>`")
            return
    
        if not token_pool:
            await message.channel.send("No tokens loaded. Use `.host <token>` first.")
            return
    
        # Optional: allow custom delay (e.g., .spamall 2 hello world)
        # If first arg is a float, treat as delay (override default)
        delay = spamall_interval
        # (We'll keep it simple; use fixed delay. You can add .spamall <delay> <msg> later)
    
        # Cancel any existing spamall tasks
        for alias, task in list(spamall_tasks.items()):
            if not task.done():
                task.cancel()
        spamall_tasks.clear()
    
        async def spam_worker(token_info, channel_id, alias, msg, interval):
            token = token_info["token"]
            headers = {"Authorization": token, "Content-Type": "application/json"}
            url = f"https://discord.com/api/v9/channels/{channel_id}/messages"
            try:
                async with aiohttp.ClientSession() as session:
                    # Verify token
                    async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                        if resp.status != 200:
                            print(f"[Spam] {alias} token invalid: HTTP {resp.status}")
                            return
                        user_data = await resp.json()
                        print(f"[Spam] {alias} authenticated as {user_data['username']}")
    
                    # Main spam loop
                    while True:
                        await asyncio.sleep(0)  # cancellation point
                        payload = {"content": msg}
                        async with session.post(url, json=payload, headers=headers) as resp:
                            if resp.status not in (200, 204):
                                print(f"[Spam] {alias} send failed: {resp.status}")
                            else:
                                print(f"[Spam] {alias} sent: {msg}")
                        await asyncio.sleep(interval)
            except asyncio.CancelledError:
                print(f"[Spam] {alias} task cancelled")
            except Exception as e:
                print(f"[Spam] {alias} error: {e}")
                await message.channel.send(f" **{alias}** spam error: {e}")
    
        for token_info in token_pool:
            alias = token_info.get("alias", "unknown")
            task = asyncio.create_task(spam_worker(token_info, target_channel_id, alias, spam_msg, spamall_interval))
            spamall_tasks[alias] = task
            await asyncio.sleep(1)  # slight delay between starting workers
    
        await message.channel.send(f" Spamall started with {len(token_pool)} token(s) in <#{target_channel_id}>: `{spam_msg[:50]}`")
    
    elif cmd == ".spamallstop":
        if not spamall_tasks:
            await message.channel.send("No active spamall tasks to stop.")
            return
        count = 0
        for alias, task in list(spamall_tasks.items()):
            if not task.done():
                task.cancel()
                count += 1
        spamall_tasks.clear()
        await message.channel.send(f" Stopped {count} spamall task(s).")

    elif cmd == ".joinall" and len(args) >= 1:
        # Extract invite code from full link or just the code
        invite_input = args[0]
        # Match discord.gg/xxxx, discord.com/invite/xxxx, or just xxxx
        import re
        match = re.search(r'(?:discord(?:(?:app)?\.com|\.gg)/invite/|discord\.gg/)([a-zA-Z0-9_-]+)', invite_input)
        if match:
            code = match.group(1)
        else:
            # Assume the input is already the code (e.g., "supreme")
            code = invite_input
    
        if not token_pool:
            await message.channel.send("No tokens loaded. Use `.host <token>` first.")
            return
    
        results = []
        async with aiohttp.ClientSession() as session:
            for token_info in token_pool:
                alias = token_info.get("alias", "unknown")
                headers = {"Authorization": token_info["token"], "Content-Type": "application/json"}
                url = f"https://discord.com/api/v9/invites/{code}"
                try:
                    async with session.post(url, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            guild_name = data.get("guild", {}).get("name", "Unknown server")
                            results.append(f" **{alias}** joined `{guild_name}`")
                        elif resp.status == 400:
                            results.append(f" **{alias}** – Invalid invite or already in server")
                        elif resp.status == 404:
                            results.append(f" **{alias}** – Invite link expired or invalid")
                        else:
                            results.append(f" **{alias}** – Failed (HTTP {resp.status})")
                except Exception as e:
                    results.append(f" **{alias}** – Error: {e}")
                await asyncio.sleep(0.5)  # slight delay to avoid rate limits
    
        # Send results in chunks to avoid message length limit
        full_msg = "\n".join(results)
        if len(full_msg) > 1900:
            for i in range(0, len(results), 15):
                chunk = "\n".join(results[i:i+15])
                await message.channel.send(chunk)
        else:
            await message.channel.send(full_msg)

    elif cmd == ".vcspam" and len(args) == 2:
        ch_id = int(args[0])
        loops = int(args[1])
        channel = client.get_channel(ch_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            await message.channel.send("Invalid voice channel ID.")
            return
        for _ in range(loops):
            try:
                vc = await channel.connect()
                await asyncio.sleep(1)
                await vc.disconnect()
                await asyncio.sleep(1)
            except:
                pass
        await message.channel.send(f"jvc done in {channel.name}.")

    elif cmd == ".upload" and len(args) == 1:
        url = args[0]
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    # Get filename from URL
                    filename = url.split('/')[-1] or "downloaded_file"
                    await message.channel.send(file=discord.File(fp=data, filename=filename))
                else:
                    await message.channel.send(f"Failed to download (HTTP {resp.status})")

    elif cmd == ".linkgen" and len(args) >= 1:
        name = args[0].lower().replace(" ", "_")
        domains = [
            "https://{}.github.io",           # GitHub Pages
            "https://{}.vercel.app",          # Vercel
            "https://{}.netlify.app",         # Netlify
            "https://{}.herokuapp.com",       # Heroku (deprecated but still works)
            "https://{}.replit.app",          # Replit
            "https://{}.glitch.me",           # Glitch
            "https://{}.codepen.io",          # CodePen
            "https://{}.discord.com/users/",  # Discord user ID? not ideal
            "https://www.{}.com",             # generic .com
            "https://{}.xyz",                 # .xyz domain
            "https://{}.blog",                # .blog domain
            "https://linktr.ee/{}",           # Linktree
            "https://{}.substack.com",        # Substack
            "https://{}.medium.com",          # Medium
            "https://dev.to/{}",              # Dev.to
            "https://{}.hashnode.dev",        # Hashnode
            "https://{}.wixsite.com",         # Wix
            "https://{}.wordpress.com",       # WordPress
            "https://{}.tumblr.com",          # Tumblr
            "https://{}.bandcamp.com",        # Bandcamp
            "https://{}.soundcloud.com",      # SoundCloud
            "https://{}.twitch.tv",           # Twitch
            "https://{}.youtube.com/c/",      # YouTube custom URL
            "https://instagram.com/{}",       # Instagram
            "https://twitter.com/{}",         # Twitter
            "https://facebook.com/{}",        # Facebook
            "https://t.me/{}",                # Telegram
            "https://wa.me/{}",               # WhatsApp (requires number, not name)
            "https://discord.gg/{}"           # Discord invite (requires code, not name)
        ]
        paths = ["", "/profile", "/watch", "/home", "/bio", "/contact", "/view"] 
        domain_template = random.choice(domains)
        # Special handling for domains that need extra formatting
        if "users/" in domain_template or "discord.gg/" in domain_template:
            link = domain_template.format(name) + random.choice(paths)
        elif "wa.me/" in domain_template:
            link = domain_template.format(name) + random.choice(paths)
        else:
            link = domain_template.format(name) + random.choice(paths)
        await message.channel.send(f" Random link for **{name}**: {link}")

    elif cmd == ".pack" and len(args) >= 4:
        ch_id = int(args[0]); times = int(args[1]); lines = int(args[2]); pack_type = " ".join(args[3:])
        channel = client.get_channel(ch_id)
        if not channel:
            await message.channel.send("Invalid channel")
            return
        for _ in range(times):
            pack_msg = generate_ai_pack(pack_type, lines)
            await channel.send(pack_msg)
            await asyncio.sleep(1)
        await message.channel.send(f"Sent {times} packs to {ch_id}")

    elif cmd == ".nuke" and len(args) == 1:
        server_id = int(args[0])
        result = await nuke_server(server_id)
        await message.channel.send(result)

    elif cmd == ".checktoken" and len(args) == 1:
        test_token = args[0]
        headers = {"Authorization": test_token}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await message.channel.send(f" Token is **VALID**\nUser: `{data['username']}#{data.get('discriminator', '0')}`\nID: `{data['id']}`")
                else:
                    await message.channel.send(f" Token is **INVALID** (HTTP {resp.status})")

    elif cmd == ".snipeset":
        snipe_enabled.add(message.channel.id)
        await message.channel.send("Snipe enabled in this chat")
        
    elif cmd == ".snipestop":
        if message.channel.id in snipe_enabled:
            snipe_enabled.remove(message.channel.id)
            await message.channel.send("Snipe disabled here")

    elif cmd == ".date":
        now = datetime.now()
        date_str = now.strftime("%A, %B %d, %Y")
        await message.channel.send(f" **Today is** {date_str}")

    elif cmd == ".uptime":
        uptime_seconds = int(time.time() - start_time)
        hours = uptime_seconds // 3600
        minutes = (uptime_seconds % 3600) // 60
        seconds = uptime_seconds % 60
        await message.channel.send(f"Uptime: {hours}h {minutes}m {seconds}s")

    elif cmd == ".ping":
        latency = round(client.latency * 1000)
        await message.channel.send(f"Ping is {latency}ms")

# ========== MENU PAGINATION ==========
menu_pages = []
current_menu_page = 0

def build_menu_pages():
    commands_list = [
        (".ab", ".ab <channel_id> <delay> <file.txt>"),
        (".abstop", ".abstop <channel_id>"),
        (".startnames", ".startnames name1,name2,name3"),
        (".stopnames", "No arguments"),
        (".spam", ".spam <message>"),
        (".spamall", ".spamall <message>"),
        (".stopspam", "No arguments"),
        (".check", ".check @user <limit>"),
        (".stopafk", "No arguments"),
        (".wordlist", ".wordlist <name>"),
        (".wordlists", "No arguments"),
        (".importwl", ".importwl <name> (then upload .txt manually)"),
        (".autopaste", ".autopaste <channel_id> <delay> <message>"),
        (".autopastelist", ".autopastelist <channel_id>"),
        (".autopasteremove", ".autopasteremove <channel_id> <index>"),
        (".stopautopaste", ".stopautopaste <channel_id>"),
        (".stam", ".stam <channel_id> <delay> <message>"),
        (".stamlist", ".stamlist <channel_id>"),
        (".stamremove", ".stamremove <channel_id> <index>"),
        (".stopstam", ".stopstam <channel_id>"),
        (".autocount", ".autocount <channel_id> <start> [end]"),
        (".count", ".count <channel_id> <start>"),
        (".stopac", "No arguments"),
        (".react", ".react <emoji1> <emoji2> ..."),
        (".stopreact", "No arguments"),
        (".stream", ".stream Title1,Title2,..."),
        (".streamend", "No arguments"),
        (".ar", ".ar @user <channel_id> <reply_message>"),
        (".sar", "No arguments"),
        (".ar2", ".ar2 @user <number_of_messages> <message>"),
        (".gcname", ".gcname <channel_id> <delay> <new_name>"),
        (".stopgc", "No arguments"),
        (".lockgc", ".lockgc <channel_id> <locked_name>"),
        (".agct", "No arguments (stub)"),
        (".purge", ".purge <amount> [channel_id]"),
        (".aball", "No arguments (requires hosted tokens)"),
        (".aballstop", "No arguments (stub)"),
        (".streamall", "No arguments"),
        (".reactall", ".reactall <emoji>"),
        (".reactallstop", "No arguments"),
        (".mimic on/off", "No arguments"),
        (".listtokens", "No arguments"),
        (".checktoken", ".checktoken <token>"),
        (".host", ".host <token>"),
        (".anti", ".anti <channel_id>"),
        (".offanti", "No arguments"),
        (".pack", ".pack <channel_id> <times> <lines> <pack_type>"),
        (".nuke", ".nuke <server_id>"),
        (".uptime", "No arguments"),
        (".date", "No arguments"),
        (".snipeset", "No arguments"),
        (".snipestop", "No arguments>"),
        (".joinall", ".joinall <server_link/alias/invite>"),
        (".vcspam", "No arguments"),
        (".archive", "No arguments(for exporting chat)"),
        (".upload", "No arguments(for uploading files)"),
        (".linkgen", ".linkgen <name>"),
        (".ping", "No arguments"),
        (".menu", "No arguments"),
    ]
    per_page = 10
    pages = []
    for i in range(0, len(commands_list), per_page):
        pages.append(commands_list[i:i+per_page])
    return pages

menu_pages = build_menu_pages()

async def show_menu_page(channel, page_num):
    if page_num < 0 or page_num >= len(menu_pages):
        return
    page = menu_pages[page_num]
    msg = "## =============== Supreme/2295 Tool  (page {}/{}) ===============".format(page_num+1, len(menu_pages))
    for cmd, desc in page:
        msg += f"```{cmd} – {desc}```\n"
    msg += "\nUse `.n` for next page, `.p` for previous page"
    await channel.send(msg)

async def next_menu_page(channel):
    global current_menu_page, menu_pages
    if current_menu_page + 1 < len(menu_pages):
        current_menu_page += 1
        await show_menu_page(channel, current_menu_page)
    else:
        await channel.send("Already on last page")

async def prev_menu_page(channel):
    global current_menu_page, menu_pages
    if current_menu_page - 1 >= 0:
        current_menu_page -= 1
        await show_menu_page(channel, current_menu_page)
    else:
        await channel.send("Already on first page")

# ========== RUN ==========
if __name__ == "__main__":
    if not TOKEN:
        print(" Set TOKEN environment variable")
    else:
        client.run(TOKEN)
