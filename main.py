import os
import re
import shutil
import subprocess
import asyncio
from typing import Optional, Dict, Any
from pymongo import MongoClient
from pyrogram import Client, filters
from pyrogram.types import Message, ForceReply

# ---------------- CONFIG ----------------
BOT_TOKEN = ""
API_ID = 
API_HASH = ""
LOG_CHANNEL = 
MONGO_URL = ""

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ---------------- MONGODB ----------------
mongo_client = MongoClient(MONGO_URL)
db = mongo_client["rename_bot"]
sessions_col = db["sessions"]
metadata_col = db["metadata"]
thumbnails_col = db["thumbnails"]

# ---------------- LOCKS ----------------
PROCESSING_LOCKS: Dict[int, asyncio.Lock] = {}

# ---------------- HELPERS ----------------
def parse_filename(filename: str):
    s, e, q = None, None, None
    patterns = [r"[Ss](\d{1,2})[Ee](\d{1,2})", r"(\d{1,2})[xX](\d{1,2})", r"[Ee](\d{1,2})"]
    for p in patterns:
        m = re.search(p, filename)
        if m:
            if len(m.groups()) == 2:
                s, e = m.group(1).zfill(2), m.group(2).zfill(2)
            else:
                e = m.group(1).zfill(2)
            break
    mq = re.search(r"(\d{3,4}p|2k|4k|480p|720p|1080p|360p|2160p)", filename, flags=re.IGNORECASE)
    if mq:
        q = mq.group(1).lower()
        if q == "360p": q = "480p"
    return {"sn": s, "ep": e, "quality": q}

def normalize_quality(q: Optional[str]) -> Optional[str]:
    if not q: return None
    q = q.lower()
    if q == "360p": return "480p"
    return q

def build_new_filename(fmt: str, ep: Optional[str], sn: Optional[str], quality: Optional[str]):
    ep_val = ep.zfill(2) if ep and ep.isdigit() else (ep or "")
    sn_val = sn.zfill(2) if sn and sn.isdigit() else (sn or "")
    quality_val = quality or ""
    out = fmt
    out = out.replace("{ep}", ep_val).replace("{Sn}", sn_val).replace("{quality}", quality_val)
    out = re.sub(r"\s+", " ", out).strip()
    return out

def _user_temp_dir(user_id: int) -> str:
    d = os.path.join(DOWNLOAD_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d

async def apply_metadata(src_file, dst_file, title, audio_title=None, subtitle_path=None):
    loop = asyncio.get_event_loop()
    def _ffmpeg_run():
        cmd = ["ffmpeg", "-y", "-i", src_file, "-map", "0", "-c", "copy", "-metadata", f"title={title}"]
        if audio_title:
            cmd += ["-metadata:s:a:0", f"title={audio_title}"]
        if subtitle_path and os.path.exists(subtitle_path):
            cmd += ["-i", subtitle_path, "-c:s", "mov_text"]
        cmd += [dst_file]
        subprocess.run(cmd, check=True)
    try:
        await loop.run_in_executor(None, _ffmpeg_run)
        return True
    except Exception as e:
        print("Metadata error:", e)
        try:
            shutil.copy(src_file, dst_file)
            return True
        except Exception as e2:
            print("Fallback copy failed:", e2)
            return False

async def cleanup_file(path):
    try: os.remove(path)
    except: pass

# ---------------- DATABASE OPERATIONS ----------------
async def create_session(user_id: int):
    PROCESSING_LOCKS.setdefault(user_id, asyncio.Lock())
    sessions_col.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id, "thumbnail": None, "metadata": None, "format": None, "episodes": [], "processing": False}},
        upsert=True
    )

async def get_session(user_id: int):
    return sessions_col.find_one({"user_id": user_id})

async def update_session(user_id: int, update: Dict[str, Any]):
    sessions_col.update_one({"user_id": user_id}, {"$set": update})

async def add_episode_entry(user_id: int, entry: Dict[str, Any]):
    sessions_col.update_one({"user_id": user_id}, {"$push": {"episodes": entry}})

async def delete_session(user_id: int):
    sessions_col.delete_one({"user_id": user_id})

async def set_processing(user_id: int, flag: bool):
    await update_session(user_id, {"processing": flag})

async def save_user_metadata(user_id: int, text: str):
    metadata_col.update_one({"user_id": user_id}, {"$set": {"metadata": text}}, upsert=True)

async def get_user_metadata(user_id: int) -> Optional[str]:
    doc = metadata_col.find_one({"user_id": user_id})
    return doc.get("metadata") if doc else None

async def save_user_thumbnail(user_id: int, path: str):
    thumbnails_col.update_one({"user_id": user_id}, {"$set": {"thumbnail": path}}, upsert=True)

async def get_user_thumbnail(user_id: int) -> Optional[str]:
    doc = thumbnails_col.find_one({"user_id": user_id})
    return doc.get("thumbnail") if doc else None

async def remove_user_thumbnail(user_id: int):
    thumbnails_col.delete_one({"user_id": user_id})

# ---------------- PROGRESS ----------------
async def progress(current, total, chat_id, message_id, prefix="Progress"):
    now = int(current / total * 100)
    text = f"{prefix}: {now}% [{current}/{total}]"
    try:
        await app.edit_message_text(chat_id, message_id, text)
    except: pass

# ---------------- BOT ----------------
app = Client("rename_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------------- COMMANDS ----------------
@app.on_message(filters.private & filters.command("start"))
async def start_handler(client, message):
    await message.reply_text(
        "👋 Hello! I am your Rename Bot.\n\n"
        "Commands:\n"
        "/auto_rename - Start automatic rename session\n"
        "/view_thumb - View saved thumbnail\n"
        "/delete_thumb - Delete saved thumbnail\n"
        "/metadata - Set metadata\n"
        "/rename_all - Process auto rename queue\n"
        "/reset_session - Reset all saved data"
    )

@app.on_message(filters.command("view_thumb") & filters.private)
async def view_thumb(client, message):
    thumb = await get_user_thumbnail(message.from_user.id)
    if thumb and os.path.exists(thumb):
        await client.send_photo(message.chat.id, thumb, caption="📸 Saved Thumbnail")
    else:
        await message.reply_text("❌ No thumbnail saved.")

@app.on_message(filters.command("delete_thumb") & filters.private)
async def delete_thumb(client, message):
    await remove_user_thumbnail(message.from_user.id)
    await message.reply_text("✅ Thumbnail deleted.")

@app.on_message(filters.command("reset_session") & filters.private)
async def reset_session(client, message):
    uid = message.from_user.id
    await delete_session(uid)
    await remove_user_thumbnail(uid)
    await save_user_metadata(uid, "")
    await message.reply_text("✅ All session data reset.")

@app.on_message(filters.command("metadata") & filters.private)
async def set_metadata(client, message):
    uid = message.from_user.id
    text = message.text.split(None, 1)
    if len(text) < 2:
        await message.reply_text("Send as: /metadata Your Metadata Here")
        return
    await save_user_metadata(uid, text[1])
    await message.reply_text("✅ Metadata saved!")

# ---------------- MANUAL RENAME ----------------
@app.on_message(filters.private & (filters.document | filters.video))
async def manual_rename(client, message: Message):
    media = getattr(message, message.media.value)
    orig_name = getattr(media, "file_name", f"file_{message.id}")
    await message.reply_text(
        f"**Please Enter New Filename...**\n\n**Old File Name:** `{orig_name}`",
        reply_markup=ForceReply(True)
    )

@app.on_message(filters.private & filters.reply)
async def manual_reply(client, message: Message):
    reply = message.reply_to_message
    if not reply or not isinstance(reply.reply_markup, ForceReply):
        return
    new_name = message.text
    await message.delete()
    media = getattr(reply, reply.media.value)
    ext = os.path.splitext(media.file_name)[1] or ".mkv"
    if not new_name.endswith(ext):
        new_name += ext
    tmpdir = _user_temp_dir(message.from_user.id)
    dl_path = os.path.join(tmpdir, f"{new_name}")
    await client.download_media(media.file_id, file_name=dl_path)
    metadata_title = await get_user_metadata(message.from_user.id)
    out_path = os.path.join(tmpdir, f"renamed_{new_name}")
    thumb = await get_user_thumbnail(message.from_user.id)
    await apply_metadata(dl_path, out_path, title=new_name, audio_title=metadata_title)
    await client.send_video(message.chat.id, out_path, caption=f"**{new_name}**",
                            supports_streaming=True, thumb=thumb)
    await client.send_video(LOG_CHANNEL, out_path, caption=f"**{new_name}**",
                            supports_streaming=True, thumb=thumb)
    await cleanup_file(dl_path)
    await cleanup_file(out_path)

# ---------------- AUTO RENAME ----------------
@app.on_message(filters.command("auto_rename") & filters.private)
async def cmd_auto_rename(client, message: Message):
    uid = message.from_user.id
    await create_session(uid)
    await message.reply_text("📸 Send thumbnail for auto rename or /skip to continue without it.")

@app.on_message(filters.photo & filters.private)
async def auto_thumb_save(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session: return
    temp = _user_temp_dir(uid)
    thumb_path = os.path.join(temp, "thumb.jpg")
    await message.download(file_name=thumb_path)
    await save_user_thumbnail(uid, thumb_path)
    await message.reply_text("✅ Thumbnail saved! Now send metadata using /metadata command.")

@app.on_message(filters.command("skip") & filters.private)
async def skip_thumb(client, message: Message):
    await message.reply_text("✅ Skipped thumbnail. Send metadata next using /metadata command.")

@app.on_message(filters.private & filters.text)
async def auto_text_handler(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session: return
    # Save metadata if not set
    if not session.get("metadata"):
        await save_user_metadata(uid, message.text)
        await message.reply_text("✅ Metadata saved! Now send rename format using {ep} {Sn} {quality}")
        return
    # Save format if not set
    if not session.get("format"):
        fmt = message.text
        await update_session(uid, {"format": fmt})
        await message.reply_text("✅ Format saved! Now upload your video/files.")

@app.on_message(filters.private & (filters.document | filters.video))
async def auto_file_handler(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session or not session.get("format"):
        await message.reply_text("❗ Set format first with /auto_rename and send format text.")
        return
    media = getattr(message, message.media.value)
    orig_fname = getattr(media, "file_name", None) or f"file_{message.id}"
    parsed = parse_filename(orig_fname)
    ep = parsed.get("ep")
    sn = parsed.get("sn")
    quality = normalize_quality(parsed.get("quality")) or "480p"
    file_id = media.file_id

    entry = {
        "ep": ep or "",
        "sn": sn or "",
        "quality": quality,
        "file_id": file_id,
        "orig_name": orig_fname,
        "state": "pending"
    }
    await add_episode_entry(uid, entry)
    display_ep = ep if ep else "Unknown"
    await message.reply_text(f"📥 Saved Episode {display_ep} • {quality}")

@app.on_message(filters.command("rename_all") & filters.private)
async def cmd_rename_all(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session or not session.get("episodes"):
        return await message.reply_text("❗ No active session or episodes to rename.")

    lock = PROCESSING_LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return await message.reply_text("⚠️ Rename already in progress. Wait.")

    await message.reply_text(f"🚀 Starting rename for {len(session.get('episodes', []))} items...")

    async with lock:
        await set_processing(uid, True)
        try:
            await _process_session(client, uid, message)
        finally:
            await set_processing(uid, False)
            await delete_session(uid)
            await message.reply_text("✅ All episodes renamed and uploaded successfully!")

# ---------------- PROCESSING ----------------
async def _process_session(client, user_id: int, trigger_message: Message):
    session = await get_session(user_id)
    if not session:
        return
    episodes = session.get("episodes", [])
    for i, entry in enumerate(episodes, 1):
        if entry.get("state") != "pending":
            continue
        try:
            await _process_single_entry(client, user_id, session, entry, trigger_message, index=i, total=len(episodes))
            entry["state"] = "done"
        except Exception as e:
            print("Error processing entry:", e)
            entry["state"] = "failed"

async def _process_single_entry(client, user_id: int, session: Dict[str, Any], entry: Dict[str, Any], trigger_message: Message, index=1, total=1):
    file_id = entry.get("file_id")
    if not file_id:
        return

    tmpdir = _user_temp_dir(user_id)
    orig_name = entry.get("orig_name") or "file"
    ext = os.path.splitext(orig_name)[1] or ".mkv"
    dl_path = os.path.join(tmpdir, f"dl_{entry.get('ep')}_{entry.get('quality')}{ext}")

    # Download with progress
    msg = await client.send_message(user_id, f"⬇️ Downloading {orig_name} ({index}/{total})")
    await client.download_media(file_id, file_name=dl_path, progress=lambda c, t: asyncio.create_task(progress(c, t, user_id, msg.id, "Downloading")))
    await msg.edit_text(f"✅ Downloaded {orig_name}")

    # Build new filename
    fmt = session.get("format") or "{ep} {quality}"
    new_name = build_new_filename(fmt, entry.get("ep"), entry.get("sn"), entry.get("quality"))
    if not os.path.splitext(new_name)[1]:
        new_name += ext
    out_path = os.path.join(tmpdir, f"renamed_{new_name}")

    metadata_title = session.get("metadata") or ""
    thumb = session.get("thumbnail")

    # Apply metadata
    msg_meta = await client.send_message(user_id, f"🔧 Applying metadata to {new_name}")
    await apply_metadata(dl_path, out_path, title=new_name, audio_title=metadata_title)
    await msg_meta.edit_text(f"✅ Metadata applied for {new_name}")

    caption = f"**{new_name}**"

    # Send to user and log channel
    if ext.lower() in (".mp4", ".mkv", ".mov", ".webm", ".avi"):
        msg_upload = await client.send_message(user_id, f"⬆️ Uploading {new_name}")
        await client.send_video(user_id, out_path, thumb=thumb if thumb and os.path.exists(thumb) else None, caption=caption, supports_streaming=True)
        await client.send_video(LOG_CHANNEL, out_path, thumb=thumb if thumb and os.path.exists(thumb) else None, caption=caption, supports_streaming=True)
        await msg_upload.edit_text(f"✅ Uploaded {new_name}")
    else:
        await client.send_document(user_id, out_path, thumb=thumb if thumb and os.path.exists(thumb) else None, caption=caption)
        await client.send_document(LOG_CHANNEL, out_path, thumb=thumb if thumb and os.path.exists(thumb) else None, caption=caption)

    # Cleanup
    await cleanup_file(dl_path)
    await cleanup_file(out_path)

# ---------------- RUN BOT ----------------
print("✅ Bot is starting...")
app.run()
