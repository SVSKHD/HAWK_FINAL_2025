import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "8218723760:AAEhMW_D2aLOhOHnKq_bu1rDMCRE_p8Xbkc"   # <-- Replace with your NEW token


# -------- Handlers -------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi 👋!\nBot skeleton is running."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start – say hi\n"
        "/help – help menu\n"
        "More features coming soon 🚀"
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"You said: {update.message.text}")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I don't know that command yet 🙈")


# ---- TEMP: get your chat id ---- #
async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID is: {update.message.chat_id}")


# -------- Main App -------- #

def main():
    print("ℹ️  Starting Telegram bot…")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", get_id))   # <-- To get chat_id
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("✅ Bot is running. You can now use the services.")

    # ---- Send “Bot is running” message to your Telegram ---- #
    async def send_startup_message(application):
        OWNER_CHAT_ID =  946079827  # <-- Replace with your Telegram chat ID
        try:
            await application.bot.send_message(
                OWNER_CHAT_ID,
                "🟢 *Bot is now running*\nYou can start using all services.",
                parse_mode="Markdown"
            )
            print("📨 Startup message sent to Telegram.")
        except Exception as e:
            print("⚠️ Could not send startup message:", e)

    app.post_init = send_startup_message

    # Start polling
    app.run_polling()


if __name__ == "__main__":
    main()
