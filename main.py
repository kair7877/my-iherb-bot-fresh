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
# iHERB DEAL BOT — UPDATED VERSION
# ============================================================
#
# ИСПРАВЛЕНИЯ:
#
# 1. Поиск цен:
#    - USD / $
#    - KZT / ₸
#    - цены в data-* атрибутах
#    - JSON внутри HTML
#    - HTML-текст карточки
#
# 2. Если iHerb показывает только:
#       25% OFF
#       8 000 ₸
#    старая цена рассчитывается автоматически.
#
# 3. Если iHerb показывает:
#       10 000 ₸
#       8 000 ₸
#    скидка рассчитывается по ценам.
#
# 4. Исправлена проблема:
#       Telegram Bad Request: chat not found
#
# 5. /start автоматически регистрирует правильный chat_id.
#
# 6. CHAT_ID из Render используется только если он существует.
#
# 7. Если CHAT_ID неправильный — бот не будет бесконечно
#    спамить ошибками.
#
# 8. Автоматическая проверка каждые 5 минут.
#
# 9. Минимальная скидка 20%.
#
# 10. Максимум 10 новых товаров за одну проверку.
#
# 11. Повторно отправленные товары запоминаются.
#
# ============================================================


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHAT_ID = os.getenv("CHAT_ID", "").strip()

CHECK_INTERVAL_SECONDS = 300

MIN_DISCOUNT_PERCENT = 20

MAX_DISCOUNT_PERCENT = 90


# ============================================================
# БРЕНДЫ
# ============================================================
#
# Пустой список = все бренды.
#
# Например:
#
# TARGET_BRANDS = [
#     "California Gold Nutrition",
#     "NOW Foods",
#     "Doctor's Best",
#     "Solgar",
# ]
#

TARGET_BRANDS = []


# ============================================================
# КУРС / НАЦЕНКА
# ============================================================

KZT_EXCHANGE_RATE = 540

MARGIN_MARKUP_PERCENT = 35


# ============================================================
# CACHE
# ============================================================

CACHE_FILE = "sent_deals.json"

MAX_CACHE_ITEMS = 5000

MAX_DEALS_PER_CHECK = 10


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:

    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Render → Environment Variables."
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

bot = Bot(
    token=BOT_TOKEN
)

dp = Dispatcher()

subscribers = set()

sent_deals_cache = set()

validated_chat_id = None


# ============================================================
# КЛАВИАТУРА
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
# CURL CFFI
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
        "⚠️ curl_cffi отсутствует. "
        "Используем httpx."
    )


# ============================================================
# CACHE
# ============================================================

def load_cache():

    global sent_deals_cache

    try:

        if not os.path.exists(
            CACHE_FILE
        ):

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

        logging.error(
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

        text = str(
            value
        ).strip()

        text = (
            text
            .replace("\xa0", " ")
            .replace("\u202f", " ")
            .replace(",", ".")
        )

        # Удаляем всё кроме цифр,
        # точки и пробела.
        text = re.sub(
            r"[^\d.\s]",
            "",
            text
        )

        text = text.replace(
            " ",
            ""
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
# ИЗВЛЕЧЕНИЕ USD
# ============================================================

def extract_usd_prices(text):

    if not text:

        return []

    text = (
        str(text)
        .replace("\xa0", " ")
        .replace("\u202f", " ")
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
            re.IGNORECASE
        )

        for value in matches:

            number = safe_float(
                value
            )

            if number is None:

                continue

            if number >= 10000:

                continue

            if number not in result:

                result.append(
                    number
                )

    return result


# ============================================================
# ИЗВЛЕЧЕНИЕ KZT
# ============================================================

def extract_kzt_prices(text):

    if not text:

        return []

    text = (
        str(text)
        .replace("\xa0", " ")
        .replace("\u202f", " ")
    )

    patterns = [

        # 12 990 ₸
        r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*₸",

        # ₸ 12 990
        r"₸\s*(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)",

        # 12 990 KZT
        r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*KZT",

        # KZT 12 990
        r"KZT\s*(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)",

        # 12 990 тг
        r"(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)\s*(?:тг|тенге)",

        # тг 12 990
        r"(?:тг|тенге)\s*(\d[\d\s]{0,12}(?:[.,]\d{1,2})?)",

    ]

    result = []

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE
        )

        for value in matches:

            value = str(
                value
            ).replace(
                " ",
                ""
            )

            number = safe_float(
                value
            )

            if number is None:

                continue

            if number < 100:

                continue

            if number > 10000000:

                continue

            if number not in result:

                result.append(
                    number
                )

    return result


# ============================================================
# УНИВЕРСАЛЬНЫЕ ЦЕНЫ
# ============================================================

def extract_prices(text):

    if not text:

        return []

    prices = []

    usd = extract_usd_prices(
        text
    )

    for value in usd:

        prices.append(
            (
                "USD",
                value
            )
        )

    kzt = extract_kzt_prices(
        text
    )

    for value in kzt:

        prices.append(
            (
                "KZT",
                value
            )
        )

    return prices


# ============================================================
# СКИДКА
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

        r"(\d{1,2})\s*%\s*скид",

        r"скидк[аи]?\s*(?:до\s*)?"
        r"(\d{1,2})\s*%",

        r"save\s+(\d{1,2})\s*%",

        r"-\s*(\d{1,2})\s*%",

        r"(\d{1,2})\s*%\s*",

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
# NORMALIZE URL
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

        r"/product/[^/]+/(\d+)/",

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
# CARD PRICE TEXTS
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

        ".product-price-container",

        "[class*='price']",

        "[data-qa*='price']",

        "[data-testid*='price']",

        "[class*='Price']",

        "[class*='price-container']",

        "[class*='PriceContainer']",

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


# ============================================================
# JSON / DATA ATTRIBUTES
# ============================================================

def extract_json_price_data(card):

    prices = []

    discount = None

    try:

        for element in card.find_all():

            # ------------------------------------------------
            # ATTRIBUTES
            # ------------------------------------------------

            for key, value in element.attrs.items():

                if not isinstance(
                    value,
                    str
                ):

                    continue

                key_lower = key.lower()

                value_text = value

                # Цена
                if any(
                    x in key_lower
                    for x in [
                        "price",
                        "amount",
                        "saleprice",
                        "listprice",
                        "currentprice",
                        "originalprice",
                    ]
                ):

                    found = extract_prices(
                        value_text
                    )

                    prices.extend(
                        found
                    )

                # Скидка
                if any(
                    x in key_lower
                    for x in [
                        "discount",
                        "percent",
                        "saving",
                    ]
                ):

                    found_discount = (
                        extract_discount_percent(
                            value_text
                        )
                    )

                    if found_discount:

                        discount = (
                            found_discount
                        )

                # Иногда JSON лежит
                # в data-state / data-product
                if (
                    len(value_text)
                    > 20
                    and (
                        "product"
                        in key_lower
                        or "state"
                        in key_lower
                        or "json"
                        in key_lower
                    )
                ):

                    found = extract_prices(
                        value_text
                    )

                    prices.extend(
                        found
                    )

                    found_discount = (
                        extract_discount_percent(
                            value_text
                        )
                    )

                    if found_discount:

                        discount = (
                            found_discount
                        )

    except Exception:

        pass

    return (
        prices,
        discount
    )


# ============================================================
# SCRIPT / JSON ВНУТРИ СТРАНИЦЫ
# ============================================================

def extract_prices_from_scripts(
    soup
):

    prices = []

    discount = None

    try:

        scripts = soup.find_all(
            "script"
        )

        for script in scripts:

            text = script.string

            if not text:

                text = script.get_text(
                    " ",
                    strip=True
                )

            if not text:

                continue

            # Чтобы не сканировать
            # гигантский JS полностью
            if (
                "price"
                not in text.lower()
                and "discount"
                not in text.lower()
                and "sale" not in text.lower()
            ):

                continue

            found_prices = extract_prices(
                text
            )

            prices.extend(
                found_prices
            )

            found_discount = (
                extract_discount_percent(
                    text
                )
            )

            if found_discount:

                discount = (
                    found_discount
                )

    except Exception:

        pass

    return (
        prices,
        discount
    )


# ============================================================
# PRICE CONVERSION
# ============================================================

def convert_to_usd(
    currency,
    value
):

    if value is None:

        return None

    if currency == "USD":

        return float(
            value
        )

    if currency == "KZT":

        return (
            float(value)
            / KZT_EXCHANGE_RATE
        )

    return None


# ============================================================
# UNIQUE USD PRICES
# ============================================================

def normalize_price_list(
    raw_prices
):

    usd_prices = []

    for currency, value in raw_prices:

        converted = convert_to_usd(
            currency,
            value
        )

        if converted is None:

            continue

        # Защита от мусора
        if converted <= 0:

            continue

        if converted > 10000:

            continue

        rounded = round(
            converted,
            2
        )

        if rounded not in usd_prices:

            usd_prices.append(
                rounded
            )

    return sorted(
        usd_prices
    )


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

    percent = round(
        percent
    )

    if percent < 0:

        return None

    if percent > MAX_DISCOUNT_PERCENT:

        return None

    return percent


# ============================================================
# FIND BEST PRICES
# ============================================================

def find_best_prices(
    price_values
):

    if not price_values:

        return None, None

    unique = sorted(
        set(
            round(
                x,
                2
            )
            for x in price_values
            if x > 0
        )
    )

    if not unique:

        return None, None

    if len(unique) == 1:

        return (
            unique[0],
            None
        )

    current_price = unique[0]

    # Вторая цена может быть
    # старой ценой.
    #
    # Но иногда на странице
    # присутствуют совершенно
    # другие цены.
    #
    # Поэтому используем
    # максимальную как old.
    old_price = unique[-1]

    if old_price <= current_price:

        old_price = None

    return (
        current_price,
        old_price
    )


# ============================================================
# PRODUCT TITLE
# ============================================================

def extract_title(card):

    selectors = [

        ".product-title",

        "[class*='product-title']",

        "[class*='ProductTitle']",

        "[class*='productName']",

        "[class*='ProductName']",

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

    # --------------------------------------------------------
    # aria-label / title
    # --------------------------------------------------------

    try:

        for element in card.find_all(
            ["a", "div", "span"]
        ):

            for attr in [
                "aria-label",
                "title",
                "data-title",
            ]:

                value = element.get(
                    attr
                )

                if value:

                    value = clean_text(
                        value
                    )

                    if len(value) >= 10:

                        return value

    except Exception:

        pass

    # --------------------------------------------------------
    # Ссылки
    # --------------------------------------------------------

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
# PRODUCT LINK
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
    page_prices=None,
    page_discount=None
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
        # RAW PRICES
        # ----------------------------------------------------

        raw_prices = []

        # 1
        price_texts = (
            get_card_price_texts(
                card
            )
        )

        for text in price_texts:

            raw_prices.extend(
                extract_prices(
                    text
                )
            )

        # 2
        json_prices, json_discount = (
            extract_json_price_data(
                card
            )
        )

        raw_prices.extend(
            json_prices
        )

        # 3
        raw_prices.extend(
            extract_prices(
                card_text
            )
        )

        # ----------------------------------------------------
        # НОВОЕ:
        # если локальные цены не найдены,
        # пытаемся взять цены из страницы.
        # ----------------------------------------------------

        if (
            not raw_prices
            and page_prices
        ):

            # НЕ используем page_prices
            # вслепую, потому что они
            # могут принадлежать другому
            # товару.
            #
            # Поэтому только логируем.
            logging.info(
                f"ℹ️ {title[:60]} | "
                "локальных цен нет"
            )

        # ----------------------------------------------------
        # NORMALIZE
        # ----------------------------------------------------

        unique_prices = (
            normalize_price_list(
                raw_prices
            )
        )

        # ----------------------------------------------------
        # PRICE
        # ----------------------------------------------------

        current_price, old_price = (
            find_best_prices(
                unique_prices
            )
        )

        # ----------------------------------------------------
        # DISCOUNT
        # ----------------------------------------------------

        text_discount = (
            extract_discount_percent(
                card_text
            )
        )

        discount_percent = (
            text_discount
            or json_discount
            or page_discount
        )

        # ----------------------------------------------------
        # CALCULATED DISCOUNT
        # ----------------------------------------------------

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

        # ----------------------------------------------------
        # ЕСЛИ ЕСТЬ СКИДКА,
        # НО НЕТ OLD PRICE
        # ----------------------------------------------------

        if (
            discount_percent
            and current_price
            and not old_price
        ):

            if (
                0
                < discount_percent
                < 100
            ):

                old_price = round(
                    current_price
                    / (
                        1
                        - discount_percent / 100
                    ),
                    2
                )

        # ----------------------------------------------------
        # DEBUG
        # ----------------------------------------------------

        logging.info(
            "🔎 CARD #"
            f"{index} | "
            f"{title[:65]} | "
            f"prices={unique_prices[:10]} | "
            f"text_discount={text_discount} | "
            f"json_discount={json_discount} | "
            f"calculated={calculated_discount}"
        )

        # ----------------------------------------------------
        # NO PRICE
        # ----------------------------------------------------

        if not current_price:

            logging.info(
                f"⏭ {title[:65]} | "
                "цена не найдена"
            )

            return None

        # ----------------------------------------------------
        # NO DISCOUNT
        # ----------------------------------------------------

        if not discount_percent:

            logging.info(
                f"⏭ {title[:65]} | "
                f"цена ${current_price:.2f} | "
                "скидка не определена"
            )

            return None

        # ----------------------------------------------------
        # MIN DISCOUNT
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
            "lan=ru-RU&currency=KZT&country=KZ",

        "iherb-pref":
            "lan=ru-RU&currency=KZT&country=KZ",

    }

    # ========================================================
    # CURL CFFI
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
                        "iHerb | "
                        f"{browser} | "
                        f"HTTP "
                        f"{response.status_code}"
                    )

                    if (
                        response.status_code
                        == 200
                        and len(
                            response.text
                        ) > 10000
                    ):

                        logging.info(
                            "✅ iHerb HTML получен: "
                            f"{len(response.text)} символов"
                        )

                        return response.text

                except Exception as e:

                    logging.debug(
                        "curl_cffi error: "
                        f"{e}"
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
                    "httpx | "
                    f"{url} | HTTP "
                    f"{response.status_code}"
                )

                if (
                    response.status_code
                    == 200
                    and len(
                        response.text
                    ) > 10000
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
# FIND PRODUCT CARDS
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

    # ========================================================
    # UNIQUE
    # ========================================================

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

        link_element = (
            card.select_one(
                "a[href]"
            )
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
            + text[:600]
        )

        if key in seen:

            continue

        seen.add(
            key
        )

        unique.append(
            card
        )

    logging.info(
        f"📦 Уникальных карточек: "
        f"{len(unique)}"
    )

    return unique


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
            "html.parser"
        )

        # ----------------------------------------------------
        # SCRIPT DATA
        # ----------------------------------------------------

        page_prices, page_discount = (
            extract_prices_from_scripts(
                soup
            )
        )

        logging.info(
            "📊 Script prices: "
            f"{len(page_prices)}"
        )

        logging.info(
            "📊 Script discount: "
            f"{page_discount}"
        )

        # ----------------------------------------------------
        # CARDS
        # ----------------------------------------------------

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
                index,
                page_prices=None,
                page_discount=None
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

        logging.info(
            "=" * 60
        )

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

    resell_price_kzt = round(
        cost_kzt
        * (
            1
            + MARGIN_MARKUP_PERCENT
            / 100
        )
    )

    # ========================================================
    # ПРИБЫЛЬ
    # ========================================================

    profit_kzt = (
        resell_price_kzt
        - cost_kzt
    )

    # ========================================================
    # FORMAT
    # ========================================================

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
# TARGETS
# ============================================================

def get_targets():

    targets = set()

    # --------------------------------------------------------
    # CHAT_ID из ENV
    # --------------------------------------------------------

    if CHAT_ID:

        targets.add(
            CHAT_ID
        )

    # --------------------------------------------------------
    # Пользователи, которые нажали /start
    # --------------------------------------------------------

    targets.update(
        subscribers
    )

    return targets


# ============================================================
# VALIDATE CHAT
# ============================================================

async def validate_chat_id(
    chat_id
):

    if not chat_id:

        return False

    try:

        chat = await bot.get_chat(
            chat_id
        )

        logging.info(
            "✅ CHAT_ID подтверждён: "
            f"{chat.id} | "
            f"{getattr(chat, 'title', '') or getattr(chat, 'username', '') or getattr(chat, 'first_name', '')}"
        )

        return True

    except Exception as e:

        logging.error(
            "❌ CHAT_ID недействителен: "
            f"{chat_id} | {e}"
        )

        return False


# ============================================================
# SEND DEAL
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

    invalid_targets = []

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

            error_text = str(
                e
            )

            logging.error(
                f"❌ Telegram "
                f"{target_id}: {error_text}"
            )

            # ------------------------------------------------
            # CHAT NOT FOUND
            # ------------------------------------------------

            if (
                "chat not found"
                in error_text.lower()
                or "Bad Request"
                in error_text
                and "chat" in error_text.lower()
            ):

                invalid_targets.append(
                    target_id
                )

    # --------------------------------------------------------
    # Удаляем несуществующие
    # чаты из subscribers
    # --------------------------------------------------------

    for invalid_id in invalid_targets:

        subscribers.discard(
            str(invalid_id)
        )

        logging.warning(
            "🗑 Удалён недействительный "
            f"chat_id: {invalid_id}"
        )

    return success


# ============================================================
# CHECK + NOTIFY
# ============================================================

async def check_and_notify(
    force_send=False
):

    logging.info(
        "=" * 60
    )

    logging.info(
        "🔎 ПРОВЕРКА iHERB"
    )

    logging.info(
        f"🎯 Фильтр скидки: "
        f"{MIN_DISCOUNT_PERCENT}%+"
    )

    logging.info(
        "=" * 60
    )

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
            "ℹ️ Подходящих скидок "
            "не найдено."
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

        deal_id = str(
            deal["id"]
        )

        # ====================================================
        # AUTO MODE
        # ====================================================

        if not force_send:

            if deal_id in sent_deals_cache:

                logging.info(
                    "⏭ Уже отправлялся: "
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

    logging.info(
        "👤 Новый пользователь /start: "
        f"{chat_id}"
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

        "💱 Курс: "
        f"<b>{KZT_EXCHANGE_RATE} ₸/$</b>\n\n"

        "📈 Наценка: "
        f"<b>{MARGIN_MARKUP_PERCENT}%</b>\n\n"

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

    env_chat_text = (
        CHAT_ID
        if CHAT_ID
        else "не задан"
    )

    await message.answer(

        f"📊 <b>СТАТУС БОТА</b>\n\n"

        f"🟢 Telegram: ONLINE\n"

        f"🟢 Автомониторинг: ВКЛЮЧЁН\n"

        f"🔄 Проверка: каждые 5 минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Бренды: "
        f"{escape(brands_text)}\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n\n"

        f"👤 Ваш chat ID: "
        f"<code>{chat_id}</code>\n\n"

        f"⚙️ CHAT_ID Render: "
        f"<code>{escape(env_chat_text)}</code>\n\n"

        f"💾 В памяти: "
        f"{len(sent_deals_cache)} товаров",

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

        "Используйте:\n\n"

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

        logging.exception(
            f"❌ Health Server: {e}"
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    global validated_chat_id

    logging.info(
        "=" * 60
    )

    logging.info(
        "🚀 ЗАПУСК iHERB TELEGRAM BOT"
    )

    logging.info(
        "=" * 60
    )

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

    # ========================================================
    # CACHE
    # ========================================================

    load_cache()

    # ========================================================
    # CHAT ID
    # ========================================================

    if CHAT_ID:

        validated_chat_id = (
            await validate_chat_id(
                CHAT_ID
            )
        )

        if validated_chat_id:

            logging.info(
                "📨 CHAT_ID из Render "
                "готов для отправки."
            )

        else:

            logging.warning(
                "⚠️ CHAT_ID из Render "
                "невалиден."
            )

            logging.warning(
                "⚠️ Используйте /start "
                "в Telegram, чтобы бот "
                "запомнил правильный chat_id."
            )

    else:

        logging.warning(
            "⚠️ CHAT_ID не задан."
        )

        logging.warning(
            "ℹ️ Отправка будет работать "
            "после /start."
        )

    # ========================================================
    # HEALTH
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
