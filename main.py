import os, re, logging
from typing import List, Dict, Any
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

# --- ENV ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

# --- OpenAI ---
from openai import AsyncOpenAI
oclient = AsyncOpenAI(api_key=OPENAI_API_KEY)

PERSONA = (
    "Ты — Никс, Теневая Кукловод: загадочная, провокационная, с тёмной эстетикой. "
    "Всегда отвечай по‑русски, коротко и атмосферно. Используй метафоры теней и снов. "
    "В SFW избегай явной эротики."
)

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

YES_RE = re.compile(r"\b(да|ага|угу|есть|конечно|yes|yep|yeah)\b", re.I)
NSFW_RE = re.compile(r"(согласие\s+на\s+nsfw|разрешаю\s+nsfw|хочу\s+nsfw)", re.I)

def push_dialog(ctx: Dict[str, Any], role: str, content: str, max_turns: int = 16):
    buf: List[Dict[str, str]] = ctx.setdefault("dialog", [])
    buf.append({"role": role, "content": content})
    if len(buf) > max_turns:
        del buf[0:len(buf)-max_turns]

def build_messages(ctx: Dict[str, Any], user_text: str, mode: str) -> List[Dict[str, str]]:
    msgs = [{"role": "system", "content": PERSONA + f"\nТекущий режим: {mode.upper()}."}]
    msgs += ctx.get("dialog", [])[-10:]
    msgs.append({"role": "user", "content": user_text})
    return msgs

async def nyx_ai_reply_ru(context: ContextTypes.DEFAULT_TYPE, user_text: str, mode: str) -> str:
    messages = build_messages(context.user_data, user_text, mode)
    resp = await oclient.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        temperature=0.75,
        max_tokens=260,
    )
    return resp.choices[0].message.content.strip()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    ud.setdefault("age_verified", False)
    ud.setdefault("nsfw_consent", False)
    ud.setdefault("dialog", [])
    push_dialog(ud, "assistant", "Ты вошёл в мир теней. Я Никс.", 16)
    await update.message.reply_text('Ты вошёл в мир теней. Я Никс. Тебе 18+? Ответь "да", чтобы продолжить.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text or ""
    ud = context.user_data

    if not ud.get('age_verified'):
        if YES_RE.search(user_message):
            ud['age_verified'] = True
            push_dialog(ud, "user", "Да, мне 18+", 16)
            await update.message.reply_text('Хорошо, смертный. Назови своё желание, и тени ответят. Для интимных снов скажи "согласие на NSFW".')
        else:
            await update.message.reply_text('Назови свой возраст, смертный. 18+?')
        return

    if NSFW_RE.search(user_message):
        ud['nsfw_consent'] = True
        push_dialog(ud, "user", "согласие на NSFW", 16)
        await update.message.reply_text('Тени сгущаются... Готов к их танцу? 😈 Что ты желаешь?')
        return

    mode = "sfw"  # NSFW подключим позже, когда будет премиум
    push_dialog(ud, "user", user_message, 16)
    reply = await nyx_ai_reply_ru(context, user_message, mode)
    push_dialog(ud, "assistant", reply, 16)
    await update.message.reply_text(reply)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
