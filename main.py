import asyncio
import json
import logging
import os
import re
from datetime import datetime
from html import escape
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramRetryAfter


# ============================================================
# НАСТРОЙКИ
# ============================================================

# Токен берём из Render → Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Ваш Telegram чат/канал
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Проверка каждые 5 минут
CHECK_INTERVAL_SECONDS = 300

# Минимальная скидка
MIN_DISCOUNT_PERCENT = 20

# Отслеживаемые бренды
TARGET_BRANDS = [
    "California Gold Nutrition",
    "NOW Foods",
    "Doctor's Best",
    "Solgar",
]

# Курс USD → KZT
KZT_EXCHANGE_RATE = 540

# Наценка
MARGIN_MARKUP_PERCENT = 35

# Память отправленных товаров
CACHE_FILE = "sent_deals.json"

# Максимум товаров за одну автоматическую проверку
MAX_DEALS_PER_CHECK = 10


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Проверьте Render → Environment Variables."
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID не найден. "
        "Добавьте CHAT_ID в Render → Environment Variables."
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

subscribers = set()
sent_deals_cache = set()


# ============================================================
# КЛАВИАТУРА
# ============================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔥 Получить скидки"),
            KeyboardButton(text="ℹ️ Статус"),
        ]
    ],
    resize_keyboard=True,
)


# ============================================================
# CURL_CFFI
# ============================================================

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
    logging.info("✅ curl_cffi доступен")

except ImportError:
    HAS_CURL_CFFI = False
    logging.warning(
        "⚠️ curl_cffi не установлен. Используем httpx."
    )


# ============================================================
# CACHE
# ============================================================

def load_cache():
    global sent_deals_cache

    try:
        if not os.path.exists(CACHE_FILE):
            sent_deals_cache = set()
            logging.info("💾 Cache пока пустой")
            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if isinstance(data, list):
            sent_deals_cache = set(data)

        logging.info(
            f"💾 Загружено ранее отправленных товаров: "
            f"{len(sent_deals_cache)}"
        )

    except Exception as e:
        logging.error(
            f"❌ Ошибка загрузки cache: {e}"
        )
        sent_deals_cache = set()


def save_cache():
    try:
        data = list(sent_deals_cache)[-5000:]

        with open(
            CACHE_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:
        logging.error(
            f"❌ Ошибка сохранения cache: {e}"
        )


# ============================================================
# HTTP HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": (
        "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):
    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def parse_price(text):
    """
    Извлекает цену из текста.

    Поддерживает:
    $12.99
    $ 12.99
    12,99
    12.99
    """

    if not text:
        return None

    text = str(text)

    # Убираем пробелы между валютой и цифрами
    text = text.replace("\xa0", " ")

    match = re.search(
        r"(?:\$|USD|US\$)?\s*(\d+(?:[.,]\d{1,2})?)",
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    try:
        value = float(
            match.group(1).replace(",", ".")
        )

        if value <= 0:
            return None

        return value

    except Exception:
        return None


def extract_prices(text):
    """
    Извлекает все возможные цены из текста.
    """

    if not text:
        return []

    text = text.replace("\xa0", " ")

    values = []

    patterns = [
        r"\$\s*(\d+(?:[.,]\d{1,2})?)",
        r"USD\s*(\d+(?:[.,]\d{1,2})?)",
        r"(\d+(?:[.,]\d{1,2})?)\s*\$",
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for value in matches:

            try:
                number = float(
                    value.replace(",", ".")
                )

                if 0 < number < 10000:
                    values.append(number)

            except Exception:
                pass

    # Убираем дубли
    result = []

    for value in values:

        if value not in result:
            result.append(value)

    return result


def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return "https://www.iherb.com" + url

    if url.startswith("http://"):
        return "https://" + url[7:]

    if url.startswith("https://"):
        return url

    return urljoin(
        "https://www.iherb.com/",
        url
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
        r"/pr/[^/]+/(\d+)/",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            link
        )

        if match:
            return match.group(1)

    return link


def extract_discount_percent(text):
    """
    Ищет непосредственно процент скидки
    в карточке товара.

    Примеры:
    20% off
    25% off
    30% off
    -40%
    скидка 25%
    """

    if not text:
        return None

    patterns = [
        r"(\d{1,2})\s*%\s*off",
        r"(\d{1,2})\s*%\s*OFF",
        r"-\s*(\d{1,2})\s*%",
        r"скидк[аи]?\s*(?:до\s*)?(\d{1,2})\s*%",
        r"(\d{1,2})\s*%\s*скид",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            try:
                value = int(
                    match.group(1)
                )

                if 1 <= value <= 90:
                    return value

            except Exception:
                pass

    return None


def find_price_elements(card):
    """
    Собирает элементы, которые могут содержать цену.
    """

    selectors = [
        ".price",
        ".price-discount",
        ".price-original",
        ".price-old",
        ".original-price",
        ".discount-price",
        ".product-price",
        "[class*='price']",
        "[data-qa*='price']",
        "[data-testid*='price']",
    ]

    elements = []

    for selector in selectors:

        try:
            found = card.select(selector)

            for element in found:

                if element not in elements:
                    elements.append(element)

        except Exception:
            pass

    return elements


# ============================================================
# ПОЛУЧЕНИЕ IHERB
# ============================================================

async def get_iherb_html(url):
    """
    Пытаемся получить страницу iHerb.
    """

    urls_to_try = [
        url,
        "https://kz.iherb.com/deals",
        "https://www.iherb.com/deals",
        "https://kz.iherb.com/c/specials",
        "https://www.iherb.com/c/specials",
        "https://ru.iherb.com/c/specials",
    ]

    cookies = {
        "ih-pref":
            "lan=ru-RU&currency=USD&country=KZ",

        "iherb-pref":
            "lan=ru-RU&currency=USD&country=KZ",
    }

    # ========================================================
    # CURL_CFFI
    # ========================================================

    if HAS_CURL_CFFI:

        for target_url in urls_to_try:

            for impersonate in [
                "chrome124",
                "chrome120",
                "chrome116",
                "safari15_5",
            ]:

                try:

                    response = await asyncio.to_thread(
                        curl_requests.get,
                        target_url,
                        headers=HEADERS,
                        cookies=cookies,
                        impersonate=impersonate,
                        timeout=20,
                    )

                    logging.info(
                        f"iHerb | "
                        f"{impersonate} | "
                        f"HTTP "
                        f"{response.status_code}"
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 3000
                    ):

                        logging.info(
                            "✅ iHerb HTML получен: "
                            f"{len(response.text)} символов"
                        )

                        return response.text

                except Exception as e:

                    logging.debug(
                        f"curl_cffi ошибка: {e}"
                    )

    # ========================================================
    # HTTPX
    # ========================================================

    for target_url in urls_to_try:

        try:

            async with httpx.AsyncClient(
                timeout=20,
                headers=HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    target_url
                )

                logging.info(
                    f"httpx | "
                    f"{target_url} | "
                    f"HTTP "
                    f"{response.status_code}"
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 3000
                ):

                    logging.info(
                        "✅ HTML получен через httpx"
                    )

                    return response.text

        except Exception as e:

            logging.debug(
                f"httpx ошибка: {e}"
            )

    logging.error(
        "❌ iHerb не удалось получить."
    )

    return ""


# ============================================================
# ПАРСИНГ IHERB
# ============================================================

async def fetch_iherb_specials():

    logging.info(
        "🔎 Начинаем проверку iHerb..."
    )

    html = await get_iherb_html(
        "https://kz.iherb.com/deals"
    )

    if not html:
        return []

    deals = []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        # ====================================================
        # КАРТОЧКИ ТОВАРОВ
        # ====================================================

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

                if cards:

                    logging.info(
                        f"🔍 {selector}: "
                        f"{len(cards)} карточек"
                    )

                    product_cards.extend(
                        cards
                    )

                    # Если уже нашли много карточек,
                    # дальше не надо собирать дубли
                    if len(product_cards) >= 50:
                        break

            except Exception:
                pass

        # ====================================================
        # УНИКАЛЬНЫЕ КАРТОЧКИ
        # ====================================================

        unique_cards = []

        seen_cards = set()

        for card in product_cards:

            card_text = clean_text(
                card.get_text(
                    " ",
                    strip=True
                )
            )

            if not card_text:
                continue

            # Берём первые 500 символов
            # как отпечаток карточки
            card_key = card_text[:500]

            if card_key in seen_cards:
                continue

            seen_cards.add(
                card_key
            )

            unique_cards.append(
                card
            )

        logging.info(
            f"📦 Уникальных карточек: "
            f"{len(unique_cards)}"
        )

        # ====================================================
        # ОБРАБОТКА КАЖДОЙ КАРТОЧКИ
        # ====================================================

        for card in unique_cards:

            try:

                card_text = clean_text(
                    card.get_text(
                        " ",
                        strip=True
                    )
                )

                if not card_text:
                    continue

                # ------------------------------------------------
                # НАЗВАНИЕ
                # ------------------------------------------------

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
                        strip=True
                    )
                )

                if not title:
                    continue

                # ------------------------------------------------
                # ССЫЛКА
                # ------------------------------------------------

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
                        ""
                    )
                )

                if not link:
                    continue

                # ------------------------------------------------
                # БРЕНД
                # ------------------------------------------------

                brand = find_brand(
                    title
                )

                # Оставляем только нужные бренды
                if TARGET_BRANDS and not brand:
                    continue

                # ------------------------------------------------
                # ПРОЦЕНТ СКИДКИ
                # ------------------------------------------------

                discount_percent = (
                    extract_discount_percent(
                        card_text
                    )
                )

                # ------------------------------------------------
                # ЦЕНЫ
                # ------------------------------------------------

                price_values = []

                price_elements = (
                    find_price_elements(
                        card
                    )
                )

                for element in price_elements:

                    text = clean_text(
                        element.get_text(
                            " ",
                            strip=True
                        )
                    )

                    values = extract_prices(
                        text
                    )

                    for value in values:

                        if value not in price_values:
                            price_values.append(
                                value
                            )

                # Если через элементы цены
                # не нашли — ищем в тексте
                if not price_values:

                    price_values = (
                        extract_prices(
                            card_text
                        )
                    )

                if not price_values:
                    continue

                # ------------------------------------------------
                # ОПРЕДЕЛЯЕМ ТЕКУЩУЮ ЦЕНУ
                # ------------------------------------------------

                discount_price = min(
                    price_values
                )

                # ------------------------------------------------
                # ОПРЕДЕЛЯЕМ СТАРУЮ ЦЕНУ
                # ------------------------------------------------

                orig_price = None

                higher_prices = [
                    value
                    for value in price_values
                    if value > discount_price
                ]

                if higher_prices:

                    orig_price = max(
                        higher_prices
                    )

                # ------------------------------------------------
                # ЕСЛИ ПРОЦЕНТ ЕСТЬ,
                # НО СТАРОЙ ЦЕНЫ НЕТ
                # ------------------------------------------------

                if (
                    discount_percent
                    and not orig_price
                ):

                    orig_price = round(
                        discount_price
                        / (
                            1
                            - discount_percent / 100
                        ),
                        2
                    )

                # ------------------------------------------------
                # ЕСЛИ ПРОЦЕНТА НЕТ,
                # НО ЕСТЬ 2 ЦЕНЫ
                # ------------------------------------------------

                if (
                    not discount_percent
                    and orig_price
                    and orig_price > discount_price
                ):

                    discount_percent = round(
                        (
                            1
                            - discount_price
                            / orig_price
                        )
                        * 100
                    )

                # ------------------------------------------------
                # ПРОВЕРКА
                # ------------------------------------------------

                if not discount_percent:
                    continue

                if discount_percent < MIN_DISCOUNT_PERCENT:
                    continue

                if not orig_price:
                    continue

                if orig_price <= discount_price:
                    continue

                # ------------------------------------------------
                # ID
                # ------------------------------------------------

                product_id = extract_product_id(
                    link
                )

                if not product_id:
                    product_id = link

                # ------------------------------------------------
                # ДОБАВЛЯЕМ
                # ------------------------------------------------

                deal = {
                    "id": product_id,
                    "title": title,
                    "brand": brand,
                    "orig_price_usd": round(
                        orig_price,
                        2
                    ),
                    "discount_price_usd": round(
                        discount_price,
                        2
                    ),
                    "discount_percent": int(
                        discount_percent
                    ),
                    "link": link,
                }

                deals.append(
                    deal
                )

                logging.info(
                    "🔥 НАЙДЕНА СКИДКА | "
                    f"-{discount_percent}% | "
                    f"{brand} | "
                    f"{title[:70]} | "
                    f"${discount_price:.2f}"
                )

            except Exception as e:

                logging.debug(
                    f"Ошибка обработки карточки: {e}"
                )

        # ====================================================
        # УДАЛЯЕМ ДУБЛИ
        # ====================================================

        unique_deals = {}

        for deal in deals:

            unique_deals[
                deal["id"]
            ] = deal

        deals = list(
            unique_deals.values()
        )

        # ====================================================
        # СОРТИРОВКА
        # Сначала самые большие скидки
        # ====================================================

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"]
            ),
            reverse=True
        )

        logging.info(
            "🔥 Найдено подходящих скидок: "
            f"{len(deals)}"
        )

        for deal in deals[:20]:

            logging.info(
                f"💊 "
                f"{deal['brand']} | "
                f"-{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:60]}"
            )

        return deals

    except Exception as e:

        logging.exception(
            f"❌ Ошибка парсинга iHerb: {e}"
        )

        return []


# ============================================================
# ФОРМИРОВАНИЕ TELEGRAM СООБЩЕНИЯ
# ============================================================

def format_deal_message(deal):

    title = escape(
        deal["title"]
    )

    brand = escape(
        deal["brand"]
    )

    orig_usd = deal[
        "orig_price_usd"
    ]

    disc_usd = deal[
        "discount_price_usd"
    ]

    percent = deal[
        "discount_percent"
    ]

    link = deal[
        "link"
    ]

    # --------------------------------------------------------
    # РАСЧЁТ В ТЕНГЕ
    # --------------------------------------------------------

    cost_kzt = round(
        disc_usd
        * KZT_EXCHANGE_RATE
    )

    resell_price_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT / 100
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

    # --------------------------------------------------------
    # СООБЩЕНИЕ
    # --------------------------------------------------------

    message = (
        f"🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n"
        f"\n"
        f"🏷 <b>Бренд:</b> {brand}\n"
        f"\n"
        f"💊 <b>Товар:</b>\n"
        f"{title}\n"
        f"\n"
        f"📉 <b>СКИДКА: -{percent}%</b>\n"
        f"\n"
        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${orig_usd:.2f}</s> "
        f"➡️ <b>${disc_usd:.2f}</b>\n"
        f"\n"
        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n"
        f"\n"
        f"🏪 <b>Цена продажи:</b> "
        f"{resell_str} ₸\n"
        f"\n"
        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n"
        f"\n"
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

    return (
        message,
        keyboard
    )


# ============================================================
# ПОЛУЧАТЕЛИ
# ============================================================

def get_targets():

    targets = set()

    if CHAT_ID:
        targets.add(
            CHAT_ID
        )

    targets.update(
        subscribers
    )

    return targets


# ============================================================
# ОТПРАВКА ОДНОЙ СКИДКИ
# ============================================================

async def send_deal(
    deal,
    targets
):

    message, keyboard = (
        format_deal_message(
            deal
        )
    )

    success = False

    for target_id in targets:

        try:

            await bot.send_message(
                chat_id=target_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            logging.info(
                f"✅ Отправлено в "
                f"{target_id}: "
                f"{deal['title'][:60]}"
            )

            success = True

            await asyncio.sleep(2)

        except TelegramRetryAfter as e:

            retry_after = int(
                getattr(
                    e,
                    "retry_after",
                    30
                )
            )

            logging.warning(
                "⏳ Telegram Flood Control. "
                f"Ждём {retry_after + 2} сек."
            )

            await asyncio.sleep(
                retry_after + 2
            )

            try:

                await bot.send_message(
                    chat_id=target_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )

                success = True

            except Exception as retry_error:

                logging.error(
                    "❌ Повторная отправка: "
                    f"{retry_error}"
                )

        except Exception as e:

            logging.error(
                f"❌ Ошибка Telegram "
                f"для {target_id}: {e}"
            )

    return success


# ============================================================
# ГЛАВНАЯ ПРОВЕРКА
# ============================================================

async def check_and_notify(
    force_send=False
):

    logging.info("=" * 60)
    logging.info("🔎 ПРОВЕРКА iHERB")
    logging.info("=" * 60)

    try:

        deals = (
            await fetch_iherb_specials()
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка проверки: {e}"
        )

        return

    if not deals:

        logging.info(
            "ℹ️ Подходящих скидок нет."
        )

        return

    targets = get_targets()

    if not targets:

        logging.warning(
            "⚠️ Нет получателей Telegram."
        )

        return

    logging.info(
        f"👥 Получателей: "
        f"{len(targets)}"
    )

    sent_count = 0

    for deal in deals:

        deal_id = deal["id"]

        # При автоматической проверке
        # отправляем только новые товары
        if not force_send:

            if deal_id in sent_deals_cache:

                logging.info(
                    f"⏭ Уже отправлялся: "
                    f"{deal['title'][:60]}"
                )

                continue

        success = await send_deal(
            deal,
            targets
        )

        if success:

            sent_deals_cache.add(
                deal_id
            )

            save_cache()

            sent_count += 1

        if sent_count >= MAX_DEALS_PER_CHECK:
            break

    logging.info(
        "📤 Отправлено новых скидок: "
        f"{sent_count}"
    )


# ============================================================
# /START
# ============================================================

@dp.message(
    Command("start")
)
async def start_handler(
    message
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "👋 <b>iHerb Deal Bot работает!</b>\n\n"
        "🔥 Я автоматически отслеживаю "
        "скидки iHerb.\n\n"
        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"
        f"⏱ Проверка: "
        f"<b>каждые 5 минут</b>\n\n"
        "Новые подходящие скидки "
        "будут отправляться автоматически.",
        reply_markup=main_keyboard,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# /DEALS
# ============================================================

@dp.message(
    Command("deals")
)
@dp.message(
    F.text == "🔥 Получить скидки"
)
async def deals_handler(
    message
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас..."
    )

    await check_and_notify(
        force_send=True
    )


# ============================================================
# /STATUS
# ============================================================

@dp.message(
    Command("status")
)
@dp.message(
    F.text == "ℹ️ Статус"
)
async def status_handler(
    message
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        f"📊 <b>СТАТУС БОТА</b>\n\n"
        f"🟢 Telegram: ONLINE\n"
        f"🟢 Автомониторинг: ВКЛЮЧЁН\n"
        f"🔄 Проверка: каждые 5 минут\n"
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%\n\n"
        f"🏷 Бренды:\n"
        f"{', '.join(TARGET_BRANDS)}\n\n"
        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"
        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n\n"
        f"💾 Товаров в памяти: "
        f"{len(sent_deals_cache)}",
        reply_markup=main_keyboard,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ЛЮБОЕ ДРУГОЕ СООБЩЕНИЕ
# ============================================================

@dp.message()
async def any_message_handler(
    message
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "👋 <b>iHerb Deal Bot</b>\n\n"
        "🔥 Я автоматически отслеживаю "
        "скидки iHerb.\n\n"
        "Новые скидки отправлю "
        "самостоятельно.",
        reply_markup=main_keyboard,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
# ============================================================

async def scheduler():

    logging.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН"
    )

    # --------------------------------------------------------
    # ПЕРВАЯ ПРОВЕРКА СРАЗУ
    # --------------------------------------------------------

    logging.info(
        "⚡ Первая проверка выполняется СРАЗУ..."
    )

    try:

        await check_and_notify(
            force_send=False
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка первой проверки: {e}"
        )

    # --------------------------------------------------------
    # ЦИКЛ
    # --------------------------------------------------------

    while True:

        try:

            logging.info(
                "💤 Следующая проверка через "
                "5 минут..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL_SECONDS
            )

            logging.info(
                "⏰ 5 минут прошло. "
                "Запускаем новую проверку..."
            )

            await check_and_notify(
                force_send=False
            )

        except asyncio.CancelledError:

            logging.info(
                "🛑 Мониторинг остановлен."
            )

            break

        except Exception as e:

            logging.error(
                f"❌ Ошибка scheduler: {e}"
            )

            await asyncio.sleep(
                30
            )


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def start_dummy_server():

    try:

        from aiohttp import web

        port = int(
            os.environ.get(
                "PORT",
                "10000"
            )
        )

        app = web.Application()

        async def home(request):

            return web.Response(
                text=(
                    "iHerb Telegram Bot "
                    "is running!"
                )
            )

        async def health(request):

            return web.Response(
                text="OK"
            )

        app.router.add_get(
            "/",
            home
        )

        app.router.add_get(
            "/health",
            health
        )

        runner = web.AppRunner(
            app
        )

        await runner.setup()

        site = web.TCPSite(
            runner,
            "0.0.0.0",
            port
        )

        await site.start()

        logging.info(
            "🌐 Render Health Server "
            f"запущен на порту {port}"
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка Health Server: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.info("=" * 60)
    logging.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )
    logging.info("=" * 60)

    # Загружаем память
    load_cache()

    # Render Health Server
    await start_dummy_server()

    # Автоматический мониторинг
    scheduler_task = asyncio.create_task(
        scheduler()
    )

    # ========================================================
    # TELEGRAM POLLING
    # ========================================================

    while True:

        try:

            try:

                await bot.delete_webhook(
                    drop_pending_updates=True
                )

            except Exception as e:

                logging.warning(
                    f"Webhook: {e}"
                )

            logging.info(
                "🤖 Telegram polling запущен."
            )

            await dp.start_polling(
                bot
            )

            break

        except Exception as e:

            error_text = str(e)

            logging.error(
                "❌ Telegram polling ошибка: "
                f"{error_text}"
            )

            if (
                "Conflict" in error_text
                or "409" in error_text
                or "terminated by other"
                in error_text
            ):

                logging.warning(
                    "⚠️ Telegram Conflict. "
                    "Ждём 10 секунд..."
                )

                await asyncio.sleep(
                    10
                )

            elif (
                "Unauthorized"
                in error_text
            ):

                logging.error(
                    "❌ TELEGRAM UNAUTHORIZED!\n"
                    "Проверьте BOT_TOKEN "
                    "в Render → Environment Variables."
                )

                await asyncio.sleep(
                    30
                )

            else:

                logging.warning(
                    "🔄 Перезапуск через 5 секунд..."
                )

                await asyncio.sleep(
                    5
                )

    scheduler_task.cancel()

    try:

        await scheduler_task

    except asyncio.CancelledError:

        pass

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

        logging.info(
            "🛑 Бот остановлен."
        )
