# Деплой AI-Шеф на Vercel

## 1. Подготовка

Убедись, что в корне есть `vercel.json`, `api/index.py`, `api/main.py`.

## 2. Переменные окружения (Vercel → Settings → Environment Variables)

Добавь все переменные из `.env`:

| Переменная | Описание |
|------------|----------|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather |
| `YANDEX_FOLDER_ID` | ID каталога Yandex Cloud |
| `YANDEX_API_KEY` | API-ключ Yandex Cloud |
| `YOOKASSA_SHOP_ID` | ID магазина ЮKassa |
| `YOOKASSA_SECRET_KEY` | Секретный ключ ЮKassa |
| `YOOKASSA_RETURN_URL` | URL возврата (например `https://t.me/your_bot`) |
| `ADMIN_IDS` | Telegram user_id через запятую |

**Важно:** На Vercel можно задать `DB_PATH=/tmp/ai_chef.db` — данные в `/tmp` не сохраняются между cold start. Для продакшена с сохранением данных используй внешнюю БД (например Turso, PlanetScale).

## 3. Деплой

```bash
vercel
# или
vercel --prod
```

После деплоя получишь URL вида `https://ai-chef-xxx.vercel.app`.

## 4. Настройка webhook Telegram

После деплоя укажи webhook (замени `YOUR_VERCEL_URL` на твой URL):

```bash
curl "https://api.telegram.org/bot<ТВОЙ_ТОКЕН>/setWebhook?url=https://YOUR_VERCEL_URL/api/webhook"
```

Или в браузере:
```
https://api.telegram.org/bot<ТОКЕН>/setWebhook?url=https://ai-chef-xxx.vercel.app/api/webhook
```

## 5. Webhook ЮKassa

1. Зайди в [личный кабинет ЮKassa](https://yookassa.ru/)
2. Настройки → HTTP-уведомления
3. URL уведомлений: `https://YOUR_VERCEL_URL/api/yookassa-webhook`
4. События: отметь `payment.succeeded`

После оплаты ЮKassa пришлёт уведомление, токены зачислятся автоматически, пользователь получит сообщение в Telegram.

## 6. Прогрев (опционально)

Чтобы уменьшить cold start при первом сообщении, настрой cron в Vercel для пинга `/api/health` раз в 1–2 минуты.

## 7. Локальный режим (polling)

Для разработки используй `python bot.py` — бот работает в режиме polling и не требует webhook.

Для переключения на webhook снова:
```bash
curl "https://api.telegram.org/bot<ТОКЕН>/setWebhook?url=https://..."
```

Для возврата к polling:
```bash
curl "https://api.telegram.org/bot<ТОКЕН>/deleteWebhook"
```
