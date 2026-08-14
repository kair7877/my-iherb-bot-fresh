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
from aiogram.exceptions import TelegramRetryAfter


# ============================================================
# НАСТРОЙКИ
# ============================================================

# !!! ТОКЕН НЕ МЕНЯЕМ !!!
# Render -> Environment -> BOT_TOKEN
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Ваш CHAT_ID
# Если CHAT_ID есть в Render Environment — используется он.
# Если случайно удалили переменную, используется ваш ID.
CHAT_ID = os.getenv("CHAT_ID", "217141303").strip()

CHECK_INTERVAL = 300  # 5 минут

MIN_DISCOUNT = 20
MAX_DISCOUNT = 90

USD_KZT = 540
MARKUP_PERCENT = 35

# Сколько новых скидок максимум отправлять за одну проверку
MAX_NEW_DEALS_PER_CHECK = 10

# Файл памяти отправленных скидок
CACHE_FILE = "sent_deals.json"

# Сколько записей хранить
MAX_CACHE_ITEMS = 5000


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("iherb_bot")


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не найден!\n"
        "Добавьте BOT_TOKEN в Render -> Environment."
    )

if not CHAT_ID:
    raise RuntimeError(
        "❌ CHAT_ID не найден!"
    )

try:
    CHAT_ID_INT = int(CHAT_ID)
except ValueError:
    raise RuntimeError(
        "❌ CHAT_ID должен быть числом."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# ПАМЯТЬ ОТПРАВЛЕННЫХ СКИДОК
# ============================================================

sent_deals = set()


def load_cache():
    global sent_deals

    try:
        if not os.path.exists(CACHE_FILE):
            sent_deals = set()
            logger.info("💾 Файл памяти пока отсутствует.")
            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if isinstance(data, list):
            sent_deals = set(str(x) for x in data)
        else:
            sent_deals = set()

        logger.info(
            "💾 Загружено отправленных скидок: %s",
            len(sent_deals)
        )

    except Exception as e:
        logger.error(
            "❌ Ошибка загрузки памяти: %s",
            e
        )
        sent_deals = set()


def save_cache():
    try:
        data = list(sent_deals)

        if len(data) > MAX_CACHE_ITEMS:
            data = data[-MAX_CACHE_ITEMS:]

        with open(
            CACHE_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        logger.error(
            "❌ Ошибка сохранения памяти: %s",
            e
        )


# ============================================================
# CURL CFFI
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
        "⚠️ curl_cffi не найден. Используем httpx."
    )


# ============================================================
# TELEGRAM КНОПКИ
# ============================================================

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(
                text="🔥 Получить скидки"
            ),
            KeyboardButton(
                text="ℹ️ Статус"
            ),
        ]
    ],
    resize_keyboard=True
)


# ============================================================
# HTTP HEADERS
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
        "en-US;q=0.8,en;q=0.7"
    ),
    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,"
        "image/webp,"
        "*/*;q=0.8"
    ),
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# UTILS
# ============================================================

def clean_text(value):
    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def safe_float(value):
    if value is None:
        return None

    try:
        text = str(value)

        text = (
            text
            .replace("\xa0", " ")
            .replace(",", ".")
            .strip()
        )

        text = re.sub(
            r"[^\d.]",
            "",
            text
        )

        if not text:
            return None

        number = float(text)

        if number <= 0:
            return None

        return number

    except Exception:
        return None


# ============================================================
# ЦЕНЫ
# ============================================================

def extract_prices(text):

    if not text:
        return []

    text = str(text)

    patterns = [
        r"\$\s*(\d+(?:[.,]\d{1,2})?)",
        r"US\$\s*(\d+(?:[.,]\d{1,2})?)",
        r"USD\s*(\d+(?:[.,]\d{1,2})?)",
        r"(\d+(?:[.,]\d{1,2})?)\s*\$",
        r"(\d+(?:[.,]\d{1,2})?)\s*USD",
    ]

    result = []

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for value in matches:

            number = safe_float(value)

            if number is None:
                continue

            if number >= 10000:
                continue

            if number not in result:
                result.append(number)

    return result


def clean_prices(values):

    result = []

    for value in values:

        try:
            value = float(value)
        except Exception:
            continue

        if value <= 0:
            continue

        if value >= 10000:
            continue

        if value.is_integer() and value >= 1000:
            continue

        value = round(value, 2)

        if value not in result:
            result.append(value)

    return result


# ============================================================
# СКИДКА
# ============================================================

def extract_discount(text):

    if not text:
        return None

    text = clean_text(text)

    patterns = [
        r"(\d{1,2})\s*%\s*off",
        r"(\d{1,2})\s*%\s*discount",
        r"-\s*(\d{1,2})\s*%",
        r"(\d{1,2})\s*%\s*скид",
        r"скидк[аи]?\s*(?:до\s*)?(\d{1,2})\s*%",
        r"save\s+(\d{1,2})\s*%",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if not match:
            continue

        try:
            value = int(match.group(1))

            if MIN_DISCOUNT <= value <= MAX_DISCOUNT:
                return value

        except Exception:
            pass

    return None


def calculate_discount(old_price, current_price):

    if not old_price or not current_price:
        return None

    if old_price <= current_price:
        return None

    percent = (
        1 - current_price / old_price
    ) * 100

    percent = round(percent)

    if percent < MIN_DISCOUNT:
        return percent

    if percent > MAX_DISCOUNT:
        return None

    return percent


# ============================================================
# URL
# ============================================================

def normalize_url(url):

    if not url:
        return ""

    url = str(url).strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return (
            "https://www.iherb.com"
            + url
        )

    if url.startswith("http://"):
        return "https://" + url[7:]

    if url.startswith("https://"):
        return url

    return urljoin(
        "https://www.iherb.com/",
        url
    )


def product_id_from_url(url):

    if not url:
        return ""

    patterns = [
        r"/(\d+)$",
        r"/(\d+)\?",
        r"/pr/[^/]+/(\d+)",
        r"/pr/[^/]+/(\d+)/",
        r"/product/[^/]+/(\d+)",
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            url
        )

        if match:
            return match.group(1)

    return url


# ============================================================
# НАЗВАНИЕ
# ============================================================

def extract_title(card):

    selectors = [
        ".product-title",
        "[class*='product-title']",
        "[class*='ProductTitle']",
        "[class*='title']",
        "[class*='Title']",
    ]

    for selector in selectors:

        try:

            element = card.select_one(
                selector
            )

            if element:

                title = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                if len(title) >= 3:
                    return title

        except Exception:
            pass

    try:

        for link in card.select("a[href]"):

            text = clean_text(
                link.get_text(
                    " ",
                    strip=True
                )
            )

            if len(text) >= 10:
                return text

    except Exception:
        pass

    return ""


# ============================================================
# ССЫЛКА
# ============================================================

def extract_link(card):

    selectors = [
        "a[href*='/pr/']",
        "a[href*='/product/']",
        "a.absolute-link",
        "a[href]",
    ]

    for selector in selectors:

        try:

            element = card.select_one(
                selector
            )

            if not element:
                continue

            href = normalize_url(
                element.get(
                    "href",
                    ""
                )
            )

            if href:
                return href

        except Exception:
            pass

    return ""


# ============================================================
# КАРТОЧКИ
# ============================================================

def find_cards(soup):

    selectors = [
        ".product-cell-container",
        "[class*='product-cell-container']",
        ".product-inner",
        ".product-card",
        "[data-qa='product-card']",
        ".product-tile",
        "[class*='product-card']",
        "[class*='ProductCard']",
    ]

    cards = []

    for selector in selectors:

        try:

            found = soup.select(
                selector
            )

            if found:

                logger.info(
                    "🔍 %s -> %s карточек",
                    selector,
                    len(found)
                )

                cards.extend(found)

                if len(found) >= 30:
                    break

        except Exception:
            pass

    unique = []
    seen = set()

    for card in cards:

        text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        link_element = card.select_one(
            "a[href]"
        )

        link = ""

        if link_element:

            link = normalize_url(
                link_element.get(
                    "href",
                    ""
                )
            )

        key = (
            link
            + "|"
            + text[:500]
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(card)

    logger.info(
        "📦 Уникальных карточек: %s",
        len(unique)
    )

    return unique


# ============================================================
# ЦЕНЫ КАРТОЧКИ
# ============================================================

def get_price_texts(card):

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
        "[class*='Price']",
    ]

    texts = []

    for selector in selectors:

        try:

            for element in card.select(
                selector
            ):

                text = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                if text:
                    texts.append(text)

        except Exception:
            pass

    return texts


# ============================================================
# JSON ЦЕНЫ
# ============================================================

CURRENT_KEYS = {
    "price",
    "saleprice",
    "sale_price",
    "currentprice",
    "current_price",
    "discountprice",
    "discount_price",
    "finalprice",
    "final_price",
    "sellingprice",
    "selling_price",
    "offerprice",
    "offer_price",
}

OLD_KEYS = {
    "originalprice",
    "original_price",
    "oldprice",
    "old_price",
    "regularprice",
    "regular_price",
    "listprice",
    "list_price",
    "wasprice",
    "was_price",
}


def scan_json(
    data,
    current_prices,
    old_prices
):

    if isinstance(data, dict):

        for key, value in data.items():

            key_clean = re.sub(
                r"[^a-z0-9_]",
                "",
                str(key).lower()
            )

            if key_clean in CURRENT_KEYS:

                if isinstance(
                    value,
                    (int, float)
                ):
                    number = float(value)

                    if 0 < number < 10000:
                        current_prices.append(
                            number
                        )

                else:

                    current_prices.extend(
                        extract_prices(
                            str(value)
                        )
                    )

            elif key_clean in OLD_KEYS:

                if isinstance(
                    value,
                    (int, float)
                ):
                    number = float(value)

                    if 0 < number < 10000:
                        old_prices.append(
                            number
                        )

                else:

                    old_prices.extend(
                        extract_prices(
                            str(value)
                        )
                    )

            scan_json(
                value,
                current_prices,
                old_prices
            )

    elif isinstance(data, list):

        for item in data:

            scan_json(
                item,
                current_prices,
                old_prices
            )


def find_prices(card):

    current = []
    old = []

    # --------------------------------------------------------
    # ТЕКСТ ЦЕН
    # --------------------------------------------------------

    price_texts = get_price_texts(card)

    for text in price_texts:

        values = extract_prices(
            text
        )

        current.extend(values)

        if (
            "old" in text.lower()
            or "original" in text.lower()
            or "was" in text.lower()
            or "regular" in text.lower()
        ):
            old.extend(values)

    # --------------------------------------------------------
    # DATA ATTRIBUTES
    # --------------------------------------------------------

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                key_lower = str(key).lower()

                if not isinstance(
                    value,
                    str
                ):
                    continue

                values = extract_prices(
                    value
                )

                if not values:
                    number = safe_float(value)

                    if number and number < 10000:
                        values = [number]

                if not values:
                    continue

                if any(
                    word in key_lower
                    for word in [
                        "original",
                        "old",
                        "regular",
                        "was"
                    ]
                ):
                    old.extend(values)

                elif any(
                    word in key_lower
                    for word in [
                        "price",
                        "sale",
                        "current",
                        "discount"
                    ]
                ):
                    current.extend(values)

    except Exception:
        pass

    # --------------------------------------------------------
    # JSON SCRIPT
    # --------------------------------------------------------

    try:

        for script in card.select("script"):

            text = (
                script.string
                or script.get_text()
            )

            if not text:
                continue

            text = text.strip()

            try:

                data = json.loads(text)

                scan_json(
                    data,
                    current,
                    old
                )

            except Exception:
                pass

            for match in re.finditer(
                r'"(?:price|salePrice|currentPrice|discountPrice|finalPrice)"'
                r'\s*:\s*"?\$?\s*(\d+(?:[.,]\d{1,2})?)',
                text,
                re.IGNORECASE
            ):

                value = safe_float(
                    match.group(1)
                )

                if value:
                    current.append(value)

            for match in re.finditer(
                r'"(?:originalPrice|oldPrice|regularPrice|listPrice|wasPrice)"'
                r'\s*:\s*"?\$?\s*(\d+(?:[.,]\d{1,2})?)',
                text,
                re.IGNORECASE
            ):

                value = safe_float(
                    match.group(1)
                )

                if value:
                    old.append(value)

    except Exception:
        pass

    # --------------------------------------------------------
    # META
    # --------------------------------------------------------

    try:

        for meta in card.select("meta"):

            content = meta.get(
                "content",
                ""
            )

            if not content:
                continue

            attr_text = " ".join([
                str(meta.get("property", "")),
                str(meta.get("name", "")),
                str(meta.get("itemprop", "")),
            ]).lower()

            if "price" not in attr_text:
                continue

            value = safe_float(
                content
            )

            if not value or value >= 10000:
                continue

            if any(
                word in attr_text
                for word in [
                    "old",
                    "original",
                    "regular"
                ]
            ):
                old.append(value)
            else:
                current.append(value)

    except Exception:
        pass

    current = clean_prices(current)
    old = clean_prices(old)

    current_price = (
        min(current)
        if current
        else None
    )

    valid_old = [
        x for x in old
        if current_price
        and x > current_price
    ]

    old_price = (
        max(valid_old)
        if valid_old
        else None
    )

    return (
        current_price,
        old_price
    )


# ============================================================
# ПАРСИНГ ТОВАРА
# ============================================================

def parse_card(card, index):

    try:

        text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        title = extract_title(card)

        if not title:
            return None

        link = extract_link(card)

        if not link:
            return None

        discount_from_text = extract_discount(
            text
        )

        current_price, old_price = find_prices(
            card
        )

        logger.info(
            "🔎 CARD #%s | %s | current=%s | old=%s | text_discount=%s",
            index,
            title[:70],
            current_price,
            old_price,
            discount_from_text
        )

        if not current_price:
            logger.info(
                "⏭ %s | цена не найдена",
                title[:70]
            )
            return None

        calculated_discount = calculate_discount(
            old_price,
            current_price
        )

        discount = (
            discount_from_text
            or calculated_discount
        )

        if not discount:
            return None

        if discount < MIN_DISCOUNT:
            return None

        if not old_price:

            old_price = round(
                current_price
                / (
                    1 - discount / 100
                ),
                2
            )

        if old_price <= current_price:
            return None

        product_id = product_id_from_url(
            link
        )

        if not product_id:
            product_id = link

        # ----------------------------------------------------
        # КЛЮЧ ДЛЯ АНТИДУБЛИКАТА
        #
        # Один товар + та же цена + та же скидка
        # второй раз НЕ отправляется.
        #
        # Если цена или скидка изменится —
        # это считается новой скидкой.
        # ----------------------------------------------------

        deal_key = (
            f"{product_id}|"
            f"{discount}|"
            f"{current_price:.2f}"
        )

        return {
            "id": deal_key,
            "product_id": product_id,
            "title": title,
            "brand": (
                title.split(",")[0]
                if "," in title
                else "iHerb"
            ),
            "old_price": round(
                old_price,
                2
            ),
            "current_price": round(
                current_price,
                2
            ),
            "discount": int(discount),
            "link": link,
        }

    except Exception as e:

        logger.exception(
            "❌ Ошибка карточки #%s: %s",
            index,
            e
        )

        return None


# ============================================================
# ПОЛУЧЕНИЕ iHERB
# ============================================================

async def get_iherb_html():

    urls = [
        "https://kz.iherb.com/deals",
        "https://www.iherb.com/deals",
        "https://kz.iherb.com/c/specials",
        "https://www.iherb.com/c/specials",
    ]

    cookies = {
        "ih-pref":
            "lan=ru-RU&currency=USD&country=KZ",
        "iherb-pref":
            "lan=ru-RU&currency=USD&country=KZ",
    }

    # --------------------------------------------------------
    # CURL CFFI
    # --------------------------------------------------------

    if HAS_CURL_CFFI:

        for url in urls:

            for browser in [
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
                        impersonate=browser,
                        timeout=30
                    )

                    logger.info(
                        "iHerb | %s | HTTP %s | %s",
                        browser,
                        response.status_code,
                        url
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        logger.info(
                            "✅ HTML iHerb получен: %s символов",
                            len(response.text)
                        )

                        return response.text

                except Exception as e:

                    logger.debug(
                        "curl error: %s",
                        e
                    )

    # --------------------------------------------------------
    # HTTPX
    # --------------------------------------------------------

    for url in urls:

        try:

            async with httpx.AsyncClient(
                timeout=30,
                headers=HEADERS,
                cookies=cookies,
                follow_redirects=True
            ) as client:

                response = await client.get(
                    url
                )

                logger.info(
                    "httpx | HTTP %s | %s",
                    response.status_code,
                    url
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    logger.info(
                        "✅ HTML iHerb получен через httpx."
                    )

                    return response.text

        except Exception as e:

            logger.debug(
                "httpx error: %s",
                e
            )

    logger.error(
        "❌ iHerb HTML получить не удалось."
    )

    return ""


# ============================================================
# ПОЛУЧЕНИЕ СКИДОК
# ============================================================

async def fetch_deals():

    logger.info(
        "🌐 Запрашиваю товары iHerb..."
    )

    html = await get_iherb_html()

    if not html:
        return []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        cards = find_cards(
            soup
        )

        deals = []

        for index, card in enumerate(
            cards,
            start=1
        ):

            deal = parse_card(
                card,
                index
            )

            if deal:
                deals.append(
                    deal
                )

        unique = {}

        for deal in deals:
            unique[deal["id"]] = deal

        deals = list(
            unique.values()
        )

        deals.sort(
            key=lambda x: (
                x["discount"],
                -x["current_price"]
            ),
            reverse=True
        )

        logger.info(
            "🔥 Найдено подходящих товаров: %s",
            len(deals)
        )

        return deals

    except Exception as e:

        logger.exception(
            "❌ Ошибка обработки iHerb: %s",
            e
        )

        return []


# ============================================================
# TELEGRAM СООБЩЕНИЕ
# ============================================================

def format_deal(deal):

    old_price = deal["old_price"]
    current_price = deal["current_price"]
    discount = deal["discount"]

    cost_kzt = round(
        current_price * USD_KZT
    )

    sale_price = round(
        cost_kzt
        * (1 + MARKUP_PERCENT / 100)
    )

    profit = (
        sale_price - cost_kzt
    )

    cost_str = (
        f"{cost_kzt:,}"
        .replace(",", " ")
    )

    sale_str = (
        f"{sale_price:,}"
        .replace(",", " ")
    )

    profit_str = (
        f"{profit:,}"
        .replace(",", " ")
    )

    title = escape(
        deal["title"]
    )

    brand = escape(
        deal["brand"]
    )

    text = (
        "🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n\n"

        f"🏷 <b>Бренд:</b> {brand}\n\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n\n"

        f"📉 <b>СКИДКА: -{discount}%</b>\n\n"

        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${old_price:.2f}</s> "
        f"➡️ <b>${current_price:.2f}</b>\n\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{sale_str} ₸\n\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n\n"

        f"💱 <b>Курс:</b> "
        f"1 USD = {USD_KZT} ₸\n\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть товар на iHerb",
                    url=deal["link"]
                )
            ]
        ]
    )

    return text, keyboard


# ============================================================
# ОТПРАВКА В TELEGRAM
# ============================================================

async def send_message_safe(
    text,
    reply_markup=None
):

    try:

        await bot.send_message(
            chat_id=CHAT_ID_INT,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

        return True

    except TelegramRetryAfter as e:

        wait_time = int(
            getattr(
                e,
                "retry_after",
                30
            )
        ) + 2

        logger.warning(
            "⏳ Telegram Flood Control. Ждём %s сек.",
            wait_time
        )

        await asyncio.sleep(
            wait_time
        )

        try:

            await bot.send_message(
                chat_id=CHAT_ID_INT,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

            return True

        except Exception as retry_error:

            logger.error(
                "❌ Повторная отправка не удалась: %s",
                retry_error
            )

            return False

    except Exception as e:

        logger.error(
            "❌ Telegram ошибка: %s",
            e
        )

        return False


# ============================================================
# ПРОВЕРКА
# ============================================================

check_number = 0


async def check_deals(
    manual=False
):

    global check_number

    check_number += 1

    start_time = datetime.now()

    logger.info("=" * 65)

    logger.info(
        "🔎 ПРОВЕРКА №%s",
        check_number
    )

    if manual:
        logger.info(
            "👆 Запущена вручную из Telegram."
        )
    else:
        logger.info(
            "🤖 Запущена автоматически."
        )

    logger.info(
        "🎯 Минимальная скидка: %s%%",
        MIN_DISCOUNT
    )

    logger.info("=" * 65)

    try:

        deals = await fetch_deals()

        logger.info(
            "📦 Всего найдено скидок: %s",
            len(deals)
        )

        if not deals:

            logger.info(
                "ℹ️ Подходящих скидок не найдено."
            )

            if manual:

                await send_message_safe(
                    "ℹ️ <b>Сейчас новых скидок "
                    f"{MIN_DISCOUNT}%+ не найдено.</b>\n\n"
                    "🤖 Автоматический мониторинг "
                    "продолжает работать.",
                    main_keyboard
                )

            return

        new_count = 0
        duplicate_count = 0

        for deal in deals:

            deal_id = str(
                deal["id"]
            )

            # ------------------------------------------------
            # АНТИДУБЛИКАТ
            # ------------------------------------------------

            if deal_id in sent_deals:

                duplicate_count += 1

                logger.info(
                    "⏭ Уже отправлялся: %s",
                    deal["title"][:80]
                )

                continue

            # ------------------------------------------------
            # НОВАЯ СКИДКА
            # ------------------------------------------------

            text, keyboard = format_deal(
                deal
            )

            success = await send_message_safe(
                text,
                keyboard
            )

            if success:

                sent_deals.add(
                    deal_id
                )

                save_cache()

                new_count += 1

                logger.info(
                    "📤 НОВАЯ СКИДКА ОТПРАВЛЕНА: %s",
                    deal["title"][:80]
                )

                await asyncio.sleep(2)

            if (
                new_count
                >= MAX_NEW_DEALS_PER_CHECK
            ):
                logger.info(
                    "🛑 Достигнут лимит %s новых скидок.",
                    MAX_NEW_DEALS_PER_CHECK
                )
                break

        elapsed = (
            datetime.now()
            - start_time
        ).total_seconds()

        logger.info(
            "📊 Результат проверки:"
        )

        logger.info(
            "🔥 Новых скидок: %s",
            new_count
        )

        logger.info(
            "⏭ Уже отправленных: %s",
            duplicate_count
        )

        logger.info(
            "💾 Всего в памяти: %s",
            len(sent_deals)
        )

        logger.info(
            "⏱ Время проверки: %.1f сек.",
            elapsed
        )

        logger.info(
            "💤 Следующая проверка через 5 минут."
        )

    except Exception as e:

        logger.exception(
            "❌ Ошибка проверки iHerb: %s",
            e
        )

        try:

            await send_message_safe(
                "⚠️ <b>Ошибка проверки iHerb</b>\n\n"
                f"<code>{escape(str(e)[:800])}</code>\n\n"
                "🔄 Бот не остановлен.\n"
                "Следующая проверка будет автоматически.",
                main_keyboard
            )

        except Exception:
            pass


# ============================================================
# АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
# ============================================================

async def monitor():

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН."
    )

    logger.info(
        "⚡ Первая проверка выполняется СРАЗУ."
    )

    # Первая проверка сразу
    try:
        await check_deals()
    except Exception as e:
        logger.exception(
            "❌ Ошибка первой проверки: %s",
            e
        )

    while True:

        try:

            logger.info(
                "💤 Следующая проверка через 300 секунд..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL
            )

            logger.info(
                "⏰ 5 минут прошло."
            )

            await check_deals()

        except asyncio.CancelledError:

            logger.info(
                "🛑 Автоматический мониторинг остановлен."
            )

            raise

        except Exception as e:

            logger.exception(
                "❌ Ошибка monitor: %s",
                e
            )

            # Главное — цикл НЕ умирает
            await asyncio.sleep(
                30
            )


# ============================================================
# TELEGRAM /START
# ============================================================

@dp.message(
    Command("start")
)
async def start_handler(
    message: Message
):

    user_chat_id = message.chat.id

    logger.info(
        "👤 /start от CHAT_ID=%s",
        user_chat_id
    )

    await message.answer(
        "🟢 <b>iHerb бот работает!</b>\n\n"
        "🤖 Автоматический мониторинг: "
        "<b>ВКЛЮЧЁН</b>\n\n"
        "⏱ Проверка каждые 5 минут.\n\n"
        "🔥 Новые скидки будут приходить "
        "автоматически.\n\n"
        "💾 Повторно одна и та же скидка "
        "отправляться не будет.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard
    )


# ============================================================
# КНОПКА ПОЛУЧИТЬ СКИДКИ
# ============================================================

@dp.message(
    F.text == "🔥 Получить скидки"
)
async def deals_handler(
    message: Message
):

    logger.info(
        "👆 Пользователь нажал «Получить скидки»."
    )

    await message.answer(
        "🔎 <b>Проверяю iHerb прямо сейчас...</b>\n\n"
        "🤖 Автоматический мониторинг "
        "при этом продолжает работать.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard
    )

    # ВАЖНО:
    # force_send НЕ используется.
    # Поэтому кнопка тоже не создаёт дубликаты.
    await check_deals(
        manual=True
    )


# ============================================================
# СТАТУС
# ============================================================

@dp.message(
    Command("status")
)
@dp.message(
    F.text == "ℹ️ Статус"
)
async def status_handler(
    message: Message
):

    await message.answer(
        "📊 <b>СТАТУС iHERB БОТА</b>\n\n"

        "🟢 Telegram: ONLINE\n"
        "🟢 Автомониторинг: ВКЛЮЧЁН\n"
        "🟢 Первая проверка: сразу после запуска\n"
        "🔄 Интервал: каждые 5 минут\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT}%</b>\n"

        f"💱 Курс: "
        f"1 USD = {USD_KZT} ₸\n"

        f"📈 Наценка: "
        f"+{MARKUP_PERCENT}%\n\n"

        f"🔎 Выполнено проверок: "
        f"<b>{check_number}</b>\n"

        f"💾 В памяти скидок: "
        f"<b>{len(sent_deals)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard
    )


# ============================================================
# ЛЮБОЕ ДРУГОЕ СООБЩЕНИЕ
# ============================================================

@dp.message()
async def other_message(
    message: Message
):

    await message.answer(
        "🤖 <b>iHerb Deal Bot</b>\n\n"
        "🔥 Нажмите «Получить скидки» "
        "для проверки прямо сейчас.\n\n"
        "ℹ️ Нажмите «Статус», чтобы "
        "посмотреть состояние бота.\n\n"
        "🤖 Автоматический мониторинг "
        "работает самостоятельно.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard
    )


# ============================================================
# RENDER WEB SERVER
# ============================================================

async def start_web_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app = web.Application()

    async def home(request):

        return web.Response(
            text=(
                "🟢 iHerb Telegram Bot "
                "is running automatically."
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

    logger.info(
        "🌐 Render Web Server запущен на порту %s",
        port
    )

    return runner


# ============================================================
# СООБЩЕНИЕ О ЗАПУСКЕ
# ============================================================

async def send_startup_message():

    try:

        me = await bot.get_me()

        logger.info(
            "🤖 Telegram подключён: @%s | ID=%s",
            me.username,
            me.id
        )

        startup_text = (
            "🟢 <b>БОТ ЗАПУЩЕН</b>\n\n"

            "🤖 iHerb бот успешно подключился "
            "к Telegram.\n\n"

            "🔎 Автоматический мониторинг: "
            "<b>ВКЛЮЧЁН</b>\n\n"

            "⏱ Проверка каждые "
            "<b>5 минут</b>.\n\n"

            "⚡ Первая проверка запускается "
            "<b>сразу</b>.\n\n"

            "💾 Повторные одинаковые скидки "
            "отправляться не будут.\n\n"

            "🎯 CHAT_ID: "
            f"<code>{CHAT_ID_INT}</code>"
        )

        success = await send_message_safe(
            startup_text,
            main_keyboard
        )

        if success:

            logger.info(
                "✅ Сообщение «БОТ ЗАПУЩЕН» отправлено."
            )

        else:

            logger.error(
                "❌ Не удалось отправить сообщение запуска."
            )

    except Exception as e:

        logger.exception(
            "❌ Ошибка startup Telegram: %s",
            e
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("=" * 70)

    logger.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logger.info("=" * 70)

    logger.info(
        "🎯 CHAT_ID: %s",
        CHAT_ID_INT
    )

    logger.info(
        "🎯 Минимальная скидка: %s%%",
        MIN_DISCOUNT
    )

    logger.info(
        "💱 USD/KZT: %s",
        USD_KZT
    )

    logger.info(
        "📈 Наценка: %s%%",
        MARKUP_PERCENT
    )

    # --------------------------------------------------------
    # Загружаем память
    # --------------------------------------------------------

    load_cache()

    # --------------------------------------------------------
    # Render server
    # --------------------------------------------------------

    web_runner = await start_web_server()

    # --------------------------------------------------------
    # Telegram webhook удаляем
    # --------------------------------------------------------

    try:

        await bot.delete_webhook(
            drop_pending_updates=True
        )

        logger.info(
            "✅ Telegram webhook очищен."
        )

    except Exception as e:

        logger.warning(
            "⚠️ Не удалось очистить webhook: %s",
            e
        )

    # --------------------------------------------------------
    # СООБЩЕНИЕ О ЗАПУСКЕ
    # --------------------------------------------------------

    await send_startup_message()

    # --------------------------------------------------------
    # АВТОМАТИЧЕСКИЙ МОНИТОРИНГ
    # --------------------------------------------------------

    monitor_task = asyncio.create_task(
        monitor()
    )

    logger.info(
        "🟢 БОТ ПОЛНОСТЬЮ ЗАПУЩЕН."
    )

    logger.info(
        "🤖 Telegram polling работает."
    )

    logger.info(
        "🚀 Автоматический мониторинг работает."
    )

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    try:

        while True:

            try:

                logger.info(
                    "📡 Запускаю Telegram polling..."
                )

                await dp.start_polling(
                    bot,
                    polling_timeout=30
                )

            except Exception as e:

                error_text = str(e)

                logger.error(
                    "❌ Telegram polling ошибка: %s",
                    error_text
                )

                if (
                    "Conflict" in error_text
                    or "409" in error_text
                    or "terminated by other" in error_text
                ):

                    logger.warning(
                        "⚠️ ОБНАРУЖЕН TELEGRAM CONFLICT."
                    )

                    logger.warning(
                        "⚠️ Возможно, этот же бот запущен "
                        "в Termux или ещё на одном сервере."
                    )

                    await asyncio.sleep(
                        10
                    )

                elif (
                    "Unauthorized" in error_text
                    or "Token is invalid" in error_text
                ):

                    logger.error(
                        "❌ BOT_TOKEN НЕВЕРНЫЙ!"
                    )

                    await asyncio.sleep(
                        30
                    )

                else:

                    logger.warning(
                        "🔄 Перезапуск Telegram через 5 секунд..."
                    )

                    await asyncio.sleep(
                        5
                    )

    finally:

        logger.info(
            "🛑 Завершение работы."
        )

        monitor_task.cancel()

        try:
            await monitor_task
        except asyncio.CancelledError:
            pass

        try:
            await web_runner.cleanup()
        except Exception:
            pass

        await bot.session.close()


# ============================================================
# START
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
            e
        )
