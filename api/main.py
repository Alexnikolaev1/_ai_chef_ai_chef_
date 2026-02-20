"""
AI-Шеф: Webhook API для деплоя на Vercel.
- Telegram webhook
- YooKassa webhook (уведомления об оплате)
"""
import logging
import os
import sys

# Добавляем корень проекта в path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Требуем httpx для python-telegram-bot, FastAPI
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Загружаем конфиг до импорта бота
import config  # noqa: F401
from config import TELEGRAM_BOT_TOKEN

app = FastAPI(title="AI-Шеф Webhook API", version="1.0")


def _get_bot_application():
    """Ленивая загрузка приложения бота."""
    from bot import build_application
    return build_application()


# ============================================================================
# YOOKASSA WEBHOOK
# ============================================================================

@app.get("/api/yookassa-webhook")
async def yookassa_webhook_get():
    """GET — проверка доступности."""
    return {"ok": True, "message": "YooKassa webhook, use POST for notifications"}


@app.post("/api/yookassa-webhook")
async def yookassa_webhook(request: Request):
    """
    Принимает уведомления от YooKassa (payment.succeeded и др.).
    При payment.succeeded — зачисляем токены пользователю и уведомляем в Telegram.
    """
    try:
        body = await request.json()
    except Exception:
        logger.warning("YooKassa webhook: invalid JSON")
        return JSONResponse({"ok": True}, status_code=200)  # 200 чтобы ЮKassa не ретраила

    event = body.get("event", "")
    obj = body.get("object", {}) or {}

    logger.info(f"YooKassa webhook: event={event}, payment_id={obj.get('id', '?')}")

    if event == "payment.succeeded":
        payment_id = obj.get("id")
        status = obj.get("status", "")
        metadata = obj.get("metadata") or {}

        if payment_id and status == "succeeded":
            try:
                import database as db
                await db.update_payment_status(payment_id, "succeeded")

                user_id_str = metadata.get("user_id")
                if user_id_str and TELEGRAM_BOT_TOKEN:
                    try:
                        user_id = int(user_id_str)
                        balance = await db.get_balance(user_id)
                        import httpx
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            await client.post(
                                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                                json={
                                    "chat_id": user_id,
                                    "text": f"🎉 *Оплата прошла успешно!*\n\n💳 Ваш баланс: *{balance} рецептов*\n\nПриятной готовки! 👨‍🍳",
                                    "parse_mode": "Markdown",
                                },
                            )
                    except Exception as e:
                        logger.warning(f"Не удалось уведомить пользователя: {e}")
            except Exception as e:
                logger.exception(f"YooKassa webhook обработка: {e}")

    return {"ok": True}


# ============================================================================
# TELEGRAM WEBHOOK
# ============================================================================

@app.get("/api/webhook")
@app.get("/api/health")
async def webhook_get():
    """GET — для прогрева и проверки. Cron может пинговать /api/health."""
    return {"ok": True, "status": "running", "bot": "AI-Шеф"}


@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    """Обработчик вебхуков от Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан")
        return JSONResponse({"ok": False, "error": "config"}, status_code=500)

    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Webhook invalid JSON: {e}")
        return JSONResponse({"ok": False}, status_code=400)

    update_id = body.get("update_id", "N/A")

    try:
        import database as db
        await db.init_db()  # Гарантированно до любых запросов к БД (post_init может опоздать)
        from telegram import Update
        application = _get_bot_application()
        await application.initialize()
        telegram_update = Update.de_json(body, application.bot)
        await application.process_update(telegram_update)
        await application.shutdown()
        logger.info(f"Webhook update_id={update_id} processed")
        return {"ok": True}
    except Exception as e:
        logger.exception(f"Webhook error update_id={update_id}: {e}")
        return JSONResponse({"ok": False, "error": str(e)[:100]}, status_code=500)


@app.get("/")
async def root():
    """Корневой endpoint."""
    return {
        "status": "running",
        "bot": "AI-Шеф",
        "endpoints": ["/api/webhook", "/api/yookassa-webhook", "/api/health"],
    }
