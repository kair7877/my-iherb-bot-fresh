import asyncio
import logging
import os
import re
import sys
from datetime import datetime

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Можно указать несколько chat_id через запятую:
# TELEGRAM_CHAT_IDS=-1001234567890,123456789
CHAT_IDS_RAW = os.getenv("TELEGRAM_CHAT_IDS", "").strip()

CHECK_INTERVAL = 300  # 5 минут

# Минимальная скидка
MIN_DISCOUNT = 20

# Порт Render
PORT = int(os.getenv("PORT", "10000"))

# Сколько карточек проверять
MAX_PRODUCTS = 100


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("iherb-bot")


# ============================================================
# TELEGRAM CHAT ID
# ============================================================

def get_chat_ids():
    result = []

    if CHAT_IDS_RAW:
        for item in CHAT_IDS_RAW.split(","):
            item = item.strip()
            if item:
                try:
                    result.append(int(item))
                except ValueError:
                    logger.warning("⚠️ Неверный CHAT_ID: %s", item)

    return result


CHAT_IDS = get_chat_ids()


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# КНОПКИ
# ============================================================

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔥 Получить скидки"),
            KeyboardButton(text="📊 Статус"),
        ],
    ],
    resize_keyboard=True,
)


# ============================================================
# СОСТОЯНИЕ
# ============================================================

is_running = False
check_number = 0
last_check_time = None
last_found = 0
last_sent = 0

# Чтобы одна и та же скидка не отправлялась бесконечно
sent_products = set()


# ============================================================
# HTTP SERVER ДЛЯ RENDER
# ============================================================

async def health(request):
    return web.Response(
        text="iHerb discount bot is running",
        status=200
    )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info("🌐 Web server запущен на порту %s", PORT)


# ============================================================
# ПАРСИНГ ЦЕН
# ============================================================

def extract_prices(text):
    prices = []

    patterns = [
        r'[\$€£]\s?[\d,.]+',
        r'[\d,.]+\s?(?:USD|EUR|GBP)',
    ]

    for pattern in patterns:
        found = re.findall(pattern, text, re.I)

        for value in found:
            value = value.replace(",", ".")

            number = re.search(r"\d+(?:\.\d+)?", value)

            if number:
                try:
                    price = float(number.group())
                    if price > 0:
                        prices.append(price)
                except Exception:
                    pass

    return prices


def extract_discount(text):
    patterns = [
        r'(\d{1,2})\s?%\s?(?:off|скид)',
        r'(?:скидка|discount)\s?(\d{1,2})\s?%',
        r'-\s?(\d{1,2})\s?%',
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.I)

        if match:
            try:
                value = int(match.group(1))

                if 1 <= value <= 90:
                    return value
            except Exception:
                pass

    return None


# ============================================================
# ПОЛУЧЕНИЕ СТРАНИЦЫ
# ============================================================

async def fetch_iherb_page():
    url = "https://www.iherb.com/c/sale"

    headers = {
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "user-agent": (
            "Mozilla/5.0 (Linux; Android 10; K) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0.0.0 Mobile Safari/537.36"
        ),
    }

    try:
        async with AsyncSession(
            impersonate="chrome",
            timeout=30,
        ) as session:

            response = await session.get(
                url,
                headers=headers,
            )

            logger.info(
                "🌐 iHerb HTTP status: %s",
                response.status_code
            )

            if response.status_code != 200:
                logger.error(
                    "❌ iHerb вернул HTTP %s",
                    response.status_code
                )
                return None

            return response.text

    except Exception as e:
        logger.exception(
            "❌ Ошибка подключения к iHerb: %s",
            e
        )

        return None


# ============================================================
# ПОИСК ТОВАРОВ
# ============================================================

def parse_products(html):
    soup = BeautifulSoup(html, "html.parser")

    products = []

    cards = soup.select(
        "[data-product-id], "
        "[data-testid*='product'], "
        ".product-cell, "
        ".product-cell-container"
    )

    if not cards:
        cards = soup.select("a[href*='/pr/']")

    logger.info(
        "🔎 Найдено элементов-кандидатов: %s",
        len(cards)
    )

    seen = set()

    for card in cards[:MAX_PRODUCTS]:

        try:
            text = card.get_text(
                " ",
                strip=True
            )

            if not text:
                continue

            link = ""

            if getattr(card, "name", None) == "a":
                link = card.get("href", "")

            if not link:
                a = card.find("a", href=True)

                if a:
                    link = a.get("href", "")

            if link.startswith("/"):
                link = "https://www.iherb.com" + link

            if not link:
                continue

            if link in seen:
                continue

            seen.add(link)

            title = text[:180]

            discount = extract_discount(text)

            prices = extract_prices(text)

            products.append(
                {
                    "title": title,
                    "url": link,
                    "discount": discount,
                    "prices": prices,
                    "text": text,
                }
            )

        except Exception:
            continue

    return products


# ============================================================
# ПРОВЕРКА СКИДОК
# ============================================================

async def check_discounts():
    global check_number
    global last_check_time
    global last_found

    check_number += 1
    last_check_time = datetime.now()

    logger.info("")
    logger.info("=" * 60)
    logger.info(
        "🔎 АВТОМАТИЧЕСКАЯ ПРОВЕРКА #%s",
        check_number
    )
    logger.info(
        "🕐 Время: %s",
        last_check_time.strftime("%d.%m.%Y %H:%M:%S")
    )
    logger.info("=" * 60)

    html = await fetch_iherb_page()

    if not html:
        logger.error("❌ Не удалось получить страницу iHerb")
        last_found = 0
        return []

    products = parse_products(html)

    logger.info(
        "📦 Обработано товаров: %s",
        len(products)
    )

    result = []

    for index, product in enumerate(products, 1):

        discount = product.get("discount")

        logger.info(
            "🔎 CARD #%s | %s | discount=%s | prices=%s",
            index,
            product["title"][:100],
            discount,
            product["prices"],
        )

        if discount is None:
            continue

        if discount < MIN_DISCOUNT:
            continue

        result.append(product)

    last_found = len(result)

    logger.info(
        "🔥 Найдено товаров со скидкой %s%%+: %s",
        MIN_DISCOUNT,
        len(result)
    )

    return result


# ============================================================
# ОТПРАВКА В TELEGRAM
# ============================================================

async def send_discount(product, target_chat_ids=None):
    global last_sent

    if target_chat_ids is None:
        target_chat_ids = CHAT_IDS

    title = product["title"]
    discount = product["discount"]
    url = product["url"]

    message = (
        "🔥 <b>НАЙДЕНА СКИДКА iHERB</b>\n\n"
        f"📦 <b>{title}</b>\n\n"
        f"💥 Скидка: <b>{discount}%</b>\n\n"
        f"🔗 <a href=\"{url}\">Открыть товар</a>\n\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    success = 0

    for chat_id in target_chat_ids:

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )

            success += 1
            last_sent += 1

            logger.info(
                "📤 Отправлена скидка → Telegram %s",
                chat_id
            )

        except Exception as e:

            logger.error(
                "❌ Telegram %s: %s",
                chat_id,
                e
            )

    return success


# ============================================================
# ОТПРАВКА РЕЗУЛЬТАТОВ
# ============================================================

async def process_discounts(products, target_chat_ids=None):
    if target_chat_ids is None:
        target_chat_ids = CHAT_IDS

    if not products:
        logger.info(
            "ℹ️ Подходящих скидок не найдено."
        )
        return 0

    sent = 0

    for product in products:

        # Уникальный ключ
        key = (
            product["url"],
            product["discount"]
        )

        # Не отправляем одно и то же бесконечно
        if key in sent_products:
            continue

        count = await send_discount(
            product,
            target_chat_ids
        )

        if count > 0:
            sent_products.add(key)
            sent += count

    logger.info(
        "📤 Отправлено новых скидок: %s",
        sent
    )

    return sent


# ============================================================
# АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
# ============================================================

async def automatic_monitor():
    global is_running

    is_running = True

    logger.info("")
    logger.info("🤖 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН")
    logger.info(
        "⏰ Интервал проверки: %s секунд",
        CHECK_INTERVAL
    )

    # Первая проверка СРАЗУ после запуска
    try:
        products = await check_discounts()

        await process_discounts(products)

    except Exception as e:
        logger.exception(
            "❌ Ошибка первой проверки: %s",
            e
        )

    while True:

        logger.info(
            "💤 Следующая автоматическая проверка через 5 минут..."
        )

        await asyncio.sleep(CHECK_INTERVAL)

        try:

            products = await check_discounts()

            await process_discounts(products)

        except asyncio.CancelledError:
            logger.info(
                "🛑 Автоматический мониторинг остановлен."
            )
            raise

        except Exception as e:
            logger.exception(
                "❌ Ошибка автоматической проверки: %s",
                e
            )

            # Не даём одной ошибке убить мониторинг
            await asyncio.sleep(10)


# ============================================================
# КОМАНДА /start
# ============================================================

@dp.message(CommandStart())
async def cmd_start(message: Message):

    await message.answer(
        "🤖 <b>iHerb Discount Bot</b>\n\n"
        "✅ Бот работает.\n"
        "🔄 Автоматический мониторинг активен.\n"
        "⏰ Проверка каждые 5 минут.\n\n"
        "Нажмите кнопку для ручной проверки.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


# ============================================================
# КНОПКА ПОЛУЧИТЬ СКИДКИ
# ============================================================

@dp.message(F.text == "🔥 Получить скидки")
async def manual_check(message: Message):

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас..."
    )

    try:

        products = await check_discounts()

        # Отправляем именно пользователю,
        # даже если его chat_id ещё не прописан в Render.
        await process_discounts(
            products,
            [message.chat.id]
        )

        if not products:

            await message.answer(
                "ℹ️ Сейчас скидок 20%+ не найдено.\n\n"
                "🤖 Автоматический мониторинг продолжает работать."
            )

    except Exception as e:

        logger.exception(
            "❌ Ошибка ручной проверки: %s",
            e
        )

        await message.answer(
            "❌ При проверке произошла ошибка.\n"
            "Посмотрите Render Logs."
        )


# ============================================================
# СТАТУС
# ============================================================

@dp.message(F.text == "📊 Статус")
async def status(message: Message):

    if last_check_time:

        time_text = last_check_time.strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    else:
        time_text = "проверок ещё не было"

    await message.answer(
        "📊 <b>СТАТУС БОТА</b>\n\n"
        f"🤖 Мониторинг: "
        f"{'🟢 работает' if is_running else '🔴 остановлен'}\n"
        f"🔎 Проверок выполнено: <b>{check_number}</b>\n"
        f"🕐 Последняя проверка: <b>{time_text}</b>\n"
        f"🔥 Найдено в последней проверке: <b>{last_found}</b>\n"
        f"📤 Отправлено: <b>{last_sent}</b>\n"
        f"⏰ Интервал: <b>5 минут</b>",
        parse_mode="HTML",
    )


# ============================================================
# ОТПРАВКА СООБЩЕНИЯ О ЗАПУСКЕ
# ============================================================

async def send_startup_message():
    """
    Сразу после запуска Render отправляет сообщение в Telegram.
    """

    if not CHAT_IDS:

        logger.warning(
            "⚠️ TELEGRAM_CHAT_IDS не задан."
        )

        logger.warning(
            "⚠️ Поэтому сообщение 'БОТ ЗАПУЩЕН' "
            "автоматически отправить некуда."
        )

        return

    startup_text = (
        "🤖 <b>БОТ ЗАПУЩЕН</b>\n\n"
        "✅ iHerb Discount Bot успешно запущен на Render.\n"
        "🟢 Автоматический мониторинг активирован.\n"
        "🔎 Первая проверка выполняется сейчас.\n"
        "⏰ Следующая проверка — через 5 минут.\n\n"
        "Вы можете просто ждать — "
        "нажимать «Получить скидки» для автоматической работы НЕ нужно."
    )

    for chat_id in CHAT_IDS:

        try:

            await bot.send_message(
                chat_id=chat_id,
                text=startup_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )

            logger.info(
                "🚀 Сообщение 'БОТ ЗАПУЩЕН' отправлено → %s",
                chat_id
            )

        except Exception as e:

            logger.error(
                "❌ Не удалось отправить сообщение о запуске "
                "в Telegram %s: %s",
                chat_id,
                e
            )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("")
    logger.info("=" * 60)
    logger.info("🤖 iHERB DISCOUNT BOT")
    logger.info("=" * 60)

    if not BOT_TOKEN:

        logger.error(
            "❌ BOT_TOKEN не найден!"
        )

        logger.error(
            "Добавьте BOT_TOKEN в Environment Variables Render."
        )

        return

    if not CHAT_IDS:

        logger.warning(
            "⚠️ TELEGRAM_CHAT_IDS не задан."
        )

        logger.warning(
            "⚠️ Автоматическое сообщение о запуске "
            "отправить будет невозможно."
        )

    logger.info(
        "📱 Telegram chat IDs: %s",
        CHAT_IDS
    )

    # Проверяем Telegram
    try:

        me = await bot.get_me()

        logger.info(
            "✅ Telegram подключен: @%s",
            me.username
        )

    except Exception as e:

        logger.exception(
            "❌ Не удалось подключиться к Telegram: %s",
            e
        )

        return

    # Web server для Render
    await start_web_server()

    # СООБЩЕНИЕ О ЗАПУСКЕ
    await send_startup_message()

    # Автоматический мониторинг
    monitor_task = asyncio.create_task(
        automatic_monitor()
    )

    logger.info(
        "🚀 Все системы запущены."
    )

    logger.info(
        "🔄 Бот будет автоматически проверять iHerb каждые 5 минут."
    )

    try:

        await dp.start_polling(bot)

    finally:

        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "🛑 Бот остановлен."
        )

    except Exception as e:

        logger.exception(
            "💥 КРИТИЧЕСКАЯ ОШИБКА: %s",
            e
        )
