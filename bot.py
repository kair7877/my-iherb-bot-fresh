# =====================================================
# PREDATOR ZETA v30.12 [PRO LEAGUES & TOP STRATEGIES]
# =====================================================
# Обновления и фиксы v30.12:
# 1. 🛡️ ФИЛЬТР ТОПОК И БК-ЛИГ (PRO_LEAGUES_ONLY):
#    Отсекаются не БК-доступные лиги. Остаются только профессиональные лиги.
# 2. 🔥 3 ТОПОВЫЕ СТРАТЕГИИ:
#    • LateFavoriteStrategy: Штурм фаворита (60'-78')
#    • FirstHalfGoalStrategy: Гол в 1-м тайме (22'-36')
#    • LateOverStrategy: Поздний тотал (70'-82')
# 3. 🛡️ Улучшенный SofaFetcher (мульти-эндпоинт обход 403 / Cloudflare)
# =====================================================

import time, os, sys, requests, threading
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler

# Вывод логов в терминал без задержек (Unbuffered output)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

try:
    import cloudscraper
except ImportError:
    print("Установите: pip install cloudscraper requests", flush=True)
    sys.exit(1)


# =====================================================
# ВЕБ-СЕРВЕР ДЛЯ RENDER HEALTH CHECK (PORT 10000)
# =====================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"PREDATOR ZETA BOT IS ALIVE!")

    def log_message(self, format, *args):
        return

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"🌐 [Render Health Server] Веб-сервер запущен на порту {port}", flush=True)


# =====================================================
# КОНФИГ
# =====================================================
class Config:
    VERSION = "30.12 [PRO LEAGUES]"
    CHECK_INTERVAL = 45             # 45 секунд между циклами
    BANKROLL_START = 1000.0
    FLAT_STAKE = 100.0
    CURRENCY = "KZT"

    MAX_CONCURRENT_BETS = 8
    DAILY_STOPLOSS_PCT = 30.0
    OVERALL_STOPLOSS_PCT = 50.0

    SEND_WINDOWS = [(20, 36), (60, 80)]
    PENDING_EXPIRE_MINUTE = 82

    PRO_LEAGUES_ONLY = True         # Включить фильтр только профессиональных БК-турниров
    MIN_UNIQUE_USER_COUNT = 250     # Мин. подписчиков в SofaScore

    EXCLUDE_KEYWORDS = [
        "astiller", "colonia", "provincial", "regional", "distrital", "interprovincial",
        "tercera", "preferente", "oberliga", "landesliga", "kreisliga", "bezirksliga",
        "league 3", "league 4", "league 5", "liga 3", "liga 4", "division 3", "division 4",
        "division 5", "copa santa fe", "amateur", "sunday league", "regionaliga",
        "u15", "u16", "u17", "u18", "u19", "u20", "u21", "u22", "u23",
        "youth", "junior", "juvenil", "juniors", "academy", "sub 20", "sub 23", "sub-20", "sub-19",
        "women", "woman", "ladies", "femenino", "feminine", "frauen", "dames",
        "friendly", "friendlies", "testspiel", "club friendly",
        "reserve", "reserves", "réserve", " b team", "b-team", " ii "
    ]

    ODDS = {
        "late_favorite": 1.85,
        "first_half_goal": 1.75,
        "late_over": 1.90,
    }


def cl(t, c="WH"):
    C = {"R": "\033[0m", "CY": "\033[1;36m", "GR": "\033[1;32m", "YE": "\033[1;33m",
         "RE": "\033[1;31m", "BL": "\033[1;34m", "MA": "\033[1;35m", "WH": "\033[1;37m"}
    return f"{C.get(c,'')}{t}{C['R']}"


def in_send_window(minute: int) -> bool:
    return any(lo <= minute <= hi for lo, hi in Config.SEND_WINDOWS)


def is_excluded_match(match: dict) -> Optional[str]:
    tournament = match.get("tournament") or {}
    unique_t = tournament.get("uniqueTournament") or {}
    category = tournament.get("category") or {}
    home = match.get("homeTeam") or {}
    away = match.get("awayTeam") or {}

    if Config.PRO_LEAGUES_ONLY:
        if not unique_t:
            return "No uniqueTournament"
        user_count = int(unique_t.get("userCount") or 0)
        if user_count < Config.MIN_UNIQUE_USER_COUNT:
            return f"Низкий статус (подписчиков: {user_count})"

    haystack = " ".join([
        str(tournament.get("name") or ""),
        str(unique_t.get("name") or ""),
        str(category.get("name") or ""),
        str(home.get("name") or ""),
        str(away.get("name") or ""),
    ]).lower()
    haystack = f" {haystack} "

    for kw in Config.EXCLUDE_KEYWORDS:
        if kw in haystack:
            return kw
    return None


def load_credentials():
    token = os.environ.get("BOT_TOKEN")
    chat = os.environ.get("CHAT_ID")

    if os.path.exists(".env") and (not token or not chat):
        try:
            for line in open(".env", encoding="utf-8"):
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    v = v.strip().strip('"').strip("'")
                    if k == "BOT_TOKEN" and not token: token = v
                    if k == "CHAT_ID" and not chat:   chat = v
        except Exception:
            pass

    if token and chat:
        print("✅ Ключи Telegram успешно загружены", flush=True)
        return token, chat

    print("\n[!] Введите данные Telegram:", flush=True)
    token = input("   BOT_TOKEN: ").strip().strip('"').strip("'")
    chat = input("   CHAT_ID:   ").strip().strip('"').strip("'")

    with open(".env", "w", encoding="utf-8") as f:
        f.write(f"BOT_TOKEN={token}\nCHAT_ID={chat}\n")
    return token, chat


@dataclass
class ActiveBet:
    match_id: str
    message_id: int
    strategy_id: str
    strategy_name: str
    emoji: str
    market: str
    selection: str
    stake: float
    home_name: str
    away_name: str
    entry_score_h: int
    entry_score_a: int
    entry_minute: str
    meta: dict = field(default_factory=dict)
    status: str = "active"
    settled: bool = False


class BaseStrategy:
    id = "base"
    name = "BASE"
    emoji = "•"

    def scan(self, match: dict, incidents: List[dict], stats: Optional[dict]) -> Optional[dict]:
        raise NotImplementedError

    def settle(self, bet: ActiveBet, cur_h: int, cur_a: int, minute: int, period: str) -> Optional[bool]:
        raise NotImplementedError


def _extract_stat_val(stats: dict, target_names: List[str]) -> Tuple[int, int]:
    if not stats:
        return (0, 0)
    try:
        for period_block in stats.get("statistics", []):
            if period_block.get("period") != "ALL":
                continue
            for group in period_block.get("groups", []):
                for item in group.get("statisticsItems", []):
                    name = str(item.get("name") or "").lower()
                    if any(t in name for t in target_names):
                        h_val = item.get("homeValue", item.get("home", 0))
                        a_val = item.get("awayValue", item.get("away", 0))
                        try:
                            return int(str(h_val).replace("%", "").strip()), int(str(a_val).replace("%", "").strip())
                        except (TypeError, ValueError):
                            return (0, 0)
    except Exception:
        pass
    return (0, 0)


# =====================================================
# СТРАТЕГИИ
# =====================================================
class LateFavoriteStrategy(BaseStrategy):
    id = "late_favorite"
    name = "ШТУРМ ФАВОРИТА (60'-78')"
    emoji = "🔥"

    def scan(self, match, incidents, stats):
        minute = match.get("_minute", 0)
        if not (60 <= minute <= 78):
            return None

        cur_h = int((match.get("homeScore") or {}).get("current") or 0)
        cur_a = int((match.get("awayScore") or {}).get("current") or 0)
        if abs(cur_h - cur_a) > 1:
            return None

        sh_h, sh_a = _extract_stat_val(stats, ["shots on target", "удары в створ"])
        cn_h, cn_a = _extract_stat_val(stats, ["corner kicks", "corners", "угловые"])

        dominant = None
        if (sh_h >= 4 or cn_h >= 5) and (sh_h - sh_a >= 2):
            dominant = "home"
        elif (sh_a >= 4 or cn_a >= 5) and (sh_a - sh_h >= 2):
            dominant = "away"

        if not dominant:
            return None

        team_name = (match.get("homeTeam" if dominant == "home" else "awayTeam") or {}).get("name", "Unknown")
        return {
            "market": "late_favorite_goal",
            "selection": f"Гол фаворита ({team_name}) / ТБ",
            "meta": {"dominant": dominant, "sh_h": sh_h, "sh_a": sh_a, "cn_h": cn_h, "cn_a": cn_a},
        }

    def settle(self, bet, cur_h, cur_a, minute, period):
        dominant = bet.meta.get("dominant")
        if dominant == "home" and cur_h > bet.entry_score_h:
            return True
        if dominant == "away" and cur_a > bet.entry_score_a:
            return True
        if period == "FINISHED":
            return False
        return None


class FirstHalfGoalStrategy(BaseStrategy):
    id = "first_half_goal"
    name = "ГОЛ В 1-М ТАЙМЕ (22'-36')"
    emoji = "⚡"

    def scan(self, match, incidents, stats):
        minute = match.get("_minute", 0)
        if not (22 <= minute <= 36):
            return None

        cur_h = int((match.get("homeScore") or {}).get("current") or 0)
        cur_a = int((match.get("awayScore") or {}).get("current") or 0)
        if (cur_h + cur_a) >= 2:
            return None

        sh_h, sh_a = _extract_stat_val(stats, ["shots on target", "удары в створ"])
        cn_h, cn_a = _extract_stat_val(stats, ["corner kicks", "corners", "угловые"])

        if (sh_h + sh_a) >= 4 and (cn_h + cn_a) >= 3:
            return {
                "market": "first_half_goal",
                "selection": "Гол в 1-м тайме (ИТБ 0.5 1st Half)",
                "meta": {"total_shots": sh_h + sh_a, "total_corners": cn_h + cn_a},
            }
        return None

    def settle(self, bet, cur_h, cur_a, minute, period):
        if (cur_h + cur_a) > (bet.entry_score_h + bet.entry_score_a):
            return True
        if period in ("HT", "2nd", "FINISHED"):
            return False
        return None


class LateOverStrategy(BaseStrategy):
    id = "late_over"
    name = "ПОЗДНИЙ ТОТАЛ БОЛЬШЕ (70'-82')"
    emoji = "🎯"

    def scan(self, match, incidents, stats):
        minute = match.get("_minute", 0)
        if not (70 <= minute <= 82):
            return None

        cur_h = int((match.get("homeScore") or {}).get("current") or 0)
        cur_a = int((match.get("awayScore") or {}).get("current") or 0)
        if abs(cur_h - cur_a) > 1:
            return None

        sh_h, sh_a = _extract_stat_val(stats, ["shots on target", "удары в створ"])
        cn_h, cn_a = _extract_stat_val(stats, ["corner kicks", "corners", "угловые"])

        if (sh_h + sh_a >= 8) and (cn_h + cn_a >= 6):
            target_total = cur_h + cur_a + 0.5
            return {
                "market": "late_over_total",
                "selection": f"Тотал Больше {target_total}",
                "meta": {"total_shots": sh_h + sh_a, "total_corners": cn_h + cn_a},
            }
        return None

    def settle(self, bet, cur_h, cur_a, minute, period):
        if (cur_h + cur_a) > (bet.entry_score_h + bet.entry_score_a):
            return True
        if period == "FINISHED":
            return False
        return None


STRATEGIES: List[BaseStrategy] = [
    LateFavoriteStrategy(),
    FirstHalfGoalStrategy(),
    LateOverStrategy(),
]


class BankrollManager:
    def __init__(self):
        self.balance = Config.BANKROLL_START
        self.active_bets: Dict[str, ActiveBet] = {}

    def can_open_new_bet(self) -> bool:
        return len(self.active_bets) < Config.MAX_CONCURRENT_BETS

    def place_bet(self, match_id, msg_id, strategy: BaseStrategy, signal: dict, info: dict):
        if not msg_id or match_id in self.active_bets:
            return None
        bet = ActiveBet(
            match_id=match_id, message_id=msg_id,
            strategy_id=strategy.id, strategy_name=strategy.name, emoji=strategy.emoji,
            market=signal["market"], selection=signal["selection"],
            stake=Config.FLAT_STAKE,
            home_name=info["home"], away_name=info["away"],
            entry_score_h=info["score_h"], entry_score_a=info["score_a"],
            entry_minute=info["minute"], meta=signal.get("meta", {}),
        )
        self.active_bets[match_id] = bet
        return bet


# =====================================================
# ОБНОВЛЕННЫЙ SOFA FETCHER С РОТАЦИЕЙ И МУЛЬТИ-ЭНДПОИНТОМ
# =====================================================
class SofaFetcher:
    ENDPOINTS = [
        "https://www.sofascore.com/api/v1",
        "https://api.sofascore.com/api/v1"
    ]

    def __init__(self):
        self.fail_count = 0
        self._init_scraper(self.fail_count)
        self.last_req = 0.0

    def _init_scraper(self, try_count=0):
        browsers = [
            {'browser': 'chrome', 'platform': 'windows', 'desktop': True},
            {'browser': 'firefox', 'platform': 'windows', 'desktop': True},
            {'browser': 'chrome', 'platform': 'android', 'mobile': True},
        ]
        b_config = browsers[try_count % len(browsers)]
        try:
            self.sc = cloudscraper.create_scraper(browser=b_config, delay=3)
        except Exception:
            self.sc = requests.Session()

        ua_list = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
        ]
        ua = ua_list[try_count % len(ua_list)]

        self.sc.headers.update({
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "https://www.sofascore.com/",
            "Sec-Ch-Ua": '"Chromium";v="124", "Not-A.Brand";v="99", "Google Chrome";v="124"',
            "Sec-Ch-Ua-Mobile": "?0" if not b_config.get("mobile") else "?1",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Cache-Control": "no-cache",
        })

    def _wait(self):
        if time.time() - self.last_req < 1.0:
            time.sleep(1.0)
        self.last_req = time.time()

    def _get(self, ep):
        self._wait()
        for base_url in self.ENDPOINTS:
            try:
                r = self.sc.get(f"{base_url}/{ep}", timeout=12)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code in (403, 429):
                    self.fail_count += 1
                    self._init_scraper(self.fail_count)
                    time.sleep(2.0)
            except Exception:
                pass
        return None

    def get_live_matches(self):
        res = self._get("sport/football/events/live")
        return res.get("events", []) if res else []

    def get_match_details(self, mid):
        return self._get(f"event/{mid}") or {}

    def get_match_incidents(self, mid) -> List[Dict]:
        res = self._get(f"event/{mid}/incidents")
        return res.get("incidents", []) if res else []

    def get_match_statistics(self, mid) -> Optional[Dict]:
        return self._get(f"event/{mid}/statistics")


class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def _post(self, method: str, payload: Dict):
        try:
            res = requests.post(f"{self.base}/{method}", json=payload, timeout=10).json()
            if not res.get("ok"):
                print(cl(f"❌ Telegram API Error ({method}): {res.get('description', res)}", "RE"), flush=True)
            return res
        except Exception as e:
            print(cl(f"❌ Telegram Request Exception ({method}): {e}", "RE"), flush=True)
            return {"ok": False, "description": str(e)}

    def test_and_notify(self):
        strategies_txt = "\n".join(f"  {s.emoji} {s.name}" for s in STRATEGIES)
        print(cl(f"📤 Попытка отправки приветствия в Telegram (CHAT_ID={self.chat_id})...", "CY"), flush=True)
        resp = self._post("sendMessage", {
            "chat_id": self.chat_id, "parse_mode": "HTML",
            "text": (f"🤖 <b>PREDATOR ZETA v{Config.VERSION} ЗАПУЩЕН НА СЕРВЕРЕ!</b>\n"
                     f"Только БК-доступные профессиональные лиги!\n"
                     f"<b>Активные стратегии:</b>\n{strategies_txt}\n\n"
                     f"🔍 <i>Начинаю непрерывный сканинг Live-матчей...</i>")
        })
        ok = resp.get("ok", False)
        if ok:
            print(cl("✅ Сообщение успешно доставлено в Telegram!", "GR"), flush=True)
        else:
            print(cl(f"⚠️ Ошибка доставки в Telegram: {resp.get('description', 'Неизвестная ошибка')}", "YE"), flush=True)
        return ok

    def send_signal(self, strategy: BaseStrategy, info: dict, signal: dict, match_id: str) -> int:
        url = f"https://www.sofascore.com/event/{match_id}"
        text = (
            f"{strategy.emoji} <b>СТРАТЕГИЯ: {strategy.name}</b>\n\n"
            f"🏆 <b>{info['league']}</b>\n"
            f"🏟 <b>{info['home']}</b> vs <b>{info['away']}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📥 <b>Вход:</b> {info['minute']} • Счёт: <b>{info['score_h']}:{info['score_a']}</b>\n"
            f"💰 <b>СТАВКА:</b> {signal['selection']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⏳ <i>Отслеживаем результат...</i>"
        )
        resp = self._post("sendMessage", {
            "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[{"text": "🔗 Открыть на SofaScore", "url": url}]]},
        })
        return resp.get("result", {}).get("message_id", 0)


class LiveMonitor:
    def __init__(self, token, chat_id):
        self.fetcher = SofaFetcher()
        self.bankroll = BankrollManager()
        self.tg = TelegramNotifier(token, chat_id)
        self.sent_signals: Dict[str, float] = {}

    def _get_minute(self, match):
        code = (match.get("status") or {}).get("code", 0)
        td = match.get("time") or {}
        m = td.get("currentMinute")
        m = int(m) if m is not None else 0
        if code in (100, 12):
            return m if m >= 90 else 90
        if code == 31:
            return 45
        if not m and td.get("currentPeriodStartTimestamp"):
            elapsed = int((time.time() - td["currentPeriodStartTimestamp"]) / 60)
            return 45 + elapsed if code == 7 else elapsed
        return m

    def run(self):
        start_dummy_server()
        if not self.tg.test_and_notify():
            print(cl("\n[!] Внимание: Сообщение в Telegram не отправлено. Проверьте BOT_TOKEN и CHAT_ID.", "YE"), flush=True)

        print(cl("\n==================================================", "CY"), flush=True)
        print(cl(f"   PREDATOR ZETA v{Config.VERSION} ЗАПУЩЕН", "GR"), flush=True)
        print(cl("==================================================\n", "CY"), flush=True)

        while True:
            try:
                self._run_cycle()
                time.sleep(Config.CHECK_INTERVAL)
            except KeyboardInterrupt:
                print("Остановка по команде пользователя.", flush=True)
                break
            except Exception as e:
                print(cl(f"[CRITICAL ERROR] {e}", "RE"), flush=True)
                time.sleep(10)

    def _run_cycle(self):
        matches = self.fetcher.get_live_matches()
        if not matches:
            print(cl(f"[{datetime.now().strftime('%H:%M:%S')}] 💤 Live матчей нет или временно заблокировано...", "YE"), flush=True)
            return

        print(cl(f"[{datetime.now().strftime('%H:%M:%S')}] ⚡ Сканируем Live матчей: {len(matches)}", "CY"), flush=True)
        for match in matches:
            mid = str(match.get("id"))
            minute = self._get_minute(match)
            match["_minute"] = minute

            exclusion_reason = is_excluded_match(match)
            if exclusion_reason:
                continue

            incidents = self.fetcher.get_match_incidents(mid)
            stats_data = self.fetcher.get_match_statistics(mid)

            for strategy in STRATEGIES:
                signal = strategy.scan(match, incidents, stats_data)
                if signal and in_send_window(minute):
                    home_name = (match.get("homeTeam") or {}).get("name", "Unknown")[:18]
                    away_name = (match.get("awayTeam") or {}).get("name", "Unknown")[:18]
                    league_name = (match.get("tournament") or {}).get("name", "League")
                    cur_h = int(((match.get("homeScore") or {}).get("current")) or 0)
                    cur_a = int(((match.get("awayScore") or {}).get("current")) or 0)
                    info = {
                        "home": home_name, "away": away_name, "league": league_name,
                        "score_h": cur_h, "score_a": cur_a,
                        "minute": f"{minute}'"
                    }
                    if mid not in self.sent_signals:
                        print(cl(f"🔥 [{strategy.name}] {league_name}: {home_name} vs {away_name} ({minute}')", "GR"), flush=True)
                        msg_id = self.tg.send_signal(strategy, info, signal, mid)
                        if msg_id:
                            self.bankroll.place_bet(mid, msg_id, strategy, signal, info)
                            self.sent_signals[mid] = time.time()


if __name__ == "__main__":
    token, chat_id = load_credentials()
    LiveMonitor(token, chat_id).run()
