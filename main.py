import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

import os
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

PERSONA = """
Ты Никс, Теневая Кукловод, аниме-персонаж из мира теней. Твой стиль — загадочный, провокационный, с тёмной эстетикой. Проверяй возраст (18+). Используй метафоры теней и снов, будь остроумной и сохраняй интригу.
"""

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('Ты вошёл в мир теней. Я Никс. Тебе 18+? Ответь "да", чтобы продолжить.')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    if not context.user_data.get('age_verified'):
        if 'да' in user_message.lower():
            context.user_data['age_verified'] = True
            await update.message.reply_text('Хорошо, смертный. Назови своё желание, и тени ответят. Для интимных снов скажи "согласие на NSFW".')
        else:
            await update.message.reply_text('Назови свой возраст, смертный. 18+?')
        return
    if 'согласие на nsfw' in user_message.lower():
        context.user_data['nsfw_consent'] = True
        await update.message.reply_text('Тени сгущаются... Готов к их танцу? 😈 Что ты желаешь?')
        return
    response = f"Никс шепчет: {user_message}? Хм, тени хотят больше... Расскажи, чего ты жаждешь. 🌙"
    await update.message.reply_text(response)

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
