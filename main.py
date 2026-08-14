import asyncio
import json
import logging
import os
import re
from datetime import datetime

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
# iHERB DEAL BOT — ПОЛНОСТЬЮ ОБНОВЛЁННАЯ ВЕРСИЯ
# ============================================================
#
# ВАЖНО ДЛЯ RENDER:
#
# Файл должен называться:
# main.py
#
# Start Command:
# python main.py
#
# Бот:
# 1. Проверяет iHerb сразу после запуска
# 2. Потом проверяет каждые 5 минут
# 3. Ищет реальные цены прямо в HTML
# 4. Поддерживает цены в USD и KZT
# 5. Берёт скидку из текста iHerb
# 6. Не требует старой цены, если iHerb уже указал %
# 7. Не отправляет один и тот же товар повторно
# 8. Показывает живой статус проверки
# 9. Удаляет неправильный CHAT_ID после ошибки
# 10. При ручной проверке отправляет результаты заново
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

KZT_EXCHANGE_RATE = 540
MARGIN_MARKUP_PERCENT = 35

MAX_DEALS_PER_CHECK = 10

CACHE_FILE = "sent_deals.json"
MAX_CACHE_ITEMS = 5000


# ============================================================
# БРЕНДЫ
# ============================================================
#
# [] = ВСЕ БРЕНДЫ
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
# ============================================================

TARGET_BRANDS = []


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден в Render Environment Variables"
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID не найден в Render Environment Variables"
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
        ],
        [
            KeyboardButton(text="🔄 Проверить сейчас"),
        ],
    ],
    resize_keyboard=True,
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
        "⚠️ curl_cffi отсутствует"
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
                "💾 Cache пуст"
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

        else:

            sent_deals_cache = set()

        logging.info(
            "💾 Загружено из cache: "
            f"{len(sent_deals_cache)}"
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка cache: {e}"
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
            .replace("\xa0", "")
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
# ЦЕНЫ
# ============================================================

def extract_prices(text):

    if not text:
        return []

    text = str(text)

    text = text.replace(
        "\xa0",
        " ",
    )

    result = []

    patterns = [

        # USD
        r"(?:US\s*\$|USD|\$)\s*"
        r"(\d+(?:[.,]\d{1,2})?)",

        r"(\d+(?:[.,]\d{1,2})?)"
        r"\s*(?:US\s*\$|USD|\$)",

        # KZT
        r"(?:₸|KZT)\s*"
        r"(\d[\d\s]*(?:[.,]\d{1,2})?)",

        r"(\d[\d\s]*(?:[.,]\d{1,2})?)"
        r"\s*(?:₸|KZT)",

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

        for value in matches:

            value = (
                str(value)
                .replace(" ", "")
            )

            number = safe_float(
                value
            )

            if number is None:
                continue

            if number > 1000000:
                continue

            result.append(
                number
            )

    return result


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

        r"скидк[аи]?"
        r"\s*(?:до\s*)?"
        r"(\d{1,2})\s*%",

        r"save\s+(\d{1,2})\s*%",

        r"-\s*(\d{1,2})\s*%",

        r"(\d{1,2})\s*%\s*",

    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

        for match in matches:

            try:

                value = int(match)

                if (
                    MIN_DISCOUNT_PERCENT
                    <= value
                    <= MAX_DISCOUNT_PERCENT
                ):

                    return value

            except Exception:

                continue

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

    return (
        "https://www.iherb.com/"
        + url.lstrip("/")
    )


# ============================================================
# PRODUCT ID
# ============================================================

def extract_product_id(link):

    if not link:
        return ""

    patterns = [

        r"/(\d+)(?:\?|$)",

        r"/pr/[^/]+/(\d+)",

        r"/product/[^/]+/(\d+)",

    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            link,
            re.IGNORECASE,
        )

        if match:

            return match.group(1)

    return link


# ============================================================
# BRAND
# ============================================================

def find_brand(title):

    if not title:
        return "iHerb"

    if not TARGET_BRANDS:

        return "iHerb"

    title_lower = title.lower()

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:

            return brand

    return ""


# ============================================================
# HTML
# ============================================================

async def get_iherb_html():

    urls = [

        "https://kz.iherb.com/deals",

        "https://www.iherb.com/deals",

        "https://kz.iherb.com/deals?soa=false",

        "https://www.iherb.com/deals?soa=false",

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
            ]:

                try:

                    response = (
                        await asyncio.to_thread(
                            curl_requests.get,
                            url,
                            headers=HEADERS,
                            cookies=cookies,
                            impersonate=browser,
                            timeout=40,
                        )
                    )

                    logging.info(
                        "🌐 iHerb "
                        f"{browser} "
                        f"HTTP {response.status_code} "
                        f"{len(response.text)} chars"
                    )

                    if (
                        response.status_code == 200
                        and len(response.text) > 10000
                    ):

                        logging.info(
                            "✅ HTML iHerb получен"
                        )

                        return response.text

                except Exception as e:

                    logging.warning(
                        f"curl error: {e}"
                    )

    # ========================================================
    # HTTPX
    # ========================================================

    for url in urls:

        try:

            async with httpx.AsyncClient(
                timeout=40,
                headers=HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:

                response = await client.get(
                    url
                )

                logging.info(
                    "🌐 httpx "
                    f"HTTP {response.status_code} "
                    f"{len(response.text)} chars"
                )

                if (
                    response.status_code == 200
                    and len(response.text) > 10000
                ):

                    return response.text

        except Exception as e:

            logging.warning(
                f"httpx error: {e}"
            )

    return ""


# ============================================================
# PRODUCT CARDS
# ============================================================

def find_product_cards(soup):

    selectors = [

        # Основные
        "[data-qa='product-card']",

        ".product-cell-container",

        "[class*='product-cell-container']",

        # Новые варианты
        "[class*='product-card']",

        "[class*='ProductCard']",

        "[class*='product-cell']",

        "[class*='ProductCell']",

        # Дополнительные
        ".product-inner",

        ".product-tile",

    ]

    cards = []

    seen = set()

    for selector in selectors:

        try:

            found = soup.select(
                selector
            )

        except Exception:

            continue

        if not found:
            continue

        logging.info(
            f"🔍 {selector}: "
            f"{len(found)}"
        )

        for card in found:

            text = clean_text(
                card.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) < 20:
                continue

            links = card.select(
                "a[href]"
            )

            link = ""

            if links:

                link = normalize_url(
                    links[0].get(
                        "href",
                        "",
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

    logging.info(
        f"📦 Всего уникальных карточек: "
        f"{len(cards)}"
    )

    return cards


# ============================================================
# TITLE
# ============================================================

def extract_title(card):

    selectors = [

        ".product-title",

        "[class*='product-title']",

        "[class*='ProductTitle']",

        "[class*='product-name']",

        "[class*='ProductName']",

        "[data-qa*='product-name']",

        "h2",

        "h3",

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
                        strip=True,
                    )
                )

                if len(text) >= 5:

                    return text

        except Exception:

            pass

    # alt изображения
    try:

        images = card.select(
            "img[alt]"
        )

        for image in images:

            alt = clean_text(
                image.get(
                    "alt",
                    "",
                )
            )

            if len(alt) >= 10:

                return alt

    except Exception:

        pass

    # Ссылки
    try:

        for link in card.select(
            "a[href]"
        ):

            text = clean_text(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) >= 15:

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

        "a[href*='/deals/']",

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

            if (
                "iherb.com"
                in href
            ):

                return href

        except Exception:

            pass

    return ""


# ============================================================
# PRICE FROM ATTRIBUTES
# ============================================================

def extract_attribute_prices(card):

    prices = []

    discount = None

    try:

        for element in card.find_all():

            for key, value in element.attrs.items():

                if isinstance(
                    value,
                    list,
                ):

                    value = " ".join(
                        str(x)
                        for x in value
                    )

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
                    or "sale"
                    in key_lower
                    or "cost"
                    in key_lower
                ):

                    prices.extend(
                        extract_prices(
                            value
                        )
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

    return prices, discount


# ============================================================
# PRICE ELEMENTS
# ============================================================

def extract_element_prices(card):

    prices = []

    selectors = [

        "[class*='price']",

        "[class*='Price']",

        "[data-qa*='price']",

        "[data-testid*='price']",

        "[aria-label*='$']",

        "[aria-label*='₸']",

    ]

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

                    prices.extend(
                        extract_prices(
                            text
                        )
                    )

                for attr in [
                    "aria-label",
                    "data-price",
                    "data-value",
                    "content",
                    "value",
                ]:

                    value = element.get(
                        attr
                    )

                    if value:

                        prices.extend(
                            extract_prices(
                                str(value)
                            )
                        )

        except Exception:

            pass

    return prices


# ============================================================
# FIND CURRENT + OLD
# ============================================================

def find_prices(card):

    prices = []

    # 1
    p1, _ = extract_attribute_prices(
        card
    )

    prices.extend(
        p1
    )

    # 2
    prices.extend(
        extract_element_prices(
            card
        )
    )

    # 3 полный текст
    text = clean_text(
        card.get_text(
            " ",
            strip=True,
        )
    )

    prices.extend(
        extract_prices(
            text
        )
    )

    # Уникальные
    unique = []

    for price in prices:

        price = round(
            price,
            2,
        )

        if price <= 0:
            continue

        if price not in unique:

            unique.append(
                price
            )

    unique.sort()

    if not unique:

        return None, None

    # Если только одна цена
    if len(unique) == 1:

        return unique[0], None

    return (
        unique[0],
        unique[-1],
    )


# ============================================================
# CONVERT KZT -> USD
# ============================================================

def normalize_price_pair(
    current,
    old,
    card_text,
):

    if not current:
        return None, None

    # Если цены выглядят как KZT
    if current > 1000:

        current_usd = (
            current
            / KZT_EXCHANGE_RATE
        )

        old_usd = None

        if old:

            old_usd = (
                old
                / KZT_EXCHANGE_RATE
            )

        return (
            round(current_usd, 2),
            round(old_usd, 2)
            if old_usd
            else None,
        )

    return (
        round(current, 2),
        round(old, 2)
        if old
        else None,
    )


# ============================================================
# CALCULATE DISCOUNT
# ============================================================

def calculate_discount(
    old,
    current,
):

    if not old or not current:
        return None

    if old <= current:
        return None

    value = (
        1
        - current / old
    ) * 100

    value = round(
        value
    )

    if (
        MIN_DISCOUNT_PERCENT
        <= value
        <= MAX_DISCOUNT_PERCENT
    ):

        return value

    return None


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

        if len(card_text) < 20:
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
        # DISCOUNT
        # ====================================================

        discount = (
            extract_discount_percent(
                card_text
            )
        )

        attribute_prices, attribute_discount = (
            extract_attribute_prices(
                card
            )
        )

        if not discount:
            discount = attribute_discount

        # ====================================================
        # PRICES
        # ====================================================

        current, old = find_prices(
            card
        )

        current_usd, old_usd = (
            normalize_price_pair(
                current,
                old,
                card_text,
            )
        )

        # ====================================================
        # CALCULATED DISCOUNT
        # ====================================================

        calculated = calculate_discount(
            old_usd,
            current_usd,
        )

        if calculated:

            if (
                not discount
                or calculated > discount
            ):

                discount = calculated

        # ====================================================
        # ВАЖНО:
        #
        # iHerb иногда показывает:
        #
        # $27.89 $39.84
        # 30% off
        #
        # Иногда парсер видит только:
        #
        # $27.89
        # 30% off
        #
        # Поэтому если есть скидка,
        # рассчитываем старую цену.
        # ====================================================

        if (
            discount
            and current_usd
            and not old_usd
        ):

            old_usd = round(
                current_usd
                / (
                    1
                    - discount / 100
                ),
                2,
            )

        # ====================================================
        # DEBUG
        # ====================================================

        logging.info(
            f"🔎 #{index} | "
            f"{title[:70]} | "
            f"current={current_usd} | "
            f"old={old_usd} | "
            f"discount={discount}"
        )

        # ====================================================
        # FILTER
        # ====================================================

        if not current_usd:

            logging.info(
                f"⏭ {title[:65]} | "
                "цена не найдена"
            )

            return None

        if not discount:

            logging.info(
                f"⏭ {title[:65]} | "
                "скидка не найдена"
            )

            return None

        if discount < MIN_DISCOUNT_PERCENT:

            return None

        # ====================================================
        # ID
        # ====================================================

        product_id = extract_product_id(
            link
        )

        # ====================================================
        # DEAL
        # ====================================================

        deal = {

            "id": product_id,

            "title": title,

            "brand": (
                brand
                if brand
                else "iHerb"
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
                discount
            ),

            "link": link,

        }

        logging.info(
            "🔥 DEAL | "
            f"-{discount}% | "
            f"${current_usd:.2f} | "
            f"{title[:80]}"
        )

        return deal

    except Exception as e:

        logging.exception(
            f"❌ Ошибка карточки #{index}: {e}"
        )

        return None


# ============================================================
# FETCH DEALS
# ============================================================

async def fetch_iherb_specials():

    logging.info(
        "=" * 60
    )

    logging.info(
        "🔎 НАЧИНАЕМ ПРОВЕРКУ iHERB"
    )

    logging.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        "=" * 60
    )

    html = await get_iherb_html()

    if not html:

        logging.error(
            "❌ HTML iHerb не получен"
        )

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

        # ====================================================
        # UNIQUE
        # ====================================================

        unique = {}

        for deal in deals:

            unique[
                deal["id"]
            ] = deal

        deals = list(
            unique.values()
        )

        # ====================================================
        # SORT
        # ====================================================

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                x["discount_price_usd"],
            ),
            reverse=True,
        )

        logging.info(
            "=" * 60
        )

        logging.info(
            "🔥 РЕЗУЛЬТАТ ПРОВЕРКИ"
        )

        logging.info(
            "🔥 Подходящих товаров: "
            f"{len(deals)}"
        )

        for deal in deals[:20]:

            logging.info(
                f"💊 -{deal['discount_percent']}% | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:70]}"
            )

        logging.info(
            "=" * 60
        )

        return deals

    except Exception as e:

        logging.exception(
            f"❌ Ошибка парсинга: {e}"
        )

        return []


# ============================================================
# FORMAT TELEGRAM
# ============================================================

def format_deal_message(
    deal
):

    title = (
        deal["title"]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    brand = (
        deal["brand"]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
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
        .replace(",", " ")
    )

    sale_str = (
        f"{sale_kzt:,}"
        .replace(",", " ")
    )

    profit_str = (
        f"{profit_kzt:,}"
        .replace(",", " ")
    )

    now = datetime.now().strftime(
        "%d.%m.%Y %H:%M"
    )

    message = (

        "🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n"
        "\n"

        f"🏷 <b>Бренд:</b> {brand}\n"
        "\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n"
        "\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n"
        "\n"

        f"💰 <b>Цена iHerb:</b>\n"
        f"<s>${old_usd:.2f}</s> "
        f"➡️ <b>${current_usd:.2f}</b>\n"
        "\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {cost_str} ₸\n"
        "\n"

        f"🏪 <b>Продажа:</b> "
        f"{sale_str} ₸\n"
        "\n"

        f"📈 <b>Прибыль:</b> "
        f"+{profit_str} ₸\n"
        "\n"

        f"💱 <b>Курс:</b> "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"
        "\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{now}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 Открыть товар на iHerb",
                    url=deal["link"],
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
                "✅ Отправлено: "
                f"{target_id} | "
                f"{deal['title'][:70]}"
            )

            success = True

            await asyncio.sleep(
                1
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
                f"⏳ Flood control: "
                f"{retry_after} сек."
            )

            await asyncio.sleep(
                retry_after + 2
            )

        except Exception as e:

            error_text = str(e)

            logging.error(
                f"❌ Telegram {target_id}: "
                f"{error_text}"
            )

            # =================================================
            # ЕСЛИ CHAT NOT FOUND
            # Больше не пытаемся слать туда
            # =================================================

            if (
                "chat not found"
                in error_text.lower()
            ):

                if target_id in subscribers:

                    subscribers.discard(
                        target_id
                    )

                logging.warning(
                    "🗑 Неверный Telegram CHAT_ID "
                    f"удалён из subscribers: "
                    f"{target_id}"
                )

    return success


# ============================================================
# CHECK
# ============================================================

async def check_and_notify(
    force_send=False,
    requester_chat_id=None,
):

    start_time = datetime.now()

    logging.info(
        ""
    )

    logging.info(
        "🚀 НОВЫЙ ЦИКЛ ПРОВЕРКИ"
    )

    logging.info(
        f"🕐 Время: "
        f"{start_time.strftime('%d.%m.%Y %H:%M:%S')}"
    )

    deals = await fetch_iherb_specials()

    if not deals:

        logging.info(
            "ℹ️ Подходящих скидок сейчас нет"
        )

        if requester_chat_id:

            try:

                await bot.send_message(
                    chat_id=requester_chat_id,
                    text=(
                        "🔎 <b>Проверка завершена</b>\n\n"
                        "К сожалению, сейчас не найдено "
                        f"товаров со скидкой "
                        f"<b>{MIN_DISCOUNT_PERCENT}%+</b>.\n\n"
                        "⏱ Автоматический мониторинг "
                        "продолжается."
                    ),
                    parse_mode=ParseMode.HTML,
                )

            except Exception as e:

                logging.error(
                    f"Ошибка сообщения: {e}"
                )

        return

    targets = get_targets()

    if not targets:

        logging.warning(
            "⚠️ Получателей нет"
        )

        return

    sent_count = 0

    for deal in deals:

        deal_id = str(
            deal["id"]
        )

        # ====================================================
        # АВТОМАТИКА
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
        f"📤 Новых отправлено: "
        f"{sent_count}"
    )

    # ========================================================
    # ЕСЛИ РУЧНАЯ ПРОВЕРКА
    # ========================================================

    if requester_chat_id:

        try:

            if force_send:

                await bot.send_message(
                    chat_id=requester_chat_id,
                    text=(
                        "✅ <b>Проверка завершена</b>\n\n"
                        f"🔥 Найдено подходящих товаров: "
                        f"<b>{len(deals)}</b>\n"
                        f"📤 Отправлено: "
                        f"<b>{sent_count}</b>\n\n"
                        "🤖 Автомониторинг продолжает "
                        "работать каждые 5 минут."
                    ),
                    parse_mode=ParseMode.HTML,
                )

        except Exception as e:

            logging.error(
                f"Ошибка итогового сообщения: {e}"
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

        "🔥 Я постоянно отслеживаю "
        "скидки iHerb.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        "⏱ Автопроверка: "
        "<b>каждые 5 минут</b>\n\n"

        "Новые товары будут приходить "
        "автоматически.\n\n"

        "👇 Используйте кнопки ниже.",

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
@dp.message(
    F.text == "🔄 Проверить сейчас"
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
        "🔎 <b>Проверяю iHerb...</b>\n\n"
        "⏳ Подождите несколько секунд.",
        parse_mode=ParseMode.HTML,
    )

    await check_and_notify(
        force_send=True,
        requester_chat_id=chat_id,
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

        "📊 <b>СТАТУС iHERB BOT</b>\n\n"

        "🟢 Telegram: ONLINE\n"

        "🟢 Мониторинг: ВКЛЮЧЁН\n"

        "🟢 iHerb parser: ONLINE\n"

        "🔄 Интервал: каждые 5 минут\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Бренды: "
        f"{brands_text}\n\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n\n"

        f"💾 Уже отправлено: "
        f"{len(sent_deals_cache)}\n\n"

        f"👥 Подписчиков: "
        f"{len(subscribers)}\n\n"

        "🤖 Бот продолжает работать "
        "автоматически.",

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

        f"🔥 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n\n"

        "Используйте кнопки:\n\n"

        "🔥 <b>Получить скидки</b>\n"
        "— найти скидки прямо сейчас\n\n"

        "🔄 <b>Проверить сейчас</b>\n"
        "— ручной запуск проверки\n\n"

        "ℹ️ <b>Статус</b>\n"
        "— посмотреть состояние бота.",

        reply_markup=main_keyboard,

        parse_mode=ParseMode.HTML,
    )


# ============================================================
# SCHEDULER
# ============================================================

async def scheduler():

    logging.info(
        "=" * 60
    )

    logging.info(
        "🤖 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН"
    )

    logging.info(
        "⚡ Первая проверка запускается СРАЗУ"
    )

    logging.info(
        f"⏱ Интервал: "
        f"{CHECK_INTERVAL_SECONDS} секунд"
    )

    logging.info(
        "=" * 60
    )

    # ========================================================
    # ПЕРВАЯ ПРОВЕРКА
    # ========================================================

    try:

        await check_and_notify(
            force_send=False
        )

    except Exception as e:

        logging.exception(
            f"❌ Ошибка первой проверки: {e}"
        )

    # ========================================================
    # БЕСКОНЕЧНЫЙ ЦИКЛ
    # ========================================================

    while True:

        try:

            logging.info(
                ""
            )

            logging.info(
                f"💤 Следующая проверка через "
                f"{CHECK_INTERVAL_SECONDS // 60} минут"
            )

            await asyncio.sleep(
                CHECK_INTERVAL_SECONDS
            )

            logging.info(
                ""
            )

            logging.info(
                "⏰ ИНТЕРВАЛ ЗАКОНЧИЛСЯ"
            )

            logging.info(
                "🔄 ЗАПУСКАЕМ НОВУЮ ПРОВЕРКУ"
            )

            await check_and_notify(
                force_send=False
            )

            logging.info(
                "✅ ЦИКЛ ПРОВЕРКИ ЗАВЕРШЁН"
            )

        except asyncio.CancelledError:

            logging.info(
                "🛑 Scheduler остановлен"
            )

            break

        except Exception as e:

            logging.exception(
                f"❌ Ошибка scheduler: {e}"
            )

            logging.info(
                "🔄 Повтор через 30 секунд"
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
                "10000",
            )
        )

        app = web.Application()

        async def home(
            request
        ):

            return web.Response(
                text=(
                    "iHerb Deal Bot is running!"
                )
            )

        async def health(
            request
        ):

            return web.Response(
                text="OK"
            )

        async def status(
            request
        ):

            return web.json_response(
                {
                    "status": "online",
                    "bot": "iHerb Deal Bot",
                    "interval": CHECK_INTERVAL_SECONDS,
                    "min_discount": MIN_DISCOUNT_PERCENT,
                    "subscribers": len(subscribers),
                    "cache": len(
                        sent_deals_cache
                    ),
                    "time": datetime.now().isoformat(),
                }
            )

        app.router.add_get(
            "/",
            home,
        )

        app.router.add_get(
            "/health",
            health,
        )

        app.router.add_get(
            "/status",
            status,
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
            f"🌐 Render Health Server: "
            f"0.0.0.0:{port}"
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
        ""
    )

    logging.info(
        "=" * 70
    )

    logging.info(
        "🚀 ЗАПУСК iHERB TELEGRAM DEAL BOT"
    )

    logging.info(
        "=" * 70
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

    logging.info(
        f"⏱ Проверка каждые: "
        f"{CHECK_INTERVAL_SECONDS // 60} минут"
    )

    if TARGET_BRANDS:

        logging.info(
            "🏷 Бренды: "
            + ", ".join(
                TARGET_BRANDS
            )
        )

    else:

        logging.info(
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
                "🤖 Telegram polling запущен"
            )

            await dp.start_polling(
                bot
            )

            break

        except Exception as e:

            error_text = str(e)

            logging.error(
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

                logging.warning(
                    "⚠️ Telegram Conflict"
                )

                await asyncio.sleep(
                    10
                )

            elif (
                "Unauthorized"
                in error_text
            ):

                logging.error(
                    "❌ Неверный BOT_TOKEN"
                )

                await asyncio.sleep(
                    30
                )

            else:

                logging.warning(
                    "🔄 Перезапуск Telegram "
                    "через 5 секунд"
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
            "🛑 Бот остановлен"
        )
