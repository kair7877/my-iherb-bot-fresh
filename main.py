import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
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
# iHERB DEAL BOT
# STABLE RENDER VERSION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("iherb_bot")


# ============================================================
# ENV
# ============================================================

def env_str(name, default=""):
    value = os.getenv(name, default)

    if value is None:
        return default

    value = str(value).strip()

    if value.upper() == name.upper():
        return default

    return value


def env_int(name, default):
    value = env_str(name, "")

    if not value:
        return default

    try:
        return int(float(value))
    except Exception:
        logger.warning(
            "⚠️ %s=%s некорректно. Использую %s",
            name,
            value,
            default,
        )
        return default


def env_float(name, default):
    value = env_str(name, "")

    if not value:
        return default

    try:
        return float(value.replace(",", "."))
    except Exception:
        logger.warning(
            "⚠️ %s=%s некорректно. Использую %s",
            name,
            value,
            default,
        )
        return default


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = env_str("BOT_TOKEN", "")
CHAT_ID = env_str("CHAT_ID", "")

KZT_EXCHANGE_RATE = env_float(
    "KZT_EXCHANGE_RATE",
    540,
)

MARGIN_MARKUP_PERCENT = env_float(
    "MARGIN_MARKUP_PERCENT",
    35,
)

MIN_DISCOUNT_PERCENT = env_int(
    "MIN_DISCOUNT_PERCENT",
    20,
)

MAX_DISCOUNT_PERCENT = env_int(
    "MAX_DISCOUNT_PERCENT",
    90,
)

MAX_DEALS_PER_CHECK = env_int(
    "MAX_DEALS_PER_CHECK",
    10,
)

CHECK_INTERVAL_SECONDS = max(
    60,
    env_int(
        "CHECK_INTERVAL_SECONDS",
        300,
    ),
)

HEARTBEAT_SECONDS = max(
    30,
    env_int(
        "HEARTBEAT_SECONDS",
        60,
    ),
)

DATABASE_URL = env_str(
    "DATABASE_URL",
    "",
)


# ============================================================
# BRANDS
#
# [] = ВСЕ БРЕНДЫ
# ============================================================

TARGET_BRANDS = []


# ============================================================
# BOT
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "❌ BOT_TOKEN не найден в Render Environment Variables."
    )


bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()


# ============================================================
# OPTIONAL POSTGRES
# ============================================================

try:
    import asyncpg

    HAS_POSTGRES = True

except ImportError:
    asyncpg = None
    HAS_POSTGRES = False

    logger.warning(
        "⚠️ asyncpg не установлен."
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
    curl_requests = None
    HAS_CURL_CFFI = False

    logger.warning(
        "⚠️ curl_cffi отсутствует. Используем httpx."
    )


# ============================================================
# CACHE
#
# НИКОГДА НЕ ИСПОЛЬЗУЕМ /var/data
# Render Free может запретить запись туда.
#
# /tmp разрешён.
# ============================================================

CACHE_DIR = os.path.join(
    tempfile.gettempdir(),
    "iherb_bot",
)

CACHE_FILE = os.path.join(
    CACHE_DIR,
    "sent_deals.json",
)

MAX_CACHE_ITEMS = 5000

sent_deals_cache = set()


# ============================================================
# RUNTIME
# ============================================================

subscribers = set()

validated_chat_id = None

last_check_started = None
last_check_finished = None

last_check_ok = False
last_check_found = 0
last_check_sent = 0

next_check_at = None

check_in_progress = False

monitor_started_at = (
    datetime.now(timezone.utc)
)


# ============================================================
# KEYBOARD
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
    resize_keyboard=True,
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
        "en-US,en;q=0.9,ru;q=0.8"
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
# STORAGE
# ============================================================

async def init_storage():

    global sent_deals_cache

    # --------------------------------------------------------
    # POSTGRES
    # --------------------------------------------------------

    if DATABASE_URL and HAS_POSTGRES:

        try:

            conn = await asyncpg.connect(
                DATABASE_URL
            )

            try:

                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sent_deals (
                        product_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        discount INTEGER,
                        price_usd DOUBLE PRECISION,
                        link TEXT,
                        sent_at TIMESTAMPTZ
                            NOT NULL DEFAULT NOW()
                    )
                    """
                )

            finally:

                await conn.close()

            logger.info(
                "💾 PostgreSQL подключён."
            )

            return

        except Exception as e:

            logger.exception(
                "❌ PostgreSQL недоступен: %s",
                e,
            )

            logger.warning(
                "⚠️ Перехожу на временный cache /tmp."
            )

    # --------------------------------------------------------
    # TEMP CACHE
    # --------------------------------------------------------

    try:

        os.makedirs(
            CACHE_DIR,
            exist_ok=True,
        )

        load_cache()

        logger.info(
            "💾 Cache: %s",
            CACHE_FILE,
        )

    except Exception as e:

        logger.warning(
            "⚠️ Cache init ошибка: %s",
            e,
        )

        sent_deals_cache = set()


def load_cache():

    global sent_deals_cache

    try:

        if not os.path.exists(
            CACHE_FILE
        ):

            sent_deals_cache = set()

            logger.info(
                "💾 Cache пока пуст."
            )

            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as file:

            data = json.load(
                file
            )

        if isinstance(
            data,
            list,
        ):

            sent_deals_cache = {
                str(x)
                for x in data
            }

        else:

            sent_deals_cache = set()

        logger.info(
            "💾 Cache загружен: %s товаров.",
            len(sent_deals_cache),
        )

    except Exception as e:

        logger.warning(
            "⚠️ Ошибка загрузки cache: %s",
            e,
        )

        sent_deals_cache = set()


def save_cache():

    try:

        # ----------------------------------------------------
        # ГАРАНТИРОВАННО /tmp
        # ----------------------------------------------------

        os.makedirs(
            CACHE_DIR,
            exist_ok=True,
        )

        data = list(
            sent_deals_cache
        )[-MAX_CACHE_ITEMS:]

        temp_file = (
            CACHE_FILE
            + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as file:

            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            temp_file,
            CACHE_FILE,
        )

        logger.debug(
            "💾 Cache сохранён: %s",
            CACHE_FILE,
        )

    except Exception as e:

        logger.warning(
            "⚠️ Cache не сохранён: %s",
            e,
        )


async def is_sent(product_id):

    product_id = str(
        product_id
    )

    # --------------------------------------------------------
    # POSTGRES
    # --------------------------------------------------------

    if DATABASE_URL and HAS_POSTGRES:

        try:

            conn = await asyncpg.connect(
                DATABASE_URL
            )

            try:

                result = await conn.fetchval(
                    """
                    SELECT 1
                    FROM sent_deals
                    WHERE product_id=$1
                    LIMIT 1
                    """,
                    product_id,
                )

                return result is not None

            finally:

                await conn.close()

        except Exception as e:

            logger.warning(
                "⚠️ DB is_sent: %s",
                e,
            )

    # --------------------------------------------------------
    # TEMP CACHE
    # --------------------------------------------------------

    return (
        product_id
        in sent_deals_cache
    )


async def mark_sent(deal):

    global sent_deals_cache

    product_id = str(
        deal["id"]
    )

    # --------------------------------------------------------
    # СНАЧАЛА добавляем в память
    # --------------------------------------------------------

    sent_deals_cache.add(
        product_id
    )

    if len(
        sent_deals_cache
    ) > MAX_CACHE_ITEMS:

        sent_deals_cache = set(
            list(
                sent_deals_cache
            )[-MAX_CACHE_ITEMS:]
        )

    # --------------------------------------------------------
    # POSTGRES
    # --------------------------------------------------------

    if DATABASE_URL and HAS_POSTGRES:

        try:

            conn = await asyncpg.connect(
                DATABASE_URL
            )

            try:

                await conn.execute(
                    """
                    INSERT INTO sent_deals
                    (
                        product_id,
                        title,
                        discount,
                        price_usd,
                        link
                    )
                    VALUES
                    (
                        $1,
                        $2,
                        $3,
                        $4,
                        $5
                    )
                    ON CONFLICT(product_id)
                    DO NOTHING
                    """,
                    product_id,
                    deal["title"],
                    deal["discount_percent"],
                    deal["discount_price_usd"],
                    deal["link"],
                )

            finally:

                await conn.close()

            logger.debug(
                "💾 DB сохранён: %s",
                product_id,
            )

            return

        except Exception as e:

            logger.warning(
                "⚠️ DB mark_sent: %s",
                e,
            )

    # --------------------------------------------------------
    # TEMP CACHE
    # --------------------------------------------------------

    save_cache()


async def cache_count():

    if DATABASE_URL and HAS_POSTGRES:

        try:

            conn = await asyncpg.connect(
                DATABASE_URL
            )

            try:

                value = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM sent_deals
                    """
                )

                return int(
                    value or 0
                )

            finally:

                await conn.close()

        except Exception as e:

            logger.warning(
                "⚠️ DB count: %s",
                e,
            )

    return len(
        sent_deals_cache
    )


# ============================================================
# HELPERS
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text),
    ).strip()


def safe_float(value):

    if value is None:
        return None

    try:

        text = str(
            value
        )

        text = (
            text
            .replace("\xa0", "")
            .replace(" ", "")
        )

        if (
            "," in text
            and "." not in text
        ):

            text = text.replace(
                ",",
                ".",
            )

        else:

            text = text.replace(
                ",",
                "",
            )

        text = re.sub(
            r"[^\d.]",
            "",
            text,
        )

        if not text:
            return None

        number = float(
            text
        )

        if number <= 0:
            return None

        if number >= 1000000:
            return None

        return number

    except Exception:

        return None


# ============================================================
# PRICE PARSER
# ============================================================

def extract_currency_prices(text):

    if not text:
        return []

    text = str(
        text
    ).replace(
        "\xa0",
        " ",
    )

    patterns = [

        (
            "USD",
            r"US\$\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)",
        ),

        (
            "USD",
            r"\$\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)",
        ),

        (
            "USD",
            r"USD\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)",
        ),

        (
            "USD",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*(?:USD|US\$|\$)",
        ),

        (
            "KZT",
            r"₸\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)",
        ),

        (
            "KZT",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*₸",
        ),

        (
            "KZT",
            r"KZT\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)",
        ),

        (
            "KZT",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*KZT",
        ),
    ]

    result = []

    for currency, pattern in patterns:

        try:

            matches = re.findall(
                pattern,
                text,
                re.IGNORECASE,
            )

            for value in matches:

                number = safe_float(
                    value
                )

                if number is None:
                    continue

                result.append(
                    (
                        currency,
                        number,
                    )
                )

        except Exception:
            pass

    return result


# ============================================================
# DISCOUNT
# ============================================================

def extract_discount_percent(text):

    if not text:
        return None

    text = clean_text(
        text
    )

    patterns = [

        r"(\d{1,2})\s*%\s*off",

        r"(\d{1,2})\s*%\s*discount",

        r"-\s*(\d{1,2})\s*%",

        r"(\d{1,2})\s*%\s*скид",

        r"скидк[аи]?\s*(?:до\s*)?"
        r"(\d{1,2})\s*%",

        r"save\s+(\d{1,2})\s*%",

        r"(\d{1,2})\s*%\s*save",
    ]

    for pattern in patterns:

        try:

            match = re.search(
                pattern,
                text,
                re.IGNORECASE,
            )

            if not match:
                continue

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

    url = str(
        url
    ).strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return (
            "https://www.iherb.com"
            + url
        )

    if url.startswith(
        "http://"
    ):

        return (
            "https://"
            + url[7:]
        )

    if url.startswith(
        "https://"
    ):

        return url

    return urljoin(
        "https://www.iherb.com/",
        url,
    )


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

        try:

            match = re.search(
                pattern,
                link,
            )

            if match:
                return match.group(1)

        except Exception:
            pass

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

        if (
            brand.lower()
            in title_lower
        ):

            return brand

    return ""


# ============================================================
# DISCOUNT CALCULATIONS
# ============================================================

def calculate_discount(
    old_price,
    current_price,
):

    if not old_price:
        return None

    if not current_price:
        return None

    if old_price <= current_price:
        return None

    percent = round(
        (
            1
            - current_price
            / old_price
        )
        * 100
    )

    return percent


def infer_old_price(
    current_price,
    discount_percent,
):

    if not current_price:
        return None

    if not discount_percent:
        return None

    if discount_percent >= 100:
        return None

    return round(
        current_price
        / (
            1
            - discount_percent / 100
        ),
        2,
    )


def choose_prices(
    currency_prices,
    discount_percent,
):

    if not currency_prices:

        return (
            None,
            None,
            None,
        )

    by_currency = {
        "USD": [],
        "KZT": [],
    }

    for currency, value in currency_prices:

        if currency not in by_currency:
            continue

        value = round(
            value,
            2,
        )

        if value not in by_currency[
            currency
        ]:

            by_currency[
                currency
            ].append(
                value
            )

    # --------------------------------------------------------
    # USD
    # --------------------------------------------------------

    if by_currency["USD"]:

        values = sorted(
            by_currency["USD"]
        )

        current = values[0]

        old = (
            values[-1]
            if len(values) >= 2
            else None
        )

        if (
            old is not None
            and old <= current
        ):

            old = None

        if (
            old is None
            and discount_percent
        ):

            old = infer_old_price(
                current,
                discount_percent,
            )

        return (
            current,
            old,
            "USD",
        )

    # --------------------------------------------------------
    # KZT
    # --------------------------------------------------------

    if by_currency["KZT"]:

        values = sorted(
            by_currency["KZT"]
        )

        current_kzt = values[0]

        old_kzt = (
            values[-1]
            if len(values) >= 2
            else None
        )

        if (
            old_kzt is not None
            and old_kzt <= current_kzt
        ):

            old_kzt = None

        if (
            old_kzt is None
            and discount_percent
        ):

            old_kzt = infer_old_price(
                current_kzt,
                discount_percent,
            )

        current_usd = (
            current_kzt
            / KZT_EXCHANGE_RATE
        )

        old_usd = (
            old_kzt
            / KZT_EXCHANGE_RATE
            if old_kzt
            else None
        )

        return (
            current_usd,
            old_usd,
            "KZT",
        )

    return (
        None,
        None,
        None,
    )


# ============================================================
# CARD PRICE TEXT
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
                        strip=True,
                    )
                )

                if text:
                    texts.append(
                        text
                    )

        except Exception:
            pass

    return texts


# ============================================================
# JSON DATA
# ============================================================

def extract_json_data(card):

    prices = []
    discounts = []

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                if not isinstance(
                    value,
                    str,
                ):
                    continue

                key_lower = key.lower()

                if (
                    "price"
                    in key_lower
                    or "amount"
                    in key_lower
                    or "cost"
                    in key_lower
                ):

                    prices.extend(
                        extract_currency_prices(
                            value
                        )
                    )

                if (
                    "discount"
                    in key_lower
                    or "percent"
                    in key_lower
                ):

                    discount = (
                        extract_discount_percent(
                            value
                        )
                    )

                    if discount:
                        discounts.append(
                            discount
                        )

    except Exception:
        pass

    return (
        prices,
        discounts,
    )


# ============================================================
# SCRIPT DATA
# ============================================================

def extract_script_price_data(card):

    prices = []
    discounts = []

    try:

        for script in card.find_all(
            "script"
        ):

            text = (
                script.string
                or script.get_text()
            )

            if not text:
                continue

            prices.extend(
                extract_currency_prices(
                    text
                )
            )

            discount = (
                extract_discount_percent(
                    text
                )
            )

            if discount:
                discounts.append(
                    discount
                )

    except Exception:
        pass

    return (
        prices,
        discounts,
    )


# ============================================================
# IHERB HTML
# ============================================================

async def get_iherb_html():

    urls = [

        (
            "https://www.iherb.com/deals"
            "?lang=en-US&currency=USD"
        ),

        "https://www.iherb.com/deals",

        "https://kz.iherb.com/deals",

        "https://kz.iherb.com/specials",

        "https://www.iherb.com/specials",
    ]

    cookies = {
        "ih-pref":
            "lan=en-US&currency=USD&country=KZ",

        "iherb-pref":
            "lan=en-US&currency=USD&country=KZ",
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
                        "iHerb | curl %s | HTTP %s | %s chars",
                        browser,
                        response.status_code,
                        len(response.text),
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        return response.text

                except Exception as e:

                    logger.debug(
                        "curl error: %s",
                        e,
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
                    "iHerb | httpx | HTTP %s | %s chars | %s",
                    response.status_code,
                    len(response.text),
                    url,
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    return response.text

        except Exception as e:

            logger.debug(
                "httpx error: %s",
                e,
            )

    logger.error(
        "❌ Не удалось получить HTML iHerb."
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
    ]

    cards = []

    for selector in selectors:

        try:

            found = soup.select(
                selector
            )

            if found:

                logger.info(
                    "🔍 %s: %s карточек",
                    selector,
                    len(found),
                )

                cards.extend(
                    found
                )

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
                strip=True,
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
                    "",
                )
            )

        key = (
            link
            + "|"
            + text[:600]
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            card
        )

    logger.info(
        "📦 Уникальных карточек: %s",
        len(unique),
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
                        strip=True,
                    )
                )

                if len(title) >= 3:
                    return title

        except Exception:
            pass

    try:

        links = card.select(
            "a[href]"
        )

        for link in links:

            text = clean_text(
                link.get_text(
                    " ",
                    strip=True,
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

            href = normalize_url(
                element.get(
                    "href",
                    "",
                )
            )

            if href:
                return href

        except Exception:
            pass

    return ""


# ============================================================
# PARSE CARD
# ============================================================

def parse_product_card(
    card,
    index,
):

    try:

        card_text = clean_text(
            card.get_text(
                " ",
                strip=True,
            )
        )

        if not card_text:
            return None

        title = extract_title(
            card
        )

        if not title:
            return None

        link = extract_link(
            card
        )

        if not link:
            return None

        brand = find_brand(
            title
        )

        if (
            TARGET_BRANDS
            and not brand
        ):
            return None

        # ----------------------------------------------------
        # DISCOUNT
        # ----------------------------------------------------

        text_discount = (
            extract_discount_percent(
                card_text
            )
        )

        # ----------------------------------------------------
        # PRICES
        # ----------------------------------------------------

        currency_prices = []

        for text in get_card_price_texts(
            card
        ):

            currency_prices.extend(
                extract_currency_prices(
                    text
                )
            )

        json_prices, json_discounts = (
            extract_json_data(
                card
            )
        )

        currency_prices.extend(
            json_prices
        )

        script_prices, script_discounts = (
            extract_script_price_data(
                card
            )
        )

        currency_prices.extend(
            script_prices
        )

        currency_prices.extend(
            extract_currency_prices(
                card_text
            )
        )

        # ----------------------------------------------------
        # DISCOUNT CANDIDATES
        # ----------------------------------------------------

        discount_candidates = []

        if text_discount:
            discount_candidates.append(
                text_discount
            )

        discount_candidates.extend(
            json_discounts
        )

        discount_candidates.extend(
            script_discounts
        )

        discount_percent = (
            max(
                discount_candidates
            )
            if discount_candidates
            else None
        )

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        (
            current_usd,
            old_usd,
            currency,
        ) = choose_prices(
            currency_prices,
            discount_percent,
        )

        # ----------------------------------------------------
        # CALCULATED DISCOUNT
        # ----------------------------------------------------

        calculated_discount = (
            calculate_discount(
                old_usd,
                current_usd,
            )
        )

        if calculated_discount is not None:

            if (
                discount_percent is None
                or calculated_discount > discount_percent
            ):

                discount_percent = (
                    calculated_discount
                )

        # ----------------------------------------------------
        # OLD PRICE
        # ----------------------------------------------------

        if (
            current_usd
            and discount_percent
            and not old_usd
        ):

            old_usd = infer_old_price(
                current_usd,
                discount_percent,
            )

        logger.info(
            "🔎 CARD #%s | %s | current=%s | old=%s | discount=%s",
            index,
            title[:80],
            current_usd,
            old_usd,
            discount_percent,
        )

        # ----------------------------------------------------
        # VALIDATION
        # ----------------------------------------------------

        if not current_usd:
            return None

        if not discount_percent:
            return None

        if (
            discount_percent
            < MIN_DISCOUNT_PERCENT
        ):
            return None

        if (
            discount_percent
            > MAX_DISCOUNT_PERCENT
        ):
            return None

        if not old_usd:
            return None

        if old_usd <= current_usd:
            return None

        product_id = (
            extract_product_id(
                link
            )
            or link
        )

        deal = {

            "id": product_id,

            "title": title,

            "brand": (
                brand
                or "iHerb"
            ),

            "orig_price_usd": round(
                old_usd,
                2,
            ),

            "discount_price_usd": round(
                current_usd,
                2,
            ),

            "discount_percent": int(
                discount_percent
            ),

            "link": link,
        }

        logger.info(
            "🔥 ПРОШЁЛ ФИЛЬТР | -%s%% | $%.2f | %s",
            deal["discount_percent"],
            deal["discount_price_usd"],
            deal["title"][:80],
        )

        return deal

    except Exception as e:

        logger.exception(
            "❌ Ошибка карточки #%s: %s",
            index,
            e,
        )

        return None


# ============================================================
# FETCH DEALS
# ============================================================

async def fetch_iherb_specials():

    logger.info(
        "🔎 Начинаем проверку iHerb..."
    )

    html = await get_iherb_html()

    if not html:
        return []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        cards = find_product_cards(
            soup
        )

        deals = []

        for index, card in enumerate(
            cards,
            start=1,
        ):

            deal = parse_product_card(
                card,
                index,
            )

            if deal:
                deals.append(
                    deal
                )

        # ----------------------------------------------------
        # UNIQUE
        # ----------------------------------------------------

        unique = {}

        for deal in deals:

            unique[
                deal["id"]
            ] = deal

        deals = list(
            unique.values()
        )

        # ----------------------------------------------------
        # SORT
        # ----------------------------------------------------

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"],
            ),
            reverse=True,
        )

        logger.info(
            "🔥 Найдено подходящих товаров: %s",
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
# TELEGRAM CHAT
# ============================================================

async def validate_configured_chat():

    global validated_chat_id

    if not CHAT_ID:

        logger.warning(
            "⚠️ CHAT_ID не задан."
        )

        return

    try:

        chat = await bot.get_chat(
            CHAT_ID
        )

        validated_chat_id = str(
            chat.id
        )

        chat_name = (
            getattr(
                chat,
                "title",
                None,
            )
            or getattr(
                chat,
                "username",
                None,
            )
            or ""
        )

        logger.info(
            "✅ CHAT_ID подтверждён: %s | %s",
            validated_chat_id,
            chat_name,
        )

    except Exception as e:

        validated_chat_id = None

        logger.error(
            "❌ CHAT_ID недоступен: %s",
            e,
        )


# ============================================================
# FORMAT MESSAGE
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

    link = deal[
        "link"
    ]

    # --------------------------------------------------------
    # KZT COST
    # --------------------------------------------------------

    cost_kzt = round(
        current_usd
        * KZT_EXCHANGE_RATE
    )

    # --------------------------------------------------------
    # SALE PRICE
    # --------------------------------------------------------

    resell_price_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT
            / 100
        )
    )

    # --------------------------------------------------------
    # PROFIT
    # --------------------------------------------------------

    profit_kzt = (
        resell_price_kzt
        - cost_kzt
    )

    cost_str = (
        f"{cost_kzt:,}"
        .replace(
            ",",
            " ",
        )
    )

    resell_str = (
        f"{resell_price_kzt:,}"
        .replace(
            ",",
            " ",
        )
    )

    profit_str = (
        f"{profit_kzt:,}"
        .replace(
            ",",
            " ",
        )
    )

    now = datetime.now(
        timezone(
            timedelta(hours=5)
        )
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
        f"1 USD = {KZT_EXCHANGE_RATE:g} ₸\n\n"

        f"📈 <b>Наценка:</b> "
        f"+{MARGIN_MARKUP_PERCENT:g}%\n\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{now.strftime('%d.%m.%Y %H:%M')}"
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
        keyboard,
    )


# ============================================================
# TARGETS
# ============================================================

def get_targets():

    targets = set()

    if validated_chat_id:

        targets.add(
            validated_chat_id
        )

    targets.update(
        subscribers
    )

    return targets


# ============================================================
# SEND DEAL
# ============================================================

async def send_deal(
    deal,
    targets,
):

    message, keyboard = (
        format_deal_message(
            deal
        )
    )

    success = False

    for target_id in list(
        targets
    ):

        try:

            await bot.send_message(
                chat_id=target_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            logger.info(
                "📤 НОВАЯ СКИДКА ОТПРАВЛЕНА: %s",
                deal["title"][:100],
            )

            success = True

            await asyncio.sleep(
                1.5
            )

        except TelegramRetryAfter as e:

            retry_after = int(
                getattr(
                    e,
                    "retry_after",
                    30,
                )
            )

            logger.warning(
                "⏳ Telegram Flood Control. Ждём %s сек.",
                retry_after + 2,
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
                    "❌ Повторная отправка: %s",
                    retry_error,
                )

        except Exception as e:

            error_text = str(e)
            low = error_text.lower()

            if (
                "chat not found"
                in low
                or "bot was kicked"
                in low
                or "user is deactivated"
                in low
            ):

                logger.error(
                    "🚫 Недоступный получатель %s: %s",
                    target_id,
                    error_text,
                )

                subscribers.discard(
                    str(target_id)
                )

                continue

            logger.error(
                "❌ Telegram %s: %s",
                target_id,
                error_text,
            )

    return success


# ============================================================
# CHECK AND NOTIFY
# ============================================================

async def check_and_notify():

    global last_check_started
    global last_check_finished
    global last_check_ok
    global last_check_found
    global last_check_sent
    global check_in_progress

    if check_in_progress:

        logger.warning(
            "⏭ Проверка уже идёт."
        )

        return

    check_in_progress = True

    last_check_started = (
        datetime.now(timezone.utc)
    )

    last_check_ok = False
    last_check_sent = 0

    logger.info(
        "=" * 70
    )

    logger.info(
        "🔎 ПРОВЕРКА iHERB"
    )

    logger.info(
        "🎯 Скидка: %s%%–%s%%",
        MIN_DISCOUNT_PERCENT,
        MAX_DISCOUNT_PERCENT,
    )

    logger.info(
        "📦 Лимит: %s",
        MAX_DEALS_PER_CHECK,
    )

    logger.info(
        "=" * 70
    )

    try:

        deals = await fetch_iherb_specials()

        last_check_found = len(
            deals
        )

        if not deals:

            last_check_ok = True

            logger.info(
                "ℹ️ Подходящих скидок не найдено."
            )

            return

        targets = get_targets()

        if not targets:

            last_check_ok = True

            logger.warning(
                "⚠️ Нет получателей Telegram."
            )

            logger.warning(
                "⚠️ Нажмите /start."
            )

            return

        sent_count = 0
        skipped_count = 0

        for deal in deals:

            deal_id = str(
                deal["id"]
            )

            # ------------------------------------------------
            # DUPLICATE PROTECTION
            # ------------------------------------------------

            if await is_sent(
                deal_id
            ):

                skipped_count += 1

                logger.info(
                    "⏭ Уже отправлялся: %s",
                    deal["title"][:100],
                )

                continue

            success = await send_deal(
                deal,
                targets,
            )

            if success:

                await mark_sent(
                    deal
                )

                sent_count += 1

            if (
                sent_count
                >= MAX_DEALS_PER_CHECK
            ):

                logger.info(
                    "🛑 Достигнут лимит %s.",
                    MAX_DEALS_PER_CHECK,
                )

                break

        last_check_sent = (
            sent_count
        )

        last_check_ok = True

        logger.info(
            "📊 РЕЗУЛЬТАТ:"
        )

        logger.info(
            "🔥 Найдено: %s",
            len(deals),
        )

        logger.info(
            "📤 Отправлено: %s",
            sent_count,
        )

        logger.info(
            "⏭ Уже отправлялось: %s",
            skipped_count,
        )

        logger.info(
            "💾 Сохранено: %s",
            await cache_count(),
        )

    except Exception as e:

        logger.exception(
            "❌ Ошибка проверки iHerb: %s",
            e,
        )

    finally:

        last_check_finished = (
            datetime.now(timezone.utc)
        )

        check_in_progress = False


# ============================================================
# /START
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

    await message.answer(

        "👋 <b>iHerb Deal Bot работает!</b>\n\n"

        "🔥 Автоматически ищу "
        "товары со скидкой.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🎯 Максимальная скидка: "
        f"<b>{MAX_DISCOUNT_PERCENT}%</b>\n"

        f"⏱ Проверка: "
        f"<b>каждые {CHECK_INTERVAL_SECONDS // 60} минут</b>\n\n"

        "💰 Поддерживаются "
        "<b>$</b> и <b>₸</b>.\n\n"

        "📈 Цена продажи и прибыль "
        "рассчитываются автоматически.",

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
async def deals_handler(message):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас...\n"
        "⏳ Подождите несколько секунд."
    )

    await check_and_notify()


# ============================================================
# DATETIME
# ============================================================

def format_dt(value):

    if not value:
        return "—"

    try:

        return value.astimezone().strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    except Exception:

        return str(value)


# ============================================================
# /STATUS
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
        else ", ".join(
            TARGET_BRANDS
        )
    )

    chat_text = (
        f"🟢 {validated_chat_id}"
        if validated_chat_id
        else "🔴 CHAT_ID не подключён"
    )

    storage_text = (
        "PostgreSQL"
        if (
            DATABASE_URL
            and HAS_POSTGRES
        )
        else "Временный cache /tmp"
    )

    await message.answer(

        "📊 <b>СТАТУС БОТА</b>\n\n"

        "🟢 Telegram: ONLINE\n"

        "🟢 Автомониторинг: ВКЛЮЧЁН\n"

        f"🔄 Проверка: каждые "
        f"{CHECK_INTERVAL_SECONDS // 60} минут\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🎯 Максимальная скидка: "
        f"<b>{MAX_DISCOUNT_PERCENT}%</b>\n"

        f"📦 Лимит за проверку: "
        f"<b>{MAX_DEALS_PER_CHECK}</b>\n\n"

        f"🏷 Бренды: {brands_text}\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE:g} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT:g}%\n\n"

        f"📨 Основной чат: "
        f"{chat_text}\n"

        f"👥 Получателей: "
        f"{len(get_targets())}\n"

        f"💾 Хранилище: "
        f"{storage_text}\n"

        f"💾 Отправлено ранее: "
        f"{await cache_count()} товаров\n\n"

        f"🕒 Последняя проверка: "
        f"{format_dt(last_check_finished)}\n"

        f"📦 Найдено: "
        f"{last_check_found}\n"

        f"📤 Отправлено: "
        f"{last_check_sent}\n"

        f"➡️ Следующая: "
        f"{format_dt(next_check_at)}",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# OTHER MESSAGE
# ============================================================

@dp.message()
async def any_message_handler(message):

    subscribers.add(
        str(message.chat.id)
    )

    await message.answer(

        "👋 <b>iHerb Deal Bot</b>\n\n"

        f"🔥 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте:\n\n"

        "🔥 <b>Получить скидки</b> — "
        "проверить iHerb сейчас.\n\n"

        "ℹ️ <b>Статус</b> — "
        "посмотреть настройки.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():

    global next_check_at

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН"
    )

    logger.info(
        "⚡ Первая проверка выполняется СРАЗУ"
    )

    while True:

        try:

            await check_and_notify()

            next_check_at = (
                datetime.now(timezone.utc)
                + timedelta(
                    seconds=CHECK_INTERVAL_SECONDS
                )
            )

            logger.info(
                "💤 Следующая проверка: %s",
                format_dt(
                    next_check_at
                ),
            )

            remaining = (
                CHECK_INTERVAL_SECONDS
            )

            while remaining > 0:

                sleep_time = min(
                    30,
                    remaining,
                )

                await asyncio.sleep(
                    sleep_time
                )

                remaining -= (
                    sleep_time
                )

        except asyncio.CancelledError:

            logger.info(
                "🛑 Мониторинг остановлен."
            )

            raise

        except Exception as e:

            logger.exception(
                "❌ Scheduler: %s",
                e,
            )

            next_check_at = (
                datetime.now(timezone.utc)
                + timedelta(
                    seconds=30
                )
            )

            await asyncio.sleep(
                30
            )


# ============================================================
# HEARTBEAT
# ============================================================

async def heartbeat():

    while True:

        try:

            logger.info(
                "❤️ HEARTBEAT | alive | "
                "last=%s | "
                "next=%s | "
                "checking=%s",
                format_dt(
                    last_check_finished
                ),
                format_dt(
                    next_check_at
                ),
                check_in_progress,
            )

            await asyncio.sleep(
                HEARTBEAT_SECONDS
            )

        except asyncio.CancelledError:

            return


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def start_health_server():

    try:

        from aiohttp import web

        port = int(
            os.environ.get(
                "PORT",
                "10000",
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
            home,
        )

        app.router.add_get(
            "/health",
            health,
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
            "🌐 Health Server запущен: %s",
            port,
        )

    except Exception as e:

        logger.exception(
            "❌ Health Server: %s",
            e,
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info(
        "=" * 70
    )

    logger.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logger.info(
        "=" * 70
    )

    logger.info(
        "🎯 Скидка: %s%%–%s%%",
        MIN_DISCOUNT_PERCENT,
        MAX_DISCOUNT_PERCENT,
    )

    logger.info(
        "💱 USD/KZT: %s",
        KZT_EXCHANGE_RATE,
    )

    logger.info(
        "📈 Наценка: %s%%",
        MARGIN_MARKUP_PERCENT,
    )

    logger.info(
        "📦 Лимит: %s",
        MAX_DEALS_PER_CHECK,
    )

    logger.info(
        "⏱ Интервал: %s секунд",
        CHECK_INTERVAL_SECONDS,
    )

    logger.info(
        "💾 Cache path: %s",
        CACHE_FILE,
    )

    # --------------------------------------------------------
    # STORAGE
    # --------------------------------------------------------

    await init_storage()

    # --------------------------------------------------------
    # HEALTH
    # --------------------------------------------------------

    await start_health_server()

    # --------------------------------------------------------
    # TELEGRAM CHAT
    # --------------------------------------------------------

    await validate_configured_chat()

    # --------------------------------------------------------
    # TASKS
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(
        scheduler()
    )

    heartbeat_task = asyncio.create_task(
        heartbeat()
    )

    # --------------------------------------------------------
    # TELEGRAM POLLING
    # --------------------------------------------------------

    while True:

        try:

            try:

                await bot.delete_webhook(
                    drop_pending_updates=True
                )

            except Exception as e:

                logger.warning(
                    "Webhook cleanup: %s",
                    e,
                )

            logger.info(
                "📡 Запускаю Telegram polling..."
            )

            await dp.start_polling(
                bot
            )

            break

        except asyncio.CancelledError:

            raise

        except Exception as e:

            error_text = str(
                e
            )

            logger.error(
                "❌ Telegram polling: %s",
                error_text,
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
                    "⚠️ Telegram Conflict."
                )

                logger.warning(
                    "⚠️ Возможно запущен второй экземпляр."
                )

                await asyncio.sleep(
                    15
                )

            elif (
                "Unauthorized"
                in error_text
                or "401"
                in error_text
            ):

                logger.error(
                    "❌ TELEGRAM UNAUTHORIZED!"
                )

                logger.error(
                    "❌ Проверьте BOT_TOKEN."
                )

                await asyncio.sleep(
                    30
                )

            else:

                logger.warning(
                    "🔄 Повторный запуск Telegram через 10 секунд."
                )

                await asyncio.sleep(
                    10
                )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    scheduler_task.cancel()
    heartbeat_task.cancel()

    for task in (
        scheduler_task,
        heartbeat_task,
    ):

        try:

            await task

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
