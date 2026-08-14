import asyncio
import json
import logging
import os
import re
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
    Update,
)
from aiogram.exceptions import TelegramRetryAfter

from aiohttp import web


# ============================================================
# iHERB SALE BOT
# STABLE RENDER WEBHOOK VERSION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# ENVIRONMENT
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).strip()

PORT = int(
    os.getenv(
        "PORT",
        "10000"
    )
)

CHECK_INTERVAL_SECONDS = int(
    os.getenv(
        "CHECK_INTERVAL_SECONDS",
        "300"
    )
)

MIN_DISCOUNT_PERCENT = int(
    os.getenv(
        "MIN_DISCOUNT_PERCENT",
        "20"
    )
)

MAX_DISCOUNT_PERCENT = int(
    os.getenv(
        "MAX_DISCOUNT_PERCENT",
        "90"
    )
)

MAX_DEALS_PER_CHECK = int(
    os.getenv(
        "MAX_DEALS_PER_CHECK",
        "10"
    )
)

KZT_EXCHANGE_RATE = float(
    os.getenv(
        "KZT_EXCHANGE_RATE",
        "540"
    )
)

MARGIN_MARKUP_PERCENT = float(
    os.getenv(
        "MARGIN_MARKUP_PERCENT",
        "35"
    )
)

HEARTBEAT_SECONDS = int(
    os.getenv(
        "HEARTBEAT_SECONDS",
        "60"
    )
)

DATA_DIR = os.getenv(
    "DATA_DIR",
    "/var/data"
)

CACHE_FILE = os.path.join(
    DATA_DIR,
    "sent_deals.json"
)

MAX_CACHE_ITEMS = 5000


if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден в Environment Variables."
    )


# ============================================================
# OPTIONAL POSTGRES
# ============================================================

try:
    import asyncpg

    HAS_POSTGRES = True

except ImportError:
    asyncpg = None
    HAS_POSTGRES = False


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

    curl_requests = None
    HAS_CURL_CFFI = False

    logger.warning(
        "⚠️ curl_cffi отсутствует. Используем httpx."
    )


# ============================================================
# TELEGRAM
# ============================================================

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()


# ============================================================
# RUNTIME STATE
# ============================================================

subscribers = set()

sent_deals_cache = set()

validated_chat_id = None

last_check_started = None
last_check_finished = None
last_check_ok = False
last_check_found = 0
last_check_sent = 0
next_check_at = None

check_in_progress = False

monitor_started_at = datetime.now(
    timezone.utc
)

scheduler_task = None
heartbeat_task = None


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

    if DATABASE_URL:

        if not HAS_POSTGRES:

            raise RuntimeError(
                "DATABASE_URL задан, но asyncpg отсутствует. "
                "Добавьте asyncpg в requirements.txt."
            )

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

    try:

        os.makedirs(
            DATA_DIR,
            exist_ok=True
        )

    except Exception as e:

        logger.warning(
            f"⚠️ DATA_DIR: {e}"
        )

    if os.path.exists(
        CACHE_FILE
    ):

        load_cache()

    else:

        logger.warning(
            "⚠️ DATABASE_URL не задан. "
            "Локальная память может потеряться "
            "после restart Render Free."
        )


def load_cache():

    global sent_deals_cache

    try:

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            list
        ):

            sent_deals_cache = set(
                str(x)
                for x in data
            )

        else:

            sent_deals_cache = set()

        logger.info(
            f"💾 Загружено из памяти: "
            f"{len(sent_deals_cache)}"
        )

    except Exception as e:

        logger.error(
            f"❌ Ошибка cache: {e}"
        )

        sent_deals_cache = set()


def save_cache():

    try:

        os.makedirs(
            DATA_DIR,
            exist_ok=True
        )

        data = list(
            sent_deals_cache
        )[-MAX_CACHE_ITEMS:]

        tmp_file = (
            CACHE_FILE
            + ".tmp"
        )

        with open(
            tmp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            tmp_file,
            CACHE_FILE
        )

    except Exception as e:

        logger.error(
            f"❌ Ошибка сохранения cache: {e}"
        )


async def is_sent(
    product_id
):

    product_id = str(
        product_id
    )

    if DATABASE_URL:

        conn = await asyncpg.connect(
            DATABASE_URL
        )

        try:

            row = await conn.fetchrow(
                """
                SELECT 1
                FROM sent_deals
                WHERE product_id=$1
                """,
                product_id
            )

            return row is not None

        finally:

            await conn.close()

    return (
        product_id
        in sent_deals_cache
    )


async def mark_sent(
    deal
):

    global sent_deals_cache

    product_id = str(
        deal["id"]
    )

    if DATABASE_URL:

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
                deal["link"]
            )

        finally:

            await conn.close()

    else:

        sent_deals_cache.add(
            product_id
        )

        if (
            len(sent_deals_cache)
            > MAX_CACHE_ITEMS
        ):

            sent_deals_cache = set(
                list(
                    sent_deals_cache
                )[-MAX_CACHE_ITEMS:]
            )

        save_cache()


async def cache_count():

    if DATABASE_URL:

        conn = await asyncpg.connect(
            DATABASE_URL
        )

        try:

            value = await conn.fetchval(
                "SELECT COUNT(*) FROM sent_deals"
            )

            return int(
                value or 0
            )

        finally:

            await conn.close()

    return len(
        sent_deals_cache
    )


# ============================================================
# HELPERS
# ============================================================

def clean_text(
    text
):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text)
    ).strip()


def safe_float(
    value
):

    if value is None:
        return None

    try:

        text = str(
            value
        )

        text = (
            text
            .replace(
                "\xa0",
                " "
            )
            .replace(
                " ",
                ""
            )
            .replace(
                ",",
                "."
            )
        )

        text = re.sub(
            r"[^\d.]",
            "",
            text
        )

        if not text:
            return None

        number = float(
            text
        )

        if number <= 0:
            return None

        return number

    except Exception:

        return None


# ============================================================
# PRICE PARSER
# ============================================================

def extract_currency_prices(
    text
):

    if not text:
        return []

    text = str(
        text
    ).replace(
        "\xa0",
        " "
    )

    patterns = [

        (
            "USD",
            r"US\$\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)"
        ),

        (
            "USD",
            r"\$\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)"
        ),

        (
            "USD",
            r"USD\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)"
        ),

        (
            "USD",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*(?:USD|US\$|\$)"
        ),

        (
            "KZT",
            r"₸\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)"
        ),

        (
            "KZT",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*₸"
        ),

        (
            "KZT",
            r"KZT\s*"
            r"([\d\s]+(?:[.,]\d{1,2})?)"
        ),

        (
            "KZT",
            r"([\d\s]+(?:[.,]\d{1,2})?)"
            r"\s*KZT"
        ),
    ]

    result = []

    for currency, pattern in patterns:

        try:

            matches = re.findall(
                pattern,
                text,
                re.IGNORECASE
            )

        except Exception:

            continue

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
                    number
                )
            )

    return result


# ============================================================
# DISCOUNT
# ============================================================

def extract_discount_percent(
    text
):

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

        r"скидк[аи]?"
        r"\s*(?:до\s*)?"
        r"(\d{1,2})\s*%",

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
# URL / PRODUCT ID
# ============================================================

def normalize_url(
    url
):

    if not url:
        return ""

    url = str(
        url
    ).strip()

    if url.startswith(
        "//"
    ):

        return (
            "https:"
            + url
        )

    if url.startswith(
        "/"
    ):

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
        url
    )


def extract_product_id(
    link
):

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

            return match.group(
                1
            )

    return link


# ============================================================
# PRICE CALCULATIONS
# ============================================================

def infer_old_price(
    current_price,
    discount_percent
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
        2
    )


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

    return round(
        (
            1
            - current_price / old_price
        )
        * 100
    )


def choose_prices(
    currency_prices,
    discount_percent
):

    if not currency_prices:

        return (
            None,
            None,
            None
        )

    usd = sorted(
        set(
            round(
                value,
                2
            )
            for currency, value
            in currency_prices
            if currency == "USD"
        )
    )

    kzt = sorted(
        set(
            round(
                value,
                2
            )
            for currency, value
            in currency_prices
            if currency == "KZT"
        )
    )

    # --------------------------------------------------------
    # USD
    # --------------------------------------------------------

    if usd:

        current = usd[0]

        old = (
            usd[-1]
            if len(usd) >= 2
            else None
        )

        if (
            old is None
            and discount_percent
        ):

            old = infer_old_price(
                current,
                discount_percent
            )

        return (
            current,
            old,
            "USD"
        )

    # --------------------------------------------------------
    # KZT
    # --------------------------------------------------------

    if kzt:

        current_kzt = kzt[0]

        old_kzt = (
            kzt[-1]
            if len(kzt) >= 2
            else None
        )

        if (
            old_kzt is None
            and discount_percent
        ):

            old_kzt = infer_old_price(
                current_kzt,
                discount_percent
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
            "KZT"
        )

    return (
        None,
        None,
        None
    )


# ============================================================
# CARD PARSING
# ============================================================

def get_card_price_texts(
    card
):

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
                        strip=True
                    )
                )

                if text:
                    texts.append(
                        text
                    )

        except Exception:
            pass

    return texts


def extract_json_data(
    card
):

    prices = []

    discounts = []

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                if not isinstance(
                    value,
                    str
                ):
                    continue

                key_lower = key.lower()

                if any(
                    word in key_lower
                    for word in (
                        "price",
                        "amount",
                        "cost"
                    )
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
        discounts
    )


def extract_script_data(
    card
):

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
        discounts
    )


def extract_title(
    card
):

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

        for link in card.select(
            "a[href]"
        ):

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


def extract_link(
    card
):

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


def find_product_cards(
    soup
):

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

    seen = set()

    for selector in selectors:

        try:

            found = soup.select(
                selector
            )

            if not found:
                continue

            logger.info(
                f"🔍 {selector} -> "
                f"{len(found)} карточек"
            )

            for card in found:

                text = clean_text(
                    card.get_text(
                        " ",
                        strip=True
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

                cards.append(
                    card
                )

            if len(cards) >= 48:
                break

        except Exception:
            pass

    logger.info(
        f"📦 Уникальных карточек: "
        f"{len(cards)}"
    )

    return cards


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
            return None

        link = extract_link(
            card
        )

        if not link:
            return None

        # ----------------------------------------------------
        # DISCOUNT
        # ----------------------------------------------------

        discount_candidates = []

        text_discount = (
            extract_discount_percent(
                card_text
            )
        )

        if text_discount:

            discount_candidates.append(
                text_discount
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

        discount_candidates.extend(
            json_discounts
        )

        script_prices, script_discounts = (
            extract_script_data(
                card
            )
        )

        currency_prices.extend(
            script_prices
        )

        discount_candidates.extend(
            script_discounts
        )

        currency_prices.extend(
            extract_currency_prices(
                card_text
            )
        )

        discount_percent = (
            max(
                discount_candidates
            )
            if discount_candidates
            else None
        )

        # ----------------------------------------------------
        # PRICES
        # ----------------------------------------------------

        (
            current_usd,
            old_usd,
            currency
        ) = choose_prices(
            currency_prices,
            discount_percent
        )

        # ----------------------------------------------------
        # CALCULATE
        # ----------------------------------------------------

        calculated_discount = (
            calculate_discount(
                old_usd,
                current_usd
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
        # ONLY CURRENT + DISCOUNT
        # ----------------------------------------------------

        if (
            current_usd
            and discount_percent
            and not old_usd
        ):

            old_usd = infer_old_price(
                current_usd,
                discount_percent
            )

        logger.info(
            f"🔎 CARD #{index} | "
            f"{title[:65]} | "
            f"current={current_usd} | "
            f"old={old_usd} | "
            f"text_discount={text_discount}"
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
            not old_usd
            or old_usd <= current_usd
        ):
            return None

        product_id = (
            extract_product_id(
                link
            )
            or link
        )

        return {

            "id": product_id,

            "title": title,

            "brand": "iHerb",

            "orig_price_usd": round(
                old_usd,
                2
            ),

            "discount_price_usd": round(
                current_usd,
                2
            ),

            "discount_percent": int(
                discount_percent
            ),

            "link": link,
        }

    except Exception as e:

        logger.exception(
            f"❌ Ошибка карточки "
            f"#{index}: {e}"
        )

        return None


# ============================================================
# IHERB REQUEST
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

    # --------------------------------------------------------
    # CURL CFFI
    # --------------------------------------------------------

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

                    response = await asyncio.to_thread(
                        curl_requests.get,
                        url,
                        headers=HEADERS,
                        cookies=cookies,
                        impersonate=browser,
                        timeout=30
                    )

                    logger.info(
                        "iHerb | "
                        f"{browser} | "
                        f"HTTP "
                        f"{response.status_code} | "
                        f"{url}"
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        logger.info(
                            "✅ HTML iHerb получен: "
                            f"{len(response.text)} символов"
                        )

                        return response.text

                except Exception as e:

                    logger.debug(
                        f"curl error: {e}"
                    )

    # --------------------------------------------------------
    # HTTPX FALLBACK
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
                    "iHerb | httpx | "
                    f"{response.status_code} | "
                    f"{len(response.text)} | "
                    f"{url}"
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    return response.text

        except Exception as e:

            logger.debug(
                f"httpx error: {e}"
            )

    logger.error(
        "❌ Не удалось получить iHerb HTML."
    )

    return ""


# ============================================================
# FETCH DEALS
# ============================================================

async def fetch_iherb_specials():

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

        unique = {}

        for deal in deals:

            unique[
                deal["id"]
            ] = deal

        deals = list(
            unique.values()
        )

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"]
            ),
            reverse=True
        )

        logger.info(
            f"🔥 Найдено подходящих товаров: "
            f"{len(deals)}"
        )

        return deals

    except Exception as e:

        logger.exception(
            f"❌ Ошибка парсинга: {e}"
        )

        return []


# ============================================================
# TELEGRAM
# ============================================================

async def validate_chat():

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

        logger.info(
            "✅ CHAT_ID подтверждён: "
            f"{validated_chat_id}"
        )

    except Exception as e:

        validated_chat_id = None

        logger.error(
            "❌ CHAT_ID ошибка: "
            f"{e}"
        )


def get_targets():

    targets = set()

    if validated_chat_id:

        targets.add(
            str(
                validated_chat_id
            )
        )

    targets.update(
        subscribers
    )

    return targets


def format_deal_message(
    deal
):

    title = escape(
        deal["title"]
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

    cost_kzt = round(
        current_usd
        * KZT_EXCHANGE_RATE
    )

    sale_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT
            / 100
        )
    )

    profit_kzt = (
        sale_kzt
        - cost_kzt
    )

    cost_str = (
        f"{cost_kzt:,}"
        .replace(
            ",",
            " "
        )
    )

    sale_str = (
        f"{sale_kzt:,}"
        .replace(
            ",",
            " "
        )
    )

    profit_str = (
        f"{profit_kzt:,}"
        .replace(
            ",",
            " "
        )
    )

    message = (

        "🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n\n"

        "💊 <b>Товар:</b>\n"
        f"{title}\n\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n\n"

        "💰 <b>Цена iHerb:</b>\n"
        f"<s>${old_usd:.2f}</s> "
        f"➡️ <b>${current_usd:.2f}</b>\n\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{sale_str} ₸\n\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n\n"

        f"💱 <b>Курс:</b> "
        f"1 USD = {KZT_EXCHANGE_RATE:g} ₸\n\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
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

    return (
        message,
        keyboard
    )


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

    for target_id in list(
        targets
    ):

        try:

            await bot.send_message(
                chat_id=target_id,
                text=message,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True
            )

            logger.info(
                "📤 НОВАЯ СКИДКА ОТПРАВЛЕНА: "
                f"{deal['title'][:100]}"
            )

            success = True

            await asyncio.sleep(
                2
            )

        except TelegramRetryAfter as e:

            retry_after = int(
                getattr(
                    e,
                    "retry_after",
                    30
                )
            )

            logger.warning(
                f"⏳ Telegram flood control. "
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
                    disable_web_page_preview=True
                )

                success = True

            except Exception as retry_error:

                logger.error(
                    f"❌ Повторная отправка: "
                    f"{retry_error}"
                )

        except Exception as e:

            error_text = str(
                e
            )

            logger.error(
                f"❌ Telegram {target_id}: "
                f"{error_text}"
            )

            if any(
                x in error_text.lower()
                for x in (
                    "chat not found",
                    "bot was kicked",
                    "user is deactivated"
                )
            ):

                subscribers.discard(
                    str(target_id)
                )

    return success


# ============================================================
# CHECK
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
            "⏭ Проверка уже выполняется."
        )

        return

    check_in_progress = True

    last_check_started = (
        datetime.now(
            timezone.utc
        )
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
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logger.info(
        f"📦 Лимит: "
        f"{MAX_DEALS_PER_CHECK}"
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
                "ℹ️ Новых подходящих скидок нет."
            )

            return

        targets = get_targets()

        if not targets:

            last_check_ok = True

            logger.warning(
                "⚠️ Нет получателей."
            )

            return

        sent_count = 0

        skipped_count = 0

        for deal in deals:

            if await is_sent(
                deal["id"]
            ):

                skipped_count += 1

                logger.info(
                    "⏭ Уже отправлялся: "
                    f"{deal['title'][:90]}"
                )

                continue

            success = await send_deal(
                deal,
                targets
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
                    f"🛑 Достигнут лимит "
                    f"{MAX_DEALS_PER_CHECK}."
                )

                break

        last_check_sent = (
            sent_count
        )

        last_check_ok = True

        logger.info(
            "📊 Результат проверки:"
        )

        logger.info(
            f"🔥 Найдено: {len(deals)}"
        )

        logger.info(
            f"📤 Отправлено: {sent_count}"
        )

        logger.info(
            f"⏭ Уже отправлялось: "
            f"{skipped_count}"
        )

        logger.info(
            f"💾 Всего в памяти: "
            f"{await cache_count()}"
        )

    except Exception as e:

        logger.exception(
            f"❌ Ошибка проверки: {e}"
        )

    finally:

        last_check_finished = (
            datetime.now(
                timezone.utc
            )
        )

        check_in_progress = False


# ============================================================
# COMMAND /start
# ============================================================

@dp.message(
    Command("start")
)
async def start_handler(
    message
):

    subscribers.add(
        str(
            message.chat.id
        )
    )

    await message.answer(
        "👋 <b>iHerb Deal Bot работает!</b>\n\n"

        "🔥 Автоматически ищу "
        "товары со скидкой.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        "⏱ Проверка: "
        "<b>каждые 5 минут</b>\n\n"

        "Новые подходящие товары "
        "будут отправляться автоматически.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML
    )


# ============================================================
# /deals
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

    subscribers.add(
        str(
            message.chat.id
        )
    )

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас...\n"
        "⏳ Подождите..."
    )

    await check_and_notify()


# ============================================================
# STATUS
# ============================================================

def format_dt(
    value
):

    if not value:
        return "—"

    try:

        return value.astimezone().strftime(
            "%d.%m.%Y %H:%M:%S"
        )

    except Exception:

        return str(
            value
        )


@dp.message(
    Command("status")
)
@dp.message(
    F.text == "ℹ️ Статус"
)
async def status_handler(
    message
):

    subscribers.add(
        str(
            message.chat.id
        )
    )

    if validated_chat_id:

        chat_text = (
            f"🟢 {validated_chat_id}"
        )

    else:

        chat_text = (
            "🔴 CHAT_ID не подключён"
        )

    await message.answer(

        "📊 <b>СТАТУС БОТА</b>\n\n"

        "🟢 Telegram: ONLINE\n"

        "🟢 Автомониторинг: ВКЛЮЧЁН\n"

        "🔄 Проверка: каждые 5 минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE:g} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT:g}%\n\n"

        f"📨 Основной чат: "
        f"{chat_text}\n"

        f"👥 Получателей: "
        f"{len(get_targets())}\n"

        f"💾 В памяти: "
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

        parse_mode=ParseMode.HTML
    )


# ============================================================
# ANY MESSAGE
# ============================================================

@dp.message()
async def any_message_handler(
    message
):

    subscribers.add(
        str(
            message.chat.id
        )
    )

    await message.answer(

        "👋 <b>iHerb Deal Bot</b>\n\n"

        f"🔥 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте:\n\n"

        "🔥 <b>Получить скидки</b> — "
        "проверить сейчас\n\n"

        "ℹ️ <b>Статус</b> — "
        "состояние бота.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():

    global next_check_at

    logger.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН."
    )

    logger.info(
        "⚡ Первая проверка выполняется СРАЗУ."
    )

    while True:

        try:

            await check_and_notify()

            next_check_at = (
                datetime.now(
                    timezone.utc
                )
                + timedelta(
                    seconds=CHECK_INTERVAL_SECONDS
                )
            )

            logger.info(
                "💤 Следующая проверка через "
                f"{CHECK_INTERVAL_SECONDS} секунд."
            )

            remaining = (
                CHECK_INTERVAL_SECONDS
            )

            while remaining > 0:

                await asyncio.sleep(
                    min(
                        30,
                        remaining
                    )
                )

                remaining -= 30

        except asyncio.CancelledError:

            logger.info(
                "🛑 Scheduler остановлен."
            )

            raise

        except Exception as e:

            logger.exception(
                f"❌ Scheduler error: {e}"
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
                "❤️ HEARTBEAT | "
                "alive | "
                f"last_check={format_dt(last_check_finished)} | "
                f"next={format_dt(next_check_at)} | "
                f"in_progress={check_in_progress}"
            )

            await asyncio.sleep(
                HEARTBEAT_SECONDS
            )

        except asyncio.CancelledError:

            return


# ============================================================
# WEBHOOK
# ============================================================

WEBHOOK_PATH = (
    "/telegram/webhook"
)


def get_webhook_url():

    base = (
        RENDER_EXTERNAL_URL
        or os.getenv(
            "RENDER_EXTERNAL_HOSTNAME",
            ""
        )
    )

    if not base:
        return None

    if not base.startswith(
        "http"
    ):

        base = (
            "https://"
            + base
        )

    base = base.rstrip(
        "/"
    )

    return (
        base
        + WEBHOOK_PATH
    )


async def webhook_handler(
    request
):

    try:

        data = await request.json()

        update = Update.model_validate(
            data
        )

        await dp.feed_update(
            bot,
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as e:

        logger.exception(
            f"❌ Webhook error: {e}"
        )

        return web.Response(
            status=500,
            text="ERROR"
        )


async def health_handler(
    request
):

    return web.json_response(
        {
            "status": "ok",
            "bot": "iHerb Deal Bot",
            "telegram": "webhook",
            "monitoring": True,
            "last_check": format_dt(
                last_check_finished
            ),
            "next_check": format_dt(
                next_check_at
            ),
            "check_in_progress":
                check_in_progress,
            "deals_found":
                last_check_found,
            "deals_sent":
                last_check_sent,
        }
    )


async def home_handler(
    request
):

    return web.Response(
        text=(
            "iHerb Telegram Bot is running!\n"
            "Telegram mode: WEBHOOK\n"
            "Monitoring: ON"
        )
    )


async def start_web_server():

    app = web.Application()

    app.router.add_get(
        "/",
        home_handler
    )

    app.router.add_get(
        "/health",
        health_handler
    )

    app.router.add_post(
        WEBHOOK_PATH,
        webhook_handler
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    logger.info(
        f"🌐 Render Web Server запущен "
        f"на порту {PORT}"
    )

    return runner


# ============================================================
# SET TELEGRAM WEBHOOK
# ============================================================

async def setup_webhook():

    webhook_url = get_webhook_url()

    if not webhook_url:

        logger.error(
            "❌ RENDER_EXTERNAL_URL "
            "не найден."
        )

        logger.error(
            "Добавьте в Render Environment:"
        )

        logger.error(
            "RENDER_EXTERNAL_URL="
            "https://my-iherb-bot-fresh.onrender.com"
        )

        return False

    try:

        # Удаляем старый webhook
        # только перед установкой нового.
        await bot.delete_webhook(
            drop_pending_updates=True
        )

        await asyncio.sleep(
            1
        )

        await bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )

        info = await bot.get_webhook_info()

        logger.info(
            "✅ Telegram WEBHOOK установлен:"
        )

        logger.info(
            f"🔗 {webhook_url}"
        )

        logger.info(
            f"📡 Telegram pending updates: "
            f"{info.pending_update_count}"
        )

        if info.last_error_message:

            logger.warning(
                "⚠️ Telegram webhook last error: "
                f"{info.last_error_message}"
            )

        return True

    except Exception as e:

        logger.exception(
            f"❌ Не удалось установить webhook: {e}"
        )

        return False


# ============================================================
# MAIN
# ============================================================

async def main():

    global scheduler_task
    global heartbeat_task

    logger.info(
        "=" * 70
    )

    logger.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logger.info(
        "🛡 STABLE WEBHOOK VERSION"
    )

    logger.info(
        "=" * 70
    )

    logger.info(
        f"🎯 CHAT_ID: {CHAT_ID or 'не задан'}"
    )

    logger.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logger.info(
        f"💱 USD/KZT: "
        f"{KZT_EXCHANGE_RATE:g}"
    )

    logger.info(
        f"📈 Наценка: "
        f"{MARGIN_MARKUP_PERCENT:g}%"
    )

    logger.info(
        f"⏱ Интервал: "
        f"{CHECK_INTERVAL_SECONDS} сек."
    )

    logger.info(
        f"📦 Максимум за проверку: "
        f"{MAX_DEALS_PER_CHECK}"
    )

    # --------------------------------------------------------
    # STORAGE
    # --------------------------------------------------------

    await init_storage()

    # --------------------------------------------------------
    # BOT INFO
    # --------------------------------------------------------

    try:

        me = await bot.get_me()

        logger.info(
            f"🤖 Telegram подключён: "
            f"@{me.username} | "
            f"ID={me.id}"
        )

    except Exception as e:

        logger.exception(
            f"❌ Telegram getMe error: {e}"
        )

        raise

    # --------------------------------------------------------
    # CHAT
    # --------------------------------------------------------

    await validate_chat()

    # --------------------------------------------------------
    # WEB SERVER
    # --------------------------------------------------------

    runner = await start_web_server()

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    webhook_ok = await setup_webhook()

    if not webhook_ok:

        logger.warning(
            "⚠️ Webhook не установлен."
        )

    else:

        logger.info(
            "🟢 TELEGRAM WEBHOOK РАБОТАЕТ."
        )

    # --------------------------------------------------------
    # MONITOR
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(
        scheduler()
    )

    heartbeat_task = asyncio.create_task(
        heartbeat()
    )

    logger.info(
        "🟢 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН."
    )

    logger.info(
        "🔥 БОТ ПОЛНОСТЬЮ ЗАПУЩЕН."
    )

    # --------------------------------------------------------
    # KEEP PROCESS ALIVE
    # --------------------------------------------------------

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    except asyncio.CancelledError:

        logger.info(
            "🛑 Main cancelled."
        )

    finally:

        logger.info(
            "🛑 Останавливаем задачи..."
        )

        if scheduler_task:

            scheduler_task.cancel()

        if heartbeat_task:

            heartbeat_task.cancel()

        for task in (
            scheduler_task,
            heartbeat_task
        ):

            if task:

                try:

                    await task

                except asyncio.CancelledError:

                    pass

        try:

            await bot.delete_webhook()

        except Exception:
            pass

        await bot.session.close()

        await runner.cleanup()

        logger.info(
            "👋 Бот остановлен."
        )


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
            "🛑 Бот остановлен вручную."
        )
