import asyncio
import json
import logging
import os
import re
import time
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

# Максимальная скидка, которую принимаем
MAX_DISCOUNT_PERCENT = 90

# ------------------------------------------------------------
# БРЕНДЫ
# ------------------------------------------------------------
# Если хотите искать только эти бренды — оставляем True.
#
# Если хотите ВСЕ товары iHerb со скидкой 20%+,
# поставьте:
#
# ONLY_TARGET_BRANDS = False
#
# ------------------------------------------------------------

ONLY_TARGET_BRANDS = True

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

# Максимум новых товаров за одну автоматическую проверку
MAX_DEALS_PER_CHECK = 10

# Максимум товаров при ручной кнопке
MAX_MANUAL_DEALS = 30


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ============================================================
# ПРОВЕРКА ENV
# ============================================================

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Добавьте его в Render → Environment Variables."
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID не найден. "
        "Добавьте его в Render → Environment Variables."
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
        "Будет использоваться httpx."
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
            "💾 Загружено ранее отправленных: "
            f"{len(sent_deals_cache)}"
        )

    except Exception as e:

        logging.error(
            f"❌ Ошибка загрузки cache: {e}"
        )

        sent_deals_cache = set()


def save_cache():

    try:

        # Оставляем последние 5000 ID
        data = list(
            sent_deals_cache
        )[-5000:]

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
        "en-US;q=0.8,"
        "en;q=0.7"
    ),

    "Accept": (
        "text/html,"
        "application/xhtml+xml,"
        "application/xml;q=0.9,"
        "image/avif,"
        "image/webp,"
        "image/apng,"
        "*/*;q=0.8"
    ),

    "Cache-Control": "no-cache",

    "Pragma": "no-cache",

    "Upgrade-Insecure-Requests": "1",

    "Sec-Fetch-Dest": "document",

    "Sec-Fetch-Mode": "navigate",

    "Sec-Fetch-Site": "none",

    "Sec-Fetch-User": "?1",
}


# ============================================================
# URLS IHERB
# ============================================================

IHERB_URLS = [
    "https://kz.iherb.com/deals",
    "https://kz.iherb.com/c/specials",
    "https://www.iherb.com/deals",
    "https://www.iherb.com/c/specials",
]


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def clean_text(text):

    if not text:
        return ""

    text = str(text)

    text = text.replace(
        "\xa0",
        " "
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# ЧИСЛО
# ============================================================

def safe_float(value):

    if value is None:
        return None

    try:

        value = str(value)

        value = value.replace(
            "\xa0",
            ""
        )

        value = value.replace(
            " ",
            ""
        )

        value = value.replace(
            ",",
            "."
        )

        value = re.sub(
            r"[^\d.]",
            "",
            value
        )

        if not value:
            return None

        number = float(value)

        if number <= 0:
            return None

        return number

    except Exception:

        return None


# ============================================================
# ИЗВЛЕЧЕНИЕ ЦЕН
# ============================================================

def extract_money_values(text):

    """
    Очень осторожно извлекаем именно денежные значения.

    Поддерживаем:
    $12.99
    US$12.99
    USD 12.99
    12.99 $
    12,99 $
    """

    if not text:
        return []

    text = clean_text(text)

    values = []

    patterns = [

        # $12.99
        r"\$\s*(\d+(?:[.,]\d{1,2})?)",

        # US$12.99
        r"US\$\s*(\d+(?:[.,]\d{1,2})?)",

        # USD 12.99
        r"USD\s*(\d+(?:[.,]\d{1,2})?)",

        # 12.99$
        r"(\d+(?:[.,]\d{1,2})?)\s*\$",

        # 12.99 USD
        r"(\d+(?:[.,]\d{1,2})?)\s*USD",
    ]

    for pattern in patterns:

        matches = re.findall(
            pattern,
            text,
            re.IGNORECASE,
        )

        for item in matches:

            number = safe_float(
                item
            )

            if number is None:
                continue

            # Реалистичный диапазон цены
            if 0.01 <= number <= 10000:

                if number not in values:

                    values.append(
                        number
                    )

    return values


# ============================================================
# ИЗВЛЕЧЕНИЕ ПРОЦЕНТА
# ============================================================

def extract_discount_percent(text):

    """
    Ищем только явные обозначения скидки.

    Примеры:

    20% off
    25% off
    30% OFF
    -30%
    Скидка 30%
    Скидка: 30%
    30% скидка
    Save 30%
    """

    if not text:
        return None

    text = clean_text(
        text
    )

    patterns = [

        # 30% off
        r"(?<!\d)(\d{1,2})\s*%\s*off\b",

        # 30% OFF
        r"(?<!\d)(\d{1,2})\s*%\s*OFF\b",

        # Save 30%
        r"\bsave\s+(\d{1,2})\s*%",

        # -30%
        r"-\s*(\d{1,2})\s*%",

        # скидка 30%
        r"скидк\w*\s*:?\s*(?:до\s*)?(\d{1,2})\s*%",

        # 30% скидка
        r"(\d{1,2})\s*%\s*скидк",

        # экономия 30%
        r"эконом\w*\s*:?\s*(\d{1,2})\s*%",
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
# РАСЧЁТ СКИДКИ ПО ЦЕНАМ
# ============================================================

def calculate_discount(
    old_price,
    new_price,
):

    if not old_price or not new_price:
        return None

    if old_price <= 0:
        return None

    if new_price <= 0:
        return None

    if old_price <= new_price:
        return None

    discount = (
        1
        - (
            new_price
            / old_price
        )
    ) * 100

    discount = round(
        discount
    )

    if discount < 1:
        return None

    if discount > 99:
        return None

    return discount


# ============================================================
# НОРМАЛИЗАЦИЯ URL
# ============================================================

def normalize_url(url):

    if not url:
        return ""

    url = str(
        url
    ).strip()

    if url.startswith("//"):

        return (
            "https:"
            + url
        )

    if url.startswith("/"):

        return urljoin(
            "https://www.iherb.com",
            url
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

    try:

        parsed = urlparse(
            link
        )

        path = parsed.path.rstrip(
            "/"
        )

        # Последнее число
        match = re.search(
            r"/(\d+)$",
            path
        )

        if match:

            return match.group(1)

        # /pr/name/123
        match = re.search(
            r"/(\d+)(?:/)?$",
            path
        )

        if match:

            return match.group(1)

        # Если ID нет — используем URL
        return link.lower()

    except Exception:

        return link.lower()


# ============================================================
# ПОИСК БРЕНДА
# ============================================================

def find_brand(title):

    title_lower = (
        title.lower()
    )

    for brand in TARGET_BRANDS:

        if brand.lower() in title_lower:

            return brand

    return ""


# ============================================================
# ПРОВЕРКА БРЕНДА
# ============================================================

def brand_allowed(
    title
):

    if not ONLY_TARGET_BRANDS:

        return True

    return bool(
        find_brand(
            title
        )
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

        for target_url in IHERB_URLS:

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
                        timeout=25,
                    )

                    status = (
                        response.status_code
                    )

                    logging.info(
                        f"iHerb | "
                        f"{impersonate} | "
                        f"HTTP {status} | "
                        f"{target_url}"
                    )

                    if (
                        status == 200
                        and len(
                            response.text
                        ) > 5000
                    ):

                        logging.info(
                            "✅ iHerb HTML получен: "
                            f"{len(response.text)} символов"
                        )

                        return response.text

                except Exception as e:

                    logging.debug(
                        "curl_cffi ошибка: "
                        f"{e}"
                    )

    # ========================================================
    # HTTPX
    # ========================================================

    for target_url in IHERB_URLS:

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
                    f"HTTP {response.status_code} | "
                    f"{target_url}"
                )

                if (
                    response.status_code == 200
                    and len(
                        response.text
                    ) > 5000
                ):

                    logging.info(
                        "✅ HTML получен через httpx: "
                        f"{len(response.text)} символов"
                    )

                    return response.text

        except Exception as e:

            logging.debug(
                f"httpx ошибка: {e}"
            )

    logging.error(
        "❌ Не удалось получить iHerb."
    )

    return ""


# ============================================================
# ПОИСК КАРТОЧЕК
# ============================================================

def find_product_cards(
    soup
):

    selectors = [

        ".product-cell-container",

        "[data-qa='product-card']",

        "[data-testid='product-card']",

        ".product-card",

        ".product-inner",

        ".product-tile",

        "[class*='product-cell-container']",

        "[class*='product-card']",

        "[class*='product-tile']",
    ]

    all_cards = []

    used_keys = set()

    for selector in selectors:

        try:

            cards = soup.select(
                selector
            )

            if not cards:
                continue

            logging.info(
                f"🔍 {selector}: "
                f"{len(cards)} карточек"
            )

            for card in cards:

                text = clean_text(
                    card.get_text(
                        " ",
                        strip=True,
                    )
                )

                if not text:
                    continue

                key = text[:700]

                if key in used_keys:
                    continue

                used_keys.add(
                    key
                )

                all_cards.append(
                    card
                )

            # Нам нужна первая нормальная
            # коллекция карточек.
            if len(all_cards) >= 20:

                break

        except Exception as e:

            logging.debug(
                f"Ошибка selector "
                f"{selector}: {e}"
            )

    return all_cards


# ============================================================
# ПОИСК НАЗВАНИЯ
# ============================================================

def extract_title(
    card
):

    selectors = [

        ".product-title",

        "[class*='product-title']",

        "[data-qa*='product-title']",

        "[data-testid*='product-title']",

        "a[href*='/pr/']",

        "a[href*='/product/']",

    ]

    for selector in selectors:

        try:

            element = card.select_one(
                selector
            )

            if not element:
                continue

            text = clean_text(
                element.get_text(
                    " ",
                    strip=True,
                )
            )

            if len(text) >= 3:

                return text

        except Exception:
            pass

    return ""


# ============================================================
# ПОИСК ССЫЛКИ
# ============================================================

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
# ПОИСК ЦЕН В CARD
# ============================================================

def extract_card_prices(
    card
):

    prices = []

    # --------------------------------------------------------
    # Сначала специальные price-классы
    # --------------------------------------------------------

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

    seen_text = set()

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

                if not text:
                    continue

                if text in seen_text:
                    continue

                seen_text.add(
                    text
                )

                values = (
                    extract_money_values(
                        text
                    )
                )

                for value in values:

                    if value not in prices:

                        prices.append(
                            value
                        )

        except Exception:
            pass

    # --------------------------------------------------------
    # Если price-классы не помогли,
    # ищем в полном тексте
    # --------------------------------------------------------

    if len(prices) < 2:

        full_text = clean_text(
            card.get_text(
                " ",
                strip=True,
            )
        )

        values = (
            extract_money_values(
                full_text
            )
        )

        for value in values:

            if value not in prices:

                prices.append(
                    value
                )

    return prices


# ============================================================
# ПОИСК ЦЕНЫ В JSON / HTML
# ============================================================

def extract_prices_from_raw_html(
    card
):

    """
    Дополнительный резервный метод.

    Ищем цены прямо в HTML карточки.
    """

    try:

        raw = str(
            card
        )

    except Exception:

        return []

    prices = (
        extract_money_values(
            raw
        )
    )

    return prices


# ============================================================
# ОПРЕДЕЛЕНИЕ ЦЕН
# ============================================================

def determine_prices(
    card,
    card_text,
    explicit_discount
):

    prices = (
        extract_card_prices(
            card
        )
    )

    # Резерв
    if len(prices) < 2:

        raw_prices = (
            extract_prices_from_raw_html(
                card
            )
        )

        for price in raw_prices:

            if price not in prices:

                prices.append(
                    price
                )

    # --------------------------------------------------------
    # Убираем явно нереальные значения
    # --------------------------------------------------------

    prices = [
        p
        for p in prices
        if 0.01 <= p <= 10000
    ]

    prices = sorted(
        set(prices)
    )

    if not prices:

        return None, None, None

    # --------------------------------------------------------
    # СКИДКА ИЗ ТЕКСТА + ТОЛЬКО ОДНА ЦЕНА
    # --------------------------------------------------------

    if (
        explicit_discount
        and len(prices) >= 1
    ):

        new_price = min(
            prices
        )

        old_price = round(
            new_price
            / (
                1
                - explicit_discount / 100
            ),
            2
        )

        return (
            old_price,
            new_price,
            explicit_discount,
        )

    # --------------------------------------------------------
    # ДВЕ И БОЛЕЕ ЦЕНЫ
    # --------------------------------------------------------

    if len(prices) >= 2:

        new_price = min(
            prices
        )

        higher = [
            p
            for p in prices
            if p > new_price
        ]

        if higher:

            old_price = max(
                higher
            )

            calculated_discount = (
                calculate_discount(
                    old_price,
                    new_price,
                )
            )

            if calculated_discount:

                return (
                    old_price,
                    new_price,
                    calculated_discount,
                )

    # --------------------------------------------------------
    # Если есть только одна цена
    # и процент не найден — пропускаем
    # --------------------------------------------------------

    return None, None, None


# ============================================================
# ПАРСИНГ IHERB
# ============================================================

async def fetch_iherb_specials():

    logging.info("=" * 60)

    logging.info(
        "🔎 Начинаем проверку iHerb..."
    )

    logging.info(
        f"🎯 Минимальная скидка: "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        f"🏷 Только целевые бренды: "
        f"{ONLY_TARGET_BRANDS}"
    )

    html = await get_iherb_html()

    if not html:

        return []

    deals = []

    try:

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        cards = find_product_cards(
            soup
        )

        logging.info(
            f"📦 Уникальных карточек: "
            f"{len(cards)}"
        )

        if not cards:

            logging.warning(
                "⚠️ Карточки товаров не найдены."
            )

            return []

        # ====================================================
        # СТАТИСТИКА ДИАГНОСТИКИ
        # ====================================================

        total_cards = 0
        no_title = 0
        no_link = 0
        wrong_brand = 0
        no_discount = 0
        low_discount = 0
        no_price = 0
        accepted = 0

        # ====================================================
        # КАРТОЧКИ
        # ====================================================

        for index, card in enumerate(
            cards,
            start=1,
        ):

            total_cards += 1

            try:

                card_text = clean_text(
                    card.get_text(
                        " ",
                        strip=True,
                    )
                )

                if not card_text:

                    continue

                # ------------------------------------------------
                # НАЗВАНИЕ
                # ------------------------------------------------

                title = extract_title(
                    card
                )

                if not title:

                    no_title += 1

                    logging.info(
                        f"⛔ Карточка #{index}: "
                        "нет названия"
                    )

                    continue

                # ------------------------------------------------
                # БРЕНД
                # ------------------------------------------------

                brand = find_brand(
                    title
                )

                if not brand_allowed(
                    title
                ):

                    wrong_brand += 1

                    logging.debug(
                        f"⏭ Не наш бренд: "
                        f"{title[:80]}"
                    )

                    continue

                # ------------------------------------------------
                # LINK
                # ------------------------------------------------

                link = extract_link(
                    card
                )

                if not link:

                    no_link += 1

                    logging.debug(
                        f"⛔ Нет ссылки: "
                        f"{title[:80]}"
                    )

                    continue

                # ------------------------------------------------
                # ПРОЦЕНТ
                # ------------------------------------------------

                explicit_discount = (
                    extract_discount_percent(
                        card_text
                    )
                )

                # ------------------------------------------------
                # ЦЕНЫ
                # ------------------------------------------------

                (
                    old_price,
                    new_price,
                    discount_percent,
                ) = determine_prices(
                    card,
                    card_text,
                    explicit_discount,
                )

                # ------------------------------------------------
                # НЕТ ЦЕН
                # ------------------------------------------------

                if (
                    old_price is None
                    or new_price is None
                ):

                    no_price += 1

                    logging.debug(
                        f"⛔ Нет цены: "
                        f"{title[:80]}"
                    )

                    continue

                # ------------------------------------------------
                # НЕТ СКИДКИ
                # ------------------------------------------------

                if not discount_percent:

                    no_discount += 1

                    logging.debug(
                        f"⛔ Нет скидки: "
                        f"{title[:80]}"
                    )

                    continue

                # ------------------------------------------------
                # ПРОВЕРКА 20%+
                # ------------------------------------------------

                if (
                    discount_percent
                    < MIN_DISCOUNT_PERCENT
                ):

                    low_discount += 1

                    logging.debug(
                        f"⏭ Скидка "
                        f"{discount_percent}% "
                        f"< {MIN_DISCOUNT_PERCENT}%: "
                        f"{title[:80]}"
                    )

                    continue

                # ------------------------------------------------
                # ПРОВЕРКА ДИАПАЗОНА
                # ------------------------------------------------

                if (
                    discount_percent
                    > MAX_DISCOUNT_PERCENT
                ):

                    continue

                # ------------------------------------------------
                # ПРОВЕРКА ЦЕН
                # ------------------------------------------------

                if old_price <= new_price:

                    continue

                # ------------------------------------------------
                # ID
                # ------------------------------------------------

                product_id = (
                    extract_product_id(
                        link
                    )
                )

                # ------------------------------------------------
                # DEAL
                # ------------------------------------------------

                deal = {

                    "id": product_id,

                    "title": title,

                    "brand": brand
                    if brand
                    else "iHerb",

                    "orig_price_usd": round(
                        old_price,
                        2,
                    ),

                    "discount_price_usd": round(
                        new_price,
                        2,
                    ),

                    "discount_percent": int(
                        discount_percent
                    ),

                    "link": link,

                    "found_at":
                        datetime.now().isoformat(),
                }

                deals.append(
                    deal
                )

                accepted += 1

                logging.info(
                    "🔥 ПОДХОДИТ | "
                    f"-{discount_percent}% | "
                    f"{deal['brand']} | "
                    f"${new_price:.2f} | "
                    f"{title[:80]}"
                )

            except Exception as e:

                logging.debug(
                    f"Ошибка карточки "
                    f"#{index}: {e}"
                )

        # ====================================================
        # УДАЛЕНИЕ ДУБЛЕЙ
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
        # ====================================================

        deals.sort(
            key=lambda x: (
                x["discount_percent"],
                -x["discount_price_usd"],
            ),
            reverse=True,
        )

        # ====================================================
        # ДИАГНОСТИКА
        # ====================================================

        logging.info("=" * 60)

        logging.info(
            "📊 ДИАГНОСТИКА ПАРСЕРА"
        )

        logging.info(
            f"Карточек: {total_cards}"
        )

        logging.info(
            f"Без названия: {no_title}"
        )

        logging.info(
            f"Без ссылки: {no_link}"
        )

        logging.info(
            f"Другой бренд: {wrong_brand}"
        )

        logging.info(
            f"Без цены: {no_price}"
        )

        logging.info(
            f"Без скидки: {no_discount}"
        )

        logging.info(
            f"Скидка ниже {MIN_DISCOUNT_PERCENT}%: "
            f"{low_discount}"
        )

        logging.info(
            f"ПОДХОДЯЩИХ: {accepted}"
        )

        logging.info("=" * 60)

        logging.info(
            f"🔥 ИТОГО скидок 20%+: "
            f"{len(deals)}"
        )

        # ====================================================
        # ПЕРВЫЕ 30 В ЛОГ
        # ====================================================

        for deal in deals[:30]:

            logging.info(
                "💊 "
                f"-{deal['discount_percent']}% | "
                f"{deal['brand']} | "
                f"${deal['discount_price_usd']:.2f} | "
                f"{deal['title'][:70]}"
            )

        return deals

    except Exception as e:

        logging.exception(
            f"❌ Ошибка парсинга iHerb: {e}"
        )

        return []


# ============================================================
# ФОРМАТИРОВАНИЕ ЦЕНЫ
# ============================================================

def format_kzt(
    value
):

    try:

        return (
            f"{round(value):,}"
            .replace(",", " ")
        )

    except Exception:

        return "0"


# ============================================================
# TELEGRAM СООБЩЕНИЕ
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

    new_usd = (
        deal["discount_price_usd"]
    )

    percent = (
        deal["discount_percent"]
    )

    link = deal["link"]

    # ========================================================
    # ЗАКУПКА
    # ========================================================

    cost_kzt = (
        new_usd
        * KZT_EXCHANGE_RATE
    )

    # ========================================================
    # ПРОДАЖА
    # ========================================================

    sale_price_kzt = (
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
        sale_price_kzt
        - cost_kzt
    )

    # ========================================================
    # ЭКОНОМИЯ
    # ========================================================

    saving_usd = (
        old_usd
        - new_usd
    )

    saving_kzt = (
        saving_usd
        * KZT_EXCHANGE_RATE
    )

    message = (

        "🔥 <b>СКИДКА iHERB 20%+</b> 🔥\n"
        "\n"

        f"🏷 <b>Бренд:</b> {brand}\n"
        "\n"

        f"💊 <b>Товар:</b>\n"
        f"{title}\n"
        "\n"

        f"📉 <b>СКИДКА: -{percent}%</b>\n"
        "\n"

        f"💰 <b>Цена до скидки:</b> "
        f"<s>${old_usd:.2f}</s>\n"

        f"🔥 <b>Цена сейчас:</b> "
        f"<b>${new_usd:.2f}</b>\n"

        f"💵 <b>Экономия:</b> "
        f"${saving_usd:.2f}\n"
        "\n"

        f"🇰🇿 <b>Закуп:</b> "
        f"≈ {format_kzt(cost_kzt)} ₸\n"

        f"🏪 <b>Цена продажи:</b> "
        f"{format_kzt(sale_price_kzt)} ₸\n"

        f"📈 <b>Прибыль:</b> "
        f"+{format_kzt(profit_kzt)} ₸\n"
        "\n"

        f"💱 Курс: "
        f"1 USD = {KZT_EXCHANGE_RATE} ₸\n"

        f"📈 Наценка: "
        f"+{MARGIN_MARKUP_PERCENT}%\n"
        "\n"

        f"⏰ <b>Обнаружено:</b> "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛒 ОТКРЫТЬ НА iHERB",
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
# ПОЛУЧАТЕЛИ
# ============================================================

def get_targets():

    targets = set()

    if CHAT_ID:

        targets.add(
            str(CHAT_ID)
        )

    targets.update(
        str(x)
        for x in subscribers
    )

    return targets


# ============================================================
# ОТПРАВКА
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
                "✅ Отправлено | "
                f"{target_id} | "
                f"-{deal['discount_percent']}% | "
                f"{deal['title'][:60]}"
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
                    "❌ Ошибка повторной отправки: "
                    f"{retry_error}"
                )

        except Exception as e:

            logging.error(
                f"❌ Telegram ошибка "
                f"{target_id}: {e}"
            )

    return success


# ============================================================
# ПРОВЕРКА И ОТПРАВКА
# ============================================================

async def check_and_notify(
    force_send=False,
):

    logging.info("=" * 60)

    logging.info(
        "🔎 ПРОВЕРКА iHERB"
    )

    logging.info(
        f"🎯 Ищем скидки "
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
            "ℹ️ Подходящих скидок "
            "не найдено."
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

    limit = (
        MAX_MANUAL_DEALS
        if force_send
        else MAX_DEALS_PER_CHECK
    )

    for deal in deals:

        deal_id = str(
            deal["id"]
        )

        # ----------------------------------------------------
        # АВТОМАТИЧЕСКИЙ РЕЖИМ
        # ----------------------------------------------------

        if not force_send:

            if deal_id in sent_deals_cache:

                logging.info(
                    "⏭ Уже отправлялся: "
                    f"{deal['title'][:60]}"
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

        if sent_count >= limit:

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
    message,
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
        "скидки iHerb.\n\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        "⏱ Проверка: "
        "<b>каждые 5 минут</b>\n\n"

        "🏷 Отслеживаемые бренды:\n"

        + "\n".join(
            f"• {escape(brand)}"
            for brand in TARGET_BRANDS
        )

        + "\n\n"
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
    message,
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "🔎 Проверяю iHerb прямо сейчас...\n"
        f"🎯 Ищу скидки от {MIN_DISCOUNT_PERCENT}%+"
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
    message,
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    brands_text = "\n".join(
        f"• {escape(brand)}"
        for brand in TARGET_BRANDS
    )

    await message.answer(
        "📊 <b>СТАТУС iHERB BOT</b>\n\n"

        "🟢 Telegram: ONLINE\n"
        "🟢 Мониторинг: ВКЛЮЧЁН\n"

        f"🔄 Интервал: "
        f"{CHECK_INTERVAL_SECONDS // 60} минут\n"

        f"🎯 Минимальная скидка: "
        f"<b>{MIN_DISCOUNT_PERCENT}%</b>\n"

        f"🏷 Только целевые бренды: "
        f"<b>{'ДА' if ONLY_TARGET_BRANDS else 'НЕТ'}</b>\n\n"

        f"<b>Бренды:</b>\n"
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
# RESET CACHE
# ============================================================

@dp.message(
    Command("resetcache")
)
async def reset_cache_handler(
    message,
):

    global sent_deals_cache

    sent_deals_cache = set()

    save_cache()

    await message.answer(
        "♻️ <b>Память очищена.</b>\n\n"
        "При следующей проверке "
        "бот сможет снова отправить "
        "подходящие товары.",
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ДРУГИЕ СООБЩЕНИЯ
# ============================================================

@dp.message()
async def any_message_handler(
    message,
):

    chat_id = str(
        message.chat.id
    )

    subscribers.add(
        chat_id
    )

    await message.answer(
        "👋 <b>iHerb Deal Bot</b>\n\n"

        f"🔥 Ищу скидки "
        f"от {MIN_DISCOUNT_PERCENT}%+\n\n"

        "Используйте:\n"
        "🔥 Получить скидки\n"
        "ℹ️ Статус",

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

    logging.info(
        f"🎯 Фильтр скидок: "
        f"{MIN_DISCOUNT_PERCENT}%+"
    )

    # ========================================================
    # ПЕРВАЯ ПРОВЕРКА СРАЗУ
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
                f"{CHECK_INTERVAL_SECONDS // 60} минут..."
            )

            await asyncio.sleep(
                CHECK_INTERVAL_SECONDS
            )

            logging.info(
                "⏰ Интервал завершён. "
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
            port,
        )

        await site.start()

        logging.info(
            "🌐 Render Health Server "
            f"запущен на порту {port}"
        )

    except Exception as e:

        logging.exception(
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

    logging.info(
        f"🎯 MIN_DISCOUNT_PERCENT = "
        f"{MIN_DISCOUNT_PERCENT}%"
    )

    logging.info(
        f"🏷 ONLY_TARGET_BRANDS = "
        f"{ONLY_TARGET_BRANDS}"
    )

    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    load_cache()

    # --------------------------------------------------------
    # RENDER SERVER
    # --------------------------------------------------------

    await start_dummy_server()

    # --------------------------------------------------------
    # SCHEDULER
    # --------------------------------------------------------

    scheduler_task = asyncio.create_task(
        scheduler()
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

            logging.exception(
                "❌ Telegram polling ошибка: "
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
                    "❌ TELEGRAM UNAUTHORIZED!\n"
                    "Проверьте BOT_TOKEN."
                )

                await asyncio.sleep(
                    30
                )

            else:

                logging.warning(
                    "🔄 Перезапуск Telegram "
                    "через 5 секунд..."
                )

                await asyncio.sleep(
                    5
                )

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    scheduler_task.cancel()

    try:

        await scheduler_task

    except asyncio.CancelledError:

        pass

    try:

        await bot.session.close()

    except Exception:

        pass


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
