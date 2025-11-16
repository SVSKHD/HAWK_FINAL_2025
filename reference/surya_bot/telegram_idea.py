import os
import json
from io import BytesIO
from urllib.parse import urljoin
from datetime import time
from zoneinfo import ZoneInfo  # Python 3.9+

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from playwright.async_api import async_playwright
# ========= CONFIG =========

BOT_TOKEN = "8218723760:AAEhMW_D2aLOhOHnKq_bu1rDMCRE_p8Xbkc"        # <- put your bot token
OWNER_CHAT_ID = 946079827               # <- put your chat id (int)

BASE_URL = "https://taxinformation.cbic.gov.in"

# 👉 Customs tab URL (open site, click Customs tab, copy URL)
CUSTOMS_TAB_URL = "https://taxinformation.cbic.gov.in/"

SEEN_FILE = "seen_customs_latest.json"

INDIA_TZ = ZoneInfo("Asia/Kolkata")      # for 7 PM IST


# ========= SEEN STORAGE =========

def load_last_seen() -> str | None:
    if not os.path.exists(SEEN_FILE):
        return None
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("last_url")
    except Exception:
        return None


def save_last_seen(url: str):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_url": url}, f, indent=2)
    except Exception as e:
        print("Error saving last seen customs PDF:", e)


# ========= SCRAPING LOGIC =========

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CBICCustomsBot/1.0; +https://example.com/)"
}




async def get_latest_customs_english_pdf() -> tuple[bytes, str, str]:
    """
    Use Playwright to:
    1. Open CBIC tax portal.
    2. Click on Customs tab.
    3. Find the first 'English' PDF link (latest Customs circular).
    4. Download the PDF bytes.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://taxinformation.cbic.gov.in/", wait_until="networkidle")

        # 🔧 You will need to adjust this based on actual DOM:
        # Example: click on the tab labelled "Customs"
        await page.get_by_text("Customs", exact=False).click()

        # Wait for Customs list to load – may need a better wait condition
        await page.wait_for_timeout(3000)

        # Find first 'English' link inside Customs section
        link = await page.locator("a:has-text('English')").first
        href = await link.get_attribute("href")
        if not href:
            await browser.close()
            raise RuntimeError("Could not find any English Customs PDF link on the page.")

        # Make it absolute if needed
        if href.startswith("/"):
            pdf_url = BASE_URL + href
        else:
            pdf_url = href

        # Now download the PDF via requests (simpler than Playwright download API)
        import requests
        resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
        resp.raise_for_status()

        await browser.close()

        filename = pdf_url.split("/")[-1] or "customs_circular.pdf"
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"

        return resp.content, filename, pdf_url



# ========= TELEGRAM HANDLERS =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi 👋\n\n"
        "I track *Customs* circulars from CBIC Tax Information Portal.\n"
        "Every day at *7:00 PM IST* I will send you the latest Customs English PDF.\n\n"
        "Sending the latest Customs circular now..."
    )

    # On /start, fetch and send the latest Customs PDF immediately
    try:
        pdf_bytes, filename, url = get_latest_customs_english_pdf()
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 Latest Customs English circular:\n{url}",
        )
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to fetch latest Customs circular:\n{e}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start – info + latest Customs circular now\n"
        "/help – this help\n"
        "/checknow – manually check and send latest Customs circular\n"
        "/id – show your chat ID"
    )


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        await update.message.reply_text(f"You said: {update.message.text}")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I don't know that command yet 🙈")


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat ID is: {update.message.chat_id}")


# ========= PERIODIC JOB (DAILY 7 PM) =========

async def check_customs_circulars(context: ContextTypes.DEFAULT_TYPE):
    """
    Job that runs daily at 7 PM IST:
    - Fetch latest Customs English PDF
    - Compare with last seen URL
    - If different, send to OWNER_CHAT_ID
    """
    bot = context.bot
    print("[job] Daily 7 PM check for Customs circular...")

    last_seen = load_last_seen()

    try:
        pdf_bytes, filename, url = get_latest_customs_english_pdf()
    except Exception as e:
        print("[job] Error fetching Customs PDF:", e)
        await bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"⚠️ Customs check failed:\n{e}"
        )
        return

    # Always send at 7 PM, even if same? or only if new?
    # Here: only if new
    if url == last_seen:
        print("[job] No new Customs circular found.")
        return

    print("[job] New Customs circular detected! Sending to Telegram...")
    try:
        await bot.send_document(
            chat_id=OWNER_CHAT_ID,
            document=BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 New Customs English circular:\n{url}",
        )
        save_last_seen(url)
    except Exception as e:
        print("[job] Error sending circular:", e)
        await bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"⚠️ Failed to send new Customs circular:\n{e}"
        )


async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual trigger via /checknow – only OWNER_CHAT_ID can run it.
    """
    if update.message.chat_id != OWNER_CHAT_ID:
        await update.message.reply_text("⛔ You are not authorized to run /checknow.")
        return

    await update.message.reply_text("⏳ Checking CBIC Customs tab for latest circular...")
    # run same logic once, but **always** send regardless of last_seen
    try:
        pdf_bytes, filename, url = get_latest_customs_english_pdf()
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=BytesIO(pdf_bytes),
            filename=filename,
            caption=f"📄 Latest Customs English circular:\n{url}",
        )
        save_last_seen(url)  # mark as last seen
        await update.message.reply_text("✅ Check completed.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to fetch Customs circular:\n{e}")


# ========= MAIN =========
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Please set BOT_TOKEN to your real Telegram bot token.")
    if not isinstance(OWNER_CHAT_ID, int):
        raise RuntimeError("Please set OWNER_CHAT_ID to your numeric chat ID (int).")
    if "..." in CUSTOMS_TAB_URL:
        raise RuntimeError("Please set CUSTOMS_TAB_URL to the real Customs tab URL from the portal.")

    print("ℹ️  Starting Customs Circular Telegram bot...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("checknow", checknow))

    # Normal text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))

    # Unknown commands
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # --- Make sure JobQueue exists ---
    if app.job_queue is None:
        raise RuntimeError(
            "JobQueue is not available. Install python-telegram-bot with:\n"
            '    pip install "python-telegram-bot[job-queue]"'
        )

    # 🔔 Daily job at 21:34 Asia/Kolkata (change hour/minute as you like)
    app.job_queue.run_daily(
        check_customs_circulars,
        time=time(hour=21, minute=52, tzinfo=INDIA_TZ),
        name="customs_circular_7pm",
    )

    # Startup notification
    async def on_startup(application):
        await application.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=(
                "🟢 Customs Circular Bot is now running.\n"
                "I will send the latest Customs English circular every day at the scheduled time."
            ),
        )

    app.post_init = on_startup

    print("✅ Bot is running. You can now use the services.")
    app.run_polling()



if __name__ == "__main__":
    main()
