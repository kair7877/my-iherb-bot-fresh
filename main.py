import os
import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiohttp import web

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ВАЖНО:
# Укажи здесь Telegram ID чата, куда бот должен присылать
# уведомление о запуске и найденные скидки.
#
# Например:
# CHAT_ID = -1001234567890
#
# Если переменная CHAT_ID уже создана в Render → Environment,
# она будет использована автоматически.
CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL = 300  # 5 минут

# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не задан. "
        "Добавь переменную BOT_TOKEN в Render → Environment."
    )

if not CHAT_ID:
    raise RuntimeError(
        "❌ CHAT_ID не задан. "
        "Добавь переменную CHAT_ID в Render → Environment."
    )

try:
    CHAT_ID_VALUE = int(CHAT_ID)
except ValueError:
    raise RuntimeError("❌ CHAT_ID должен быть числом.")

# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ============================================================
# КНОПКИ
# ============================================================

keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔥 Получить скидки")],
        [KeyboardButton(text="📊 Статус бота")],
    ],
    resize_keyboard=True,
)

# ============================================================
# ПРОВЕРКА СВЯЗИ С TELEGRAM
# ============================================================

async def telegram_startup():
    me = await bot.get_me()

    logger.info(
        "🤖 Telegram подключен: @%s | ID=%s",
        me.username,
        me.id,
    )

    # СРАЗУ отправляем сообщение при запуске
    try:
        await bot.send_message(
            CHAT_ID_VALUE,
            "🟢 <b>БОТ ЗАПУЩЕН</b>\n\n"
            "🤖 iHerb-бот успешно подключился к Telegram.\n"
            "🔎 Мониторинг скидок запущен.\n"
            "⏱ Проверка каждые 5 минут.\n\n"
            "Больше не нужно заходить в бота и нажимать кнопку "
            "для запуска мониторинга.",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

        logger.info("✅ Сообщение «БОТ ЗАПУЩЕН» отправлено.")

    except Exception as e:
        logger.error(
            "❌ Не удалось отправить сообщение о запуске: %s",
            e,
        )

# ============================================================
# КОМАНДА /START
# ============================================================

@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer(
        "🟢 <b>iHerb бот работает.</b>\n\n"
        "🔎 Мониторинг скидок идет автоматически.\n"
        "⏱ Новая проверка каждые 5 минут.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

# ============================================================
# КНОПКА СКИДОК
# ============================================================

@router.message(lambda message: message.text == "🔥 Получить скидки")
async def get_discounts(message: Message):
    await message.answer(
        "🔎 Выполняю проверку скидок...\n"
        "Мониторинг также работает автоматически.",
        reply_markup=keyboard,
    )

    # Здесь запускается немедленная проверка
    await check_discounts(manual=True)


# ============================================================
# СТАТУС
# ============================================================

@router.message(lambda message: message.text == "📊 Статус бота")
async def bot_status(message: Message):
    now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    await message.answer(
        "🟢 <b>БОТ РАБОТАЕТ</b>\n\n"
        f"🕐 Время проверки: {now}\n"
        "🔎 Автоматический мониторинг: ВКЛ\n"
        "⏱ Интервал: 5 минут\n\n"
        "Следующая проверка произойдет автоматически.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )

# ============================================================
# ПОЛУЧЕНИЕ ТОВАРОВ
# ============================================================

async def get_iherb_products():
    """
    Здесь должна находиться твоя текущая функция получения
    товаров iHerb.

    Если у тебя уже есть рабочий парсер из предыдущей версии,
    вставь его содержимое сюда.

    Ниже временный безопасный вариант.
    """

    logger.info("🌐 Запрашиваю товары iHerb...")

    # ВАЖНО:
    # Сейчас возвращаем пустой список, чтобы бот стабильно
    # запускался даже без парсера.
    #
    # Твой рабочий код парсинга можно подключить сюда.

    return []

# ============================================================
# ПРОВЕРКА СКИДОК
# ============================================================

async def check_discounts(manual=False):

    started = datetime.now()

    if manual:
        logger.info("👆 Запущена ручная проверка скидок.")
    else:
        logger.info("🔄 Автоматическая проверка скидок.")

    try:
        products = await get_iherb_products()

        logger.info(
            "📦 Получено товаров: %s",
            len(products),
        )

        discounts = []

        for product in products:

            try:
                name = product.get("name", "Товар")
                price = product.get("price")
                old_price = product.get("old_price")
                discount = product.get("discount", 0)
                url = product.get("url", "")

                if discount >= 20:
                    discounts.append(product)

            except Exception as e:
                logger.error(
                    "Ошибка обработки товара: %s",
                    e,
                )

        # ----------------------------------------------------
        # ЕСЛИ НАШЛИ СКИДКИ
        # ----------------------------------------------------

        if discounts:

            logger.info(
                "🔥 Найдено скидок: %s",
                len(discounts),
            )

            for product in discounts:

                name = product.get("name", "Товар")
                price = product.get("price", "?")
                old_price = product.get("old_price", "?")
                discount = product.get("discount", 0)
                url = product.get("url", "")

                text = (
                    "🔥 <b>НАЙДЕНА СКИДКА!</b>\n\n"
                    f"📦 <b>{name}</b>\n\n"
                    f"💰 Было: <s>{old_price}</s>\n"
                    f"🔥 Сейчас: <b>{price}</b>\n"
                    f"📉 Скидка: <b>{discount}%</b>\n"
                )

                if url:
                    text += f"\n🔗 {url}"

                try:
                    await bot.send_message(
                        CHAT_ID_VALUE,
                        text,
                        parse_mode="HTML",
                    )

                    logger.info(
                        "📤 Отправлена скидка: %s",
                        name[:80],
                    )

                except Exception as e:
                    logger.error(
                        "❌ Ошибка отправки Telegram: %s",
                        e,
                    )

        # ----------------------------------------------------
        # ЕСЛИ СКИДОК НЕТ
        # ----------------------------------------------------

        else:

            logger.info(
                "ℹ️ Подходящих скидок не найдено."
            )

            # При автоматической проверке каждые 5 минут
            # НЕ отправляем сообщение каждый раз,
            # чтобы Telegram не превращался в спам.

            if manual:

                await bot.send_message(
                    CHAT_ID_VALUE,
                    "ℹ️ <b>Сейчас скидок 20%+ не найдено.</b>\n\n"
                    "🔎 Автоматический мониторинг продолжает работать.",
                    parse_mode="HTML",
                )

        elapsed = (
            datetime.now() - started
        ).total_seconds()

        logger.info(
            "✅ Проверка завершена за %.1f сек.",
            elapsed,
        )

    except Exception as e:

        logger.exception(
            "❌ Ошибка во время проверки скидок."
        )

        # Сообщаем владельцу о серьезной ошибке
        try:
            await bot.send_message(
                CHAT_ID_VALUE,
                "⚠️ <b>Ошибка мониторинга</b>\n\n"
                f"<code>{str(e)[:1000]}</code>\n\n"
                "🔄 Бот продолжит работу и попробует снова.",
                parse_mode="HTML",
            )
        except Exception:
            pass

# ============================================================
# АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
# ============================================================

async def monitor_loop():

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН."
    )

    # Первая проверка сразу после запуска
    await check_discounts()

    while True:

        try:

            logger.info(
                "💤 Следующая проверка через %s секунд...",
                CHECK_INTERVAL,
            )

            await asyncio.sleep(CHECK_INTERVAL)

            logger.info(
                "⏰ Время следующей проверки."
            )

            await check_discounts()

        except asyncio.CancelledError:

            logger.info(
                "🛑 Мониторинг остановлен."
            )

            raise

        except Exception:

            logger.exception(
                "❌ Ошибка в monitor_loop."
            )

            # Даже если произошла ошибка,
            # цикл не должен умереть.
            await asyncio.sleep(30)

# ============================================================
# WEB SERVER ДЛЯ RENDER
# ============================================================

async def health(request):
    return web.Response(
        text="iHerb bot is running 🟢"
    )

async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        health,
    )

    app.router.add_get(
        "/health",
        health,
    )

    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port,
    )

    await site.start()

    logger.info(
        "🌐 Web server запущен на порту %s",
        port,
    )

    return runner

# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "============================================================"
    )

    logger.info(
        "🚀 ЗАПУСК iHERB BOT"
    )

    logger.info(
        "============================================================"
    )

    # Проверяем Telegram
    await telegram_startup()

    # Запускаем web-сервер Render
    web_runner = await start_web_server()

    # Запускаем автоматический мониторинг
    monitor_task = asyncio.create_task(
        monitor_loop()
    )

    logger.info(
        "🟢 БОТ ПОЛНОСТЬЮ ЗАПУЩЕН И РАБОТАЕТ АВТОМАТИЧЕСКИ."
    )

    try:

        # Telegram polling работает постоянно
        await dp.start_polling(
            bot
        )

    finally:

        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        await web_runner.cleanup()

        await bot.session.close()

# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "🛑 Бот остановлен вручную."
        )

    except Exception:

        logger.exception(
            "💥 КРИТИЧЕСКАЯ ОШИБКА."
        )
