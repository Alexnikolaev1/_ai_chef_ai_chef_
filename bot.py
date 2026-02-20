"""
AI-Шеф: Telegram-бот для генерации рецептов через YandexGPT.
Основной файл — точка входа и все обработчики команд.
"""
import logging
from datetime import datetime
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction

import database as db
import yandex_client as ai
import payment as pay
from config import (
    TELEGRAM_BOT_TOKEN, ADMIN_IDS, RATE_LIMIT_SECONDS,
    MAX_PROMPT_LENGTH, PACKAGES, FREE_RECIPES_ON_START,
    YANDEX_FOLDER_ID, YANDEX_API_KEY,
    YANDEX_MODEL,
)

# === НАСТРОЙКА ЛОГОВ ===
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),                          # В консоль
        logging.FileHandler("bot.log", encoding="utf-8") # В файл
    ]
)
logger = logging.getLogger(__name__)

# === ХРАНИЛИЩЕ RATE LIMIT ===
# {user_id: datetime последнего запроса}
last_request_time: dict[int, datetime] = {}

# === ПУТИ К КАРТИНКАМ ===
IMAGES_DIR = Path(__file__).resolve().parent / "images"
IMAGE_EXTENSIONS = (".jpeg", ".jpg", ".png", ".webp")


def _get_image_path(name: str) -> Path | None:
    """Найти путь к картинке по имени (start, balance, recipe). Формат: .jpeg, .jpg, .png."""
    for ext in IMAGE_EXTENSIONS:
        path = IMAGES_DIR / f"{name}{ext}"
        if path.exists():
            return path
    return None


# ======================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ======================================================

def get_main_keyboard() -> InlineKeyboardMarkup:
    """Главная клавиатура бота."""
    keyboard = [
        [InlineKeyboardButton("🍳 Создать рецепт", callback_data="new_recipe")],
        [
            InlineKeyboardButton("💰 Купить рецепты", callback_data="buy"),
            InlineKeyboardButton("📊 Мой баланс", callback_data="balance"),
        ],
        [InlineKeyboardButton("❓ Помощь", callback_data="help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_packages_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора пакета."""
    keyboard = []
    for key, pkg in PACKAGES.items():
        keyboard.append([
            InlineKeyboardButton(
                f"{pkg['name']} — {pkg['price']} руб.",
                callback_data=f"buy_{key}"
            )
        ])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(keyboard)


def check_rate_limit(user_id: int) -> int:
    """
    Проверяет rate limit.
    Возвращает 0 если можно делать запрос,
    или количество секунд ожидания.
    """
    if user_id in last_request_time:
        elapsed = (datetime.now() - last_request_time[user_id]).total_seconds()
        wait = RATE_LIMIT_SECONDS - elapsed
        if wait > 0:
            return int(wait) + 1
    return 0


def update_rate_limit(user_id: int):
    """Обновить время последнего запроса."""
    last_request_time[user_id] = datetime.now()


def _escape_md(text: str) -> str:
    """Экранирует спецсимволы Markdown для безопасного отображения."""
    for char in "_*`[":
        text = text.replace(char, f"\\{char}")
    return text


# ======================================================
# ОБЩАЯ ЛОГИКА ГЕНЕРАЦИИ РЕЦЕПТА
# ======================================================

async def _generate_recipe_for_user(
    bot,
    chat_id: int,
    user_id: int,
    user_input: str,
    edit_message=None,
):
    """
    Общая логика генерации рецепта. Используется из cmd_recipe и callback recipe_from_msg.
    edit_message: если задано — редактируем это сообщение вместо отправки нового.
    """
    # Валидация длины
    if len(user_input) > MAX_PROMPT_LENGTH:
        text = (
            f"⚠️ Запрос слишком длинный (максимум {MAX_PROMPT_LENGTH} символов).\n"
            f"Сократи описание и попробуй снова!"
        )
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await bot.send_message(chat_id, text)
        return

    wait_seconds = check_rate_limit(user_id)
    if wait_seconds > 0:
        text = f"⏳ Подожди ещё *{wait_seconds} секунд* перед следующим запросом."
        if edit_message:
            await edit_message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
        else:
            await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
        return

    balance = await db.get_balance(user_id)
    if balance <= 0:
        text = (
            "😔 *Рецепты закончились!*\n\n"
            "Купи пакет рецептов, чтобы продолжить готовить с AI-Шефом:"
        )
        if edit_message:
            await edit_message.edit_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_packages_keyboard()
            )
        else:
            await bot.send_message(
                chat_id, text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_packages_keyboard()
            )
        return

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    thinking_text = (
        "🧑‍🍳 *Шеф думает над рецептом...*\n"
        "Обычно это занимает 5-15 секунд"
    )
    if edit_message:
        await edit_message.edit_text(thinking_text, parse_mode=ParseMode.MARKDOWN)
        thinking_msg = edit_message
    else:
        thinking_msg = await bot.send_message(chat_id, thinking_text, parse_mode=ParseMode.MARKDOWN)

    try:
        update_rate_limit(user_id)
        recipe = await ai.generate_recipe(user_input)

        success = await db.deduct_token(user_id)
        if not success:
            await thinking_msg.edit_text(
                "😔 Токены закончились пока генерировался рецепт. "
                "Купи пакет и попробуй снова!",
                reply_markup=get_packages_keyboard(),
            )
            return

        await db.save_recipe(user_id, user_input, recipe)
        new_balance = await db.get_balance(user_id)

        footer = f"\n\n---\n💳 Осталось рецептов: *{new_balance}*"
        if new_balance == 0:
            footer += "\n\n👆 Пополни баланс, чтобы продолжить!"

        reply_markup = get_packages_keyboard() if new_balance == 0 else None
        full_text = recipe + footer
        try:
            await thinking_msg.edit_text(
                full_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup,
            )
        except BadRequest as e:
            if "Can't parse" in str(e) or "parse" in str(e).lower():
                await thinking_msg.edit_text(
                    full_text,
                    reply_markup=reply_markup,
                )
            else:
                raise

        if (img := _get_image_path("recipe")):
            await bot.send_photo(
                chat_id=chat_id,
                photo=img,
                caption="🧑‍🍳 _Приятной готовки!_",
                parse_mode=ParseMode.MARKDOWN,
            )

        if 0 < new_balance <= 1:
            await bot.send_message(
                chat_id,
                "⚠️ *Остался последний рецепт!*\n"
                "Не забудь пополнить баланс, чтобы не прерваться на самом интересном 😊",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=get_packages_keyboard(),
            )

        logger.info(f"✅ Рецепт для {user_id}: '{user_input[:30]}...' | Баланс: {new_balance}")

    except Exception as e:
        logger.error(f"Ошибка генерации для {user_id}: {e}", exc_info=True)
        error_text = (
            "❌ *Ошибка генерации рецепта*\n\n"
            "Что-то пошло не так. Токен не списан — попробуй снова!\n"
            f"Причина: `{str(e)[:100]}`"
        )
        await thinking_msg.edit_text(error_text, parse_mode=ParseMode.MARKDOWN)


# ======================================================
# ОБРАБОТЧИКИ КОМАНД
# ======================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start — приветствие."""
    logger.info(f"📩 /start от user_id={update.effective_user.id}")
    user = update.effective_user

    # Регистрируем/обновляем пользователя в БД
    user_data = await db.get_or_create_user(
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name or ""
    )

    balance = user_data["tokens_balance"]
    is_new = user_data["total_recipes"] == 0

    if is_new:
        welcome_text = (
            f"👨‍🍳 *Добро пожаловать в AI-Шеф, {user.first_name}!*\n\n"
            f"Я создаю уникальные рецепты за секунды на основе твоих ингредиентов "
            f"или настроения.\n\n"
            f"🎁 *Подарок:* {FREE_RECIPES_ON_START} бесплатных рецепта уже на твоём счету!\n\n"
            f"*Как пользоваться:*\n"
            f"• Напиши `/recipe курица, помидоры, чеснок`\n"
            f"• Или просто опиши что хочешь: `/recipe что-то лёгкое на ужин`\n\n"
            f"Поехали? 🚀"
        )
    else:
        welcome_text = (
            f"👨‍🍳 *С возвращением, {user.first_name}!*\n\n"
            f"💳 Баланс: *{balance} рецептов*\n\n"
            f"Готов создать что-то вкусное? 😋"
        )

    reply_kw = {"parse_mode": ParseMode.MARKDOWN, "reply_markup": get_main_keyboard()}
    if (img := _get_image_path("start")):
        await update.message.reply_photo(photo=img, caption=welcome_text, **reply_kw)
    else:
        await update.message.reply_text(welcome_text, **reply_kw)


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик команды /recipe — главная функция.
    Принимает описание и генерирует рецепт через YandexGPT.
    """
    user = update.effective_user
    user_id = user.id

    args = context.args
    if not args:
        await update.message.reply_text(
            "🤔 *Что приготовить?*\n\n"
            "Напиши ингредиенты или опиши желаемое блюдо:\n\n"
            "*Примеры:*\n"
            "• `/recipe курица, рис, лук`\n"
            "• `/recipe что-то быстрое на завтрак`\n"
            "• `/recipe романтический ужин для двоих`\n"
            "• `/recipe десерт без сахара`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_input = " ".join(args).strip()
    await _generate_recipe_for_user(
        bot=context.bot,
        chat_id=update.effective_chat.id,
        user_id=user_id,
        user_input=user_input,
        edit_message=None,
    )


async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /buy — покупка пакетов."""
    text = pay.format_packages_text()
    await update.message.reply_text(
        text + "\n\n👆 Выбери подходящий пакет:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=get_packages_keyboard()
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /balance — баланс пользователя."""
    user_id = update.effective_user.id
    user_data = await db.get_user(user_id)

    if not user_data:
        await update.message.reply_text("Сначала запусти /start")
        return

    balance = user_data["tokens_balance"]
    total_recipes = user_data["total_recipes"]
    total_spent = user_data["total_spent"]

    # Выбираем эмодзи в зависимости от баланса
    if balance == 0:
        status = "😔 Рецепты закончились"
    elif balance <= 3:
        status = "⚠️ Заканчивается"
    else:
        status = "✅ Хватает"

    text = (
        f"💳 *Ваш баланс*\n\n"
        f"📖 Доступных рецептов: *{balance}* {status}\n"
        f"🍳 Всего приготовлено: *{total_recipes}*\n"
        f"💰 Потрачено: *{total_spent:.0f} руб.*\n"
    )

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 Купить ещё", callback_data="buy")
    ]])
    reply_kw = {"parse_mode": ParseMode.MARKDOWN, "reply_markup": keyboard}
    if (img := _get_image_path("balance")):
        await update.message.reply_photo(photo=img, caption=text, **reply_kw)
    else:
        await update.message.reply_text(text, **reply_kw)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help."""
    help_text = (
        "🆘 *Помощь по AI-Шефу*\n\n"
        "*Команды:*\n"
        "• `/start` — главное меню\n"
        "• `/recipe [запрос]` — создать рецепт\n"
        "• `/balance` — проверить баланс\n"
        "• `/buy` — купить рецепты\n"
        "• `/help` — эта справка\n\n"
        "*Как составить хороший запрос:*\n"
        "✓ Перечисли ингредиенты: `куриная грудка, брокколи, соевый соус`\n"
        "✓ Опиши ситуацию: `быстрый ужин после работы`\n"
        "✓ Укажи диету: `вегетарианский обед без глютена`\n"
        "✓ Задай настроение: `что-то уютное и сытное на зиму`\n\n"
        "*Лимиты:*\n"
        f"• 1 запрос каждые {RATE_LIMIT_SECONDS} секунд\n"
        f"• Максимум {MAX_PROMPT_LENGTH} символов в запросе\n\n"
        "По вопросам: @your_support_account"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /admin — статистика для администраторов."""
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет доступа к этой команде.")
        return

    stats = await db.get_stats()

    text = (
        f"📊 *Статистика AI-Шефа*\n\n"
        f"👥 *Пользователи:*\n"
        f"   Всего: {stats['total_users']}\n"
        f"   Новых сегодня: {stats['new_today']}\n\n"
        f"🍳 *Рецепты:*\n"
        f"   Всего сгенерировано: {stats['total_recipes']}\n"
        f"   Сегодня: {stats['recipes_today']}\n\n"
        f"💰 *Доход:*\n"
        f"   Всего: {stats['total_revenue']:.0f} руб.\n\n"
        f"🔥 *Топ запросов:*\n"
    )

    for i, item in enumerate(stats['top_prompts'], 1):
        prompt = item['prompt'][:40] + "..." if len(item['prompt']) > 40 else item['prompt']
        text += f"   {i}. {prompt} ({item['cnt']}x)\n"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ======================================================
# ОБРАБОТЧИКИ CALLBACK (нажатия кнопок)
# ======================================================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Центральный обработчик всех нажатий кнопок."""
    query = update.callback_query
    await query.answer()  # Убираем "часики" на кнопке

    data = query.data
    user = query.from_user

    if data == "new_recipe":
        await query.edit_message_text(
            "🍳 *Создание рецепта*\n\n"
            "Напиши команду `/recipe` и опиши что хочешь приготовить:\n\n"
            "*Примеры:*\n"
            "• `/recipe яйца, сыр, зелень`\n"
            "• `/recipe быстрый завтрак`\n"
            "• `/recipe десерт без выпечки`",
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "balance":
        user_data = await db.get_user(user.id)
        balance = user_data["tokens_balance"] if user_data else 0
        balance_text = (
            f"💳 *Ваш баланс: {balance} рецептов*\n\n"
            f"{'😔 Рецепты закончились — купи пакет!' if balance == 0 else '✅ Можно готовить!'}"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💎 Купить рецепты", callback_data="buy"),
            InlineKeyboardButton("⬅️ Назад", callback_data="back_main")
        ]])
        if (img := _get_image_path("balance")):
            await query.message.delete()
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=img,
                caption=balance_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
        else:
            await query.edit_message_text(balance_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    elif data == "buy":
        await query.edit_message_text(
            pay.format_packages_text() + "\n\n👆 Выбери пакет:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_packages_keyboard()
        )

    elif data == "help":
        await query.edit_message_text(
            "❓ *Как пользоваться AI-Шефом:*\n\n"
            "1. Напиши `/recipe` + ингредиенты или описание блюда\n"
            "2. Подожди 5-15 секунд\n"
            "3. Получи уникальный рецепт!\n\n"
            "*Примеры запросов:*\n"
            "• `курица, лимон, тимьян`\n"
            "• `что-то вкусное из кабачков`\n"
            "• `быстрый ужин до 20 минут`\n"
            "• `веганский торт без сахара`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Назад", callback_data="back_main")
            ]])
        )

    elif data == "back_main":
        await query.edit_message_text(
            f"👨‍🍳 *AI-Шеф*\n\nЧто будем готовить?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_main_keyboard()
        )

    elif data.startswith("buy_"):
        # Пользователь выбрал конкретный пакет
        package_key = data.replace("buy_", "")
        await process_purchase(query, user.id, package_key)

    elif data == "recipe_from_msg":
        # Рецепт из текста сообщения (кнопка «Сделать рецепт из этого!»)
        reply_to = query.message.reply_to_message
        if reply_to and reply_to.text and reply_to.text.strip():
            user_input = reply_to.text.strip()
            await _generate_recipe_for_user(
                bot=context.bot,
                chat_id=query.message.chat_id,
                user_id=user.id,
                user_input=user_input,
                edit_message=query.message,
            )
        else:
            await query.edit_message_text(
                "❌ Не удалось получить текст. Напиши `/recipe` и свой запрос.",
                parse_mode=ParseMode.MARKDOWN
            )

    elif data.startswith("check_payment_"):
        # Проверка статуса платежа
        payment_id = data.replace("check_payment_", "")
        await check_payment(query, user.id, payment_id)


async def process_purchase(query, user_id: int, package_key: str):
    """Создаём платёж и отправляем ссылку пользователю."""
    if package_key not in PACKAGES:
        await query.edit_message_text("❌ Неверный пакет.")
        return

    pkg = PACKAGES[package_key]

    await query.edit_message_text(
        f"⏳ Создаю ссылку для оплаты...",
        parse_mode=ParseMode.MARKDOWN
    )

    try:
        payment_data = await pay.create_payment(user_id, package_key)

        # Сохраняем платёж в БД
        await db.save_payment(
            payment_id=payment_data["payment_id"],
            user_id=user_id,
            package_key=package_key,
            amount=payment_data["amount"],
            recipes_count=payment_data["recipes_count"]
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Оплатить", url=payment_data["payment_url"])],
            [InlineKeyboardButton(
                "✅ Проверить оплату",
                callback_data=f"check_payment_{payment_data['payment_id']}"
            )],
            [InlineKeyboardButton("⬅️ Назад", callback_data="buy")]
        ])

        await query.edit_message_text(
            f"💎 *{pkg['name']}*\n\n"
            f"📖 {pkg['recipes']} рецептов\n"
            f"💰 {pkg['price']} рублей\n\n"
            f"1. Нажми «Оплатить»\n"
            f"2. Заверши оплату на сайте\n"
            f"3. Нажми «Проверить оплату»\n\n"
            f"_ID платежа: {payment_data['payment_id'][:16]}..._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )

    except Exception as e:
        await query.edit_message_text(
            f"❌ Ошибка создания платежа. Попробуй позже.\n`{e}`",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.error(f"Ошибка создания платежа для {user_id}: {e}")


async def check_payment(query, user_id: int, payment_id: str):
    """Проверяем статус платежа и зачисляем токены."""
    await query.edit_message_text("⏳ Проверяю оплату...")

    status = await pay.check_payment_status(payment_id)

    if status == "succeeded":
        # Зачисляем рецепты
        await db.update_payment_status(payment_id, "succeeded")
        balance = await db.get_balance(user_id)

        await query.edit_message_text(
            f"🎉 *Оплата прошла успешно!*\n\n"
            f"💳 Ваш новый баланс: *{balance} рецептов*\n\n"
            f"Приятной готовки! 👨‍🍳",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🍳 Создать рецепт", callback_data="new_recipe")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")],
            ])
        )

    elif status == "canceled":
        await query.edit_message_text(
            "❌ *Платёж отменён*\n\nПопробуй снова или выбери другой пакет.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=get_packages_keyboard()
        )

    else:  # pending
        await query.edit_message_text(
            "⏳ *Платёж ещё обрабатывается*\n\n"
            "Это обычно занимает 1-2 минуты.\n"
            "Попробуй проверить снова через минуту.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔄 Проверить снова",
                    callback_data=f"check_payment_{payment_id}"
                )
            ]])
        )


# ======================================================
# ОБРАБОТЧИК ОБЫЧНЫХ СООБЩЕНИЙ
# ======================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатываем текстовые сообщения (не команды).
    Предлагаем сделать рецепт из текста — по кнопке сразу запускается генерация.
    """
    text = (update.message.text or "").strip()
    if not text:
        return

    user = update.effective_user
    await db.get_or_create_user(user.id, user.username or "", user.full_name or "")

    preview = text[:50] + "…" if len(text) > 50 else text
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🍳 Сделать рецепт из этого!", callback_data="recipe_from_msg")
    ]])

    await update.message.reply_text(
        f"🤔 Хочешь рецепт с «*{_escape_md(preview)}*»?\n\n"
        "Нажми кнопку ниже или напиши `/recipe` и свой запрос.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )


# ======================================================
# ПОСТРОЕНИЕ APPLICATION (для webhook и polling)
# ======================================================

async def post_init(application: Application):
    """Инициализация после запуска — создаём БД."""
    await db.init_db()
    bot_info = await application.bot.get_me()
    bot_id = TELEGRAM_BOT_TOKEN.split(":")[0] if TELEGRAM_BOT_TOKEN else "?"
    logger.info(f"🤖 AI-Шеф запущен! Бот: @{bot_info.username} (ID: {bot_id})")


def build_application() -> Application:
    """Собирает и возвращает настроенное приложение. Используется для polling и webhook."""
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.exception("❌ Ошибка в обработчике: %s", context.error)
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app


def main():
    """Главная функция — сборка и запуск бота (polling)."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN не задан! Проверь .env файл")
    token_id = TELEGRAM_BOT_TOKEN.split(":")[0]
    logger.info(f"📋 Загружен токен: ID={token_id} (сверь с @BotFather → API Token)")
    if not YANDEX_FOLDER_ID:
        raise ValueError("❌ YANDEX_FOLDER_ID не задан! Проверь .env файл")
    if not YANDEX_API_KEY:
        raise ValueError("❌ YANDEX_API_KEY не задан! Проверь .env файл")
    logger.info(f"🧠 Используем модель: {YANDEX_MODEL} (Yandex AI Studio)")

    app = build_application()
    logger.info("🚀 Запускаем AI-Шефа (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
