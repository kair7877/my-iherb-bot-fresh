import asyncio
import logging
import re
from datetime import datetime
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter

# Попытка обхода Cloudflare (403 Forbidden) через curl_cffi с подписью Chrome
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    import httpx

# ==========================================
# КОНФИГУРАЦИЯ БОТА И МОНИТОРИНГА IHERB
# ==========================================
BOT_TOKEN = "8910776648:AAGbhcQ7CBH46QVq3lT9x6GmU8kgkFSJhqY"
CHAT_ID = "-1004290840012"
CHECK_INTERVAL_SECONDS = 900  # Каждые 15 минут

# Фильтры отслеживания
MIN_DISCOUNT_PERCENT = 20  # Минимальная скидка 20%
TARGET_BRANDS = ["California Gold Nutrition", "NOW Foods", "Doctor's Best", "Solgar"]
KZT_EXCHANGE_RATE = 540  # Курс USD -> KZT (Тенге)
MARGIN_MARKUP_PERCENT = 35  # Наценка реселлера (+35%)

# Заголовки настоящего браузера Chrome
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
}

# Множество отправленных товаров для предотвращения дубликатов
sent_deals_cache = set()
subscribers = set()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


@dp.message()
async def start_handler(message):
    chat_id = str(message.chat.id)
    subscribers.add(chat_id)
    logging.info(f"Новый пользователь подключился: Chat ID = {chat_id}")
    await message.answer(
        f"👋 <b>Привет! Вы успешно подключили iHerb Бот Скидок!</b>\n\n"
        f"🆔 Ваш Chat ID: <code>{chat_id}</code>\n"
        f"🔔 Бот автоматически отправляет вам только выигрышные акции с расчетной маржой реселлера!\n\n"
        f"🔎 <i>Запуск первой проверки товаров...</i>",
        parse_mode=ParseMode.HTML
    )
    asyncio.create_task(check_and_notify())


@dp.channel_post()
async def channel_post_handler(message):
    chat_id = str(message.chat.id)
    subscribers.add(chat_id)
    logging.info(f"📢 Обнаружен пост из канала! Чат канала сохранен: Chat ID = {chat_id}")


async def get_iherb_html(url: str) -> str:
    """Получение HTML страницы с обходом защиты Cloudflare 403"""
    if HAS_CURL_CFFI:
        try:
            # curl_cffi имитирует настоящую подпись браузера Chrome (TLS fingerprint)
            response = await asyncio.to_thread(
                curl_requests.get,
                url,
                headers=HEADERS,
                impersonate="chrome120",
                timeout=15
            )
            if response.status_code == 200:
                return response.text
            else:
                logging.error(f"curl_cffi Status Code: {response.status_code}")
        except Exception as e:
            logging.error(f"Ошибка curl_cffi: {e}")

    # Запасной вариант через httpx
    try:
        async with httpx.AsyncClient(timeout=15.0, headers=HEADERS, follow_redirects=True) as client:
            response = await client.get(url)
            if response.status_code == 200:
                return response.text
            elif response.status_code == 403:
                logging.error("❌ 403 Forbidden: iHerb (Cloudflare) заблокировал обычный HTTP запрос.")
                logging.info("💡 РЕШЕНИЕ ДЛЯ 403: В Pydroid 3 откройте Pip -> введите 'curl_cffi' и нажмите INSTALL!")
            else:
                logging.error(f"Ошибка запроса к iHerb: Status {response.status_code}")
    except Exception as e:
        logging.error(f"Ошибка получения данных: {e}")

    return ""


async def fetch_iherb_specials():
    """
    Парсинг раздела 'Суперскидки' и 'Бренды недели' на iHerb
    """
    deals = []
    url = "https://www.iherb.com/c/specials"
    
    html = await get_iherb_html(url)
    if not html:
        return deals

    try:
        soup = BeautifulSoup(html, "html.parser")
        product_cards = soup.select(".product-cell-container") or soup.select(".product-inner")
        
        for card in product_cards:
            try:
                link_elem = card.select_one("a.absolute-link") or card.select_one("a[href*='/pr/']") or card.select_one("a")
                title_elem = card.select_one(".product-title") or link_elem
                if not link_elem:
                    continue
                
                title = title_elem.text.strip() if title_elem else "iHerb Product"
                link = link_elem.get("href", "")
                if link and not link.startswith("http"):
                    link = f"https://www.iherb.com{link}"
                if not link:
                    continue
                
                price_elem = card.select_one(".price") or card.select_one(".price-discount")
                orig_price_elem = card.select_one(".price-original") or card.select_one(".discount-price")
                
                if not price_elem:
                    continue
                    
                price_text = price_elem.text.strip().replace("$", "").replace(",", ".")
                match_disc = re.search(r"\d+\.\d+", price_text)
                discount_price = float(match_disc.group()) if match_disc else 0.0
                
                orig_price = discount_price * 1.25
                if orig_price_elem:
                    orig_text = orig_price_elem.text.strip().replace("$", "").replace(",", ".")
                    match_orig = re.search(r"\d+\.\d+", orig_text)
                    if match_orig:
                        orig_price = float(match_orig.group())
                
                if orig_price <= discount_price or orig_price == 0:
                    continue
                    
                discount_percent = int(round((1 - discount_price / orig_price) * 100))
                
                brand = "iHerb Brand"
                for tb in TARGET_BRANDS:
                    if tb.lower() in title.lower():
                        brand = tb
                        break
                
                product_id = re.search(r"/pr/[^/]+/(\d+)", link)
                deal_id = product_id.group(1) if product_id else link
                
                deals.append({
                    "id": deal_id,
                    "title": title,
                    "brand": brand,
                    "orig_price_usd": orig_price,
                    "discount_price_usd": discount_price,
                    "discount_percent": discount_percent,
                    "link": link
                })
            except Exception as e:
                logging.debug(f"Ошибка парсинга карточки: {e}")
    except Exception as e:
        logging.error(f"Ошибка разбора HTML iHerb: {e}")
            
    return deals


def format_deal_message(deal: dict) -> str:
    """Форматирование красивого сообщения для Telegram с расчетом маржи в тенге (KZT) и прямой ссылкой"""
    title = deal["title"]
    orig_usd = deal["orig_price_usd"]
    disc_usd = deal["discount_price_usd"]
    percent = deal["discount_percent"]
    link = deal["link"] or "https://www.iherb.com"
    clean_link = link.replace("&", "&amp;")
    
    cost_kzt = round(disc_usd * KZT_EXCHANGE_RATE)
    resell_price_kzt = round(cost_kzt * (1 + MARGIN_MARKUP_PERCENT / 100))
    profit_kzt = resell_price_kzt - cost_kzt
    
    c_kzt_str = f"{cost_kzt:,}".replace(",", " ")
    r_kzt_str = f"{resell_price_kzt:,}".replace(",", " ")
    p_kzt_str = f"{profit_kzt:,}".replace(",", " ")
    
    msg = (
        f"🔥 <b>СКИДКА НА iHERB: -{percent}%</b> 🔥\n\n"
        f"💊 <b>Товар:</b> {title}\n\n"
        f"💰 <b>Закуп на iHerb:</b> <s>${orig_usd:.2f}</s> ➡️ <b>${disc_usd:.2f}</b> (~{c_kzt_str} ₸)\n"
        f"📈 <b>Цена продажи клиентам:</b> <b>{r_kzt_str} ₸</b>\n"
        f"💵 <b>Ваша чистая маржа:</b> ~<b>+{p_kzt_str} ₸</b> за банку\n\n"
        f"🔗 <b>Прямая ссылка на товар:</b>\n👉 {clean_link}"
    )
    return msg


async def check_and_notify():
    """Фоновая задача проверки скидок"""
    logging.info("🔎 Проверка новых скидок iHerb...")
    deals = await fetch_iherb_specials()
    
    # Сбор всех уникальных получателей
    targets = set()
    if CHAT_ID and not CHAT_ID.startswith("YOUR_"):
        targets.add(CHAT_ID)
    targets.update(subscribers)

    if not targets:
        logging.warning("⚠️ Нет получателей! Напишите боту /start в Telegram.")
        return

    failed_targets = set()

    for deal in deals:
        if deal["discount_percent"] < MIN_DISCOUNT_PERCENT:
            continue
            
        if TARGET_BRANDS and not any(brand.lower() in deal["title"].lower() for brand in TARGET_BRANDS):
            continue
            
        deal_id = deal["id"]
        if deal_id in sent_deals_cache:
            continue
            
        message = format_deal_message(deal)
        any_sent = False

        for target_id in targets:
            if target_id in failed_targets:
                continue
            try:
                await bot.send_message(
                    chat_id=target_id,
                    text=message,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False
                )
                logging.info(f"✅ Отправлено в {target_id}: {deal['title'][:30]}...")
                any_sent = True
                await asyncio.sleep(3.0)
            except TelegramRetryAfter as e:
                retry_after = getattr(e, 'retry_after', 26)
                logging.warning(f"⏳ Ограничение Telegram (Flood Control)! Бот делает паузу на {retry_after + 2} сек и повторит...")
                await asyncio.sleep(retry_after + 2)
                try:
                    await bot.send_message(
                        chat_id=target_id,
                        text=message,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=False
                    )
                    logging.info(f"✅ Успешно отправлено в {target_id} (после паузы): {deal['title'][:30]}...")
                    any_sent = True
                    await asyncio.sleep(3.0)
                except Exception as retry_err:
                    logging.error(f"❌ Ошибка повторной отправки в {target_id}: {retry_err}")
            except Exception as e:
                err_str = str(e)
                if "chat not found" in err_str:
                    failed_targets.add(target_id)
                    logging.error(f"❌ Ошибка отправки в {target_id}: Telegram server says - Bad Request: chat not found")
                    logging.info("💡 КАПРИЗ TELEGRAM: Чат не найден! Убедитесь, что:")
                    logging.info(" 1. Канал ПУБЛИЧНЫЙ и его юзернейм в точности равен CHAT_ID.")
                    logging.info(" 2. Если канал ПРИВАТНЫЙ, CHAT_ID должен быть числовым (например -1004290840012).")
                    logging.info(" 3. Бот добавлен в Администраторы канала с правом 'Публикация сообщений'.")
                elif "too many requests" in err_str.lower() or "flood" in err_str.lower():
                    logging.warning("⏳ Превышен лимит сообщений Telegram. Пауза 25 секунд...")
                    await asyncio.sleep(25)
                else:
                    logging.error(f"Ошибка отправки в {target_id}: {e}")

        if any_sent:
            sent_deals_cache.add(deal_id)


async def scheduler():
    """Цикл регулярных проверок"""
    while True:
        try:
            await check_and_notify()
        except Exception as e:
            logging.error(f"Ошибка в цикле плановика: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def main():
    logging.info("🚀 Telegram бот для iHerb запущен!")
    logging.info("🔎 Автоматический запуск фонового мониторинга скидок...")
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
