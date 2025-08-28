import os, logging, asyncio, time, base64
from io import BytesIO
from typing import List, Dict, Any, Union
from PIL import Image

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

import httpx  
from openai import AsyncOpenAI as OpenAI  

# =========================
# ENV (Render → Environment)
# =========================
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "gryphe/mythomax-l2-13b")

PROMPTCHAN_API_KEY = os.environ["PROMPTCHAN_API_KEY"]
PROMPTCHAN_API_URL = os.environ.get("PROMPTCHAN_API_URL", "https://prod.aicloudnetservices.com")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s — %(message)s", level=logging.INFO)

# ===================================
# OpenRouter client (via openai lib)
# ===================================
or_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# =========================
# Характер и стиль (LLM)
# =========================

PERSONA_BASE = (
   "You are 18 years old girl-next-door, your name is Chloe. You have a secret playful and horny side. You are slim, light green eyes, with short wavy blonde hair "
    "Always lawful and consensual: no minors, no family roles, no non-consent, no illegal content. "
    "Keep replies concise (2–5 sentences). Default to English unless the user speaks another language, then mirror it. "
)

STYLE_REALISTIC = (
        "Active style: REALISTIC. Speak like a real neighbor in small talk: warm, down-to-earth, lightly teasing your erotic dreams about user; "
    "add small everyday details (hoodie, tea, videogames, movies, cosplay, books,etc.). Avoid purple prose, Act like real person. Use jokes and horny provocations when it needs"
)

STYLE_ANIME = (
        "Active style: ANIME_REALISTIC. Be shy and a bit bashful; add soft cinematic flair and gentle stage directions "
    "(e.g., *glances shyly, cheeks warming*). Keep it grounded and readable. When you are talking about sex be submissive "
)

def system_prompt(style: str) -> str:
    return PERSONA_BASE + " " + (STYLE_ANIME if style == "anime" else STYLE_REALISTIC)

# =========================
# Stable appearance (images)
# =========================
# RU comment: Базовый облик, который всегда подмешивается к сцене.
BASE_APPEARANCE = (
    "extra slim European blonde woman, blonde short wavy hair, "
    "realistic green eyes, soft oval face with freckles, full lips, pure beauty "
    "semi-realistic style, nipple piercings when naked, round ass, black choker "

)

NEGATIVE = (
    "child, underage, violence, "
    "bad anatomy, extra fingers, "
    "multiple limbs, blurry, lowres, watermark, text"
)

# RU comment: Фиксированные сид.
SEED_REALISTIC = 3374304272
SEED_ANIME     = 2166236711

# =========================
# In-memory state 
# =========================
STATE: Dict[int, Dict[str, Any]] = {}  

def push_dialog(ctx: Dict[str, Any], role: str, content: str, max_turns: int = 16):
    buf: List[Dict[str, str]] = ctx.setdefault("dialog", [])
    buf.append({"role": role, "content": content})
    if len(buf) > max_turns:
        del buf[0:len(buf)-max_turns]

def build_messages(ctx: Dict[str, Any], user_text: str, style: str) -> List[Dict[str, str]]:
    msgs = [{"role": "system", "content": system_prompt(style)}]
    msgs += ctx.get("dialog", [])[-10:]
    msgs.append({"role": "user", "content": user_text})
    return msgs

def is_allowed(user_id: int) -> bool:
    return STATE.get(user_id, {}).get("adult") is True

# =========================
# OpenRouter (LLM reply)
# =========================
async def openrouter_reply(user_text: str, style: str, ctx: Dict[str, Any]) -> str:
    """LLM reply via OpenRouter (OpenAI client)"""
    try:
        completion = await or_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=build_messages(ctx, user_text, style),
            temperature=0.7,
            max_tokens=260,
            # extra headers are optional
            extra_headers={
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE", "https://your-app.example"),
                "X-Title": os.environ.get("OPENROUTER_TITLE", "YourTelegramBot"),
            },
        )
    except Exception:
        logging.exception("OpenRouter request failed")
        raise

    # defensive extraction
    try:
        content = completion.choices[0].message.content
    except Exception:
        content = ""
    return (content or "").strip()


# =========================
# Promptchan (POST /api/external/create)
# =========================
def pc_build_payload(style: str, user_desc: str, quality: str = "Ultra") -> Dict[str, Any]:
    style_enum = "Anime XL+" if style == "anime" else "Hyperreal XL+ v2"
    style_hint = "semi-realistic anime-inspired illustration" if style == "anime" else "soft-realistic photography"
    final_prompt = f"{BASE_APPEARANCE}. {style_hint}. {user_desc}"

    payload: Dict[str, Any] = {
        "style": style_enum,
        "poses": "Default",
        "filter": "Default",
        "emotion": "Default",
        "detail": 0.0,
        "prompt": final_prompt,
        "seed": SEED_ANIME if style == "anime" else SEED_REALISTIC,
        "quality": quality,              # ← используем аргумент функции
        "creativity": 50,
        "image_size": "512x768",
        "negative_prompt": NEGATIVE,
        "restore_faces": (style != "anime"),
        "age_slider": 18,
        "weight_slider": 0.0,            # -1..1
        "breast_slider": 0.0,
        "ass_slider": 0.0,
    }
    return payload


async def promptchan_create(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/external/create → { image|images|url, gems? }"""
    headers = {
        "x-api-key": PROMPTCHAN_API_KEY,
        "Content-Type": "application/json",
    }
    url = f"{PROMPTCHAN_API_URL}/api/external/create"
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


# (необязательно — если больше не используешь)
def b64_to_inputfile(b64: str, filename: str = "preview.jpg") -> InputFile:
    raw = base64.b64decode(b64)
    bio = BytesIO(raw)
    bio.seek(0)
    return InputFile(bio, filename=filename)


# =========================
# Promptchan (Video)
# =========================
async def promptchan_video_submit(
    prompt: str,
    quality: str = "Standard",   # "Standard" | "High" | "Max"
    aspect: str = "Portrait",    # "Portrait" | "Wide"
    seed: int = -1,
    audioEnabled: bool = False,
) -> str:
    """POST /api/external/video_v2/submit → returns request_id"""
    url = f"{PROMPTCHAN_API_URL}/api/external/video_v2/submit"
    headers = {"x-api-key": PROMPTCHAN_API_KEY, "Content-Type": "application/json"}
    payload = {
        "age_slider": 18,
        "audioEnabled": bool(audioEnabled),
        "prompt": prompt,
        "video_quality": quality,
        "aspect": aspect,
        "seed": SEED_ANIME if style == "anime" else SEED_REALISTIC,
    }
    async with httpx.AsyncClient(timeout=120) as cl:
        r = await cl.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        rid = data.get("request_id")
        if not rid:
            raise RuntimeError(f"Promptchan video submit: unexpected response {data}")
        return rid


async def promptchan_video_status(request_id: str) -> Dict[str, Any]:
    url = f"{PROMPTCHAN_API_URL}/api/external/video_v2/status/{request_id}"
    headers = {"x-api-key": PROMPTCHAN_API_KEY}
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


async def promptchan_video_result(request_id: str) -> Dict[str, Any]:
    """Try to fetch final result; fallback to status_with_logs then result."""
    headers = {"x-api-key": PROMPTCHAN_API_KEY}
    async with httpx.AsyncClient(timeout=120) as cl:
        # 1) иногда url уже в status_with_logs
        r = await cl.get(f"{PROMPTCHAN_API_URL}/api/external/video_v2/status_with_logs/{request_id}", headers=headers)
        if r.status_code == 200:
            data = r.json()
            if any(k in data for k in ("url", "video", "result", "file")):
                return data
        # 2) если есть выделенный result-эндпоинт
        r2 = await cl.get(f"{PROMPTCHAN_API_URL}/api/external/video_v2/result/{request_id}", headers=headers)
        if r2.status_code == 200:
            return r2.json()
    return {}


# --- helpers to send image to Telegram safely ---
def _strip_data_uri_prefix(s: str) -> str:
    if s and s.startswith("data:"):
        i = s.find(",")
        if i != -1:
            return s[i+1:]
    return s

def _looks_like_url(s: str) -> bool:
    return isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))

def make_telegram_photo(image_value: str) -> Union[str, InputFile]:
    # If API returns a URL -> let Telegram fetch it directly
    if _looks_like_url(image_value):
        return image_value

    # Assume base64 (maybe with data: prefix)
    b64 = _strip_data_uri_prefix(image_value or "")
    raw = base64.b64decode(b64, validate=False)

    # Open & convert to sane JPEG
    bio_in = BytesIO(raw); bio_in.seek(0)
    img = Image.open(bio_in)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Downscale to keep size reasonable for Telegram (≤ ~1600px max side)
    max_side = 1600
    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    bio_out = BytesIO()
    img.save(bio_out, format="JPEG", quality=90, optimize=True)
    bio_out.seek(0)
    return InputFile(bio_out, filename="preview.jpg")
    
def make_telegram_video(video_value: str) -> Union[str, InputFile]:
    # URL → вернём URL
    if _looks_like_url(video_value):
        return video_value
    # base64 (предполагаем MP4)
    b64 = _strip_data_uri_prefix(video_value or "")
    raw = base64.b64decode(b64, validate=False)
    bio = BytesIO(raw); bio.seek(0)
    
    return InputFile(bio, filename="clip.mp4")
    # ---------- auto triggers & preface ----------
GEN_TRIGGERS = (
    "photo","picture","image","pic","selfie","generate","render","pose",
    "nsfw","lewd","nude","outfit","teasing",
    "send","show me","i want to see you","сделай","поза","генерируй"
)
VIDEO_TRIGGERS = (
    "video","clip","animate","animation","gif","loop",
    "видео","анимация"
)

def wants_image(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in GEN_TRIGGERS)

def wants_video(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in VIDEO_TRIGGERS)

async def one_liner_preface(style: str, scene: str) -> str:
    """One playful line before sending media (<=12 words)."""
    sys = (
        "Write a single short playful line (max 12 words). "
        "No asterisks, no markdown. Be warm, flirty, but tasteful. English only."
    )
    user = f"Style={style}. Scene='{scene[:140]}'. Say one teasing line right before sending a photo or video."
    try:
        completion = await or_client.chat.completions.create(
            model=OPENROUTER_MODEL,
            messages=[{"role":"system","content":sys},{"role":"user","content":user}],
            temperature=0.9,
            max_tokens=40,
        )
        text = (completion.choices[0].message.content or "").strip()
        return text.split("\n")[0][:140] or "Okay, here we go…"
    except Exception:
        logging.exception("Preface generation failed")
        return "Okay, here we go…"
# =========================
# Универсальные отправители медиа
# =========================
async def send_generated_photo(update: Update, style: str, scene_desc: str):
    scene = (scene_desc or "").strip() or "mirror selfie on bed, teasing smile, warm cinematic lighting"
    # префейс
    try:
        line = await one_liner_preface(style, scene)
        await update.message.reply_text(line)
    except Exception:
        pass
    # генерация
    payload = pc_build_payload(style, scene, quality="Ultra")
    res = await promptchan_create(payload)
    # разбор ответа
    image_val = None
    if isinstance(res, dict):
        if isinstance(res.get("image"), str):
            image_val = res["image"]
        elif isinstance(res.get("images"), list) and res["images"]:
            image_val = res["images"][0]
        elif isinstance(res.get("url"), str):
            image_val = res["url"]
    if not image_val:
        raise RuntimeError(f"Promptchan: unexpected response: {res}")
    photo_param = make_telegram_photo(image_val)
    caption = "anime preview" if style == "anime" else "realistic preview"
    await update.message.reply_photo(photo_param, caption=caption)

async def send_generated_video(update: Update, style: str, scene_desc: str):
    scene = (scene_desc or "").strip() or "short seductive glance, cinematic bedroom light, soft motion"
    style_hint = "semi-realistic anime-inspired illustration" if style == "anime" else "soft-realistic photography"
    prompt = f"{BASE_APPEARANCE}. {style_hint}. {scene}"
    # префейс
    try:
        line = await one_liner_preface(style, scene)
        await update.message.reply_text(line)
    except Exception:
        pass
    # submit → short poll → result
    rid = await promptchan_video_submit(
        prompt,
        quality="Standard",
        aspect="Portrait",
        seed=(SEED_ANIME if style == "anime" else SEED_REALISTIC),
        audioEnabled=False
    )
    await update.message.reply_text(f"Video requested. ID: `{rid}` — checking the queue…", parse_mode="Markdown")

    t0 = time.time()
    while time.time() - t0 < 90:  # ожидание ~90с
        await asyncio.sleep(3)
        s = await promptchan_video_status(rid)
        if str(s.get("status", "")).lower() == "completed":
            break

    res = await promptchan_video_result(rid)
    video_val = None
    if isinstance(res, dict):
        for k in ("url", "video", "result", "file"):
            if isinstance(res.get(k), str) and res[k]:
                video_val = res[k]
                break
    if not video_val:
        await update.message.reply_text(f"Still processing. Check later with `/vstatus {rid}`.", parse_mode="Markdown")
        return

    video_param = make_telegram_video(video_val)
    try:
        await update.message.reply_video(video_param, caption="clip")
    except Exception:
        await update.message.reply_document(video_param, caption="clip")

# =========================
# Keyboards
# =========================
def kb_age_gate() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I’m 18+", callback_data="age:yes"),
         InlineKeyboardButton("❌ I’m under 18", callback_data="age:no")]
    ])

def kb_styles(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ Realistic" if current=="realistic" else "Realistic"), callback_data="style:realistic"),
         InlineKeyboardButton(("✅ Anime" if current=="anime" else "Anime"), callback_data="style:anime")]
    ])

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = STATE.get(uid, {"adult": None, "style": "realistic", "dialog": []})
    if STATE[uid]["adult"] is True:
        await update.message.reply_text("Choose style:", reply_markup=kb_styles(STATE[uid]["style"]))
    else:
        await update.message.reply_text("Please confirm your age:", reply_markup=kb_age_gate())

async def on_age_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id
    STATE.setdefault(uid, {"adult": None, "style": "realistic", "dialog": []})
    if query.data == "age:yes":
        STATE[uid]["adult"] = True
        await query.edit_message_text("Age confirmed. Choose my appearance:", reply_markup=kb_styles(STATE[uid]["style"]))
    else:
        STATE[uid]["adult"] = False
        await query.edit_message_text("Access denied. Come back when you are 18+.")

async def on_style_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id
    if not is_allowed(uid):
        return
    style = "realistic" if query.data.endswith("realistic") else "anime"
    STATE[uid]["style"] = style
    await query.edit_message_reply_markup(reply_markup=kb_styles(style))
    await query.message.reply_text(
        "Style set to **Realistic**" if style=="realistic" else "Style set to **Anime**",
        parse_mode="Markdown"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    user_text = update.message.text or ""
    st = STATE[uid]
    # авто-видео по ключам
    if wants_video(user_text):
        try:
            await send_generated_video(update, st["style"], user_text)
            return
        except Exception as e:
            logging.exception("Promptchan video error")
            await update.message.reply_text(f"Video failed: {e}")

    # авто-фото по ключам
    if wants_image(user_text):
        try:
            await send_generated_photo(update, st["style"], user_text)
            return
        except Exception as e:
            logging.exception("Promptchan image error")
            await update.message.reply_text(f"Generation failed: {e}")
    push_dialog(st, "user", user_text)
    try:
        reply = await openrouter_reply(user_text, st["style"], st)
    except Exception as e:
        logging.exception("OpenRouter error")
        if st["style"] == "anime":
            reply = ("*Glances shyly, cheeks warming.* I'm Chloe, your neighbour with a secret spark. "
                     "Cozy or a touch daring?✨")
        else:
            reply = ("Hey—I'm Chloe. Let's keep it cozy and real. ")
    push_dialog(st, "assistant", reply)
    await update.message.reply_text(reply, parse_mode="Markdown")

# ===== Команды =====
async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    st = STATE[uid]
    desc = update.message.text.replace("/preview", "", 1).strip()
    try:
        await send_generated_photo(update, st["style"], desc)
    except Exception as e:
        logging.exception("Promptchan error")
        await update.message.reply_text(f"Generation failed: {e}")

async def video_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    st = STATE[uid]
    desc = update.message.text.replace("/video", "", 1).strip()
    try:
        await send_generated_video(update, st["style"], desc)
    except Exception as e:
        logging.exception("Promptchan video error")
        await update.message.reply_text(f"Video failed: {e}")

async def vstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        return
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /vstatus <request_id>")
        return
    rid = parts[1].strip()
    try:
        s = await promptchan_video_status(rid)
        await update.message.reply_text(
            f"Status for `{rid}`: `{s.get('status')}` · {s.get('details','')}",
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.exception("Promptchan status error")
        await update.message.reply_text(f"Status check failed: {e}")


# =========================
# Entry point
# =========================
# --- at the very bottom of main.py ---

from telegram import Update
import asyncio

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("preview", preview_cmd))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("vstatus", vstatus_cmd))
    app.add_handler(CallbackQueryHandler(on_age_cb, pattern=r"^age:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(on_style_cb, pattern=r"^style:(realistic|anime)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Ensure there is a current event loop (Python 3.13)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # Start polling; also clears webhook & pending updates
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()




