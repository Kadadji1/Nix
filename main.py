# main.py
import os, logging, asyncio, time, base64
from io import BytesIO
from typing import List, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

import httpx  # for Promptchan HTTP
from openai import AsyncOpenAI as OpenAI  # OpenRouter via openai client

# =========================
# ENV (Render → Environment)
# =========================
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "gryphe/mythomax-l2-13b")

PROMPTCHAN_API_KEY = os.environ["x-api-key"]
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
# Persona & style (LLM)
# =========================
# RU comment: Персона строго 18+, безопасные границы, ответы только на английском.
PERSONA_BASE = (
   "You are an 18+ virtual companion: a 18 уears old girl-next-door with a secret playful side. "
    "Always lawful and consensual: no minors, no family roles, no non-consent, no illegal content. "
    "Keep replies concise (2–5 sentences). Default to English unless the user speaks another language, then mirror it. "
)

STYLE_REALISTIC = (
        "Active style: REALISTIC. Speak like a real neighbor in small talk: warm, down-to-earth, lightly teasing; "
    "add small everyday details (hoodie, tea, soft lights). Avoid purple prose, Act like real person."
)

STYLE_ANIME = (
        "Active style: ANIME_REALISTIC. Be shy and a bit bashful; add soft cinematic flair and gentle stage directions "
    "(e.g., *glances shyly, cheeks warming*). Keep it grounded and readable."
)

def system_prompt(style: str) -> str:
    return PERSONA_BASE + " " + (STYLE_ANIME if style == "anime" else STYLE_REALISTIC)

# =========================
# Stable appearance (images)
# =========================
# RU comment: Базовый облик, который всегда подмешивается к сцене.
BASE_APPEARANCE = (
    "extra slim European blonde woman, blonde short curly hair, "
    "realistic green eyes, soft oval face with freckles, full lips, pure beauty "
    "semi-realistic style, nipple piercings"

)

NEGATIVE = (
    "child, underage, family role, violence, "
    "bad anatomy, extra fingers, "
    "multiple limbs, blurry, lowres, watermark, text"
)

# RU comment: Фиксированные сиды (по твоему запросу).
SEED_REALISTIC = 3374304272
SEED_ANIME     = 2166236711

# =========================
# In-memory state (replace with DB in prod)
# =========================
STATE: Dict[int, Dict[str, Any]] = {}  # user_id -> {"adult": None|True|False, "style": "realistic"|"anime", "dialog": [...]}

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
        "quality": "Ultra",               # Ultra | Extreme | Max
        "creativity": 50,
        "image_size": "512x768",
        "negative_prompt": NEGATIVE,
        "restore_faces": (style != "anime"),
        "age_slider": 18,
        "weight_slider": -1.0,          # optional: -1..1
        "breast_slider": -1.0,
        "ass_slider": -1.0,
    }
    return payload

async def promptchan_create(payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/external/create → { image: <base64>, gems: <int> }"""
    headers = {
        "x-api-key": PROMPTCHAN_API_KEY,      # auth per your docs
        "Content-Type": "application/json",
    }
    url = f"{PROMPTCHAN_API_URL}/api/external/create"
    async with httpx.AsyncClient(timeout=60) as cl:
        r = await cl.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()

def b64_to_inputfile(b64: str, filename: str = "preview.jpg") -> InputFile:
    raw = base64.b64decode(b64)
    bio = BytesIO(raw)
    bio.seek(0)
    return InputFile(bio, filename=filename)

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

async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        return

    st = STATE[uid]
    # RU comment: user_desc — только сцена/поза/свет/одежда. Внешность уже задана BASE_APPEARANCE.
    desc = update.message.text.replace("/preview", "", 1).strip()
    if not desc:
        desc = "mirror selfie on bed, teasing smile,lace lingerie, warm cinematic lighting"

    await update.message.reply_text("Let me show you something 👀")
    try:
        payload = pc_build_payload(st["style"], desc, quality="Ultra")
        res = await promptchan_create(payload)
        if "image" not in res:
            raise RuntimeError(f"Promptchan: unexpected response: {res}")
        photo = b64_to_inputfile(res["image"], filename="preview.jpg")
        gems_info = f" · gems used: {res.get('gems')}" if "gems" in res else ""
        seed_used = SEED_ANIME if st["style"] == "anime" else SEED_REALISTIC
        await update.message.reply_photo(photo, caption=f"{st['style']} preview · seed {seed_used}{gems_info}")
    except Exception as e:
        logging.exception("Promptchan error")
        await update.message.reply_text(f"Generation failed: {e}")

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




