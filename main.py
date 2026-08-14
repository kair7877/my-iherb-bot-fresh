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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = 300

MIN_DISCOUNT_PERCENT = 20
MAX_DISCOUNT_PERCENT = 90

TARGET_BRANDS = []

KZT_EXCHANGE_RATE = 540
MARGIN_MARKUP_PERCENT = 35

CACHE_FILE = "sent_deals.json"
MAX_CACHE_ITEMS = 5000

MAX_DEALS_PER_CHECK = 10


# ============================================================
# ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. Render → Environment Variables."
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID не найден. Render → Environment Variables."
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
# CURL CFFI
# ============================================================

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True

    logging.info("✅ curl_cffi доступен")

except ImportError:

    HAS_CURL_CFFI = False

    logging.warning(
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

            logging.info("💾 Cache пока пустой")

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

        else:

            sent_deals_cache = set()

        logging.info(
            "💾 Загружено ранее отправленных "
            f"товаров: {len(sent_deals_cache)}"
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка загрузки cache: {e}"
        )

        sent_deals_cache = set()


def save_cache():

    try:

        data = list(sent_deals_cache)[
            -MAX_CACHE_ITEMS:
        ]

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

        logging.error(
            f"❌ Ошибка сохранения cache: {e}"
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
        str(text),
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
            .replace("\xa0", " ")
            .replace(",", ".")
            .strip()
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
# PRICE EXTRACTION
# ============================================================

def extract_prices(text):

    if not text:
        return []

    text = (
        str(text)
        .replace("\xa0", " ")
        .replace(" ", " ")
    )

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
            re.IGNORECASE,
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


# ============================================================
# NUMBER PRICE
# ============================================================

def extract_number_price(value):

    if value is None:
        return []

    if isinstance(value, (int, float)):

        number = float(value)

        if 0 < number < 10000:
            return [number]

        return []

    return extract_prices(str(value))


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

            value = int(match.group(1))

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
        url,
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

    title_lower = title.lower()

    if not TARGET_BRANDS:
        return "iHerb"

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:
            return brand

    return ""


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
        "[data-qa*='price']",
        "[data-testid*='price']",
        "[class*='Price']",

    ]

    texts = []

    for selector in selectors:

        try:

            elements = card.select(selector)

            for element in elements:

                text = clean_text(
                    element.get_text(
                        " ",
                        strip=True,
                    )
                )

                if text:
                    texts.append(text)

        except Exception:
            pass

    return texts


# ============================================================
# JSON RECURSIVE PRICE SCANNER
# ============================================================

PRICE_KEYS_CURRENT = {

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
    "unitprice",
    "unit_price",
}

PRICE_KEYS_OLD = {

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
    "compareatprice",
    "compare_at_price",
}


def scan_json_for_prices(
    data,
    current_prices,
    old_prices,
    depth=0,
):

    if depth > 15:
        return

    if isinstance(data, dict):

        for key, value in data.items():

            key_clean = re.sub(
                r"[^a-z0-9_]",
                "",
                str(key).lower(),
            )

            if key_clean in PRICE_KEYS_CURRENT:

                values = extract_number_price(value)

                current_prices.extend(values)

            elif key_clean in PRICE_KEYS_OLD:

                values = extract_number_price(value)

                old_prices.extend(values)

            scan_json_for_prices(
                value,
                current_prices,
                old_prices,
                depth + 1,
            )

    elif isinstance(data, list):

        for item in data:

            scan_json_for_prices(
                item,
                current_prices,
                old_prices,
                depth + 1,
            )


# ============================================================
# JSON SCRIPT DATA
# ============================================================

def extract_script_json_prices(
    card
):

    current_prices = []
    old_prices = []

    discount = None

    scripts = card.select(
        "script"
    )

    for script in scripts:

        text = script.string or script.get_text()

        if not text:
            continue

        text = text.strip()

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        try:

            data = json.loads(text)

            scan_json_for_prices(
                data,
                current_prices,
                old_prices,
            )

        except Exception:
            pass

        # ----------------------------------------------------
        # Даже если это не чистый JSON,
        # ищем ключи price
        # ----------------------------------------------------

        patterns_current = [

            r'"(?:price|salePrice|currentPrice|discountPrice|finalPrice)"'
            r'\s*:\s*"?\$?\s*(\d+(?:[.,]\d{1,2})?)',

        ]

        patterns_old = [

            r'"(?:originalPrice|oldPrice|regularPrice|listPrice|wasPrice)"'
            r'\s*:\s*"?\$?\s*(\d+(?:[.,]\d{1,2})?)',

        ]

        for pattern in patterns_current:

            for match in re.finditer(
                pattern,
                text,
                re.IGNORECASE,
            ):

                value = safe_float(
                    match.group(1)
                )

                if value:
                    current_prices.append(value)

        for pattern in patterns_old:

            for match in re.finditer(
                pattern,
                text,
                re.IGNORECASE,
            ):

                value = safe_float(
                    match.group(1)
                )

                if value:
                    old_prices.append(value)

        found_discount = (
            extract_discount_percent(
                text
            )
        )

        if found_discount:

            discount = found_discount

    return (
        current_prices,
        old_prices,
        discount,
    )


# ============================================================
# DATA ATTRIBUTES
# ============================================================

def extract_data_attribute_prices(
    card
):

    current_prices = []
    old_prices = []

    discount = None

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                if not isinstance(
                    value,
                    str,
                ):
                    continue

                key_lower = (
                    str(key)
                    .lower()
                    .replace("-", "_")
                )

                values = extract_prices(
                    value
                )

                if not values:

                    # Иногда data-price="12.99"
                    number = safe_float(value)

                    if number and number < 10000:
                        values = [number]

                if not values:
                    continue

                if (
                    "original"
                    in key_lower
                    or "old"
                    in key_lower
                    or "regular"
                    in key_lower
                    or "was"
                    in key_lower
                ):

                    old_prices.extend(
                        values
                    )

                elif (
                    "price"
                    in key_lower
                    or "sale"
                    in key_lower
                    or "current"
                    in key_lower
                    or "discount"
                    in key_lower
                ):

                    current_prices.extend(
                        values
                    )

                if (
                    "discount"
                    in key_lower
                    or "percent"
                    in key_lower
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

    return (
        current_prices,
        old_prices,
        discount,
    )


# ============================================================
# META PRICE
# ============================================================

def extract_meta_prices(
    card
):

    current_prices = []
    old_prices = []

    try:

        metas = card.select(
            "meta"
        )

        for meta in metas:

            content = meta.get(
                "content",
                "",
            )

            if not content:
                continue

            attr_text = " ".join(
                [
                    str(
                        meta.get(
                            "property",
                            "",
                        )
                    ),
                    str(
                        meta.get(
                            "name",
                            "",
                        )
                    ),
                    str(
                        meta.get(
                            "itemprop",
                            "",
                        )
                    ),
                ]
            ).lower()

            value = safe_float(
                content
            )

            if not value:
                continue

            if value >= 10000:
                continue

            if (
                "price"
                not in attr_text
            ):
                continue

            if (
                "old"
                in attr_text
                or "original"
                in attr_text
                or "regular"
                in attr_text
            ):

                old_prices.append(value)

            else:

                current_prices.append(value)

    except Exception:
        pass

    return (
        current_prices,
        old_prices,
    )


# ============================================================
# LD+JSON
# ============================================================

def extract_ld_json_prices(
    card
):

    current_prices = []
    old_prices = []

    scripts = card.select(
        "script[type='application/ld+json']"
    )

    for script in scripts:

        text = script.string or script.get_text()

        if not text:
            continue

        try:

            data = json.loads(
                text
            )

        except Exception:
            continue

        scan_json_for_prices(
            data,
            current_prices,
            old_prices,
        )

    return (
        current_prices,
        old_prices,
    )


# ============================================================
# PRICE VALIDATION
# ============================================================

def remove_bad_prices(
    prices
):

    result = []

    for price in prices:

        try:
            price = float(price)
        except Exception:
            continue

        if price <= 0:
            continue

        if price >= 10000:
            continue

        # Защита от количества,
        # дозировок и других чисел
        if price.is_integer():

            # 1000, 2000 и т.п.
            if price >= 1000:
                continue

        if price not in result:

            result.append(
                round(price, 2)
            )

    return result


# ============================================================
# FIND PRICES
# ============================================================

def find_prices_advanced(
    card
):

    current_prices = []
    old_prices = []

    sources = []

    # --------------------------------------------------------
    # 1. LD JSON
    # --------------------------------------------------------

    try:

        cur, old = (
            extract_ld_json_prices(
                card
            )
        )

        if cur:

            current_prices.extend(cur)
            sources.append("LD+JSON current")

        if old:

            old_prices.extend(old)
            sources.append("LD+JSON old")

    except Exception:
        pass

    # --------------------------------------------------------
    # 2. Script JSON
    # --------------------------------------------------------

    try:

        cur, old, _ = (
            extract_script_json_prices(
                card
            )
        )

        if cur:

            current_prices.extend(cur)
            sources.append("SCRIPT current")

        if old:

            old_prices.extend(old)
            sources.append("SCRIPT old")

    except Exception:
        pass

    # --------------------------------------------------------
    # 3. DATA ATTRIBUTES
    # --------------------------------------------------------

    try:

        cur, old, _ = (
            extract_data_attribute_prices(
                card
            )
        )

        if cur:

            current_prices.extend(cur)
            sources.append("DATA current")

        if old:

            old_prices.extend(old)
            sources.append("DATA old")

    except Exception:
        pass

    # --------------------------------------------------------
    # 4. META
    # --------------------------------------------------------

    try:

        cur, old = (
            extract_meta_prices(
                card
            )
        )

        if cur:

            current_prices.extend(cur)
            sources.append("META current")

        if old:

            old_prices.extend(old)
            sources.append("META old")

    except Exception:
        pass

    # --------------------------------------------------------
    # 5. PRICE ELEMENTS
    # --------------------------------------------------------

    try:

        price_texts = (
            get_card_price_texts(
                card
            )
        )

        for text in price_texts:

            values = extract_prices(
                text
            )

            if values:

                current_prices.extend(
                    values
                )

        if price_texts:

            sources.append(
                "PRICE ELEMENTS"
            )

    except Exception:
        pass

    # --------------------------------------------------------
    # CLEAN
    # --------------------------------------------------------

    current_prices = remove_bad_prices(
        current_prices
    )

    old_prices = remove_bad_prices(
        old_prices
    )

    # --------------------------------------------------------
    # СТАРАЯ ЦЕНА ДОЛЖНА БЫТЬ
    # ВЫШЕ ТЕКУЩЕЙ
    # --------------------------------------------------------

    if current_prices:

        current = min(
            current_prices
        )

        valid_old = [
            x
            for x in old_prices
            if x > current
        ]

        old = (
            max(valid_old)
            if valid_old
            else None
        )

    else:

        current = None
        old = None

    source = (
        ", ".join(
            dict.fromkeys(sources)
        )
        if sources
        else "NONE"
    )

    return (
        current,
        old,
        source,
        current_prices,
        old_prices,
    )


# ============================================================
# CALCULATE DISCOUNT
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

    percent = (
        1
        - current_price / old_price
    ) * 100

    percent = round(
        percent
    )

    if percent < MIN_DISCOUNT_PERCENT:
        return percent

    if percent > MAX_DISCOUNT_PERCENT:
        return None

    return percent


# ============================================================
# EXTRACT TITLE
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

            href = element.get(
                "href",
                "",
            )

            href = normalize_url(
                href
            )

            if href:
                return href

        except Exception:
            pass

    return ""


# ============================================================
# CARDS
# ============================================================

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

        link = ""

        link_element = (
            card.select_one(
                "a[href]"
            )
        )

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
        f"📦 Уникальных карточек: "
        f"{len(unique)}"
    )

    return unique


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

            logging.info(
                f"⏭ Карточка #{index}: "
                "название не найдено"
            )

            return None

        link = extract_link(
            card
        )

        if not link:

            logging.info(
                f"⏭ {title[:60]} | "
                "ссылка не найдена"
            )

            return None

        # ----------------------------------------------------
        # BRAND
        # ----------------------------------------------------

        brand = find_brand(
            title
        )

        if (
            TARGET_BRANDS
            and not brand
        ):

            logging.info(
                f"⏭ {title[:60]} | "
                "бренд не входит в список"
            )

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
        # PRICE
        # ----------------------------------------------------

        (
            current_price,
            old_price,
            price_source,
            current_candidates,
            old_candidates,
        ) = find_prices_advanced(
            card
        )

        # ----------------------------------------------------
        # DEBUG
        # ----------------------------------------------------

        logging.info(
            f"🔎 CARD #{index} | "
            f"{title[:65]} | "
            f"current={current_price} | "
            f"old={old_price} | "
            f"discount={text_discount} | "
            f"source={price_source}"
        )

        logging.info(
            f"💰 PRICE CANDIDATES | "
            f"current={current_candidates[:15]} | "
            f"old={old_candidates[:15]}"
        )

        # ----------------------------------------------------
        # PRICE NOT FOUND
        # ----------------------------------------------------

        if not current_price:

            logging.info(
                f"⏭ {title[:65]} | "
                "цена не найдена"
            )

            return None

        # ----------------------------------------------------
        # CALCULATED DISCOUNT
        # ----------------------------------------------------

        calculated_discount = (
            calculate_discount(
                old_price,
                current_price,
            )
        )

        discount_percent = (
            text_discount
            or calculated_discount
        )

        # ----------------------------------------------------
        # ЕСЛИ СКИДКА ЕСТЬ,
        # НО СТАРОЙ ЦЕНЫ НЕТ
        # ----------------------------------------------------

        if (
            discount_percent
            and current_price
            and not old_price
        ):

            old_price = round(
                current_price
                / (
                    1
                    - discount_percent / 100
                ),
                2,
            )

            logging.info(
                f"🧮 Старая цена рассчитана: "
                f"${old_price:.2f}"
            )

        # ----------------------------------------------------
        # NO DISCOUNT
        # ----------------------------------------------------

        if not discount_percent:

            logging.info(
                f"⏭ {title[:65]} | "
                f"${current_price:.2f} | "
                "скидка не определена"
            )

            return None

        # ----------------------------------------------------
        # FILTER
        # ----------------------------------------------------

        if (
            discount_percent
            < MIN_DISCOUNT_PERCENT
        ):

            logging.info(
                f"⏭ {title[:65]} | "
                f"скидка {discount_percent}% "
                f"< {MIN_DISCOUNT_PERCENT}%"
            )

            return None

        # ----------------------------------------------------
        # OLD PRICE
        # ----------------------------------------------------

        if not old_price:

            logging.info(
                f"⏭ {title[:65]} | "
                "нет старой цены"
            )

            return None

        if old_price <= current_price:

            logging.info(
                f"⏭ {title[:65]} | "
                "старая цена <= текущей"
            )

            return None

        # ----------------------------------------------------
        # ID
        # ----------------------------------------------------

        product_id = extract_product_id(
            link
        )

        if not product_id:
            product_id = link

        # ----------------------------------------------------
        # DEAL
        # ----------------------------------------------------

        deal = {

            "id": product_id,

            "title": title,

            "brand": (
                brand
                if brand
                else "iHerb"
            ),

            "orig_price_usd": round(
                old_price,
                2,
            ),

            "discount_price_usd": round(
                current_price,
                2,
            ),

            "discount_percent": int(
                discount_percent
            ),

            "link": link,

            "price_source": price_source,

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
            f"❌ Ошибка карточки #{index}: "
            f"{e}"
        )

        return None


# ============================================================
# GET IHERB HTML
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
                        "iHerb | "
                        f"{browser} | "
                        f"HTTP "
                        f"{response.status_code}"
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        logging.info(
                            "✅ iHerb HTML получен: "
                            f"{len(response.text)} символов"
                        )

                        return response.text

                except Exception as e:

                    logging.debug(
                        f"curl_cffi error: {e}"
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
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url
                )

                logging.info(
                    "httpx | "
                    f"{url} | HTTP "
                    f"{response.status_code}"
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    logging.info(
                        "✅ HTML получен через httpx: "
                        f"{len(response.text)} символов"
                    )

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
# FETCH SPECIALS
# ============================================================

async def fetch_iherb_specials():

    logging.info(
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

        unique_deals = {}

        for deal in deals:

            unique_deals[
                deal["id"]
            ] = deal

        deals = list(
            unique_deals.values()
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

        logging.info("=" * 60)

        logging.info(
            "🔥 ИТОГОВЫЙ РЕЗУЛЬТАТ"
        )

        logging.info(
            "🔥 Найдено товаров со скидкой "
            f"{MIN_DISCOUNT_PERCENT}%+: "
            f"{len(deals)}"
        )

        for deal in deals[:20]:

            logging.info(
                f"💊 -{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:80]}"
            )

        logging.info("=" * 60)

        return deals

    except Exception as e:

        logging.exception(
            f"❌ Ошибка парсинга iHerb: {e}"
        )

        return []


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

    cost_kzt = round(
        current_usd
        * KZT_EXCHANGE_RATE
    )

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

    if CHAT_ID:
        targets.add(
            CHAT_ID
        )

    targets.update(
        subscribers
    )

    return targets


# ============================================================
# SEND
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
                "✅ Отправлено в "
                f"{target_id}: "
                f"{deal['title'][:70]}"
            )

            success = True

            await asyncio.sleep(2)

        except TelegramRetryAfter as e:

            retry_after = int(
                getattr(
                    e,
                    "retry_after",
                    30,
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
                f"❌ Telegram "
                f"{target_id}: {e}"
            )

    return success


# ============================================================
# CHECK
# ============================================================

async def check_and_notify(
    force_send=False
):

    logging.info("=" * 60)

    logging.info(
        "🔎 ПРОВЕРКА iHERB"
    )

    logging.info(
        f"🎯 Фильтр скидки: "
        f"{MIN_DISCOUNT_PERCENT}%+"
    )

    logging.info("=" * 60)

    try:

        deals = (
            await fetch_iherb_specials()
        )

    except Exception as e:

        logging.exception(
            f"❌ Ошибка проверки: {e}"
        )

        return

    if not deals:

        logging.info(
            "ℹ️ Подходящих скидок не найдено."
        )

        return

    targets = get_targets()

    if not targets:

        logging.warning(
            "⚠️ Нет получателей Telegram."
        )

        return

    logging.info(
        f"👥 Получателей: {len(targets)}"
    )

    sent_count = 0

    for deal in deals:

        deal_id = str(
            deal["id"]
        )

        if not force_send:

            if deal_id in sent_deals_cache:

                logging.info(
                    "⏭ Уже отправлялся: "
                    f"{deal['title'][:70]}"
                )

                continue

        success = await send_deal(
            deal,
            targets,
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

    logging.info(
        f"📤 Отправлено новых скидок: "
        f"{sent_count}"
    )


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

        "🔥 Я автоматически ищу "
        "товары со скидкой.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"⏱ Проверка: "
        f"<b>каждые 5 минут</b>\n\n"

        "📦 Сейчас поиск работает "
        "<b>по всем брендам</b>.\n\n"

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
        "⏳ Это может занять несколько секунд."
    )

    await check_and_notify(
        force_send=True
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

    await message.answer(

        f"📊 <b>СТАТУС БОТА</b>\n\n"

        f"🟢 Telegram: ONLINE\n"

        f"🟢 Автомониторинг: ВКЛЮЧЁН\n"

        f"🔄 Проверка: каждые 5 минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Бренды: "
        f"{brands_text}\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n\n"

        f"💾 В памяти: "
        f"{len(sent_deals_cache)} товаров",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# OTHER
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

        "🔥 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте:\n"

        "🔥 <b>Получить скидки</b> — "
        "проверить сейчас\n\n"

        "ℹ️ <b>Статус</b> — "
        "показать настройки.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():

    logging.info(
        "🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН"
    )

    logging.info(
        "⚡ Первая проверка выполняется СРАЗУ..."
    )

    try:

        await check_and_notify(
            force_send=False
        )

    except Exception as e:

        logging.exception(
            f"❌ Ошибка первой проверки: {e}"
        )

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

            logging.exception(
                f"❌ Ошибка scheduler: {e}"
            )

            await asyncio.sleep(30)


# ============================================================
# RENDER HEALTH
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

        logging.info(
            "🌐 Render Health Server "
            f"запущен на порту {port}"
        )

    except Exception as e:

        logging.exception(
            f"❌ Health Server: {e}"
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

    logging.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        f"💱 USD/KZT: "
        f"{KZT_EXCHANGE_RATE}"
    )

    logging.info(
        f"📈 Наценка: "
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
            "🏷 Фильтр брендов: "
            "ОТКЛЮЧЁН — ищем все бренды"
        )

    load_cache()

    await start_dummy_server()

    scheduler_task = asyncio.create_task(
        scheduler()
    )

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

                await asyncio.sleep(10)

            elif "Unauthorized" in error_text:

                logging.error(
                    "❌ TELEGRAM UNAUTHORIZED!"
                )

                await asyncio.sleep(30)

            else:

                logging.warning(
                    "🔄 Перезапуск через 5 секунд..."
                )

                await asyncio.sleep(5)

    scheduler_task.cancel()

    try:

        await scheduler_task

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
