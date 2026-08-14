import asyncio
import json
import logging
import os
import re
import time
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
# iHERB DEAL BOT — UPDATED VERSION
# ============================================================
# Исправления:
# - цена распознаётся в USD ($) и KZT (₸);
# - KZT автоматически переводится в USD;
# - старая цена восстанавливается по % скидки, если iHerb
#   показывает только текущую цену;
# - CHAT_ID проверяется при запуске;
# - "chat not found" больше не вызывает повторный спам;
# - сохранены cache, бренды, Render health, /start, /deals,
#   /status и автоматическая проверка каждые 5 минут.
# ============================================================


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = 300

MIN_DISCOUNT_PERCENT = 20
MAX_DISCOUNT_PERCENT = 90

# Пусто = все бренды
TARGET_BRANDS = []

KZT_EXCHANGE_RATE = 540
MARGIN_MARKUP_PERCENT = 35

# Persistence: Postgres survives Render restarts/deploys.
# Local JSON is only a fallback for development or a paid Render disk.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "/var/data").strip() or "/var/data"
CACHE_FILE = os.path.join(DATA_DIR, "sent_deals.json")
MAX_CACHE_ITEMS = 5000
MAX_DEALS_PER_CHECK = int(os.getenv("MAX_DEALS_PER_CHECK", "10"))
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "60"))



if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден в Environment Variables."
    )


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

subscribers = set()
sent_deals_cache = set()
validated_chat_id = None

# Runtime diagnostics
last_check_started = None
last_check_finished = None
last_check_ok = False
last_check_found = 0
last_check_sent = 0
next_check_at = None
check_in_progress = False
monitor_started_at = datetime.now(timezone.utc)

try:
    import asyncpg
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False



# ============================================================
# KEYBOARD
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

    logging.info(
        "✅ curl_cffi доступен"
    )

except ImportError:
    HAS_CURL_CFFI = False

    logging.warning(
        "⚠️ curl_cffi отсутствует — используем httpx."
    )


# ============================================================
# PERSISTENT CACHE
# ============================================================

async def init_storage():
    """Initialize durable storage when DATABASE_URL is configured."""
    if DATABASE_URL and not HAS_POSTGRES:
        raise RuntimeError("DATABASE_URL задан, но пакет asyncpg отсутствует. Добавьте asyncpg в requirements.txt")

    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sent_deals (
                    product_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    discount INTEGER,
                    price_usd DOUBLE PRECISION,
                    link TEXT,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
        finally:
            await conn.close()
        logging.info("💾 PostgreSQL: ПОДКЛЮЧЕН. Память переживёт restart/deploy.")
        return

    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        logging.warning(f"⚠️ Не удалось создать DATA_DIR {DATA_DIR}: {e}")

    if os.path.exists(CACHE_FILE):
        load_cache()
    else:
        logging.warning(
            "⚠️ Постоянное хранилище не подключено. "
            "На Render Free локальный файл теряется после restart/spin-down."
        )


def load_cache():
    global sent_deals_cache
    try:
        if not os.path.exists(CACHE_FILE):
            sent_deals_cache = set()
            return
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        sent_deals_cache = set(str(x) for x in data) if isinstance(data, list) else set()
        logging.info(f"💾 Local cache: {len(sent_deals_cache)} товаров")
    except Exception as e:
        logging.exception(f"❌ Ошибка загрузки cache: {e}")
        sent_deals_cache = set()


def save_cache():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        data = list(sent_deals_cache)[-MAX_CACHE_ITEMS:]
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logging.exception(f"❌ Ошибка сохранения cache: {e}")


async def is_sent(product_id):
    product_id = str(product_id)
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow("SELECT 1 FROM sent_deals WHERE product_id=$1", product_id)
            return row is not None
        finally:
            await conn.close()
    return product_id in sent_deals_cache


async def mark_sent(deal):
    global sent_deals_cache
    product_id = str(deal["id"])
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute(
                """INSERT INTO sent_deals(product_id,title,discount,price_usd,link)
                   VALUES($1,$2,$3,$4,$5) ON CONFLICT(product_id) DO NOTHING""",
                product_id, deal["title"], deal["discount_percent"],
                deal["discount_price_usd"], deal["link"]
            )
        finally:
            await conn.close()
    else:
        sent_deals_cache.add(product_id)
        if len(sent_deals_cache) > MAX_CACHE_ITEMS:
            sent_deals_cache = set(list(sent_deals_cache)[-MAX_CACHE_ITEMS:])
        save_cache()


async def cache_count():
    if DATABASE_URL:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            return int(await conn.fetchval("SELECT COUNT(*) FROM sent_deals"))
        finally:
            await conn.close()
    return len(sent_deals_cache)


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
        text = str(value)

        text = (
            text
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(",", ".")
        )

        text = re.sub(
            r"[^\d.]",
            "",
            text,
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
# PRICE PARSER
# ============================================================

def extract_prices(text):
    """
    Поддерживает:

    $27.89
    US$27.89
    27.89 $
    ₸15 850,48
    15 850,48 ₸
    KZT 15850.48
    """

    if not text:
        return []

    text = str(text).replace(
        "\xa0",
        " ",
    )

    patterns = [

        # USD
        r"US\$\s*([\d\s]+(?:[.,]\d{1,2})?)",

        r"\$\s*([\d\s]+(?:[.,]\d{1,2})?)",

        r"USD\s*([\d\s]+(?:[.,]\d{1,2})?)",

        r"([\d\s]+(?:[.,]\d{1,2})?)"
        r"\s*(?:USD|US\$|\$)",

        # KZT
        r"₸\s*([\d\s]+(?:[.,]\d{1,2})?)",

        r"([\d\s]+(?:[.,]\d{1,2})?)"
        r"\s*₸",

        r"KZT\s*([\d\s]+(?:[.,]\d{1,2})?)",

        r"([\d\s]+(?:[.,]\d{1,2})?)"
        r"\s*KZT",
    ]

    result = []

    for pattern in patterns:

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

            if number >= 1_000_000:
                continue

            result.append(
                number
            )

    return result


def extract_currency_prices(text):
    """
    Возвращает:

    ("USD", 27.89)
    ("KZT", 15850.48)
    """

    if not text:
        return []

    text = str(text).replace(
        "\xa0",
        " ",
    )

    patterns = [

        ("USD",
         r"US\$\s*([\d\s]+(?:[.,]\d{1,2})?)"),

        ("USD",
         r"\$\s*([\d\s]+(?:[.,]\d{1,2})?)"),

        ("USD",
         r"USD\s*([\d\s]+(?:[.,]\d{1,2})?)"),

        ("USD",
         r"([\d\s]+(?:[.,]\d{1,2})?)"
         r"\s*(?:USD|US\$|\$)"),

        ("KZT",
         r"₸\s*([\d\s]+(?:[.,]\d{1,2})?)"),

        ("KZT",
         r"([\d\s]+(?:[.,]\d{1,2})?)"
         r"\s*₸"),

        ("KZT",
         r"KZT\s*([\d\s]+(?:[.,]\d{1,2})?)"),

        ("KZT",
         r"([\d\s]+(?:[.,]\d{1,2})?)"
         r"\s*KZT"),
    ]

    result = []

    for currency, pattern in patterns:

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

            if number >= 1_000_000:
                continue

            result.append(
                (
                    currency,
                    number,
                )
            )

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
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
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
# URL / ID
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

    if url.startswith("http://"):
        return (
            "https://"
            + url[7:]
        )

    if url.startswith("https://"):
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

        match = re.search(
            pattern,
            link,
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
            - current_price / old_price
        )
        * 100
    )

    if (
        MIN_DISCOUNT_PERCENT
        <= percent
        <= MAX_DISCOUNT_PERCENT
    ):
        return percent

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
    """
    Если две цены:

    current = меньшая
    old = большая

    Если одна цена + скидка:

    old = current / (1 - discount/100)

    Для KZT автоматически переводим
    обе цены в USD.
    """

    if not currency_prices:
        return (
            None,
            None,
            None,
        )

    by_currency = {
        "USD": set(),
        "KZT": set(),
    }

    for currency, value in currency_prices:

        by_currency.setdefault(
            currency,
            set(),
        ).add(
            round(
                value,
                2,
            )
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
# PRICE DATA INSIDE CARD
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
# GET IHERB HTML
# ============================================================

async def get_iherb_html():

    # Сначала английская страница с USD.
    # Затем KZ-страницы с ₸.
    urls = [

        "https://www.iherb.com/deals"
        "?lang=en-US&currency=USD",

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
    # CURL_CFFI
    # ========================================================

    if HAS_CURL_CFFI:

        for url in urls:

            for browser in [

                "chrome124",

                "chrome120",

                "chrome116",

                "safari15_5",
            ]:

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

                    logging.info(
                        "iHerb | curl "
                        f"{browser} | "
                        f"{response.status_code} | "
                        f"{len(response.text)} chars"
                    )

                    if (
                        response.status_code
                        == 200
                        and len(
                            response.text
                        ) > 10000
                    ):

                        return response.text

                except Exception as e:

                    logging.debug(
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

                response = (
                    await client.get(
                        url
                    )
                )

                logging.info(
                    "iHerb | httpx | "
                    f"{response.status_code} | "
                    f"{len(response.text)} chars | "
                    f"{url}"
                )

                if (
                    response.status_code
                    == 200
                    and len(
                        response.text
                    ) > 10000
                ):

                    return response.text

        except Exception as e:

            logging.debug(
                f"httpx error: {e}"
            )

    logging.error(
        "❌ iHerb HTML получить не удалось."
    )

    return ""


# ============================================================
# FIND PRODUCT CARDS
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

                logging.info(
                    f"🔍 {selector}: "
                    f"{len(found)} карточек"
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

        link_element = (
            card.select_one(
                "a[href]"
            )
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

    logging.info(
        "📦 Уникальных карточек: "
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

        # Price elements
        for text in get_card_price_texts(
            card
        ):

            currency_prices.extend(
                extract_currency_prices(
                    text
                )
            )

        # data-* / JSON
        (
            json_prices,
            json_discounts,
        ) = extract_json_data(
            card
        )

        currency_prices.extend(
            json_prices
        )

        # script
        (
            script_prices,
            script_discounts,
        ) = extract_script_price_data(
            card
        )

        currency_prices.extend(
            script_prices
        )

        # Весь текст карточки
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
        # CHOOSE PRICES
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
                or calculated_discount
                > discount_percent
            ):

                discount_percent = (
                    calculated_discount
                )

        # ----------------------------------------------------
        # ONLY CURRENT PRICE + %
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

        # ----------------------------------------------------
        # DEBUG
        # ----------------------------------------------------

        logging.info(
            "🔎 CARD #"
            f"{index} | "
            f"{title[:65]} | "
            f"currency={currency} | "
            f"prices={currency_prices[:8]} | "
            f"discount={discount_percent}"
        )

        # ----------------------------------------------------
        # VALIDATION
        # ----------------------------------------------------

        if not current_usd:

            logging.info(
                f"⏭ {title[:65]} | "
                "цена не найдена"
            )

            return None

        if not discount_percent:

            logging.info(
                f"⏭ {title[:65]} | "
                "скидка не определена"
            )

            return None

        if (
            discount_percent
            < MIN_DISCOUNT_PERCENT
        ):

            return None

        if (
            not old_usd
            or old_usd <= current_usd
        ):

            logging.info(
                f"⏭ {title[:65]} | "
                "не удалось получить "
                "корректную старую цену"
            )

            return None

        # ----------------------------------------------------
        # ID
        # ----------------------------------------------------

        product_id = (
            extract_product_id(
                link
            )
            or link
        )

        # ----------------------------------------------------
        # DEAL
        # ----------------------------------------------------

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

        logging.info(
            "🔥 ПРОШЁЛ ФИЛЬТР | "
            f"-{deal['discount_percent']}% | "
            f"${deal['discount_price_usd']:.2f} | "
            f"{deal['title'][:70]}"
        )

        return deal

    except Exception as e:

        logging.exception(
            f"❌ Ошибка карточки "
            f"#{index}: {e}"
        )

        return None


# ============================================================
# FETCH
# ============================================================

async def fetch_iherb_specials():

    logging.info(
        "🔎 Начинаем проверку iHerb..."
    )

    html = (
        await get_iherb_html()
    )

    if not html:
        return []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        cards = (
            find_product_cards(
                soup
            )
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

        logging.info(
            "=" * 60
        )

        logging.info(
            "🔥 Найдено скидок "
            f"{MIN_DISCOUNT_PERCENT}%+: "
            f"{len(deals)}"
        )

        for deal in deals[:20]:

            logging.info(
                f"💊 "
                f"-{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:80]}"
            )

        logging.info(
            "=" * 60
        )

        return deals

    except Exception as e:

        logging.exception(
            f"❌ Ошибка парсинга iHerb: {e}"
        )

        return []


# ============================================================
# TELEGRAM CHAT VALIDATION
# ============================================================

async def validate_configured_chat():

    global validated_chat_id

    if not CHAT_ID:

        logging.warning(
            "⚠️ CHAT_ID не задан. "
            "Бот будет отправлять сообщения "
            "пользователям, которые нажали /start."
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

        logging.info(
            "✅ CHAT_ID подтверждён: "
            f"{validated_chat_id} | "
            f"{chat_name}"
        )

    except Exception as e:

        validated_chat_id = None


        logging.error(
            "❌ CHAT_ID НЕВЕРНЫЙ "
            "ИЛИ БОТ НЕ ИМЕЕТ ДОСТУПА:\n"
            f"   {CHAT_ID}\n"
            f"   {e}\n"
            "⚠️ Этот CHAT_ID отключён. "
            "Используйте /start в нужном чате "
            "или исправьте CHAT_ID в Render."
        )


# ============================================================
# TELEGRAM MESSAGE
# ============================================================

def format_deal_message(
    deal
):

    title = escape(
        deal["title"]
    )

    brand = escape(
        deal["brand"]
    )

    old_usd = (
        deal["orig_price_usd"]
    )

    current_usd = (
        deal["discount_price_usd"]
    )

    percent = (
        deal["discount_percent"]
    )

    link = deal["link"]

    # --------------------------------------------------------
    # COST
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

            logging.info(
                f"✅ Отправлено "
                f"{target_id}: "
                f"{deal['title'][:70]}"
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

            logging.warning(
                "⏳ Flood Control. "
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
                    "❌ Повторная отправка "
                    f"{target_id}: "
                    f"{retry_error}"
                )

        except Exception as e:

            error_text = str(
                e
            )

            # ВАЖНО:
            # Не спамим одним и тем же
            # "chat not found".
            if (
                "chat not found"
                in error_text.lower()
                or "bot was kicked"
                in error_text.lower()
                or "user is deactivated"
                in error_text.lower()
            ):

                logging.error(
                    "🚫 Недоступный "
                    f"получатель {target_id}: "
                    f"{error_text}"
                )

                subscribers.discard(
                    str(target_id)
                )

                continue

            logging.error(
                f"❌ Telegram "
                f"{target_id}: "
                f"{error_text}"
            )

    return success


# ============================================================
# CHECK + NOTIFY
# ============================================================

async def check_and_notify(force_send=False):
    global last_check_started, last_check_finished, last_check_ok
    global last_check_found, last_check_sent, check_in_progress

    if check_in_progress:
        logging.warning("⏭ Проверка уже идёт — пропускаем параллельный запуск.")
        return

    check_in_progress = True
    last_check_started = datetime.now(timezone.utc)
    last_check_ok = False
    last_check_sent = 0

    logging.info("=" * 70)
    logging.info("🔎 ПРОВЕРКА iHERB")
    logging.info(f"🎯 Скидка: {MIN_DISCOUNT_PERCENT}%–{MAX_DISCOUNT_PERCENT}%")
    logging.info(f"📦 Лимит отправки: {MAX_DEALS_PER_CHECK}")
    logging.info("=" * 70)

    try:
        deals = await fetch_iherb_specials()
        last_check_found = len(deals)

        if not deals:
            last_check_ok = True
            logging.info("ℹ️ Подходящих скидок не найдено.")
            return

        targets = get_targets()
        if not targets:
            last_check_ok = True
            logging.warning("⚠️ Нет получателей Telegram. Откройте бота и нажмите /start.")
            return

        sent_count = 0
        skipped_count = 0

        for deal in deals:
            deal_id = str(deal["id"])

            # force_send используется только для ручной проверки.
            # В автоматическом режиме повторов нет.
            if await is_sent(deal_id):
                skipped_count += 1
                continue

            success = await send_deal(deal, targets)
            if success:
                await mark_sent(deal)
                sent_count += 1

            if sent_count >= MAX_DEALS_PER_CHECK:
                logging.info(f"🛑 Достигнут лимит {MAX_DEALS_PER_CHECK} новых скидок.")
                break

        last_check_sent = sent_count
        last_check_ok = True
        logging.info(
            f"📊 Результат: найдено={len(deals)} | отправлено={sent_count} | "
            f"пропущено={skipped_count} | память={await cache_count()}"
        )

    except Exception as e:
        logging.exception(f"❌ Ошибка проверки iHerb: {e}")
    finally:
        last_check_finished = datetime.now(timezone.utc)
        check_in_progress = False




# ============================================================
# START
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

        "🔥 Автоматически ищу "
        "товары со скидкой.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        "⏱ Проверка: "
        "<b>каждые 5 минут</b>\n\n"

        "💰 Теперь бот понимает цены "
        "<b>$</b> и <b>₸</b>.\n\n"

        "Новые подходящие товары "
        "будут отправляться автоматически.",

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
        "🔎 Проверяю iHerb прямо сейчас...\n"
        "⏳ Подождите несколько секунд."
    )

    await check_and_notify(
        force_send=True
    )


def format_dt(value):
    if not value:
        return "—"
    try:
        return value.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(value)


# ============================================================
# STATUS
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

    await message.answer(

        "📊 <b>СТАТУС БОТА</b>\n\n"

        "🟢 Telegram: ONLINE\n"

        "🟢 Автомониторинг: ВКЛЮЧЁН\n"

        "🔄 Проверка: каждые 5 минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Бренды: {brands_text}\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n\n"

        f"📨 Основной чат: "
        f"{chat_text}\n"

        f"👥 Получателей: "
        f"{len(get_targets())}\n"

        f"💾 В памяти: "
        f"{await cache_count()} товаров\n\n"
        f"🕒 Последняя проверка: {format_dt(last_check_finished)}\n"
        f"📦 Найдено: {last_check_found}\n"
        f"📤 Отправлено: {last_check_sent}\n"
        f"➡️ Следующая: {format_dt(next_check_at)}",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# OTHER MESSAGE
# ============================================================

@dp.message()
async def any_message_handler(
    message
):

    subscribers.add(
        str(message.chat.id)
    )

    await message.answer(

        "👋 <b>iHerb Deal Bot</b>\n\n"

        f"🔥 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте:\n"

        "🔥 <b>Получить скидки</b> — "
        "проверить сейчас\n\n"

        "ℹ️ <b>Статус</b> — "
        "настройки.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():
    global next_check_at

    logging.info("🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН")
    logging.info("⚡ Первая проверка выполняется СРАЗУ")

    while True:
        try:
            await check_and_notify(force_send=False)
            next_check_at = datetime.now(timezone.utc) + timedelta(seconds=CHECK_INTERVAL_SECONDS)
            logging.info(f"💤 Следующая проверка: {format_dt(next_check_at)}")

            # Sleep in small chunks so cancellation and heartbeat stay responsive.
            remaining = CHECK_INTERVAL_SECONDS
            while remaining > 0:
                await asyncio.sleep(min(30, remaining))
                remaining -= 30

        except asyncio.CancelledError:
            logging.info("🛑 Мониторинг остановлен.")
            raise
        except Exception as e:
            logging.exception(f"❌ Ошибка scheduler: {e}")
            next_check_at = datetime.now(timezone.utc) + timedelta(seconds=30)
            await asyncio.sleep(30)


async def heartbeat():
    while True:
        try:
            logging.info(
                "❤️ HEARTBEAT | alive | last_check=%s | next=%s | in_progress=%s",
                format_dt(last_check_finished),
                format_dt(next_check_at),
                check_in_progress,
            )
            await asyncio.sleep(HEARTBEAT_SECONDS)
        except asyncio.CancelledError:
            return


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

async def start_dummy_server():

    try:

        from aiohttp import web

        port = int(
            os.environ.get(
                "PORT",
                "10000",
            )
        )

        app = web.Application()

        async def home(
            request
        ):

            return web.Response(
                text=(
                    "iHerb Telegram Bot "
                    "is running!"
                )
            )

        async def health(
            request
        ):

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

        logging.info(
            "🌐 Render Health Server "
            f"запущен: {port}"
        )

    except Exception as e:

        logging.exception(
            f"❌ Health Server: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.info(
        "=" * 60
    )

    logging.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT "
        "— UPDATED"
    )

    logging.info(
        "=" * 60
    )

    logging.info(
        "🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        "💱 USD/KZT: "
        f"{KZT_EXCHANGE_RATE}"
    )

    logging.info(
        "📈 Наценка: "
        f"{MARGIN_MARKUP_PERCENT}%"
    )

    if TARGET_BRANDS:

        logging.info(
            "🏷 Фильтр брендов: "
            + ", ".join(
                TARGET_BRANDS
            )
        )

    else:

        logging.info(
            "🏷 Фильтр брендов: ВСЕ"
        )

    # --------------------------------------------------------
    # PERSISTENT STORAGE
    # --------------------------------------------------------

    await init_storage()
    logging.info("🧠 Память: PostgreSQL" if DATABASE_URL else f"🧠 Память: локальный файл {CACHE_FILE}")
    if not DATABASE_URL:
        logging.warning("⚠️ DATABASE_URL не задан: память НЕ переживёт Render Free restart/spin-down.")
    logging.info("⏱ Интервал мониторинга: 5 минут")

    # --------------------------------------------------------
    # HEALTH
    # --------------------------------------------------------

    await start_dummy_server()

    # --------------------------------------------------------
    # CHECK CHAT_ID
    # --------------------------------------------------------

    await validate_configured_chat()

    # --------------------------------------------------------
    # SCHEDULER
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(scheduler())
    heartbeat_task = asyncio.create_task(heartbeat())

    # --------------------------------------------------------
    # TELEGRAM
    # --------------------------------------------------------

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

            error_text = str(
                e
            )

            logging.error(
                "❌ Telegram polling: "
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
                    "❌ TELEGRAM UNAUTHORIZED!"
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

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    scheduler_task.cancel()
    heartbeat_task.cancel()

    for task in (scheduler_task, heartbeat_task):
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

        logging.info(
            "🛑 Бот остановлен."
        )
