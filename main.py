import asyncio
import json
import logging
import os
import re
from datetime import datetime
from html import escape
from urllib.parse import urljoin, urlparse

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

# Проверка каждые 5 минут
CHECK_INTERVAL_SECONDS = 300

# ============================================================
# ГЛАВНОЕ:
# МИНИМАЛЬНАЯ СКИДКА = 20%
# ============================================================

MIN_DISCOUNT_PERCENT = 20


# ============================================================
# БРЕНДЫ
# ============================================================
#
# Сейчас отслеживаем только эти бренды.
#
# Если хотите ВСЕ БРЕНДЫ iHerb:
# TARGET_BRANDS = []
#

TARGET_BRANDS = [
    "California Gold Nutrition",
    "NOW Foods",
    "Doctor's Best",
    "Solgar",
]


# ============================================================
# КУРС И НАЦЕНКА
# ============================================================

KZT_EXCHANGE_RATE = 540

MARGIN_MARKUP_PERCENT = 35


# ============================================================
# CACHE
# ============================================================

CACHE_FILE = "sent_deals.json"

MAX_DEALS_PER_CHECK = 10


# ============================================================
# ПРОВЕРКА НАСТРОЕК
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Render → Environment Variables"
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID не найден. "
        "Render → Environment Variables"
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

    logging.info(
        "✅ curl_cffi доступен"
    )

except ImportError:

    HAS_CURL_CFFI = False

    logging.warning(
        "⚠️ curl_cffi не установлен. "
        "Будет использован httpx."
    )


# ============================================================
# CACHE
# ============================================================

def load_cache():

    global sent_deals_cache

    try:

        if not os.path.exists(CACHE_FILE):

            sent_deals_cache = set()

            logging.info(
                "💾 Cache пока пустой"
            )

            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8"
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

        data = list(
            sent_deals_cache
        )[-5000:]

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

        logging.error(
            f"❌ Ошибка сохранения cache: {e}"
        )


# ============================================================
# HEADERS
# ============================================================

HEADERS = {

    "User-Agent":
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36",

    "Accept-Language":
        "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",

    "Accept":
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",

    "Cache-Control":
        "no-cache",

    "Pragma":
        "no-cache",

    "Connection":
        "keep-alive",

    "Upgrade-Insecure-Requests":
        "1",
}


# ============================================================
# URL
# ============================================================

IHERB_BASE_URL = "https://www.iherb.com/"

IHERB_DEALS_URLS = [

    "https://kz.iherb.com/deals",

    "https://www.iherb.com/deals",

    "https://kz.iherb.com/c/specials",

    "https://www.iherb.com/c/specials",

]


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ
# ============================================================

def clean_text(text):

    if not text:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(text)
    ).strip()


def normalize_number(text):

    if text is None:
        return None

    text = str(text)

    text = (
        text
        .replace("\xa0", " ")
        .replace("₽", "")
        .replace("₸", "")
    )

    # Удаляем USD / US$
    text = re.sub(
        r"\bUSD\b",
        "",
        text,
        flags=re.I
    )

    text = text.replace(
        "US$",
        "$"
    )

    # Удаляем пробелы
    text = text.replace(
        " ",
        ""
    )

    # 12,99 -> 12.99
    text = text.replace(
        ",",
        "."
    )

    match = re.search(
        r"\d+(?:\.\d{1,4})?",
        text
    )

    if not match:
        return None

    try:

        value = float(
            match.group(0)
        )

        if value <= 0:
            return None

        if value > 10000:
            return None

        return value

    except Exception:

        return None


def normalize_url(url):

    if not url:
        return ""

    url = str(url).strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return urljoin(
            IHERB_BASE_URL,
            url
        )

    if url.startswith("http://"):
        return (
            "https://"
            + url[7:]
        )

    if url.startswith("https://"):
        return url

    return urljoin(
        IHERB_BASE_URL,
        url
    )


# ============================================================
# БРЕНД
# ============================================================

def find_brand(title):

    title_lower = title.lower()

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:

            return brand

    return ""


# ============================================================
# PRODUCT ID
# ============================================================

def extract_product_id(link):

    if not link:
        return ""

    parsed = urlparse(link)

    path = parsed.path

    patterns = [

        r"/(\d+)$",

        r"/(\d+)/$",

        r"/pr/[^/]+/(\d+)",

        r"/pr/[^/]+/(\d+)/",

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            path
        )

        if match:

            return match.group(1)

    # Иногда ID находится в query
    query_match = re.search(
        r"(?:pid|productId|product_id)=(\d+)",
        link,
        re.I
    )

    if query_match:

        return query_match.group(1)

    return link


# ============================================================
# ПРОЦЕНТ СКИДКИ
# ============================================================

def extract_discount_percent(text):

    if not text:
        return None

    text = clean_text(text)

    patterns = [

        r"(\d{1,2})\s*%\s*OFF",

        r"(\d{1,2})\s*%\s*off",

        r"(\d{1,2})\s*%\s*скид",

        r"скидк[аи]?\s*(?:до\s*)?(\d{1,2})\s*%",

        r"-\s*(\d{1,2})\s*%",

        r"save\s+(\d{1,2})\s*%",

        r"(\d{1,2})\s*%\s*save",

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for match in matches:

            try:

                value = int(
                    match
                )

                if 1 <= value <= 90:

                    return value

            except Exception:

                pass

    return None


# ============================================================
# ЦЕНЫ
# ============================================================

def extract_currency_prices(text):

    """
    Извлекает только цены, возле которых
    явно присутствует $ / USD.

    Это важно.

    Мы НЕ хотим считать:
    100 capsules
    500 mg
    30 servings

    ценами.
    """

    if not text:
        return []

    text = (
        str(text)
        .replace("\xa0", " ")
    )

    patterns = [

        r"\$\s*(\d+(?:[.,]\d{1,2})?)",

        r"US\$\s*(\d+(?:[.,]\d{1,2})?)",

        r"USD\s*(\d+(?:[.,]\d{1,2})?)",

        r"(\d+(?:[.,]\d{1,2})?)\s*\$",

    ]

    result = []

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.I
        )

        for value in matches:

            number = normalize_number(
                value
            )

            if number is not None:

                if number not in result:

                    result.append(
                        number
                    )

    return result


# ============================================================
# ПОИСК ЦЕНЫ В HTML-АТРИБУТАХ
# ============================================================

def extract_attribute_prices(card):

    prices = []

    attributes = [

        "data-price",

        "data-current-price",

        "data-sale-price",

        "data-discount-price",

        "data-original-price",

        "data-list-price",

        "data-old-price",

        "data-regular-price",

        "data-product-price",

        "data-price-value",

    ]

    for element in card.find_all():

        for attribute in attributes:

            value = element.get(
                attribute
            )

            if value is None:
                continue

            number = normalize_number(
                value
            )

            if number is not None:

                if number not in prices:

                    prices.append(
                        number
                    )

    return prices


# ============================================================
# ПОИСК JSON В CARD
# ============================================================

def extract_json_numbers(obj):

    """
    Рекурсивно ищет значения, похожие на цены
    и скидки внутри JSON.
    """

    prices = []
    discounts = []

    def walk(value, key=""):

        if isinstance(value, dict):

            for k, v in value.items():

                key_lower = str(k).lower()

                # --------------------------
                # DISCOUNT
                # --------------------------

                if any(
                    word in key_lower
                    for word in [
                        "discount",
                        "discountpercent",
                        "discountpercentage",
                        "savingpercent",
                        "savingspercent",
                    ]
                ):

                    if isinstance(
                        v,
                        (int, float, str)
                    ):

                        try:

                            n = float(
                                str(v)
                                .replace("%", "")
                                .replace(",", ".")
                            )

                            if 1 <= n <= 90:

                                discounts.append(
                                    int(round(n))
                                )

                        except Exception:
                            pass

                # --------------------------
                # PRICE
                # --------------------------

                if any(
                    word in key_lower
                    for word in [
                        "price",
                        "saleprice",
                        "currentprice",
                        "listprice",
                        "originalprice",
                        "regularprice",
                        "discountprice",
                    ]
                ):

                    if isinstance(
                        v,
                        (int, float, str)
                    ):

                        n = normalize_number(
                            v
                        )

                        if n is not None:

                            prices.append(
                                n
                            )

                walk(
                    v,
                    key_lower
                )

        elif isinstance(value, list):

            for item in value:

                walk(
                    item,
                    key
                )

    walk(obj)

    return (
        list(dict.fromkeys(prices)),
        list(dict.fromkeys(discounts))
    )


def extract_json_from_scripts(card):

    prices = []

    discounts = []

    scripts = card.find_all(
        "script"
    )

    for script in scripts:

        raw = script.string

        if not raw:

            raw = script.get_text()

        if not raw:

            continue

        raw = raw.strip()

        # Пытаемся JSON
        try:

            data = json.loads(
                raw
            )

            p, d = extract_json_numbers(
                data
            )

            prices.extend(p)
            discounts.extend(d)

        except Exception:

            # Иногда внутри script
            # находится JSON как часть JS.
            # Ищем цены/discount вручную.

            for pattern in [

                r'"(?:price|salePrice|currentPrice|discountPrice)"\s*:\s*"?(?:\$)?(\d+(?:\.\d+)?)',

                r'"(?:originalPrice|listPrice|regularPrice)"\s*:\s*"?(?:\$)?(\d+(?:\.\d+)?)',

                r'"(?:discountPercent|discountPercentage)"\s*:\s*"?(\d+(?:\.\d+)?)',

            ]:

                matches = re.findall(
                    pattern,
                    raw,
                    re.I
                )

                for value in matches:

                    try:

                        number = float(
                            value
                        )

                        if "discount" in pattern.lower():

                            if 1 <= number <= 90:

                                discounts.append(
                                    int(round(number))
                                )

                        else:

                            if 0 < number < 10000:

                                prices.append(
                                    number
                                )

                    except Exception:
                        pass

    return (
        list(dict.fromkeys(prices)),
        list(dict.fromkeys(discounts))
    )


# ============================================================
# НАЗВАНИЕ
# ============================================================

def extract_title(card):

    selectors = [

        "[class*='product-title']",

        ".product-title",

        "[data-qa*='product-name']",

        "[data-testid*='product-name']",

        "a[href*='/pr/']",

    ]

    for selector in selectors:

        try:

            element = card.select_one(
                selector
            )

            if element:

                text = clean_text(
                    element.get_text(
                        " ",
                        strip=True
                    )
                )

                if len(text) >= 3:

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

            if element:

                link = normalize_url(
                    element.get(
                        "href",
                        ""
                    )
                )

                if link:

                    return link

        except Exception:

            pass

    return ""


# ============================================================
# РАСЧЁТ СКИДКИ
# ============================================================

def calculate_discount(
    prices,
    explicit_discount=None
):

    # Удаляем дубли
    prices = sorted(
        set(
            round(float(x), 2)
            for x in prices
            if x and x > 0
        )
    )

    if explicit_discount:

        if 1 <= explicit_discount <= 90:

            # Если есть только одна цена,
            # рассчитываем старую цену.
            if len(prices) == 1:

                current_price = prices[0]

                old_price = round(
                    current_price
                    / (
                        1
                        - explicit_discount / 100
                    ),
                    2
                )

                return (
                    old_price,
                    current_price,
                    explicit_discount
                )

    # Нужны минимум 2 цены
    if len(prices) >= 2:

        current_price = min(
            prices
        )

        possible_old_prices = [

            p
            for p in prices
            if p > current_price

        ]

        if possible_old_prices:

            old_price = max(
                possible_old_prices
            )

            discount = round(
                (
                    1
                    - current_price
                    / old_price
                )
                * 100
            )

            if 0 < discount <= 90:

                return (
                    old_price,
                    current_price,
                    discount
                )

    return (
        None,
        None,
        None
    )


# ============================================================
# ПОЛУЧЕНИЕ HTML
# ============================================================

async def get_iherb_html():

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

        for target_url in IHERB_DEALS_URLS:

            for browser in [

                "chrome124",
                "chrome120",
                "chrome116",

            ]:

                try:

                    response = await asyncio.to_thread(

                        curl_requests.get,

                        target_url,

                        headers=HEADERS,

                        cookies=cookies,

                        impersonate=browser,

                        timeout=25,

                    )

                    logging.info(

                        f"iHerb | "
                        f"{browser} | "
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
                        f"curl ошибка: {e}"
                    )

    # ========================================================
    # HTTPX
    # ========================================================

    for target_url in IHERB_DEALS_URLS:

        try:

            async with httpx.AsyncClient(

                timeout=25,

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
        "❌ iHerb HTML получить не удалось"
    )

    return ""


# ============================================================
# ПОИСК КАРТОЧЕК
# ============================================================

def find_product_cards(soup):

    selectors = [

        ".product-cell-container",

        "[data-qa='product-card']",

        "[data-testid='product-card']",

        ".product-card",

        ".product-tile",

        ".product-inner",

        "[class*='product-cell-container']",

        "[class*='product-card']",

        "[class*='product-tile']",

    ]

    all_cards = []

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

                all_cards.extend(
                    cards
                )

        except Exception:
            pass

    # ========================================================
    # УНИКАЛЬНЫЕ
    # ========================================================

    unique = []

    seen = set()

    for card in all_cards:

        text = clean_text(
            card.get_text(
                " ",
                strip=True
            )
        )

        if not text:
            continue

        link = extract_link(
            card
        )

        if link:

            key = link

        else:

            key = text[:700]

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            card
        )

    return unique


# ============================================================
# ПАРСИНГ
# ============================================================

async def fetch_iherb_specials():

    logging.info(
        "🔎 Начинаем проверку iHerb..."
    )

    html = await get_iherb_html()

    if not html:

        return []

    deals = []

    statistics = {

        "cards": 0,

        "no_title": 0,

        "no_link": 0,

        "wrong_brand": 0,

        "no_price": 0,

        "no_discount": 0,

        "below_20": 0,

        "accepted": 0,

    }

    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        cards = find_product_cards(
            soup
        )

        statistics["cards"] = len(
            cards
        )

        logging.info(
            f"📦 Уникальных карточек: "
            f"{len(cards)}"
        )

        # ====================================================
        # ОБРАБОТКА
        # ====================================================

        for index, card in enumerate(
            cards,
            start=1
        ):

            try:

                title = extract_title(
                    card
                )

                if not title:

                    statistics["no_title"] += 1

                    continue

                link = extract_link(
                    card
                )

                if not link:

                    statistics["no_link"] += 1

                    continue

                brand = find_brand(
                    title
                )

                # =================================================
                # БРЕНД
                # =================================================

                if TARGET_BRANDS and not brand:

                    statistics["wrong_brand"] += 1

                    continue

                # =================================================
                # ТЕКСТ
                # =================================================

                card_text = clean_text(
                    card.get_text(
                        " ",
                        strip=True
                    )
                )

                # =================================================
                # ЯВНЫЙ %
                # =================================================

                explicit_discount = (
                    extract_discount_percent(
                        card_text
                    )
                )

                # =================================================
                # ЦЕНЫ ИЗ DATA-АТРИБУТОВ
                # =================================================

                prices = (
                    extract_attribute_prices(
                        card
                    )
                )

                # =================================================
                # ЦЕНЫ ИЗ JSON
                # =================================================

                json_prices, json_discounts = (
                    extract_json_from_scripts(
                        card
                    )
                )

                prices.extend(
                    json_prices
                )

                # =================================================
                # DISCOUNT ИЗ JSON
                # =================================================

                if (
                    not explicit_discount
                    and json_discounts
                ):

                    valid_discounts = [

                        x
                        for x in json_discounts
                        if 1 <= x <= 90

                    ]

                    if valid_discounts:

                        explicit_discount = max(
                            valid_discounts
                        )

                # =================================================
                # ЦЕНЫ ИЗ ЭЛЕМЕНТОВ
                # =================================================

                price_selectors = [

                    "[class*='price']",

                    "[data-qa*='price']",

                    "[data-testid*='price']",

                ]

                for selector in price_selectors:

                    try:

                        elements = card.select(
                            selector
                        )

                        for element in elements:

                            txt = clean_text(
                                element.get_text(
                                    " ",
                                    strip=True
                                )
                            )

                            extracted = (
                                extract_currency_prices(
                                    txt
                                )
                            )

                            for value in extracted:

                                if value not in prices:

                                    prices.append(
                                        value
                                    )

                    except Exception:

                        pass

                # =================================================
                # ЦЕНЫ ИЗ КАРТОЧКИ
                # =================================================
                #
                # Только если есть $ / USD.
                #

                currency_prices = (
                    extract_currency_prices(
                        card_text
                    )
                )

                for value in currency_prices:

                    if value not in prices:

                        prices.append(
                            value
                        )

                # =================================================
                # УНИКАЛЬНЫЕ ЦЕНЫ
                # =================================================

                prices = sorted(
                    set(
                        round(
                            float(x),
                            2
                        )
                        for x in prices
                        if x and x > 0
                    )
                )

                # Убираем совсем маленькие
                # подозрительные значения
                prices = [

                    x
                    for x in prices
                    if 0.5 <= x <= 5000

                ]

                if not prices:

                    statistics["no_price"] += 1

                    logging.debug(

                        f"⏭ [{index}] "
                        f"Нет цены | "
                        f"{title[:70]}"

                    )

                    continue

                # =================================================
                # РАСЧЁТ
                # =================================================

                old_price, current_price, discount = (
                    calculate_discount(
                        prices,
                        explicit_discount
                    )
                )

                # =================================================
                # Если процент найден напрямую,
                # а старой цены нет
                # =================================================

                if (

                    explicit_discount

                    and not current_price

                    and len(prices) >= 1

                ):

                    current_price = min(
                        prices
                    )

                    old_price = round(

                        current_price
                        /
                        (
                            1
                            - explicit_discount / 100
                        ),

                        2

                    )

                    discount = (
                        explicit_discount
                    )

                # =================================================
                # ПРОВЕРКА
                # =================================================

                if not discount:

                    statistics["no_discount"] += 1

                    logging.debug(

                        f"⏭ [{index}] "
                        f"Нет скидки | "
                        f"prices={prices} | "
                        f"{title[:60]}"

                    )

                    continue

                if discount < MIN_DISCOUNT_PERCENT:

                    statistics["below_20"] += 1

                    logging.debug(

                        f"⏭ [{index}] "
                        f"Скидка {discount}% "
                        f"< {MIN_DISCOUNT_PERCENT}% | "
                        f"{title[:60]}"

                    )

                    continue

                if not old_price or not current_price:

                    statistics["no_discount"] += 1

                    continue

                if old_price <= current_price:

                    statistics["no_discount"] += 1

                    continue

                # =================================================
                # ID
                # =================================================

                product_id = extract_product_id(
                    link
                )

                if not product_id:

                    product_id = link

                # =================================================
                # DEAL
                # =================================================

                deal = {

                    "id":
                        str(product_id),

                    "title":
                        title,

                    "brand":
                        brand
                        or "iHerb",

                    "orig_price_usd":
                        round(
                            old_price,
                            2
                        ),

                    "discount_price_usd":
                        round(
                            current_price,
                            2
                        ),

                    "discount_percent":
                        int(
                            round(
                                discount
                            )
                        ),

                    "link":
                        link,

                }

                deals.append(
                    deal
                )

                statistics["accepted"] += 1

                logging.info(

                    "🔥 НАЙДЕНА СКИДКА | "
                    f"-{deal['discount_percent']}% | "
                    f"{deal['brand']} | "
                    f"{deal['title'][:65]} | "
                    f"${deal['discount_price_usd']:.2f}"

                )

            except Exception as e:

                logging.debug(

                    f"Ошибка карточки "
                    f"{index}: {e}"

                )

        # ========================================================
        # УДАЛЕНИЕ ДУБЛЕЙ
        # ========================================================

        unique_deals = {}

        for deal in deals:

            unique_deals[
                deal["id"]
            ] = deal

        deals = list(
            unique_deals.values()
        )

        # ========================================================
        # СОРТИРОВКА
        # ========================================================

        deals.sort(

            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"]
            ),

            reverse=True

        )

        # ========================================================
        # СТАТИСТИКА
        # ========================================================

        logging.info(
            "============================================================"
        )

        logging.info(
            "📊 РЕЗУЛЬТАТ ПАРСИНГА"
        )

        logging.info(
            f"Карточек: "
            f"{statistics['cards']}"
        )

        logging.info(
            f"Без названия: "
            f"{statistics['no_title']}"
        )

        logging.info(
            f"Без ссылки: "
            f"{statistics['no_link']}"
        )

        logging.info(
            f"Другой бренд: "
            f"{statistics['wrong_brand']}"
        )

        logging.info(
            f"Без цены: "
            f"{statistics['no_price']}"
        )

        logging.info(
            f"Без скидки: "
            f"{statistics['no_discount']}"
        )

        logging.info(
            f"Скидка меньше "
            f"{MIN_DISCOUNT_PERCENT}%: "
            f"{statistics['below_20']}"
        )

        logging.info(
            f"ПОДХОДЯЩИХ: "
            f"{statistics['accepted']}"
        )

        logging.info(
            "============================================================"
        )

        # ========================================================
        # ТОП НАЙДЕННЫХ
        # ========================================================

        for deal in deals[:20]:

            logging.info(

                f"💊 "
                f"{deal['brand']} | "
                f"-{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:70]}"

            )

        return deals

    except Exception as e:

        logging.exception(
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

    # ========================================================
    # ЗАКУП
    # ========================================================

    cost_kzt = round(
        current_usd
        * KZT_EXCHANGE_RATE
    )

    # ========================================================
    # ПРОДАЖА
    # ========================================================

    sell_kzt = round(

        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT / 100
        )

    )

    profit_kzt = (
        sell_kzt
        - cost_kzt
    )

    # ========================================================
    # FORMAT
    # ========================================================

    def money(value):

        return (
            f"{value:,}"
            .replace(",", " ")
        )

    cost_str = money(
        cost_kzt
    )

    sell_str = money(
        sell_kzt
    )

    profit_str = money(
        profit_kzt
    )

    # ========================================================
    # MESSAGE
    # ========================================================

    message = (

        f"🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n"
        f"\n"

        f"🏷 <b>Бренд:</b> "
        f"{brand}\n"

        f"\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n"

        f"\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n"

        f"\n"

        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${old_usd:.2f}</s> "
        f"➡️ <b>${current_usd:.2f}</b>\n"

        f"\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n"

        f"\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{sell_str} ₸\n"

        f"\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n"

        f"\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"\n"

        f"⏰ "
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
# ОТПРАВКА
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

                f"✅ Отправлено "
                f"{target_id}: "
                f"{deal['title'][:60]}"

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

            logging.warning(

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

                logging.error(

                    f"❌ Повторная отправка: "
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

    logging.info(
        "============================================================"
    )

    logging.info(
        "🔎 ПРОВЕРКА iHERB"
    )

    logging.info(
        "============================================================"
    )

    logging.info(
        f"🎯 Ищем скидки от "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    try:

        deals = await fetch_iherb_specials()

    except Exception as e:

        logging.exception(
            f"❌ Ошибка проверки: {e}"
        )

        return

    if not deals:

        logging.info(
            f"ℹ️ Скидок "
            f"от {MIN_DISCOUNT_PERCENT}% "
            f"не найдено."
        )

        return

    targets = get_targets()

    if not targets:

        logging.warning(
            "⚠️ Нет Telegram получателей."
        )

        return

    logging.info(
        f"👥 Получателей: "
        f"{len(targets)}"
    )

    sent_count = 0

    for deal in deals:

        deal_id = deal["id"]

        # ====================================================
        # АВТОМАТИЧЕСКИЙ РЕЖИМ
        # ====================================================

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
        f"📤 Отправлено: "
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

        "🔥 Автоматически отслеживаю iHerb.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"⏱ Проверка: "
        f"<b>каждые 5 минут</b>\n\n"

        "Подходящие новые товары "
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
        "🔎 Проверяю iHerb прямо сейчас..."
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

    if TARGET_BRANDS:

        brands_text = ", ".join(
            TARGET_BRANDS
        )

    else:

        brands_text = (
            "ВСЕ БРЕНДЫ"
        )

    await message.answer(

        f"📊 <b>СТАТУС БОТА</b>\n\n"

        f"🟢 Telegram: ONLINE\n"
        f"🟢 Мониторинг: ВКЛЮЧЁН\n"

        f"🔄 Интервал: "
        f"5 минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        f"🏷 Бренды:\n"
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

        "🔥 Я автоматически отслеживаю "
        f"скидки от {MIN_DISCOUNT_PERCENT}%.\n\n"

        "Новые подходящие скидки "
        "будут отправляться автоматически.",

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
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    # ========================================================
    # ПЕРВАЯ ПРОВЕРКА
    # ========================================================

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

    # ========================================================
    # ЦИКЛ
    # ========================================================

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
            f"❌ Health Server: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    logging.info(
        "============================================================"
    )

    logging.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logging.info(
        "============================================================"
    )

    logging.info(
        f"🎯 СКИДКИ ОТ {MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        f"💱 USD/KZT = {KZT_EXCHANGE_RATE}"
    )

    logging.info(
        f"📈 Наценка = {MARGIN_MARKUP_PERCENT}%"
    )

    # ========================================================
    # CACHE
    # ========================================================

    load_cache()

    # ========================================================
    # RENDER
    # ========================================================

    await start_dummy_server()

    # ========================================================
    # SCHEDULER
    # ========================================================

    scheduler_task = asyncio.create_task(
        scheduler()
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

                    "❌ TELEGRAM UNAUTHORIZED! "
                    "Проверьте BOT_TOKEN."

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

    # ========================================================
    # STOP
    # ========================================================

    scheduler_task.cancel()

    try:

        await scheduler_task

    except asyncio.CancelledError:

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

        logging.info(
            "🛑 Бот остановлен."
        )
