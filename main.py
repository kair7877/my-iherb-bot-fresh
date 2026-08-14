import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
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
# iHERB DEAL BOT — ПОЛНАЯ ОБНОВЛЁННАЯ ВЕРСИЯ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# Проверка iHerb каждые 5 минут
CHECK_INTERVAL_SECONDS = 300

# Минимальная скидка
MIN_DISCOUNT_PERCENT = 20

# Максимальная скидка
MAX_DISCOUNT_PERCENT = 90

# Сколько новых товаров отправлять за одну автоматическую проверку
MAX_DEALS_PER_CHECK = 10

# Курс USD/KZT
KZT_EXCHANGE_RATE = 540

# Наценка
MARGIN_MARKUP_PERCENT = 35

# Файл памяти
CACHE_FILE = "sent_deals.json"

MAX_CACHE_ITEMS = 5000

# Бренды
# [] = ВСЕ БРЕНДЫ
TARGET_BRANDS = []


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Добавьте BOT_TOKEN в Render → Environment Variables."
    )

if not CHAT_ID:
    logging_warning = (
        "CHAT_ID не задан. "
        "Бот будет использовать пользователей, "
        "которые нажали /start."
    )
else:
    logging_warning = None


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)

if logging_warning:
    logger.warning("⚠️ " + logging_warning)


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

subscribers = set()
sent_deals_cache = set()

check_counter = 0
last_check_time = None
last_deals_found = 0
last_deals_sent = 0
bot_started_time = datetime.now(timezone.utc)


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

    logger.info("✅ curl_cffi доступен")

except ImportError:
    HAS_CURL_CFFI = False

    logger.warning(
        "⚠️ curl_cffi отсутствует. Используем httpx."
    )


# ============================================================
# CACHE
# ============================================================

def load_cache():

    global sent_deals_cache

    try:

        if not os.path.exists(CACHE_FILE):
            sent_deals_cache = set()

            logger.info("💾 Cache пока пустой")
            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(data, list):

            sent_deals_cache = {
                str(x)
                for x in data
            }

        else:

            sent_deals_cache = set()

        logger.info(
            f"💾 Загружено товаров в cache: "
            f"{len(sent_deals_cache)}"
        )

    except Exception as e:

        logger.error(
            f"❌ Ошибка загрузки cache: {e}"
        )

        sent_deals_cache = set()


def save_cache():

    try:

        data = list(
            sent_deals_cache
        )[-MAX_CACHE_ITEMS:]

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
            f"❌ Ошибка сохранения cache: {e}"
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

    "Upgrade-Insecure-Requests": "1",
}


# ============================================================
# TEXT
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text)
    ).strip()


# ============================================================
# FLOAT
# ============================================================

def safe_float(value):

    if value is None:
        return None

    try:

        text = str(value)

        text = (
            text
            .replace("\xa0", "")
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

    """
    Расширенный поиск цен.

    Поддерживает:

    $12.99
    $ 12.99
    US$12.99
    USD 12.99
    12.99 USD
    12.99 $
    12,99 $
    """

    if not text:
        return []

    text = (
        str(text)
        .replace("\xa0", " ")
        .replace("US$", "$")
    )

    patterns = [

        r"\$\s*(\d+(?:[.,]\d{1,2})?)",

        r"USD\s*(\d+(?:[.,]\d{1,2})?)",

        r"(\d+(?:[.,]\d{1,2})?)\s*USD",

        r"(\d+(?:[.,]\d{1,2})?)\s*\$",

        r"\bprice\s*[:=]?\s*(\d+(?:[.,]\d{1,2})?)",

        r'"price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"price"\s*:\s*(\d+(?:\.\d+)?)',

        r'"salePrice"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"sale_price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"currentPrice"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"originalPrice"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"listPrice"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',

        r'"discountPrice"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)',
    ]

    result = []

    for pattern in patterns:

        try:

            matches = re.findall(
                pattern,
                text,
                re.IGNORECASE
            )

            for value in matches:

                number = safe_float(value)

                if number is None:
                    continue

                # Отбрасываем явно неценовые значения
                if number > 1000:
                    continue

                if number < 0.50:
                    continue

                if number not in result:
                    result.append(number)

        except Exception:
            pass

    return result


# ============================================================
# DISCOUNT
# ============================================================

def extract_discount_percent(text):

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

        r"(\d{1,2})\s*%\s*эконом",

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

            value = int(
                match.group(1)
            )

            if (
                MIN_DISCOUNT_PERCENT
                <= value
                <= MAX_DISCOUNT_PERCENT
            ):

                return value

        except Exception:
            pass

    return None


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
        return (
            "https://"
            + url[7:]
        )

    if url.startswith("https://"):
        return url

    return urljoin(
        "https://www.iherb.com/",
        url
    )


# ============================================================
# PRODUCT ID
# ============================================================

def extract_product_id(link):

    if not link:
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
            link
        )

        if match:
            return match.group(1)

    return link


# ============================================================
# BRAND
# ============================================================

def find_brand(title):

    if not title:
        return ""

    if not TARGET_BRANDS:
        return "iHerb"

    title_lower = title.lower()

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:
            return brand

    return ""


# ============================================================
# PRICE ELEMENTS
# ============================================================

def get_card_price_texts(card):

    selectors = [

        ".price",

        ".price-discount",

        ".price-original",

        ".price-old",

        ".original-price",

        ".discount-price",

        ".product-price",

        "[class*='price']",

        "[class*='Price']",

        "[data-qa*='price']",

        "[data-testid*='price']",

        "[data-price]",

        "[data-sale-price]",

        "[data-original-price]",

    ]

    texts = []

    for selector in selectors:

        try:

            elements = card.select(
                selector
            )

            for element in elements:

                text = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                if text:
                    texts.append(text)

                # Считываем data-* атрибуты
                for attr in [
                    "data-price",
                    "data-sale-price",
                    "data-original-price",
                    "data-current-price",
                    "data-discount-price",
                ]:

                    value = element.get(
                        attr
                    )

                    if value:
                        texts.append(
                            str(value)
                        )

        except Exception:
            pass

    return texts


# ============================================================
# JSON / DATA
# ============================================================

def extract_json_price_data(card):

    prices = []
    discount = None

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                if isinstance(value, list):
                    value = " ".join(
                        str(x)
                        for x in value
                    )

                if not isinstance(
                    value,
                    str
                ):
                    continue

                key_lower = key.lower()

                if (
                    "price" in key_lower
                    or "cost" in key_lower
                ):

                    prices.extend(
                        extract_prices(value)
                    )

                if (
                    "discount" in key_lower
                    or "percent" in key_lower
                ):

                    found = (
                        extract_discount_percent(
                            value
                        )
                    )

                    if found:
                        discount = found

    except Exception:
        pass

    return prices, discount


# ============================================================
# BEST PRICES
# ============================================================

def find_best_prices(price_values):

    if not price_values:
        return None, None

    unique = sorted(
        set(
            round(
                float(x),
                2
            )
            for x in price_values
            if x and x > 0
        )
    )

    if not unique:
        return None, None

    if len(unique) == 1:
        return unique[0], None

    current_price = unique[0]
    old_price = unique[-1]

    if old_price <= current_price:
        old_price = None

    return current_price, old_price


# ============================================================
# CALCULATE DISCOUNT
# ============================================================

def calculate_discount(
    old_price,
    current_price
):

    if not old_price:
        return None

    if not current_price:
        return None

    if old_price <= current_price:
        return None

    percent = (
        1
        - (
            current_price
            / old_price
        )
    ) * 100

    percent = round(percent)

    if percent < MIN_DISCOUNT_PERCENT:
        return percent

    if percent > MAX_DISCOUNT_PERCENT:
        return None

    return percent


# ============================================================
# iHERB HTML
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

    # ========================================================
    # CURL CFFI
    # ========================================================

    if HAS_CURL_CFFI:

        browsers = [

            "chrome124",
            "chrome120",
            "chrome116",
            "safari15_5",

        ]

        for url in urls:

            for browser in browsers:

                try:

                    response = (
                        await asyncio.to_thread(
                            curl_requests.get,
                            url,
                            headers=HEADERS,
                            cookies=cookies,
                            impersonate=browser,
                            timeout=30,
                        )
                    )

                    logger.info(
                        f"iHerb | {browser} | "
                        f"HTTP {response.status_code} | "
                        f"{len(response.text)} chars"
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        logger.info(
                            "✅ iHerb HTML получен"
                        )

                        return response.text

                except Exception as e:

                    logger.debug(
                        f"curl error: {e}"
                    )

    # ========================================================
    # HTTPX
    # ========================================================

    for url in urls:

        try:

            async with httpx.AsyncClient(
                timeout=30,
                headers=HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url
                )

                logger.info(
                    f"httpx | HTTP "
                    f"{response.status_code} | "
                    f"{len(response.text)} chars"
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    logger.info(
                        "✅ HTML получен через httpx"
                    )

                    return response.text

        except Exception as e:

            logger.debug(
                f"httpx error: {e}"
            )

    logger.error(
        "❌ iHerb HTML получить не удалось"
    )

    return ""


# ============================================================
# PRODUCT CARDS
# ============================================================

def find_product_cards(soup):

    selectors = [

        ".product-cell-container",

        "[class*='product-cell-container']",

        ".product-inner",

        ".product-card",

        "[data-qa='product-card']",

        ".product-tile",

        "[class*='product-card']",

        "[class*='ProductCard']",

        "[data-product-id]",

    ]

    cards = []

    for selector in selectors:

        try:

            found = soup.select(
                selector
            )

            if found:

                logger.info(
                    f"🔍 {selector}: "
                    f"{len(found)} карточек"
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

        link = ""

        link_element = card.select_one(
            "a[href]"
        )

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
        f"📦 Уникальных карточек: "
        f"{len(unique)}"
    )

    return unique


# ============================================================
# TITLE
# ============================================================

def extract_title(card):

    selectors = [

        ".product-title",

        "[class*='product-title']",

        "[class*='ProductTitle']",

        "[class*='title']",

        "[class*='Title']",

        "[data-qa*='title']",

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

    # alt/title
    try:

        for element in card.select(
            "[title]"
        ):

            value = clean_text(
                element.get(
                    "title",
                    ""
                )
            )

            if len(value) >= 10:
                return value

    except Exception:
        pass

    # ссылка
    try:

        links = card.select(
            "a[href]"
        )

        for link in links:

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
# LINK
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

            href = element.get(
                "href",
                ""
            )

            href = normalize_url(
                href
            )

            if (
                href
                and "iherb.com" in href
            ):

                return href

        except Exception:
            pass

    return ""


# ============================================================
# PARSE CARD
# ============================================================

def parse_product_card(
    card,
    index
):

    try:

        card_text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        if not card_text:
            return None

        title = extract_title(
            card
        )

        if not title:

            logger.info(
                f"⏭ CARD #{index}: "
                "название не найдено"
            )

            return None

        link = extract_link(
            card
        )

        if not link:

            logger.info(
                f"⏭ {title[:60]} | "
                "ссылка не найдена"
            )

            return None

        # ====================================================
        # BRAND
        # ====================================================

        brand = find_brand(
            title
        )

        if (
            TARGET_BRANDS
            and not brand
        ):

            return None

        # ====================================================
        # PRICE COLLECTION
        # ====================================================

        price_values = []

        # 1
        for text in get_card_price_texts(card):

            price_values.extend(
                extract_prices(text)
            )

        # 2
        json_prices, json_discount = (
            extract_json_price_data(card)
        )

        price_values.extend(
            json_prices
        )

        # 3
        # Полный текст
        price_values.extend(
            extract_prices(card_text)
        )

        # ====================================================
        # ВАЖНЫЙ БЛОК:
        # Ищем цены в HTML всего блока
        # ====================================================

        try:

            raw_html = str(card)

            price_values.extend(
                extract_prices(raw_html)
            )

        except Exception:
            pass

        # ====================================================
        # UNIQUE
        # ====================================================

        unique_prices = []

        for price in price_values:

            price = round(
                float(price),
                2
            )

            if (
                price not in unique_prices
                and 0.5 <= price <= 1000
            ):

                unique_prices.append(
                    price
                )

        unique_prices.sort()

        current_price, old_price = (
            find_best_prices(
                unique_prices
            )
        )

        # ====================================================
        # DISCOUNT
        # ====================================================

        text_discount = (
            extract_discount_percent(
                card_text
            )
        )

        discount_percent = (
            text_discount
            or json_discount
        )

        calculated_discount = (
            calculate_discount(
                old_price,
                current_price
            )
        )

        if calculated_discount:

            if (
                discount_percent is None
                or calculated_discount
                > discount_percent
            ):

                discount_percent = (
                    calculated_discount
                )

        # ====================================================
        # DEBUG
        # ====================================================

        logger.info(
            f"🔎 CARD #{index} | "
            f"{title[:65]} | "
            f"prices={unique_prices[:15]} | "
            f"discount_text={text_discount} | "
            f"discount_json={json_discount} | "
            f"discount_calc={calculated_discount}"
        )

        # ====================================================
        # PRICE NOT FOUND
        # ====================================================

        if not current_price:

            logger.info(
                f"⏭ {title[:65]} | "
                "цена не найдена"
            )

            return None

        # ====================================================
        # DISCOUNT NOT FOUND
        # ====================================================

        if not discount_percent:

            logger.info(
                f"⏭ {title[:65]} | "
                f"${current_price:.2f} | "
                "скидка не определена"
            )

            return None

        # ====================================================
        # MIN DISCOUNT
        # ====================================================

        if (
            discount_percent
            < MIN_DISCOUNT_PERCENT
        ):

            return None

        # ====================================================
        # OLD PRICE
        # ====================================================

        if not old_price:

            # Если есть процент скидки,
            # восстанавливаем старую цену
            if discount_percent > 0:

                old_price = round(
                    current_price
                    / (
                        1
                        - discount_percent / 100
                    ),
                    2
                )

        if not old_price:

            return None

        if old_price <= current_price:

            return None

        # ====================================================
        # ID
        # ====================================================

        product_id = (
            extract_product_id(
                link
            )
        )

        if not product_id:
            product_id = link

        # ====================================================
        # DEAL
        # ====================================================

        deal = {

            "id": str(product_id),

            "title": title,

            "brand": (
                brand
                if brand
                else "iHerb"
            ),

            "orig_price_usd": round(
                old_price,
                2
            ),

            "discount_price_usd": round(
                current_price,
                2
            ),

            "discount_percent": int(
                discount_percent
            ),

            "link": link,
        }

        logger.info(
            f"🔥 ПРОШЁЛ ФИЛЬТР | "
            f"-{deal['discount_percent']}% | "
            f"${deal['discount_price_usd']:.2f} | "
            f"{deal['title'][:70]}"
        )

        return deal

    except Exception as e:

        logger.exception(
            f"❌ CARD #{index}: {e}"
        )

        return None


# ============================================================
# FETCH SPECIALS
# ============================================================

async def fetch_iherb_specials():

    logger.info(
        "=" * 60
    )

    logger.info(
        "🔎 НАЧИНАЕМ ПРОВЕРКУ iHERB"
    )

    logger.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logger.info(
        "=" * 60
    )

    html = await get_iherb_html()

    if not html:
        return []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        cards = find_product_cards(
            soup
        )

        deals = []

        for index, card in enumerate(
            cards,
            start=1
        ):

            deal = parse_product_card(
                card,
                index
            )

            if deal:
                deals.append(
                    deal
                )

        # ====================================================
        # UNIQUE
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
        # SORT
        # ====================================================

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"]
            ),
            reverse=True
        )

        logger.info(
            "=" * 60
        )

        logger.info(
            f"🔥 НАЙДЕНО СКИДОК: "
            f"{len(deals)}"
        )

        for deal in deals[:20]:

            logger.info(
                f"💊 -{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:80]}"
            )

        logger.info(
            "=" * 60
        )

        return deals

    except Exception as e:

        logger.exception(
            f"❌ Ошибка парсинга: {e}"
        )

        return []


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def format_deal_message(deal):

    title = escape(
        deal["title"]
    )

    brand = escape(
        deal["brand"]
    )

    old_usd = deal[
        "orig_price_usd"
    ]

    current_usd = deal[
        "discount_price_usd"
    ]

    percent = deal[
        "discount_percent"
    ]

    link = deal["link"]

    # ========================================================
    # COST
    # ========================================================

    cost_kzt = round(
        current_usd
        * KZT_EXCHANGE_RATE
    )

    # ========================================================
    # SELL
    # ========================================================

    resell_price_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT / 100
        )
    )

    # ========================================================
    # PROFIT
    # ========================================================

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

        f"🏷 <b>Бренд:</b> {brand}\n\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n\n"

        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${old_usd:.2f}</s> "
        f"➡️ <b>${current_usd:.2f}</b>\n\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{resell_str} ₸\n\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n\n"

        f"💱 <b>Курс:</b> "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть товар на iHerb",
                    url=link
                )
            ]
        ]
    )

    return message, keyboard


# ============================================================
# TARGETS
# ============================================================

def get_targets():

    targets = set()

    # CHAT_ID используется только если он задан
    if CHAT_ID:

        targets.add(
            CHAT_ID
        )

    # Пользователи, которые нажали /start
    targets.update(
        subscribers
    )

    return targets


# ============================================================
# SEND MESSAGE
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

    for target_id in list(targets):

        try:

            await bot.send_message(
                chat_id=target_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            logger.info(
                f"✅ Отправлено {target_id}: "
                f"{deal['title'][:70]}"
            )

            success = True

            await asyncio.sleep(1)

        except TelegramRetryAfter as e:

            retry_after = int(
                getattr(
                    e,
                    "retry_after",
                    30
                )
            )

            logger.warning(
                f"⏳ Telegram Flood Control. "
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

                logger.error(
                    f"❌ Повторная отправка: "
                    f"{retry_error}"
                )

        except Exception as e:

            error_text = str(e)

            logger.error(
                f"❌ Telegram {target_id}: "
                f"{error_text}"
            )

            # =================================================
            # УДАЛЯЕМ НЕРАБОЧИЙ CHAT_ID
            # =================================================

            if (
                "chat not found"
                in error_text.lower()
                or "bot was kicked"
                in error_text.lower()
                or "user is deactivated"
                in error_text.lower()
            ):

                if target_id in subscribers:

                    subscribers.discard(
                        target_id
                    )

                    logger.warning(
                        f"🗑 Удалён недоступный "
                        f"subscriber: {target_id}"
                    )

    return success


# ============================================================
# CHECK AND NOTIFY
# ============================================================

async def check_and_notify(
    force_send=False
):

    global check_counter
    global last_check_time
    global last_deals_found
    global last_deals_sent

    check_counter += 1

    check_number = check_counter

    started = datetime.now()

    last_check_time = started

    logger.info(
        ""
    )

    logger.info(
        "╔" + "═" * 58 + "╗"
    )

    logger.info(
        f"║ 🔄 ПРОВЕРКА #{check_number}"
    )

    logger.info(
        f"║ ⏰ {started.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    logger.info(
        f"║ 🎯 Скидка от {MIN_DISCOUNT_PERCENT}%"
    )

    logger.info(
        "╚" + "═" * 58 + "╝"
    )

    try:

        deals = await fetch_iherb_specials()

    except Exception as e:

        logger.exception(
            f"❌ Ошибка проверки: {e}"
        )

        last_deals_found = 0
        last_deals_sent = 0

        return

    last_deals_found = len(
        deals
    )

    last_deals_sent = 0

    if not deals:

        logger.info(
            f"ℹ️ Проверка #{check_number}: "
            "подходящих товаров нет."
        )

        logger.info(
            "💤 Бот продолжает работать."
        )

        return

    targets = get_targets()

    if not targets:

        logger.warning(
            "⚠️ НЕТ ПОЛУЧАТЕЛЕЙ TELEGRAM."
        )

        logger.warning(
            "👉 Откройте бота и нажмите /start"
        )

        return

    logger.info(
        f"👥 Получателей: {len(targets)}"
    )

    sent_count = 0

    for deal in deals:

        deal_id = str(
            deal["id"]
        )

        # ====================================================
        # AUTOMATIC MODE
        # ====================================================

        if not force_send:

            if deal_id in sent_deals_cache:

                logger.info(
                    f"⏭ Уже отправлялся: "
                    f"{deal['title'][:70]}"
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

        if (
            sent_count
            >= MAX_DEALS_PER_CHECK
        ):
            break

    last_deals_sent = sent_count

    logger.info(
        f"📤 Проверка #{check_number} завершена."
    )

    logger.info(
        f"📦 Найдено: {len(deals)}"
    )

    logger.info(
        f"📨 Отправлено: {sent_count}"
    )

    logger.info(
        "💤 Следующая проверка через "
        f"{CHECK_INTERVAL_SECONDS // 60} минут."
    )


# ============================================================
# START
# ============================================================

@dp.message(
    Command("start")
)
async def start_handler(message):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    logger.info(
        f"👤 Новый subscriber: {chat_id}"
    )

    await message.answer(

        "👋 <b>iHerb Deal Bot работает!</b>\n\n"

        "🔥 Я автоматически ищу "
        "товары со скидкой.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"⏱ Проверка: "
        f"<b>каждые 5 минут</b>\n\n"

        "📦 Поиск: "
        "<b>все бренды</b>\n\n"

        "Теперь этот Telegram автоматически "
        "зарегистрирован для получения скидок.\n\n"

        "🔥 Нажмите «Получить скидки», "
        "чтобы проверить iHerb прямо сейчас.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# DEALS
# ============================================================

@dp.message(
    Command("deals")
)
@dp.message(
    F.text == "🔥 Получить скидки"
)
async def deals_handler(message):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    logger.info(
        f"🔥 Ручная проверка от {chat_id}"
    )

    await message.answer(
        "🔎 <b>Проверяю iHerb...</b>\n\n"
        "⏳ Пожалуйста, подождите несколько секунд.",
        parse_mode=ParseMode.HTML,
    )

    before = last_deals_sent

    await check_and_notify(
        force_send=True
    )

    if last_deals_found:

        await message.answer(

            f"✅ Проверка завершена.\n\n"
            f"🔎 Найдено скидок: "
            f"<b>{last_deals_found}</b>\n"
            f"📨 Отправлено: "
            f"<b>{last_deals_sent}</b>",

            reply_markup=main_keyboard,

            parse_mode=ParseMode.HTML,
        )

    else:

        await message.answer(

            "ℹ️ Сейчас подходящих скидок "
            f"от {MIN_DISCOUNT_PERCENT}% "
            "не найдено.\n\n"
            "Автоматический мониторинг "
            "продолжает работать.",

            reply_markup=main_keyboard,

            parse_mode=ParseMode.HTML,
        )


# ============================================================
# STATUS
# ============================================================

@dp.message(
    Command("status")
)
@dp.message(
    F.text == "ℹ️ Статус"
)
async def status_handler(message):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    brands_text = (
        "Все бренды"
        if not TARGET_BRANDS
        else ", ".join(TARGET_BRANDS)
    )

    now = datetime.now()

    if last_check_time:

        seconds_ago = int(
            (
                now
                - last_check_time
            ).total_seconds()
        )

        if seconds_ago < 60:

            last_check_text = (
                f"{seconds_ago} сек. назад"
            )

        elif seconds_ago < 3600:

            last_check_text = (
                f"{seconds_ago // 60} мин. назад"
            )

        else:

            last_check_text = (
                f"{seconds_ago // 3600} ч. назад"
            )

    else:

        last_check_text = "ещё не было"

    await message.answer(

        f"📊 <b>СТАТУС iHERB БОТА</b>\n\n"

        f"🟢 Telegram: <b>ONLINE</b>\n"

        f"🟢 Мониторинг: <b>РАБОТАЕТ</b>\n\n"

        f"🔄 Проверка каждые: "
        f"<b>5 минут</b>\n"

        f"🔎 Проверок выполнено: "
        f"<b>{check_counter}</b>\n"

        f"⏰ Последняя проверка: "
        f"<b>{last_check_text}</b>\n\n"

        f"📦 Последняя найденная скидка: "
        f"<b>{last_deals_found}</b>\n"

        f"📨 Отправлено в последней проверке: "
        f"<b>{last_deals_sent}</b>\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Бренды: "
        f"<b>{brands_text}</b>\n\n"

        f"💱 Курс: "
        f"<b>1 USD = {KZT_EXCHANGE_RATE} ₸</b>\n"

        f"📈 Наценка: "
        f"<b>+{MARGIN_MARKUP_PERCENT}%</b>\n\n"

        f"👥 Получателей: "
        f"<b>{len(get_targets())}</b>\n"

        f"💾 В памяти: "
        f"<b>{len(sent_deals_cache)}</b> товаров",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# OTHER
# ============================================================

@dp.message()
async def any_message_handler(message):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(

        "👋 <b>iHerb Deal Bot</b>\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте:\n\n"

        "🔥 <b>Получить скидки</b> — "
        "проверить iHerb сейчас\n\n"

        "ℹ️ <b>Статус</b> — "
        "проверить работу бота.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# HEARTBEAT
# ============================================================

async def heartbeat():

    """
    Каждую минуту пишем в Render Logs,
    чтобы было видно, что процесс жив.
    """

    while True:

        try:

            await asyncio.sleep(60)

            logger.info(
                "💚 HEARTBEAT | "
                f"Бот работает | "
                f"Проверок: {check_counter} | "
                f"Получателей: {len(get_targets())} | "
                f"Cache: {len(sent_deals_cache)} | "
                f"Следующая проверка: "
                f"{CHECK_INTERVAL_SECONDS // 60} мин."
            )

        except asyncio.CancelledError:

            logger.info(
                "🛑 Heartbeat остановлен."
            )

            break

        except Exception as e:

            logger.error(
                f"❌ Heartbeat: {e}"
            )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН"
    )

    logger.info(
        "⚡ Первая проверка выполняется СРАЗУ"
    )

    try:

        await check_and_notify(
            force_send=False
        )

    except Exception as e:

        logger.exception(
            f"❌ Первая проверка: {e}"
        )

    while True:

        try:

            logger.info(
                f"💤 Следующая проверка через "
                f"{CHECK_INTERVAL_SECONDS // 60} минут..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL_SECONDS
            )

            logger.info(
                "⏰ Интервал завершён."
            )

            logger.info(
                "🔄 ЗАПУСК НОВОЙ ПРОВЕРКИ iHERB"
            )

            await check_and_notify(
                force_send=False
            )

        except asyncio.CancelledError:

            logger.info(
                "🛑 Scheduler остановлен."
            )

            break

        except Exception as e:

            logger.exception(
                f"❌ Scheduler error: {e}"
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

        logger.info(
            f"🌐 Render Health Server "
            f"запущен на порту {port}"
        )

    except Exception as e:

        logger.exception(
            f"❌ Health Server: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "=" * 60
    )

    logger.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logger.info(
        "=" * 60
    )

    logger.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logger.info(
        f"💱 USD/KZT: "
        f"{KZT_EXCHANGE_RATE}"
    )

    logger.info(
        f"📈 Наценка: "
        f"{MARGIN_MARKUP_PERCENT}%"
    )

    logger.info(
        f"🔄 Интервал: "
        f"{CHECK_INTERVAL_SECONDS // 60} минут"
    )

    if TARGET_BRANDS:

        logger.info(
            "🏷 Фильтр брендов: "
            + ", ".join(
                TARGET_BRANDS
            )
        )

    else:

        logger.info(
            "🏷 Бренды: ВСЕ"
        )

    # ========================================================
    # CACHE
    # ========================================================

    load_cache()

    # ========================================================
    # HEALTH
    # ========================================================

    await start_dummy_server()

    # ========================================================
    # BACKGROUND TASKS
    # ========================================================

    scheduler_task = asyncio.create_task(
        scheduler()
    )

    heartbeat_task = asyncio.create_task(
        heartbeat()
    )

    # ========================================================
    # TELEGRAM
    # ========================================================

    while True:

        try:

            try:

                await bot.delete_webhook(
                    drop_pending_updates=True
                )

            except Exception as e:

                logger.warning(
                    f"Webhook: {e}"
                )

            logger.info(
                "🤖 Telegram polling запущен."
            )

            await dp.start_polling(
                bot
            )

            break

        except Exception as e:

            error_text = str(e)

            logger.error(
                f"❌ Telegram polling: "
                f"{error_text}"
            )

            if (
                "Conflict"
                in error_text
                or "409"
                in error_text
                or "terminated by other"
                in error_text
            ):

                logger.warning(
                    "⚠️ Telegram Conflict. "
                    "Ждём 10 секунд..."
                )

                await asyncio.sleep(
                    10
                )

            elif "Unauthorized" in error_text:

                logger.error(
                    "❌ TELEGRAM UNAUTHORIZED!"
                )

                await asyncio.sleep(
                    30
                )

            else:

                logger.warning(
                    "🔄 Перезапуск Telegram "
                    "через 5 секунд..."
                )

                await asyncio.sleep(
                    5
                )

    # ========================================================
    # STOP
    # ========================================================

    scheduler_task.cancel()
    heartbeat_task.cancel()

    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass

    try:
        await heartbeat_task
    except asyncio.CancelledError:
        pass

    await bot.session.close()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "🛑 Бот остановлен."
        )
