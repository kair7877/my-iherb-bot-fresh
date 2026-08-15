import asyncio, json, logging, os, re, tempfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from html import escape

import httpx
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramRetryAfter

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("iherb")

# ========================= SETTINGS =========================
def env(name, default=""):
    v = os.getenv(name, default)
    if v is None or str(v).strip().upper() == name.upper(): return default
    return str(v).strip()

def intval(name, default):
    try: return int(float(env(name, default)))
    except: return default

def numval(name, default):
    try: return float(env(name, default).replace(",", "."))
    except: return default

BOT_TOKEN = env("BOT_TOKEN")
CHAT_ID = env("CHAT_ID")
KZT = numval("KZT_EXCHANGE_RATE", 540)
MARKUP = numval("MARGIN_MARKUP_PERCENT", 35)
MIN_DISCOUNT = intval("MIN_DISCOUNT_PERCENT", 20)
MAX_DISCOUNT = intval("MAX_DISCOUNT_PERCENT", 90)
MAX_SEND = max(1, intval("MAX_DEALS_PER_CHECK", 10))
INTERVAL = max(60, intval("CHECK_INTERVAL_SECONDS", 300))
HEARTBEAT = max(30, intval("HEARTBEAT_SECONDS", 60))
MIN_PROFIT_KZT = max(0, intval("MIN_PROFIT_KZT", 1000))
MIN_PRICE_USD = max(0, numval("MIN_PRICE_USD", 3))
DATABASE_URL = env("DATABASE_URL")
TARGET_BRANDS = []
EXCLUDE_KEYWORDS = [x.strip().lower() for x in env(
    "EXCLUDE_KEYWORDS", "ebook,book,audiobook,gift card,giftcard,free sample,sample only"
).split(",") if x.strip()]

if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN не найден в Render Environment Variables")

try:
    import asyncpg
except ImportError:
    asyncpg = None

try:
    from curl_cffi import requests as curl_requests
    CURL = True
except ImportError:
    curl_requests = None; CURL = False

# ========================= STORAGE =========================
# ВАЖНО: /var/data больше нигде не используется.
CACHE_DIR = os.path.join(tempfile.gettempdir(), "iherb_bot")
CACHE_FILE = os.path.join(CACHE_DIR, "sent_deals.json")
MAX_CACHE = 5000
sent_cache = set()
db_pool = None
storage_mode = "temporary /tmp"

async def storage_init():
    global db_pool, storage_mode, sent_cache

    if DATABASE_URL and asyncpg:
        try:
            db_pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=3, command_timeout=15
            )
            async with db_pool.acquire() as conn:
                await conn.execute("""CREATE TABLE IF NOT EXISTS sent_deals(
                    product_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    discount INTEGER,
                    price_usd DOUBLE PRECISION,
                    link TEXT,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            storage_mode = "PostgreSQL"
            log.info("💾 PostgreSQL подключён — постоянная память включена")
            return
        except Exception as e:
            log.warning("⚠️ PostgreSQL недоступен: %s", e)
            db_pool = None

    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            sent_cache = {str(x) for x in data} if isinstance(data, list) else set()
        log.warning("⚠️ Используется временный /tmp cache: %s товаров", len(sent_cache))
    except Exception as e:
        log.warning("⚠️ Cache load: %s", e)

def save_cache():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(sent_cache)[-MAX_CACHE:], f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        log.warning("⚠️ Cache не сохранён: %s", e)

async def is_sent(pid):
    pid = str(pid)
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                return await conn.fetchval(
                    "SELECT 1 FROM sent_deals WHERE product_id=$1", pid
                ) is not None
        except Exception as e:
            log.warning("DB is_sent: %s", e)
    return pid in sent_cache

async def mark_sent(d):
    global sent_cache
    pid = str(d["id"])
    sent_cache.add(pid)
    if len(sent_cache) > MAX_CACHE:
        sent_cache = set(list(sent_cache)[-MAX_CACHE:])

    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("""INSERT INTO sent_deals
                    (product_id,title,discount,price_usd,link)
                    VALUES($1,$2,$3,$4,$5)
                    ON CONFLICT(product_id) DO NOTHING""",
                    pid, d["title"], d["discount_percent"],
                    d["discount_price_usd"], d["link"])
            return
        except Exception as e:
            log.warning("DB mark_sent: %s", e)
    save_cache()

async def stored_count():
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                return int(await conn.fetchval(
                    "SELECT COUNT(*) FROM sent_deals") or 0)
        except Exception:
            pass
    return len(sent_cache)

# ========================= BOT =========================
bot=Bot(BOT_TOKEN); dp=Dispatcher(); subscribers=set(); validated_chat=None
last_finished=None; last_found=0; last_sent=0; next_check=None; checking=False
keyboard=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="🔥 Получить скидки"),KeyboardButton(text="ℹ️ Статус")]],resize_keyboard=True)
HEADERS={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36","Accept-Language":"en-US,en;q=0.9,ru;q=0.8","Cache-Control":"no-cache"}

# ========================= PARSING =========================
def clean(x): return re.sub(r"\s+"," ",str(x or "")).strip()

def price_num(x):
    try:
        x=str(x).replace("\xa0","").replace(" ","")
        x=x.replace(",",".") if "," in x and "." not in x else x.replace(",","")
        x=re.sub(r"[^0-9.]","",x); n=float(x)
        return n if 0<n<1000000 else None
    except: return None

def prices(text):
    if not text:return []
    p=[("USD",r"US\$\s*([\d\s]+(?:[.,]\d{1,2})?)"),("USD",r"\$\s*([\d\s]+(?:[.,]\d{1,2})?)"),("USD",r"USD\s*([\d\s]+(?:[.,]\d{1,2})?)"),("KZT",r"₸\s*([\d\s]+(?:[.,]\d{1,2})?)"),("KZT",r"([\d\s]+(?:[.,]\d{1,2})?)\s*₸")]
    out=[]
    for cur,pat in p:
        try:
            for x in re.findall(pat,str(text),re.I):
                n=price_num(x)
                if n: out.append((cur,n))
        except: pass
    return out

def discount(text):
    for pat in [r"(\d{1,2})\s*%\s*off",r"(\d{1,2})\s*%\s*discount",r"-\s*(\d{1,2})\s*%",r"(\d{1,2})\s*%\s*скид",r"скидк\w*\s*(?:до\s*)?(\d{1,2})\s*%",r"save\s+(\d{1,2})\s*%"]:
        m=re.search(pat,clean(text),re.I)
        if m:
            n=int(m.group(1))
            if MIN_DISCOUNT<=n<=MAX_DISCOUNT:return n
    return None

def normurl(u):
    u=str(u or "").strip()
    if u.startswith("//"): return "https:"+u
    if u.startswith("/"): return "https://www.iherb.com"+u
    if u.startswith("http://"): return "https://"+u[7:]
    if u.startswith("https://"): return u
    return urljoin("https://www.iherb.com/",u)

def pid(url):
    m=re.search(r"/(\d+)(?:\?|$)",url or "")
    return m.group(1) if m else url

def title(card):
    for s in [".product-title","[class*='product-title']","[class*='ProductTitle']","[class*='title']"]:
        try:
            e=card.select_one(s); t=clean(e.get_text(" ",strip=True)) if e else ""
            if len(t)>=3:return t
        except: pass
    for e in card.select("a[href]"):
        t=clean(e.get_text(" ",strip=True))
        if len(t)>=10:return t
    return ""

def link(card):
    for s in ["a[href*='/pr/']","a[href*='/product/']","a[href]"]:
        try:
            e=card.select_one(s)
            if e:
                u=normurl(e.get("href"));
                if u:return u
        except: pass
    return ""

def parse_card(card,i):
    try:
        txt=clean(card.get_text(" ",strip=True)); t=title(card); u=link(card)
        if not txt or not t or not u:return None
        if TARGET_BRANDS and not any(b.lower() in t.lower() for b in TARGET_BRANDS):return None
        if any(k in t.lower() for k in EXCLUDE_KEYWORDS):return None
        ds=[x for x in [discount(txt)] if x]
        ps=[]
        for e in card.select(".price,.price-discount,.price-original,.price-old,.original-price,.discount-price,.product-price,[class*='price'],[data-qa*='price'],[data-testid*='price']"):
            ps+=prices(e.get_text(" ",strip=True))
        for e in card.find_all():
            for k,v in e.attrs.items():
                if isinstance(v,str) and ("price" in k.lower() or "amount" in k.lower()): ps+=prices(v)
                if isinstance(v,str) and ("discount" in k.lower() or "percent" in k.lower()):
                    x=discount(v)
                    if x:ds.append(x)
        for e in card.find_all("script"):
            s=e.string or e.get_text()
            ps+=prices(s)
            x=discount(s)
            if x:ds.append(x)
        if not ps: ps=prices(txt)
        d=max(ds) if ds else None
        usd=[x for c,x in ps if c=="USD"]; kzt=[x for c,x in ps if c=="KZT"]
        if usd:
            vals=sorted(set(round(x,2) for x in usd)); cur=vals[0]; old=vals[-1] if len(vals)>1 else None
        elif kzt:
            vals=sorted(set(round(x/KZT,2) for x in kzt)); cur=vals[0]; old=vals[-1] if len(vals)>1 else None
        else:return None
        if cur < MIN_PRICE_USD:return None
        if not old and d and d<100: old=round(cur/(1-d/100),2)
        if old and cur and old>cur:
            calc=round((1-cur/old)*100)
            if not d or calc>d:d=calc
        if not d or not cur or not old or d<MIN_DISCOUNT or d>MAX_DISCOUNT or old<=cur:return None
        cost_kzt=round(cur*KZT)
        sale_kzt=round(cost_kzt*(1+MARKUP/100))
        profit_kzt=sale_kzt-cost_kzt
        if profit_kzt < MIN_PROFIT_KZT:return None
        return {"id":pid(u),"title":t,"brand":"iHerb","orig_price_usd":old,
                "discount_price_usd":cur,"discount_percent":int(d),
                "cost_kzt":cost_kzt,"sale_kzt":sale_kzt,
                "profit_kzt":profit_kzt,"link":u}
    except Exception as e:
        log.debug("card %s: %s",i,e); return None

# ========================= IHERB =========================
async def get_html():
    urls=["https://www.iherb.com/deals?lang=en-US&currency=USD","https://www.iherb.com/deals","https://kz.iherb.com/deals","https://kz.iherb.com/specials"]
    cookies={"ih-pref":"lan=en-US&currency=USD&country=KZ","iherb-pref":"lan=en-US&currency=USD&country=KZ"}
    if CURL:
        for u in urls:
            for b in ["chrome124","chrome120","chrome116"]:
                try:
                    r=await asyncio.to_thread(curl_requests.get,u,headers=HEADERS,cookies=cookies,impersonate=b,timeout=30)
                    log.info("iHerb | curl %s | %s | %s chars",b,r.status_code,len(r.text))
                    if r.status_code==200 and len(r.text)>10000:return r.text
                except Exception as e:log.debug("curl: %s",e)
    for u in urls:
        try:
            async with httpx.AsyncClient(timeout=30,headers=HEADERS,cookies=cookies,follow_redirects=True) as c:
                r=await c.get(u); log.info("iHerb | httpx | %s | %s chars",r.status_code,len(r.text))
                if r.status_code==200 and len(r.text)>10000:return r.text
        except Exception as e:log.debug("httpx: %s",e)
    return ""

async def fetch_deals():
    html=await get_html()
    if not html:return []
    soup=BeautifulSoup(html,"html.parser")
    selectors=[".product-cell-container","[class*='product-cell-container']",".product-inner",".product-card","[data-qa='product-card']",".product-tile","[class*='product-card']","[class*='ProductCard']"]
    cards=[]
    for s in selectors:
        try:
            f=soup.select(s)
            if f: log.info("🔍 %s: %s карточек",s,len(f)); cards+=f
            if len(f)>=30:break
        except:pass
    seen=set(); deals=[]
    for i,c in enumerate(cards,1):
        u=link(c); key=u+"|"+clean(c.get_text(" ",strip=True))[:300]
        if key in seen:continue
        seen.add(key); d=parse_card(c,i)
        if d:deals.append(d)
    unique={d["id"]:d for d in deals}; deals=list(unique.values())
    deals.sort(key=lambda x:(x.get("profit_kzt",0),x["discount_percent"],-x["discount_price_usd"]),reverse=True)
    log.info("🔥 Найдено подходящих товаров: %s",len(deals)); return deals

# ========================= TELEGRAM =========================
async def validate_chat():
    global validated_chat
    if not CHAT_ID:return
    try:
        c=await bot.get_chat(CHAT_ID); validated_chat=str(c.id); log.info("✅ CHAT_ID подтверждён: %s",validated_chat)
    except Exception as e:log.error("❌ CHAT_ID: %s",e)

def targets():
    x=set(subscribers)
    if validated_chat:x.add(validated_chat)
    return x

def message(d):
    old=d["orig_price_usd"]; cur=d["discount_price_usd"]; disc=d["discount_percent"]
    cost=d.get("cost_kzt",round(cur*KZT))
    sale=d.get("sale_kzt",round(cost*(1+MARKUP/100)))
    profit=d.get("profit_kzt",sale-cost)
    msg=(f"🔥 <b>НОВАЯ СКИДКА iHERB</b> 🔥\n\n🏷 <b>Бренд:</b> {escape(d['brand'])}\n\n💊 <b>Товар:</b>\n{escape(d['title'])}\n\n📉 <b>СКИДКА: -{disc}%</b>\n\n💰 <b>Цена iHerb:</b>\n<s>${old:.2f}</s> ➡️ <b>${cur:.2f}</b>\n\n🇰🇿 <b>Закуп:</b> ≈ {cost:,} ₸\n\n🏪 <b>Цена продажи:</b> {sale:,} ₸\n\n📈 <b>Прибыль:</b> +{profit:,} ₸\n\n💱 <b>Курс:</b> 1 USD = {KZT:g} ₸\n📈 <b>Наценка:</b> +{MARKUP:g}%\n\n⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}").replace(",", " ")
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Открыть товар на iHerb",url=d["link"])]])
    return msg,kb

async def send(d):
    msg,kb=message(d); ok=False
    for chat in list(targets()):
        try:
            await bot.send_message(chat,msg,parse_mode=ParseMode.HTML,reply_markup=kb,disable_web_page_preview=True);ok=True;await asyncio.sleep(1.5)
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(getattr(e,"retry_after",30))+2)
            try:await bot.send_message(chat,msg,parse_mode=ParseMode.HTML,reply_markup=kb,disable_web_page_preview=True);ok=True
            except Exception as er:log.error("retry: %s",er)
        except Exception as e:
            s=str(e).lower();log.error("Telegram %s: %s",chat,e)
            if "chat not found" in s or "bot was kicked" in s or "user is deactivated" in s:subscribers.discard(str(chat))
    return ok

async def check():
    global checking,last_finished,last_found,last_sent
    if checking:return
    checking=True;last_sent=0
    try:
        deals=await fetch_deals();last_found=len(deals)
        if not deals:return
        if not targets():log.warning("⚠️ Нет получателей. Нажмите /start");return
        for d in deals:
            if await is_sent(d["id"]):continue
            if await send(d):
                await mark_sent(d);last_sent+=1
            if last_sent>=MAX_SEND:break
        log.info("📊 Найдено=%s | Отправлено=%s | Уже сохранено=%s",last_found,last_sent,await stored_count())
    except Exception as e:log.exception("❌ Проверка: %s",e)
    finally:checking=False;last_finished=datetime.now(timezone.utc)

# ========================= HANDLERS =========================
@dp.message(Command("start"))
async def start(m):
    subscribers.add(str(m.chat.id));await m.answer(f"👋 <b>iHerb Deal Bot работает!</b>\n\n🔥 Скидки: <b>{MIN_DISCOUNT}%–{MAX_DISCOUNT}%</b>\n⏱ Проверка каждые <b>{INTERVAL//60} минут</b>.",reply_markup=keyboard,parse_mode=ParseMode.HTML)

@dp.message(Command("deals"))
@dp.message(F.text=="🔥 Получить скидки")
async def deals(m):
    subscribers.add(str(m.chat.id));await m.answer("🔎 Проверяю iHerb прямо сейчас...");await check()

def fmt(t):
    if not t:return "—"
    try:return t.astimezone().strftime("%d.%m.%Y %H:%M:%S")
    except:return str(t)

@dp.message(Command("status"))
@dp.message(F.text=="ℹ️ Статус")
async def status(m):
    subscribers.add(str(m.chat.id));storage="PostgreSQL" if DATABASE_URL and asyncpg else "временный /tmp cache"
    await m.answer(f"📊 <b>СТАТУС БОТА</b>\n\n🟢 Telegram: ONLINE\n🟢 Мониторинг: ВКЛЮЧЁН\n🔄 Интервал: {INTERVAL} сек.\n🎯 Скидка: {MIN_DISCOUNT}%–{MAX_DISCOUNT}%\n📦 Лимит: {MAX_SEND}\n💱 Курс: {KZT:g} ₸\n📈 Наценка: {MARKUP:g}%\n👥 Получателей: {len(targets())}\n💾 Хранилище: {storage}\n💾 Сохранено: {await stored_count()}\n🕒 Последняя проверка: {fmt(last_finished)}\n📦 Найдено: {last_found}\n📤 Отправлено: {last_sent}\n➡️ Следующая: {fmt(next_check)}",reply_markup=keyboard,parse_mode=ParseMode.HTML)

@dp.message()
async def other(m):
    subscribers.add(str(m.chat.id));await m.answer("👋 Используйте кнопки ниже.",reply_markup=keyboard)

# ========================= BACKGROUND =========================
async def scheduler():
    global next_check
    log.info("🚀 АВТОМАТИЧЕСКИЙ МОНИТОРИНГ ЗАПУЩЕН")
    while True:
        await check();next_check=datetime.now(timezone.utc)+timedelta(seconds=INTERVAL);await asyncio.sleep(INTERVAL)

async def heartbeat():
    while True:
        try:log.info("❤️ HEARTBEAT | alive | last_check=%s | next=%s | in_progress=%s",fmt(last_finished),fmt(next_check),checking);await asyncio.sleep(HEARTBEAT)
        except asyncio.CancelledError:return

async def health_server():
    try:
        from aiohttp import web
        app=web.Application()
        async def home(r):return web.Response(text="iHerb Telegram Bot is running!")
        async def health(r):return web.Response(text="OK")
        app.router.add_get("/",home);app.router.add_get("/health",health)
        runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,"0.0.0.0",int(os.getenv("PORT","10000"))).start();log.info("🌐 Health server запущен")
    except Exception as e:log.exception("Health server: %s",e)

async def main():
    await storage_init();await health_server();await validate_chat()
    st=asyncio.create_task(scheduler());hb=asyncio.create_task(heartbeat())
    try:
        while True:
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                log.info("📡 Telegram polling запускается")
                await dp.start_polling(bot);break
            except asyncio.CancelledError:raise
            except Exception as e:
                s=str(e);log.error("❌ Telegram polling: %s",e);await asyncio.sleep(15 if ("409" in s or "Conflict" in s) else 10)
    finally:
        st.cancel();hb.cancel()
        for t in (st,hb):
            try:await t
            except asyncio.CancelledError:pass
        if db_pool:
            try:
                await db_pool.close()
            except Exception:
                pass
        await bot.session.close()

if __name__=="__main__":
    asyncio.run(main())
