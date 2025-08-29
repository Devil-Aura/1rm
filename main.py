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
BOT_TOKEN = "YOUR_BOT_TOKEN"
API_ID = 123456
API_HASH = "YOUR_API_HASH"
LOG_CHANNEL = -1001234567890
MONGO_URL = "YOUR_MONGO_URL"

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
    out = fmt.replace("{ep}", ep_val).replace("{Sn}", sn_val).replace("{quality}", quality_val)
    return re.sub(r"\s+", " ", out).strip()

def _user_temp_dir(user_id: int) -> str:
    d = os.path.join(DOWNLOAD_DIR, str(user_id))
    os.makedirs(d, exist_ok=True)
    return d

async def cleanup_file(path):
    try: os.remove(path)
    except: pass

# ---------------- DATABASE ----------------
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
def progress_bar(current, total, length=20):
    percent = int(current / total * 100)
    bar = "█" * (percent // (100 // length)) + "░" * (length - percent // (100 // length))
    return f"[{bar}] {percent}%"

async def download_with_progress(client, file_id, file_path, chat_id):
    msg = await client.send_message(chat_id, f"⬇️ Starting download...")
    def callback(current, total):
        bar = progress_bar(current, total)
        asyncio.create_task(client.edit_message_text(chat_id, msg.message_id, f"⬇️ Downloading...\n{bar}"))
    await client.download_media(file_id, file_name=file_path, progress=callback)
    await msg.edit_text("✅ Download completed!")
    return msg

async def upload_with_progress(client, chat_id, file_path, caption, thumb):
    msg = await client.send_message(chat_id, "🚀 Starting upload...")
    def callback(current, total):
        bar = progress_bar(current, total)
        asyncio.create_task(client.edit_message_text(chat_id, msg.message_id, f"🚀 Uploading...\n{bar}"))
    ext = os.path.splitext(file_path)[1].lower()
    if ext in [".mp4", ".mkv", ".mov", ".webm", ".avi"]:
        await client.send_video(chat_id, file_path, caption=caption, thumb=thumb, supports_streaming=True, progress=callback)
    else:
        await client.send_document(chat_id, file_path, caption=caption, thumb=thumb, progress=callback)
    await msg.delete()

async def apply_metadata_with_progress(src, dst, title, audio_title=None, chat_id=None):
    loop = asyncio.get_event_loop()
    def _ffmpeg_run():
        cmd = ["ffmpeg", "-y", "-i", src, "-map", "0", "-c", "copy", "-metadata", f"title={title}"]
        if audio_title:
            cmd += ["-metadata:s:a:0", f"title={audio_title}"]
        cmd += [dst]
        subprocess.run(cmd, check=True)
    if chat_id:
        msg = await app.send_message(chat_id, "⏳ Applying metadata...")
        await loop.run_in_executor(None, _ffmpeg_run)
        await msg.edit_text("✅ Metadata applied!")
    else:
        await loop.run_in_executor(None, _ffmpeg_run)

# ---------------- CLIENT ----------------
app = Client("rename_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ---------------- START ----------------
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

# ---------------- MANUAL RENAME ----------------
@app.on_message(filters.private & (filters.document | filters.video))
async def manual_rename(client, message: Message):
    media = message.document or message.video
    orig_name = getattr(media, "file_name", f"file_{message.message_id}")
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
    media = reply.document or reply.video
    ext = os.path.splitext(getattr(media, "file_name", ""))[1] or ".mkv"
    if not new_name.endswith(ext):
        new_name += ext

    tmpdir = _user_temp_dir(message.from_user.id)
    dl_path = os.path.join(tmpdir, f"{new_name}")
    await download_with_progress(client, media.file_id, dl_path, message.chat.id)

    metadata_title = await get_user_metadata(message.from_user.id)
    out_path = os.path.join(tmpdir, f"renamed_{new_name}")
    thumb = await get_user_thumbnail(message.from_user.id)
    await apply_metadata_with_progress(dl_path, out_path, title=new_name, audio_title=metadata_title, chat_id=message.chat.id)
    
    # Upload to log channel first
    await upload_with_progress(client, LOG_CHANNEL, out_path, f"**{new_name}**", thumb)
    await upload_with_progress(client, message.chat.id, out_path, f"**{new_name}**", thumb)
    
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
    await message.reply_text("✅ Thumbnail saved! Now send metadata.")

@app.on_message(filters.command("skip") & filters.private)
async def skip_thumb(client, message: Message):
    await message.reply_text("✅ Skipped thumbnail. Now send metadata.")

@app.on_message(filters.text & filters.private)
async def auto_text_handler(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session: return

    if not session.get("metadata"):
        await save_user_metadata(uid, message.text)
        await update_session(uid, {"metadata": message.text})
        await message.reply_text("✅ Metadata saved! Now send rename format using {ep}, {Sn}, {quality}.")
        return

    if not session.get("format"):
        fmt = message.text
        await update_session(uid, {"format": fmt})
        await message.reply_text("✅ Format saved! Now upload your files for auto rename.")

# ---------------- AUTO FILE HANDLER ----------------
@app.on_message(filters.private & (filters.document | filters.video))
async def auto_file_handler(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if not session or not session.get("format"):
        await message.reply_text("❗ Set format first with /auto_rename and metadata")
        return

    media = message.document or message.video
    orig_fname = getattr(media, "file_name", f"file_{message.message_id}")
    parsed = parse_filename(orig_fname)
    ep = parsed.get("ep")
    sn = parsed.get("sn")
    quality = normalize_quality(parsed.get("quality")) or "480p"

    entry = {
        "ep": ep or "",
        "sn": sn or "",
        "quality": quality,
        "file_id": media.file_id,
        "orig_name": orig_fname,
        "state": "pending"
    }
    await add_episode_entry(uid, entry)
    display_ep = ep if ep else "Unknown"
    await message.reply_text(f"📥 Saved Episode {display_ep} • {quality} for auto rename.")

# ---------------- RENAME ALL ----------------
@app.on_message(filters.command("rename_all") & filters.private)
async def cmd_rename_all(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    episodes = session.get("episodes", [])
    if not session or not episodes:
        return await message.reply_text("❗ No active session or episodes to rename.")

    lock = PROCESSING_LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return await message.reply_text("⚠️ Rename already in progress. Please wait.")

    await message.reply_text(f"🚀 Starting rename for {len(episodes)} files...")

    async with lock:
        await set_processing(uid, True)
        try:
            for entry in episodes:
                if entry.get("state") != "pending":
                    continue
                try:
                    await process_single_entry(client, uid, session, entry, message.chat.id)
                    entry["state"] = "done"
                except Exception as e:
                    print("Error processing entry:", e)
                    entry["state"] = "failed"
        finally:
            await set_processing(uid, False)
            await delete_session(uid)
            await message.reply_text("✅ All episodes renamed and uploaded successfully!")

# ---------------- PROCESS SINGLE ENTRY ----------------
async def process_single_entry(client, user_id, session, entry, chat_id):
    tmpdir = _user_temp_dir(user_id)
    orig_name = entry.get("orig_name") or "file"
    ext = os.path.splitext(orig_name)[1] or ".mkv"
    dl_path = os.path.join(tmpdir, f"dl_{entry.get('ep')}_{entry.get('quality')}{ext}")

    # Download file with progress
    await download_with_progress(client, entry.get("file_id"), dl_path, chat_id)

    # Build new filename
    fmt = session.get("format") or "{ep} {quality}"
    new_name = build_new_filename(fmt, entry.get("ep"), entry.get("sn"), entry.get("quality"))
    if not os.path.splitext(new_name)[1]:
        new_name += ext
    out_path = os.path.join(tmpdir, f"renamed_{new_name}")

    # Apply metadata with progress
    metadata_title = session.get("metadata") or ""
    thumb = session.get("thumbnail")
    await apply_metadata_with_progress(dl_path, out_path, title=new_name, audio_title=metadata_title, chat_id=chat_id)

    # Upload to log channel first
    await upload_with_progress(client, LOG_CHANNEL, out_path, f"**{new_name}**", thumb if thumb and os.path.exists(thumb) else None)
    # Upload to user
    await upload_with_progress(client, chat_id, out_path, f"**{new_name}**", thumb if thumb and os.path.exists(thumb) else None)

    # Cleanup temp files
    await cleanup_file(dl_path)
    await cleanup_file(out_path)

# ---------------- THUMBNAIL MANAGEMENT ----------------
@app.on_message(filters.command("view_thumb") & filters.private)
async def view_thumbnail(client, message: Message):
    uid = message.from_user.id
    thumb = await get_user_thumbnail(uid)
    if thumb and os.path.exists(thumb):
        await client.send_photo(message.chat.id, thumb, caption="📸 Your saved thumbnail.")
    else:
        await message.reply_text("❌ No thumbnail saved.")

@app.on_message(filters.command("delete_thumb") & filters.private)
async def delete_thumbnail(client, message: Message):
    uid = message.from_user.id
    thumb = await get_user_thumbnail(uid)
    if thumb and os.path.exists(thumb):
        await cleanup_file(thumb)
    await remove_user_thumbnail(uid)
    await message.reply_text("✅ Thumbnail deleted successfully!")

# ---------------- RESET SESSION ----------------
@app.on_message(filters.command("reset_session") & filters.private)
async def reset_session(client, message: Message):
    uid = message.from_user.id
    session = await get_session(uid)
    if session:
        tmpdir = _user_temp_dir(uid)
        try:
            shutil.rmtree(tmpdir)
        except: pass
        await delete_session(uid)
    await remove_user_thumbnail(uid)
    await save_user_metadata(uid, "")
    await update_session(uid, {"format": None})
    await message.reply_text("♻️ Your session has been reset successfully!")

# ---------------- RUN BOT ----------------
print("✅ Bot is starting...")
app.run()
