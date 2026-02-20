"""
Модуль для работы с базой данных SQLite.
Используем aiosqlite для асинхронных операций.
"""
import aiosqlite
import logging
from datetime import datetime
from typing import Optional
from config import DB_PATH, FREE_RECIPES_ON_START

logger = logging.getLogger(__name__)


async def init_db():
    """Инициализация БД: создаём таблицы если их нет."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                username    TEXT,
                full_name   TEXT,
                tokens_balance INTEGER DEFAULT 0,
                total_spent    REAL DEFAULT 0.0,
                total_recipes  INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen   TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Таблица рецептов (история запросов)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                recipe_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                prompt      TEXT,
                response    TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        # Таблица платежей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                payment_id    TEXT PRIMARY KEY,
                user_id       INTEGER,
                package_key   TEXT,
                amount        REAL,
                recipes_count INTEGER,
                status        TEXT DEFAULT 'pending',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)

        await db.commit()
    logger.info("✅ База данных инициализирована")


async def get_or_create_user(user_id: int, username: str, full_name: str) -> dict:
    """
    Получить пользователя из БД или создать нового.
    Новый пользователь получает бесплатные рецепты.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Ищем существующего пользователя
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        user = await cursor.fetchone()

        if user:
            # Обновляем время последнего визита и username
            await db.execute(
                "UPDATE users SET last_seen = CURRENT_TIMESTAMP, username = ?, full_name = ? WHERE user_id = ?",
                (username, full_name, user_id)
            )
            await db.commit()
            return dict(user)
        else:
            # Создаём нового пользователя с бесплатными рецептами
            await db.execute(
                """INSERT INTO users (user_id, username, full_name, tokens_balance)
                   VALUES (?, ?, ?, ?)""",
                (user_id, username, full_name, FREE_RECIPES_ON_START)
            )
            await db.commit()
            logger.info(f"👤 Новый пользователь: {user_id} (@{username})")

            cursor = await db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            return dict(await cursor.fetchone())


async def get_user(user_id: int) -> Optional[dict]:
    """Получить данные пользователя."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_balance(user_id: int) -> int:
    """Получить баланс токенов пользователя."""
    user = await get_user(user_id)
    return user["tokens_balance"] if user else 0


async def deduct_token(user_id: int) -> bool:
    """
    Списать 1 токен с баланса.
    Возвращает True если успешно, False если токенов нет.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Атомарное уменьшение с проверкой
        cursor = await db.execute(
            """UPDATE users
               SET tokens_balance = tokens_balance - 1,
                   total_recipes = total_recipes + 1
               WHERE user_id = ? AND tokens_balance > 0""",
            (user_id,)
        )
        await db.commit()
        return cursor.rowcount > 0  # rowcount=1 значит успешно списали


async def add_tokens(user_id: int, count: int):
    """Пополнить баланс токенов (после оплаты)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET tokens_balance = tokens_balance + ? WHERE user_id = ?",
            (count, user_id)
        )
        await db.commit()
    logger.info(f"💰 Пользователю {user_id} добавлено {count} токенов")


async def save_recipe(user_id: int, prompt: str, response: str):
    """Сохранить рецепт в историю."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO recipes (user_id, prompt, response) VALUES (?, ?, ?)",
            (user_id, prompt, response)
        )
        await db.commit()


async def save_payment(payment_id: str, user_id: int, package_key: str,
                        amount: float, recipes_count: int):
    """Сохранить информацию о платеже."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT OR REPLACE INTO payments
               (payment_id, user_id, package_key, amount, recipes_count, status)
               VALUES (?, ?, ?, ?, ?, 'pending')""",
            (payment_id, user_id, package_key, amount, recipes_count)
        )
        await db.commit()


async def update_payment_status(payment_id: str, status: str):
    """
    Обновить статус платежа.
    Токены зачисляются только при переходе pending -> succeeded (защита от двойного клика).
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(
            "SELECT status, user_id, recipes_count, amount FROM payments WHERE payment_id = ?",
            (payment_id,)
        )
        row = await cursor.fetchone()
        if not row:
            logger.warning(f"Платёж {payment_id} не найден")
            return

        current_status, user_id, recipes_count, amount = row[0], row[1], row[2], row[3]

        # Зачисляем токены только при первом успешном подтверждении
        if status == "succeeded" and current_status == "pending":
            await conn.execute(
                """UPDATE users
                   SET tokens_balance = tokens_balance + ?,
                       total_spent = total_spent + ?
                   WHERE user_id = ?""",
                (recipes_count, amount, user_id)
            )
            logger.info(f"💰 Зачислено {recipes_count} рецептов пользователю {user_id}")

        await conn.execute(
            "UPDATE payments SET status = ? WHERE payment_id = ?",
            (status, payment_id)
        )
        await conn.commit()


# === СТАТИСТИКА ДЛЯ АДМИНА ===

async def get_stats() -> dict:
    """Получить общую статистику для администратора."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Общее количество пользователей
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM users")
        total_users = (await cursor.fetchone())["cnt"]

        # Новые пользователи за сегодня
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE DATE(created_at) = DATE('now')"
        )
        new_today = (await cursor.fetchone())["cnt"]

        # Всего рецептов сгенерировано
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM recipes")
        total_recipes = (await cursor.fetchone())["cnt"]

        # Рецептов за сегодня
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM recipes WHERE DATE(created_at) = DATE('now')"
        )
        recipes_today = (await cursor.fetchone())["cnt"]

        # Доход
        cursor = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM payments WHERE status = 'succeeded'"
        )
        total_revenue = (await cursor.fetchone())["total"]

        # Топ-5 популярных запросов (по ключевым словам)
        cursor = await db.execute(
            "SELECT prompt, COUNT(*) as cnt FROM recipes GROUP BY prompt ORDER BY cnt DESC LIMIT 5"
        )
        top_prompts = await cursor.fetchall()

        return {
            "total_users": total_users,
            "new_today": new_today,
            "total_recipes": total_recipes,
            "recipes_today": recipes_today,
            "total_revenue": total_revenue,
            "top_prompts": [dict(r) for r in top_prompts]
        }
