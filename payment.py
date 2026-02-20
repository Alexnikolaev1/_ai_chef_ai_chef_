"""
Модуль оплаты через ЮKassa.
В тестовом режиме — возвращает мок-ссылку.
В боевом режиме — реальный API ЮKassa.
"""
import asyncio
import uuid
import logging
from config import YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY, YOOKASSA_RETURN_URL, PACKAGES, YOOKASSA_USE_MOCK

logger = logging.getLogger(__name__)

# Проверяем, доступна ли библиотека ЮKassa
try:
    from yookassa import Configuration, Payment
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY
    YOOKASSA_AVAILABLE = True
    logger.info("✅ ЮKassa SDK загружен")
except ImportError:
    YOOKASSA_AVAILABLE = False
    logger.warning("⚠️ ЮKassa SDK не установлен, используем мок-режим")


async def create_payment(user_id: int, package_key: str) -> dict:
    """
    Создаёт платёж для пользователя.
    
    Возвращает словарь:
    {
        "payment_id": str,
        "payment_url": str,
        "amount": float,
        "recipes_count": int
    }
    """
    if package_key not in PACKAGES:
        raise ValueError(f"Неверный пакет: {package_key}")

    package = PACKAGES[package_key]
    amount = package["price"]
    recipes_count = package["recipes"]

    if YOOKASSA_USE_MOCK or not YOOKASSA_AVAILABLE or YOOKASSA_SHOP_ID in ("test_shop", ""):
        return await _create_mock_payment(user_id, package_key, amount, recipes_count)
    return await _create_real_payment(user_id, package_key, amount, recipes_count, package["name"])


def _create_real_payment_sync(user_id: int, package_key: str,
                               amount: float, recipes_count: int, name: str) -> dict:
    """Синхронное создание платежа (ЮKassa SDK блокирующий)."""
    idempotence_key = str(uuid.uuid4())
    payment = Payment.create({
            "amount": {
                "value": str(amount) + ".00",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": YOOKASSA_RETURN_URL
            },
            "capture": True,
            "description": f"AI-Шеф: {name} ({recipes_count} рецептов)",
            "metadata": {
                "user_id": str(user_id),
                "package_key": package_key,
                "recipes_count": str(recipes_count)
            }
    }, idempotence_key)

    logger.info(f"💳 Создан платёж {payment.id} для пользователя {user_id}")
    return {
        "payment_id": payment.id,
        "payment_url": payment.confirmation.confirmation_url,
        "amount": amount,
        "recipes_count": recipes_count
    }


async def _create_real_payment(user_id: int, package_key: str,
                               amount: float, recipes_count: int, name: str) -> dict:
    """Создаём реальный платёж через ЮKassa API (в executor, чтобы не блокировать event loop)."""
    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: _create_real_payment_sync(user_id, package_key, amount, recipes_count, name),
        )
    except Exception as e:
        err_msg = str(e)
        logger.error(f"❌ Ошибка создания платежа ЮKassa: {e}")
        if "401" in err_msg:
            logger.error(
                "401 = неверные ключи. Проверь YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY "
                "из тестового магазина: Интеграция → Ключи API"
            )
        raise RuntimeError(f"Ошибка создания платежа: {e}") from e


async def _create_mock_payment(user_id: int, package_key: str,
                                amount: float, recipes_count: int) -> dict:
    """
    Мок-платёж для тестирования.
    В реальном проекте замени на _create_real_payment.
    """
    payment_id = f"mock_{uuid.uuid4().hex[:16]}"
    # В тестовом режиме — ссылка на тестовую оплату
    payment_url = f"https://yookassa.ru/checkout/payments/{payment_id}"

    logger.info(f"🧪 Мок-платёж {payment_id} для пользователя {user_id} на {amount} руб.")
    return {
        "payment_id": payment_id,
        "payment_url": payment_url,
        "amount": amount,
        "recipes_count": recipes_count
    }


def _check_payment_status_sync(payment_id: str) -> str:
    """Синхронная проверка статуса (ЮKassa SDK блокирующий)."""
    payment = Payment.find_one(payment_id)
    return payment.status


async def check_payment_status(payment_id: str) -> str:
    """
    Проверить статус платежа в ЮKassa.
    Возвращает: 'succeeded', 'pending', 'canceled'
    """
    if payment_id.startswith("mock_"):
        return "succeeded"

    if not YOOKASSA_AVAILABLE:
        return "pending"

    try:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: _check_payment_status_sync(payment_id),
        )
    except Exception as e:
        logger.error(f"❌ Ошибка проверки платежа {payment_id}: {e}")
        return "pending"


def format_packages_text() -> str:
    """Красиво форматирует список доступных пакетов."""
    lines = ["💎 *Выберите пакет рецептов:*\n"]
    for key, pkg in PACKAGES.items():
        price_per = pkg["price"] / pkg["recipes"]
        lines.append(
            f"{pkg['name']}\n"
            f"   📖 {pkg['recipes']} рецептов\n"
            f"   💰 {pkg['price']} руб. ({price_per:.0f} руб/рецепт)\n"
        )
    return "\n".join(lines)
