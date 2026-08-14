import asyncio
import json
import logging
import os
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramRetryAfter,
    TelegramConflictError,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================

# НИКОГДА НЕ ВСТАВЛЯЙТЕ ТОКЕН СЮДА.
# Он берётся из Render → Environment → BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# CHAT_ID БОЛЬШЕ НЕ ОБЯЗАТЕЛЕН.
# Если он есть в Render и правильный — используем его.
# Если неправильный — автоматически игнорируем.
ENV_CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = 300  # 5 минут

MIN_DISCOUNT_PERCENT = 20

MAX_DEALS_PER_CHECK = 10

KZT_EXCHANGE_RATE = 540

MARGIN_MARKUP_PERCENT = 35

CACHE_FILE = "sent_deals.json"

CHAT_FILE = "chat_id.json"

# Ваши основные бренды
TARGET_BRANDS = [
    "California Gold Nutrition",
    "NOW Foods",
    "Doctor's Best",
    "Solgar",
]

# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

# ============================================================
# ПРОВЕРКА BOT TOKEN
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не найден.\n"
        "Открой Render → Environment Variables "
        "и добавь BOT_TOKEN."
    )

# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)

dp = Dispatcher()

# ============================================================
# ПЕРЕМЕННЫЕ
# ============================================================

chat_id = None

sent_deals_cache = set()

monitor_task = None

last_check_time = None

checks_count = 0

last_deals_count = 0

# ============================================================
# КЛАВИАТУРА
# ============================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔥 Получить скидки"),
        ],
        [
            KeyboardButton(text="📊 Статус бота"),
        ],
    ],
    resize_keyboard=True,
)

# ============================================================
# ЗАГРУЗКА CHAT ID
# ============================================================

def load_chat_id():
    global chat_id

    # --------------------------------------------------------
    # Сначала пытаемся взять из файла
    # --------------------------------------------------------

    try:
        if os.path.exists(CHAT_FILE):

            with open(
                CHAT_FILE,
                "r",
                encoding="utf-8",
            ) as f:

                data = json.load(f)

            saved_id = data.get("chat_id")

            if saved_id is not None:

                chat_id = int(saved_id)

                logger.info(
                    "💾 CHAT_ID загружен из файла: %s",
                    chat_id,
                )

                return

    except Exception as e:

        logger.warning(
            "⚠️ Не удалось загрузить CHAT_ID из файла: %s",
            e,
        )

    # --------------------------------------------------------
    # Если файла нет — пробуем Render Environment
    # --------------------------------------------------------

    if ENV_CHAT_ID:

        try:

            chat_id = int(ENV_CHAT_ID)

            logger.info(
                "🌐 CHAT_ID загружен из Render Environment: %s",
                chat_id,
            )

            return

        except ValueError:

            logger.warning(
                "⚠️ CHAT_ID в Render неправильный: %s",
                ENV_CHAT_ID,
            )

    chat_id = None

    logger.info(
        "ℹ️ CHAT_ID пока неизвестен."
    )


# ============================================================
# СОХРАНЕНИЕ CHAT ID
# ============================================================

def save_chat_id(new_chat_id):

    global chat_id

    try:

        chat_id = int(new_chat_id)

        with open(
            CHAT_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                {
                    "chat_id": chat_id
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        logger.info(
            "💾 CHAT_ID сохранён: %s",
            chat_id,
        )

        return True

    except Exception as e:

        logger.error(
            "❌ Ошибка сохранения CHAT_ID: %s",
            e,
        )

        return False


# ============================================================
# CACHE
# ============================================================

def load_cache():

    global sent_deals_cache

    try:

        if not os.path.exists(CACHE_FILE):

            sent_deals_cache = set()

            logger.info(
                "💾 Cache пока пустой."
            )

            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        if isinstance(data, list):

            sent_deals_cache = set(
                str(x)
                for x in data
            )

        logger.info(
            "💾 Загружено отправленных товаров: %s",
            len(sent_deals_cache),
        )

    except Exception as e:

        logger.error(
            "❌ Ошибка загрузки cache: %s",
            e,
        )

        sent_deals_cache = set()


def save_cache():

    try:

        data = list(
            sent_deals_cache
        )[-5000:]

        with open(
            CACHE_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:

        logger.error(
            "❌ Ошибка сохранения cache: %s",
            e,
        )


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def parse_price(text):

    if not text:
        return None

    text = str(text)

    text = text.replace(
        ",",
        ".",
    )

    match = re.search(
        r"\d+(?:\.\d+)?",
        text,
    )

    if not match:
        return None

    try:

        return float(
            match.group()
        )

    except Exception:

        return None


def normalize_url(url):

    if not url:
        return ""

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return (
            "https://www.iherb.com"
            + url
        )

    if url.startswith("http"):
        return url

    return (
        "https://www.iherb.com/"
        + url
    )


def find_brand(title):

    title_lower = title.lower()

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:

            return brand

    return ""


def extract_product_id(link):

    if not link:
        return ""

    patterns = [
        r"/(\d+)$",
        r"/(\d+)\?",
        r"/pr/[^/]+/(\d+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            link,
        )

        if match:
            return match.group(1)

    return link


# ============================================================
# CURL_CFFI
# ============================================================

try:

    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True

    logger.info(
        "✅ curl_cffi доступен."
    )

except ImportError:

    HAS_CURL_CFFI = False

    logger.warning(
        "⚠️ curl_cffi не установлен. "
        "Используем httpx."
    )


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/124.0.0.0 "
        "Safari/537.36"
    ),
    "Accept-Language": (
        "ru-RU,ru;q=0.9,"
        "en-US;q=0.8,"
        "en;q=0.7"
    ),
    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,"
        "image/webp,"
        "*/*;q=0.8"
    ),
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ============================================================
# ПОЛУЧЕНИЕ HTML IHERB
# ============================================================

async def get_iherb_html():

    urls_to_try = [

        "https://www.iherb.com/c/specials",

        "https://kz.iherb.com/c/specials",

        "https://ru.iherb.com/c/specials",

    ]

    cookies = {
        "ih-pref": (
            "lan=ru-RU&currency=USD&country=KZ"
        ),
        "iherb-pref": (
            "lan=ru-RU&currency=USD&country=KZ"
        ),
    }

    # --------------------------------------------------------
    # CURL CFFI
    # --------------------------------------------------------

    if HAS_CURL_CFFI:

        for url in urls_to_try:

            for impersonate in [
                "chrome124",
                "chrome120",
                "chrome116",
            ]:

                try:

                    response = await asyncio.to_thread(
                        curl_requests.get,
                        url,
                        headers=HEADERS,
                        cookies=cookies,
                        impersonate=impersonate,
                        timeout=25,
                    )

                    logger.info(
                        "iHerb | %s | HTTP %s",
                        impersonate,
                        response.status_code,
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 3000
                    ):

                        logger.info(
                            "✅ iHerb HTML получен: %s символов",
                            len(response.text),
                        )

                        return response.text

                except Exception as e:

                    logger.warning(
                        "curl_cffi ошибка: %s",
                        e,
                    )

    # --------------------------------------------------------
    # HTTPX
    # --------------------------------------------------------

    for url in urls_to_try:

        try:

            async with httpx.AsyncClient(
                timeout=25,
                headers=HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url
                )

                logger.info(
                    "httpx | %s | HTTP %s",
                    url,
                    response.status_code,
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 3000
                ):

                    logger.info(
                        "✅ HTML получен через httpx."
                    )

                    return response.text

        except Exception as e:

            logger.warning(
                "httpx ошибка: %s",
                e,
            )

    logger.error(
        "❌ iHerb HTML получить не удалось."
    )

    return ""


# ============================================================
# ПАРСЕР IHERB
# ============================================================

async def fetch_iherb_specials():

    logger.info(
        "🔎 Начинаем проверку iHerb..."
    )

    html = await get_iherb_html()

    if not html:

        return []

    deals = []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        selectors = [

            ".product-cell-container",

            ".product-inner",

            ".product-card",

            "[data-qa='product-card']",

            ".product-tile",

            "[class*='product-cell']",

            "[class*='product-card']",

        ]

        product_cards = []

        for selector in selectors:

            try:

                cards = soup.select(
                    selector
                )

            except Exception:

                cards = []

            if cards:

                logger.info(
                    "🔍 %s → %s карточек",
                    selector,
                    len(cards),
                )

                product_cards.extend(
                    cards
                )

                if len(product_cards) >= 20:

                    break

        # ----------------------------------------------------
        # Если обычные карточки не нашли
        # ----------------------------------------------------

        if not product_cards:

            logger.warning(
                "⚠️ Обычные карточки не найдены."
            )

            # Пробуем ссылки на продукты
            links = soup.select(
                "a[href*='/pr/']"
            )

            logger.info(
                "🔎 Найдено product links: %s",
                len(links),
            )

            for link in links[:100]:

                parent = (
                    link.find_parent()
                    or link
                )

                product_cards.append(
                    parent
                )

        # ----------------------------------------------------
        # Убираем дубли
        # ----------------------------------------------------

        unique_cards = []

        seen_text = set()

        for card in product_cards:

            try:

                card_text = clean_text(
                    card.get_text(
                        " ",
                        strip=True,
                    )
                )

            except Exception:

                continue

            if not card_text:
                continue

            if card_text in seen_text:
                continue

            seen_text.add(
                card_text
            )

            unique_cards.append(
                card
            )

        logger.info(
            "📦 Уникальных карточек: %s",
            len(unique_cards),
        )

        # ----------------------------------------------------
        # ОБРАБОТКА КАРТОЧЕК
        # ----------------------------------------------------

        for index, card in enumerate(
            unique_cards,
            start=1,
        ):

            try:

                # ----------------------------
                # НАЗВАНИЕ
                # ----------------------------

                title_elem = (

                    card.select_one(
                        ".product-title"
                    )

                    or card.select_one(
                        "[class*='product-title']"
                    )

                    or card.select_one(
                        "[class*='title']"
                    )

                    or card.select_one(
                        "a[href*='/pr/']"
                    )
                )

                if not title_elem:

                    continue

                title = clean_text(
                    title_elem.get_text(
                        " ",
                        strip=True,
                    )
                )

                if not title:

                    continue

                # ----------------------------
                # ССЫЛКА
                # ----------------------------

                link_elem = (

                    card.select_one(
                        "a[href*='/pr/']"
                    )

                    or card.select_one(
                        "a.absolute-link"
                    )

                    or card.select_one(
                        "a[href]"
                    )
                )

                if not link_elem:

                    continue

                link = normalize_url(
                    link_elem.get(
                        "href",
                        "",
                    )
                )

                if not link:

                    continue

                # ----------------------------
                # ЦЕНЫ
                # ----------------------------

                price_values = []

                price_selectors = [

                    ".price",

                    ".price-discount",

                    ".price-current",

                    "[class*='price']",

                    "[data-qa*='price']",

                ]

                for selector in price_selectors:

                    try:

                        elems = card.select(
                            selector
                        )

                    except Exception:

                        elems = []

                    for elem in elems:

                        value = parse_price(
                            elem.get_text(
                                " ",
                                strip=True,
                            )
                        )

                        if value:

                            price_values.append(
                                value
                            )

                # Убираем дубли
                price_values = list(
                    dict.fromkeys(
                        price_values
                    )
                )

                if not price_values:

                    logger.info(
                        "⏭ CARD #%s | %s | цены не найдены",
                        index,
                        title[:70],
                    )

                    continue

                # ------------------------------------------------
                # Ищем OLD / NEW цены
                # ------------------------------------------------

                old_price = None
                new_price = None

                old_selectors = [

                    ".price-original",

                    ".price-old",

                    ".original-price",

                    "[class*='original']",

                    "[class*='old-price']",

                    "[class*='was-price']",

                ]

                for selector in old_selectors:

                    try:

                        elems = card.select(
                            selector
                        )

                    except Exception:

                        elems = []

                    for elem in elems:

                        candidate = parse_price(
                            elem.get_text(
                                " ",
                                strip=True,
                            )
                        )

                        if (
                            candidate
                            and candidate > 0
                        ):

                            old_price = candidate

                            break

                    if old_price:

                        break

                # Если old price не нашли,
                # используем максимальную цену
                # как старую, а минимальную как новую.

                if len(price_values) >= 2:

                    sorted_prices = sorted(
                        set(price_values)
                    )

                    if not new_price:

                        new_price = (
                            sorted_prices[0]
                        )

                    if not old_price:

                        old_price = (
                            sorted_prices[-1]
                        )

                elif len(price_values) == 1:

                    new_price = price_values[0]

                # ----------------------------
                # Если старой цены нет,
                # проверяем текст скидки
                # ----------------------------

                card_text = clean_text(
                    card.get_text(
                        " ",
                        strip=True,
                    )
                )

                discount_match = re.search(
                    r"(\d{1,3})\s*%",
                    card_text,
                )

                text_discount = None

                if discount_match:

                    try:

                        text_discount = int(
                            discount_match.group(1)
                        )

                    except Exception:

                        text_discount = None

                # ----------------------------
                # Рассчитываем скидку
                # ----------------------------

                discount_percent = 0

                if (
                    old_price
                    and new_price
                    and old_price > new_price
                ):

                    discount_percent = round(
                        (
                            1
                            - new_price
                            / old_price
                        )
                        * 100
                    )

                elif text_discount:

                    discount_percent = (
                        text_discount
                    )

                if (
                    discount_percent
                    < MIN_DISCOUNT_PERCENT
                ):

                    continue

                # ----------------------------
                # БРЕНД
                # ----------------------------

                brand = find_brand(
                    title
                )

                if (
                    TARGET_BRANDS
                    and not brand
                ):

                    continue

                # ----------------------------
                # ID
                # ----------------------------

                product_id = (
                    extract_product_id(
                        link
                    )
                )

                if not product_id:

                    product_id = link

                # ----------------------------
                # Добавляем товар
                # ----------------------------

                deals.append(
                    {
                        "id": str(
                            product_id
                        ),
                        "title": title,
                        "brand": brand,
                        "orig_price_usd": (
                            old_price
                            or new_price
                            or 0
                        ),
                        "discount_price_usd": (
                            new_price
                            or 0
                        ),
                        "discount_percent": (
                            discount_percent
                        ),
                        "link": link,
                    }
                )

                logger.info(
                    "🔥 DEAL | -%s%% | %s",
                    discount_percent,
                    title[:70],
                )

            except Exception as e:

                logger.debug(
                    "Ошибка карточки #%s: %s",
                    index,
                    e,
                )

        # ----------------------------------------------------
        # Уникальные товары
        # ----------------------------------------------------

        unique_deals = {}

        for deal in deals:

            unique_deals[
                deal["id"]
            ] = deal

        deals = list(
            unique_deals.values()
        )

        deals.sort(
            key=lambda x: x[
                "discount_percent"
            ],
            reverse=True,
        )

        logger.info(
            "🔥 Найдено подходящих скидок: %s",
            len(deals),
        )

        return deals

    except Exception as e:

        logger.exception(
            "❌ Ошибка парсинга iHerb: %s",
            e,
        )

        return []


# ============================================================
# ФОРМИРОВАНИЕ СООБЩЕНИЯ
# ============================================================

def format_deal_message(deal):

    title = deal["title"]

    brand = deal["brand"]

    orig_usd = deal[
        "orig_price_usd"
    ]

    disc_usd = deal[
        "discount_price_usd"
    ]

    percent = deal[
        "discount_percent"
    ]

    link = deal["link"]

    # --------------------------------------------------------
    # Закуп
    # --------------------------------------------------------

    cost_kzt = round(
        disc_usd
        * KZT_EXCHANGE_RATE
    )

    # --------------------------------------------------------
    # Продажа
    # --------------------------------------------------------

    resell_price_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT
            / 100
        )
    )

    profit_kzt = (
        resell_price_kzt
        - cost_kzt
    )

    cost_str = (
        f"{cost_kzt:,}"
        .replace(",", " ")
    )

    resell_str = (
        f"{resell_price_kzt:,}"
        .replace(",", " ")
    )

    profit_str = (
        f"{profit_kzt:,}"
        .replace(",", " ")
    )

    message = (

        "🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n\n"

        f"🏷 <b>Бренд:</b> "
        f"{brand}\n\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n\n"

        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${orig_usd:.2f}</s> → "
        f"<b>${disc_usd:.2f}</b>\n\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{resell_str} ₸\n\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть товар на iHerb",
                    url=link,
                )
            ]
        ]
    )

    return message, keyboard


# ============================================================
# ОТПРАВКА В TELEGRAM
# ============================================================

async def send_to_chat(
    text,
    reply_markup=None,
):

    global chat_id

    if not chat_id:

        logger.warning(
            "⚠️ CHAT_ID пока неизвестен. "
            "Напишите боту /start."
        )

        return False

    try:

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

        return True

    except TelegramBadRequest as e:

        logger.error(
            "❌ Telegram BadRequest для CHAT_ID=%s: %s",
            chat_id,
            e,
        )

        # Если старый CHAT_ID неправильный,
        # сбрасываем его.
        if "chat not found" in str(e).lower():

            logger.warning(
                "⚠️ Старый CHAT_ID больше не используется."
            )

            chat_id = None

        return False

    except TelegramRetryAfter as e:

        retry_after = int(
            getattr(
                e,
                "retry_after",
                30,
            )
        )

        logger.warning(
            "⏳ Telegram Flood Control. "
            "Ждём %s секунд.",
            retry_after,
        )

        await asyncio.sleep(
            retry_after + 2
        )

        try:

            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )

            return True

        except Exception as retry_error:

            logger.error(
                "❌ Повторная отправка: %s",
                retry_error,
            )

            return False

    except Exception as e:

        logger.error(
            "❌ Ошибка Telegram: %s",
            e,
        )

        return False


# ============================================================
# СТАРТОВОЕ СООБЩЕНИЕ
# ============================================================

async def send_startup_message():

    if not chat_id:

        logger.warning(
            "⚠️ Не могу отправить «Бот запущен»: "
            "CHAT_ID ещё неизвестен."
        )

        return

    text = (

        "🟢 <b>БОТ ЗАПУЩЕН</b>\n\n"

        "🤖 iHerb Deal Bot успешно запущен.\n\n"

        "🔎 Автоматический мониторинг скидок: "
        "<b>ВКЛЮЧЁН</b>\n\n"

        "⏱ Проверка iHerb: "
        "<b>каждые 5 минут</b>\n\n"

        "🚀 Первая проверка выполняется "
        "<b>сразу после запуска</b>.\n\n"

        "❗ Вам не нужно нажимать "
        "«Получить скидки».\n\n"

        "Если появится новая скидка 20%+ — "
        "я отправлю её автоматически."
    )

    result = await send_to_chat(
        text,
        main_keyboard,
    )

    if result:

        logger.info(
            "✅ Сообщение «БОТ ЗАПУЩЕН» отправлено."
        )


# ============================================================
# ПРОВЕРКА СКИДОК
# ============================================================

async def check_and_notify(
    force_send=False,
):

    global last_check_time
    global checks_count
    global last_deals_count

    started = datetime.now()

    logger.info(
        "============================================================"
    )

    if force_send:

        logger.info(
            "👆 РУЧНАЯ ПРОВЕРКА iHERB"
        )

    else:

        logger.info(
            "🔄 АВТОМАТИЧЕСКАЯ ПРОВЕРКА iHERB"
        )

    logger.info(
        "============================================================"
    )

    try:

        deals = await fetch_iherb_specials()

        last_deals_count = len(
            deals
        )

        checks_count += 1

        last_check_time = datetime.now()

    except Exception as e:

        logger.exception(
            "❌ Ошибка получения скидок: %s",
            e,
        )

        return

    if not deals:

        logger.info(
            "ℹ️ Подходящих скидок не найдено."
        )

        if force_send:

            await send_to_chat(

                "ℹ️ <b>Сейчас скидок 20%+ "
                "не найдено.</b>\n\n"

                "🔎 Автоматический мониторинг "
                "продолжает работать.\n\n"

                "⏱ Следующая проверка "
                "через 5 минут.",

                main_keyboard,
            )

        elapsed = (
            datetime.now()
            - started
        ).total_seconds()

        logger.info(
            "✅ Проверка завершена за %.1f сек.",
            elapsed,
        )

        return

    sent_count = 0

    for deal in deals:

        deal_id = str(
            deal["id"]
        )

        # ----------------------------------------------------
        # Автоматически отправляем только новые товары
        # ----------------------------------------------------

        if not force_send:

            if (
                deal_id
                in sent_deals_cache
            ):

                logger.info(
                    "⏭ Уже отправлялся: %s",
                    deal["title"][:70],
                )

                continue

        message, keyboard = (
            format_deal_message(
                deal
            )
        )

        success = await send_to_chat(
            message,
            keyboard,
        )

        if success:

            sent_deals_cache.add(
                deal_id
            )

            save_cache()

            sent_count += 1

            logger.info(
                "📤 Отправлена скидка: %s",
                deal["title"][:70],
            )

            await asyncio.sleep(
                2
            )

        if (
            sent_count
            >= MAX_DEALS_PER_CHECK
        ):

            break

    logger.info(
        "📤 Отправлено новых скидок: %s",
        sent_count,
    )

    elapsed = (
        datetime.now()
        - started
    ).total_seconds()

    logger.info(
        "✅ Проверка завершена за %.1f сек.",
        elapsed,
    )


# ============================================================
# /START
# ============================================================

@dp.message(Command("start"))
async def start_handler(
    message: Message,
):

    # --------------------------------------------------------
    # САМОЕ ГЛАВНОЕ:
    # автоматически запоминаем реальный Telegram CHAT_ID
    # --------------------------------------------------------

    save_chat_id(
        message.chat.id
    )

    logger.info(
        "🎯 Получен CHAT_ID пользователя: %s",
        message.chat.id,
    )

    await message.answer(

        "🟢 <b>iHerb бот подключён!</b>\n\n"

        "🎯 Ваш CHAT_ID автоматически определён:\n"
        f"<code>{message.chat.id}</code>\n\n"

        "🔎 Автоматический мониторинг: "
        "<b>ВКЛЮЧЁН</b>\n"

        "⏱ Проверка каждые 5 минут.\n\n"

        "🚀 Проверка выполняется автоматически.\n\n"

        "Теперь вам не нужно каждый раз "
        "нажимать «Получить скидки».",

        parse_mode=ParseMode.HTML,

        reply_markup=main_keyboard,
    )

    # --------------------------------------------------------
    # Сразу после /start делаем проверку
    # --------------------------------------------------------

    asyncio.create_task(
        check_and_notify(
            force_send=True
        )
    )


# ============================================================
# КНОПКА ПОЛУЧИТЬ СКИДКИ
# ============================================================

@dp.message(
    F.text == "🔥 Получить скидки"
)
async def discounts_handler(
    message: Message,
):

    save_chat_id(
        message.chat.id
    )

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас...\n\n"
        "🤖 Автоматический мониторинг "
        "при этом продолжает работать.",
        reply_markup=main_keyboard,
    )

    await check_and_notify(
        force_send=True
    )


# ============================================================
# СТАТУС
# ============================================================

@dp.message(
    F.text == "📊 Статус бота"
)
async def status_handler(
    message: Message,
):

    save_chat_id(
        message.chat.id
    )

    if last_check_time:

        last_check = (
            last_check_time.strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        )

    else:

        last_check = "ещё не выполнялась"

    text = (

        "📊 <b>СТАТУС iHERB БОТА</b>\n\n"

        "🟢 Telegram: <b>ONLINE</b>\n"

        "🟢 Автоматический мониторинг: "
        "<b>ВКЛ</b>\n\n"

        f"⏱ Интервал: "
        f"<b>5 минут</b>\n\n"

        f"🔎 Последняя проверка: "
        f"<b>{last_check}</b>\n\n"

        f"📦 Подходящих товаров найдено: "
        f"<b>{last_deals_count}</b>\n\n"

        f"🔄 Проверок выполнено: "
        f"<b>{checks_count}</b>\n\n"

        f"💾 Товаров в памяти: "
        f"<b>{len(sent_deals_cache)}</b>\n\n"

        f"🎯 CHAT_ID:\n"
        f"<code>{chat_id}</code>\n\n"

        "🚀 Бот работает автоматически."
    )

    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard,
    )


# ============================================================
# ЛЮБОЕ СООБЩЕНИЕ
# ============================================================

@dp.message()
async def any_message_handler(
    message: Message,
):

    # Любое сообщение от владельца
    # автоматически запоминает CHAT_ID.

    save_chat_id(
        message.chat.id
    )

    await message.answer(

        "🤖 <b>iHerb бот работает.</b>\n\n"

        "🔎 Автоматический мониторинг "
        "включён.\n\n"

        "⏱ Проверка каждые 5 минут.\n\n"

        "Используйте кнопки ниже.",

        parse_mode=ParseMode.HTML,

        reply_markup=main_keyboard,
    )


# ============================================================
# АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
# ============================================================

async def monitor_loop():

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН."
    )

    # --------------------------------------------------------
    # ПЕРВАЯ ПРОВЕРКА СРАЗУ
    # --------------------------------------------------------

    try:

        await check_and_notify(
            force_send=False
        )

    except Exception as e:

        logger.exception(
            "❌ Ошибка первой проверки: %s",
            e,
        )

    # --------------------------------------------------------
    # БЕСКОНЕЧНЫЙ ЦИКЛ
    # --------------------------------------------------------

    while True:

        try:

            logger.info(
                "💤 Следующая проверка через %s секунд...",
                CHECK_INTERVAL_SECONDS,
            )

            await asyncio.sleep(
                CHECK_INTERVAL_SECONDS
            )

            logger.info(
                "⏰ Время автоматической проверки."
            )

            await check_and_notify(
                force_send=False
            )

        except asyncio.CancelledError:

            logger.info(
                "🛑 Автоматический мониторинг остановлен."
            )

            raise

        except Exception as e:

            logger.exception(
                "❌ Ошибка monitor_loop: %s",
                e,
            )

            # ВАЖНО:
            # даже если одна проверка сломалась,
            # цикл не останавливается.

            await asyncio.sleep(
                30
            )


# ============================================================
# WEB SERVER ДЛЯ RENDER
# ============================================================

async def health(
    request,
):

    return web.Response(
        text=(
            "iHerb Deal Bot is running 🟢"
        )
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

    runner = web.AppRunner(
        app
    )

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
# ПРОВЕРКА TELEGRAM
# ============================================================

async def telegram_check():

    me = await bot.get_me()

    logger.info(
        "🤖 Telegram подключен."
    )

    logger.info(
        "🤖 Bot username: @%s",
        me.username,
    )

    logger.info(
        "🤖 Bot ID: %s",
        me.id,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    global monitor_task

    logger.info(
        "============================================================"
    )

    logger.info(
        "🚀 ЗАПУСК iHERB DEAL BOT"
    )

    logger.info(
        "============================================================"
    )

    # --------------------------------------------------------
    # Загружаем сохранённый CHAT_ID
    # --------------------------------------------------------

    load_chat_id()

    # --------------------------------------------------------
    # Загружаем память скидок
    # --------------------------------------------------------

    load_cache()

    # --------------------------------------------------------
    # Проверяем Telegram
    # --------------------------------------------------------

    await telegram_check()

    # --------------------------------------------------------
    # Web server Render
    # --------------------------------------------------------

    web_runner = await start_web_server()

    # --------------------------------------------------------
    # Сообщение о запуске
    # --------------------------------------------------------

    if chat_id:

        await send_startup_message()

    else:

        logger.warning(
            "⚠️ CHAT_ID ещё неизвестен."
        )

        logger.warning(
            "ℹ️ Напишите боту /start один раз."
        )

    # --------------------------------------------------------
    # Запускаем автоматический мониторинг
    # --------------------------------------------------------

    monitor_task = asyncio.create_task(
        monitor_loop()
    )

    logger.info(
        "🟢 БОТ ПОЛНОСТЬЮ ЗАПУЩЕН."
    )

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ РАБОТАЕТ."
    )

    logger.info(
        "⏱ Проверка каждые %s секунд.",
        CHECK_INTERVAL_SECONDS,
    )

    # --------------------------------------------------------
    # Telegram polling
    # --------------------------------------------------------

    try:

        await dp.start_polling(
            bot,
            handle_signals=False,
        )

    except TelegramConflictError:

        logger.error(
            "❌ TELEGRAM CONFLICT."
        )

        logger.error(
            "❗ Где-то уже запущен второй экземпляр "
            "этого же Telegram-бота."
        )

        logger.error(
            "❗ Остановите Termux/старый Python-процесс."
        )

        raise

    finally:

        if monitor_task:

            monitor_task.cancel()

            try:

                await monitor_task

            except asyncio.CancelledError:

                pass

        await web_runner.cleanup()

        await bot.session.close()


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "🛑 Бот остановлен вручную."
        )

    except Exception as e:

        logger.exception(
            "💥 КРИТИЧЕСКАЯ ОШИБКА: %s",
            e,
        )
