import io
import os
import json
import time
import random
import string
import ssl
import socket
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from curl_cffi import requests as curl_requests
from unixgram import Bot, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from unixgram.exceptions import NetworkError, ApiError


# === Харднинг транспорта unixgram через curl_cffi (обход DPI) ===
from unixgram.api import ApiClient as _OrigApiClient

_orig_call = _OrigApiClient.call
_curl_session = curl_requests.Session(impersonate="chrome")


def _patched_call(self, method, params=None, timeout=None):
    import json as _json
    from unixgram.exceptions import ApiError, NetworkError
    from unixgram.api import InputFile, _encode_multipart

    payload = {k: v for k, v in (params or {}).items() if v is not None}
    files = {k: v for k, v in payload.items() if isinstance(v, InputFile)}
    if files:
        body, content_type = _encode_multipart(
            {k: v for k, v in payload.items() if k not in files}, files
        )
    else:
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        content_type = "application/json"

    url = self._url(method)
    headers = {"Content-Type": content_type, "User-Agent": "unixgram-py/1.0"}

    try:
        resp = _curl_session.post(
            url, data=body, headers=headers, timeout=timeout or self.timeout
        )
        raw = resp.text
        if resp.status_code >= 400:
            try:
                envelope = _json.loads(raw)
            except _json.JSONDecodeError:
                raise ApiError(resp.status_code, raw or resp.reason, method) from None
            raise ApiError(
                int(envelope.get("error_code", resp.status_code)),
                str(envelope.get("description", resp.reason)),
                method,
            ) from None
    except ApiError:
        raise
    except Exception as e:
        raise NetworkError(f"{method}: {e}") from None

    envelope = _json.loads(raw)
    if not envelope.get("ok"):
        raise ApiError(
            int(envelope.get("error_code", 0)),
            str(envelope.get("description", "")),
            method,
        )
    return envelope.get("result")


_OrigApiClient.call = _patched_call


bot = Bot("4477409574:kn2a99r8WvKxMqPqqw4j4FTNxbcBbF4c", timeout=15)

# "Connection": "close" защищает от разрывов сокета и ошибок SSL EOF
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "close"
}

# Сессия с автоматическим пересозданием соединений при SSL EOF
session = requests.Session()
retries = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    raise_on_status=False
)
adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
session.mount("https://", adapter)
session.mount("http://", adapter)

ADMIN_USERNAMES = {"psp"}
VIP_USERS: set[str] = set()
VIP_HISTORY: set[str] = set()
KNOWN_USERS: dict[int, str] = {}
ADMIN_STATE: dict[int, str] = {}
ACTIVE_TASKS: dict[int, float] = {}
TASK_TIMEOUT = 60

VIP_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vip_data.json")
SHOWN_WORDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shown_words.json")
SHOWN_WORDS_MAX = 200


def _save_vip_data():
    try:
        with open(VIP_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": sorted(VIP_USERS), "history": sorted(VIP_HISTORY)}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[!] Ошибка сохранения VIP: {e}")


def _load_vip_data():
    global VIP_USERS, VIP_HISTORY
    try:
        with open(VIP_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        VIP_USERS = set(data.get("users", []))
        VIP_HISTORY = set(data.get("history", []))
        print(f"[+] Загружено VIP: {len(VIP_USERS)} активных, {len(VIP_HISTORY)} всего")
    except FileNotFoundError:
        print("[*] Файл vip_data.json не найден, начинаем с пустого списка")
    except Exception as e:
        print(f"[!] Ошибка загрузки VIP: {e}")


_load_vip_data()


def _load_shown_words() -> list[str]:
    try:
        with open(SHOWN_WORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _load_shown_words_set() -> set[str]:
    return set(_load_shown_words())


def _save_shown_words(words: list[str]):
    try:
        with open(SHOWN_WORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(words[-SHOWN_WORDS_MAX:], f, ensure_ascii=False)
    except Exception:
        pass


def _filter_shown(words: list[str]) -> list[str]:
    shown = set(_load_shown_words())
    fresh = [w for w in words if w.lower() not in shown]
    if len(fresh) < len(words) // 2:
        shown.clear()
        _save_shown_words([])
        return words
    return fresh


def _mark_shown(words: list[str]):
    shown = _load_shown_words()
    shown.extend(w.lower() for w in words)
    _save_shown_words(shown)


BLOCKED_KEYWORDS = {
    "admin", "unix", "root", "help", "main", "shop", "tech", "info", "test",
    "nebula", "shadow", "zenith", "matrix", "aurora", "vortex", "phantom",
    "scream", "linch", "folio"
}

DICTIONARY_URLS = {
    "google": [
        "https://raw.githubusercontent.com/first20hours/google-10000-english/master/google-10000-english-usa-no-swears-medium.txt",
        "https://raw.githubusercontent.com/first20hours/google-10000-english/master/google-10000-english-usa-no-swears-long.txt",
    ],
    "english": [
        "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt",
    ],
    "webstandards": [
        "https://raw.githubusercontent.com/web-standards/dictionary/main/dictionary.md",
    ],
}

WORDS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "words_cache.json")
CACHE_TTL = 86400


def _load_cache() -> dict:
    try:
        with open(WORDS_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("ts", 0) < CACHE_TTL:
            return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    data["ts"] = time.time()
    try:
        with open(WORDS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch_url(url: str) -> str:
    try:
        r = _curl_session.get(url, timeout=15)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        print(f"[!] Ошибка загрузки {url}: {e}")
    return ""


def _parse_dictionary_words(raw: str) -> list[str]:
    words = []
    for line in raw.splitlines():
        w = line.strip().lower()
        if w and w.isalpha() and 3 <= len(w) <= 10 and w not in BLOCKED_KEYWORDS:
            words.append(w)
    return list(set(words))


def _parse_webstandards(raw: str) -> list[str]:
    words = []
    import re
    for match in re.finditer(r'###\s+([a-z][a-z0-9\-]+)', raw):
        w = match.group(1).strip().lower()
        if w and w.isalpha() and 3 <= len(w) <= 12 and w not in BLOCKED_KEYWORDS:
            words.append(w)
    return list(set(words))


def _categorize_words(all_words: list[str]) -> dict:
    rare_letters = set("qxzjkvwy")
    archaic_patterns = ["th", "ght", "ence", "ance", "ous", "ious", "eous", "ough"]
    narrow_patterns = ["tion", "sion", "ment", "ness", "ity", "ism", "ist", "ics", "ine", "ase", "ose", "emia", "itis", "osis"]

    categories = {"rare": [], "archaic": [], "narrow": []}

    for w in all_words:
        wl = w.lower()
        score_rare = sum(1 for c in wl if c in rare_letters)
        score_archaic = sum(1 for p in archaic_patterns if wl.endswith(p))
        score_narrow = sum(1 for p in narrow_patterns if wl.endswith(p))

        if score_rare >= 2:
            categories["rare"].append(wl)
        elif score_archaic >= 1:
            categories["archaic"].append(wl)
        elif score_narrow >= 1:
            categories["narrow"].append(wl)

    for cat in categories:
        random.shuffle(categories[cat])

    return categories


def _build_word_libraries() -> dict:
    cached = _load_cache()
    if "rare" in cached and "archaic" in cached and "narrow" in cached:
        print(f"[+] Загружено из кэша: rare={len(cached['rare'])}, archaic={len(cached['archaic'])}, narrow={len(cached['narrow'])}")
        return cached

    all_words = []
    for source, urls in DICTIONARY_URLS.items():
        for url in urls:
            raw = _fetch_url(url)
            if raw:
                if "web-standards" in url:
                    words = _parse_webstandards(raw)
                else:
                    words = _parse_dictionary_words(raw)
                all_words.extend(words)
                print(f"[+] {source}: {len(words)} слов из {url.split('/')[-1]}")

    all_words = list(set(all_words))
    print(f"[+] Всего уникальных слов: {len(all_words)}")

    categories = _categorize_words(all_words)

    result = {
        "rare": categories["rare"],
        "archaic": categories["archaic"],
        "narrow": categories["narrow"],
        "all": all_words,
    }
    _save_cache(result)

    print(f"[+] Категории: rare={len(result['rare'])}, archaic={len(result['archaic'])}, narrow={len(result['narrow'])}")
    return result


WORD_LIBRARIES = _build_word_libraries()

WORD_CATEGORY_META = {
    "rare": {"title": "💎 Дорогие/редкие", "description": "Редкие буквы, звучание ценности"},
    "archaic": {"title": "📜 Устаревшие/архаичные", "description": "Старинные, редко используемые слова"},
    "narrow": {"title": "🔬 Узкоспециализированные", "description": "Научные, технические, медицинские термины"},
}


def fetch_words_by_category(category: str, limit: int = 30) -> list[str]:
    words = WORD_LIBRARIES.get(category, [])
    if not words:
        words = WORD_LIBRARIES.get("all", [])
    words = [w for w in words if w.lower() not in BLOCKED_KEYWORDS]
    words = _filter_shown(words)
    if len(words) < limit:
        _save_shown_words([])
        words = _filter_shown(words)
    random.shuffle(words)
    picked = words[:limit]
    _mark_shown(picked)
    return picked


def _calc_username_value(word: str, cat: str) -> int:
    """Calculates username value 10-500 based on rarity."""
    rare_letters = set("qxzjkvwy")
    common_letters = set("etaoinsrhld")
    score = 50

    for c in word.lower():
        if c in rare_letters:
            score += 15
        elif c not in common_letters:
            score += 8

    cat_bonus = {"rare": 100, "archaic": 70, "narrow": 40}
    score += cat_bonus.get(cat, 0)

    length_bonus = {3: 80, 4: 50, 5: 30, 6: 10}
    score += length_bonus.get(len(word), 0)

    return max(10, min(500, score))


def fetch_words_mixed(limit: int = 30) -> list[tuple[str, str, str, int]]:
    result = []
    shown_set = _load_shown_words_set()
    for cat_key in ["rare", "archaic", "narrow"]:
        meta = WORD_CATEGORY_META.get(cat_key, {})
        emoji = meta.get("title", "📖").split()[0]
        for w in WORD_LIBRARIES.get(cat_key, []):
            if w.lower() not in BLOCKED_KEYWORDS and w.lower() not in shown_set:
                val = _calc_username_value(w, cat_key)
                result.append((w, cat_key, emoji, val))
    if len(result) < limit:
        _save_shown_words([])
        shown_set = set()
        result = []
        for cat_key in ["rare", "archaic", "narrow"]:
            meta = WORD_CATEGORY_META.get(cat_key, {})
            emoji = meta.get("title", "📖").split()[0]
            for w in WORD_LIBRARIES.get(cat_key, []):
                if w.lower() not in BLOCKED_KEYWORDS:
                    val = _calc_username_value(w, cat_key)
                    result.append((w, cat_key, emoji, val))
    random.shuffle(result)
    picked = result[:limit]
    _mark_shown([w for w, _, _, _ in picked])
    return picked


def safe_send_message(chat_id, text, **kwargs):
    """Отправка сообщений с повторными попытками при SSL-сбоях."""
    for attempt in range(3):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except (NetworkError, ApiError, ssl.SSLError, Exception) as e:
            if attempt == 2:
                print(f"[!] Окончательная ошибка отправки сообщения: {e}")
                return None
            time.sleep(0.5)


def is_user_vip(event) -> bool:
    user = getattr(event, "from_user", None)
    if not user:
        return False

    username = (getattr(user, "username", "") or "").lower().lstrip("@")
    return (username in ADMIN_USERNAMES) or (username in VIP_USERS)


def generate_random_4char() -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(random.choices(chars, k=4))


def fetch_rare_dictionary(limit: int = 30) -> list[str]:
    url = random.choice(DICTIONARY_URLS)
    try:
        response = session.get(url, headers=HEADERS, timeout=5)
        if response.status_code == 200:
            raw_words = response.text.splitlines()
            valid_words = [
                w.strip().lower() for w in raw_words 
                if 4 <= len(w.strip()) <= 6 and w.strip().isalpha() and w.strip().lower() not in BLOCKED_KEYWORDS
            ]
            if len(valid_words) >= limit:
                random.shuffle(valid_words)
                return valid_words[:limit]
    except Exception as e:
        print(f"[!] Ошибка загрузки словаря: {e}")

    random.shuffle(FALLBACK_WORDS)
    return FALLBACK_WORDS[:limit]



COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unixgram_cookies.json")


def _load_unixgram_cookies() -> dict:
    """Загружает куки unixgram.com."""
    import sqlite3, shutil, tempfile

    env_cookies = os.environ.get("UNIXGRAM_COOKIES")
    if env_cookies:
        try:
            cookies = json.loads(env_cookies)
            print(f"[+] Куки загружены из env ({len(cookies)} шт)")
            return cookies
        except Exception as e:
            print(f"[!] Ошибка парсинга кук из env: {e}")
    else:
        print("[!] UNIXGRAM_COOKIES не задана")

    cookies = {}

    firefox_path = os.path.expanduser("~/.mozilla/firefox")
    if os.path.isdir(firefox_path):
        for profile_dir in [d for d in os.listdir(firefox_path)
                            if d.endswith("-esr") or d.endswith("-default")]:
            db_path = os.path.join(firefox_path, profile_dir, "cookies.sqlite")
            if not os.path.exists(db_path):
                continue
            tmp = os.path.join(tempfile.gettempdir(), f"ff_cookies_{profile_dir}.sqlite")
            try:
                shutil.copy2(db_path, tmp)
                conn = sqlite3.connect(tmp)
                cur = conn.cursor()
                cur.execute("SELECT name, value, host FROM moz_cookies WHERE host LIKE '%unixgram%'")
                for name, value, host in cur.fetchall():
                    if "unixgram.com" in host:
                        cookies[name] = value
                conn.close()
                os.unlink(tmp)
            except Exception as e:
                print(f"[!] Ошибка чтения кук Firefox: {e}")

    if cookies:
        try:
            with open(COOKIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False)
            print(f"[+] Куки сохранены ({len(cookies)} шт)")
        except Exception:
            pass
        return cookies

    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        print(f"[+] Куки загружены из файла ({len(cookies)} шт)")
        return cookies
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    print("[!] Куки unixgram не найдены — веб-проверка не работает")
    return {}


def _web_check(username: str) -> tuple[bool | None, str]:
    """Проверка через веб. Ловит алиасы по редиректам и NEXT_REDIRECT."""
    import re
    username = username.lower().strip().lstrip("@")
    url = f"https://unixgram.com/u/{username}"

    try:
        cookies = _load_unixgram_cookies()
        _web_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        res = requests.get(url, timeout=10, allow_redirects=False, headers=_web_headers, cookies=cookies)

        if res.status_code in (301, 302, 307, 308):
            location = res.headers.get("location", "")
            if "/u/" in location:
                target = location.split("/u/")[-1].strip("/").split("?")[0]
                if target.lower() != username.lower():
                    return True, f"Занят (алиас @{target})"

        if res.status_code == 200:
            html = res.text
            redirect_match = re.search(r'NEXT_REDIRECT.*?/u/([^";]+)', html)
            if redirect_match:
                target = redirect_match.group(1)
                if target.lower() != username.lower():
                    return True, f"Занят (алиас @{target})"
            return False, "Свободен"

        if res.status_code == 404:
            return False, "Свободен"

    except Exception as e:
        print(f"[!] Web check error @{username}: {e}")

    return None, "Проверь вручную"


def _fast_check(username: str) -> tuple[bool, str]:
    """Быстрая проверка одного юзера с коротким таймаутом."""
    username = username.lower().strip().lstrip("@")
    if len(username) < 4:
        return False, "Короткий"
    if username in BLOCKED_KEYWORDS:
        return False, "Зарезервировано"

    try:
        chat = bot.get_chat(f"@{username}")
        if chat:
            return False, "Занят"
    except Exception as e:
        err_msg = str(e).lower()
        is_not_found = any(k in err_msg for k in [
            "not found", "chat not found", "404",
            "username_not_occupied", "user_not_found",
            "bad request", "invalid user",
            "чат не найден", "не найден",
        ])
        if not is_not_found:
            return False, "Ошибка API"

    web_ok, web_reason = _web_check(username)
    if web_ok is True:
        return False, web_reason
    if web_ok is False:
        return True, "Свободен"

    return False, "Не удалось проверить"


def _bulk_check(candidates: list[str], needed: int) -> list[str]:
    """Проверяет кандидатов пачками по 8 параллельных потоков."""
    import concurrent.futures
    free = []
    checked = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(_fast_check, c): c for c in candidates}
        for future in concurrent.futures.as_completed(future_map):
            checked += 1
            ok, reason = future.result()
            if ok:
                free.append(future_map[future])
                print(f"[+] [{checked}/{len(candidates)}] 🟢 @{future_map[future]}")
            if len(free) >= needed:
                break
    return free


def get_main_keyboard(event):
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("🎲 Найти 1 свободный 4-знак", callback_data="free_random"))

    if is_user_vip(event):
        keyboard.add(InlineKeyboardButton("⚡ VIP: Найти 10 свободных 4-знаков", callback_data="vip_random_10"))
        keyboard.add(InlineKeyboardButton("💎 VIP: Дорогие/редкие ники", callback_data="vip_cat_rare"))
        keyboard.add(InlineKeyboardButton("📜 VIP: Устаревшие/архаичные ники", callback_data="vip_cat_archaic"))
        keyboard.add(InlineKeyboardButton("🔬 VIP: Узкоспецилизированные ники", callback_data="vip_cat_narrow"))
        keyboard.add(InlineKeyboardButton("🌐 VIP: Все категории (с метками)", callback_data="vip_cat_mixed"))
    else:
        keyboard.add(InlineKeyboardButton("⭐ Купить VIP (100 Звёзд)", callback_data="buy"))

    keyboard.add(InlineKeyboardButton("👤 Профиль", callback_data="profile"))

    return keyboard


@bot.message_handler(commands=["start"])
def start_command(message):
    user = getattr(message, "from_user", None)
    if user:
        uid = getattr(user, "id", None)
        uname = getattr(user, "username", "") or ""
        fname = getattr(user, "first_name", "") or ""
        if uid:
            KNOWN_USERS[uid] = f"@{uname}" if uname else fname

    safe_send_message(
        message.chat.id,
        "👋 **Привет! Я помогу найти свободные юзернеймы в Unixgram.**\n\n"
        "• Отправь мне `@username`, чтобы проверить конкретный ник.\n"
        "• Нажми кнопку ниже, чтобы найти свободный 4-значный ник.\n"
        "• Для VIP-пользователей доступен поиск пачками по 10 штук и сканирование редких слов.",
        reply_markup=get_main_keyboard(message),
        parse_mode="Markdown"
    )


PROCESSED_CALLBACKS = {}
CALLBACK_TTL = 10

@bot.callback_query_handler(func=lambda call: not call.data.startswith("admin_"))
def handle_callback(call):
    now = time.time()
    if call.id in PROCESSED_CALLBACKS:
        return
    PROCESSED_CALLBACKS[call.id] = now
    for k in list(PROCESSED_CALLBACKS):
        if now - PROCESSED_CALLBACKS[k] > CALLBACK_TTL:
            del PROCESSED_CALLBACKS[k]

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user_id = getattr(call.from_user, "id", None)
    if user_id is None:
        return
    now = time.time()
    task_start = ACTIVE_TASKS.get(user_id)
    if task_start is not None and (now - task_start) < TASK_TIMEOUT:
        safe_send_message(call.message.chat.id, "⏳ Предыдущая задача ещё выполняется, подождите.")
        return
    ACTIVE_TASKS[user_id] = now
    t = threading.Thread(target=_process_callback, args=(call, user_id), daemon=True)
    t.start()


def _process_callback(call, user_id):
    try:
        if call.data == "profile":
            vip = "✅ Активна" if user_id in VIP_USERS or (call.from_user.username or "").lower() in VIP_USERS else "❌ Нет"
            text = f"👤 Профиль\n\n🆔 ID: {user_id}\n📛 @{call.from_user.username or 'нет'}\n⭐ VIP: {vip}"
            back_kb = InlineKeyboardMarkup()
            back_kb.add(InlineKeyboardButton("← Назад", callback_data="back_to_menu"))
            safe_send_message(call.message.chat.id, text, reply_markup=back_kb)
            return

        if call.data == "back_to_menu":
            safe_send_message(call.message.chat.id, "Главное меню:", reply_markup=get_main_keyboard(call))
            return

        if call.data == "free_random":
            safe_send_message(call.message.chat.id, "🎲 Генерирую 10 кандидатов...")

            candidates = list({generate_random_4char() for _ in range(20)})
            safe_send_message(call.message.chat.id, "🎯 Юзернеймы (проверь вручную):\n\n" + "\n".join(f"• @{h}" for h in candidates[:10]))

        elif call.data == "vip_random_10":
            if not is_user_vip(call):
                safe_send_message(call.message.chat.id, "🔒 Доступно только для VIP! Нажмите /buy")
                return

            candidates = list({generate_random_4char() for _ in range(30)})
            safe_send_message(call.message.chat.id, "🎯 [VIP] 20 кандидатов:\n\n" + "\n".join(f"• @{h}" for h in candidates[:20]))

        elif call.data.startswith("vip_cat_"):
            if not is_user_vip(call):
                safe_send_message(call.message.chat.id, "🔒 Доступно только для VIP! Нажмите /buy")
                return

            category = call.data.replace("vip_cat_", "")
            
            if category == "mixed":
                items = fetch_words_mixed(limit=30)
                report = "🌐 VIP: Все категории\n\n"
                report += "\n".join(f"• @{w} {emoji} 💰{val}" for w, _, emoji, val in items)
                raw_bytes = "\n".join(w for w, _, _, _ in items).encode("utf-8")
            else:
                cat_meta = WORD_CATEGORY_META.get(category, {})
                title = cat_meta.get("title", "📖")
                description = cat_meta.get("description", "")
                words = fetch_words_by_category(category, limit=30)
                report = f"{title}\n\n"
                report += "\n".join(f"• @{w}" for w in words)
                raw_bytes = "\n".join(words).encode("utf-8")
            
            safe_send_message(call.message.chat.id, report)
            try:
                bot.send_document(call.message.chat.id, InputFile(raw_bytes, name=f"words_{category}.txt"))
            except Exception:
                pass

        elif call.data == "buy":
            try:
                bot.send_invoice(
                    chat_id=call.message.chat.id,
                    title="VIP Подписка",
                    description="Поиск пачками по 10 ников и парсинг редких словарей",
                    payload="vip_access_pack",
                    amount_stars=100
                )
            except Exception as e:
                print(f"[!] Ошибка выписки инвойса: {e}")

    except Exception as e:
        print(f"[!] Сбой в обработке callback: {e}")
    finally:
        ACTIVE_TASKS.pop(user_id, None)


@bot.pre_checkout_query_handler()
def process_pre_checkout(query):
    try:
        bot.answer_pre_checkout_query(query.id, ok=True)
        user = getattr(query, "from_user", None)
        if user:
            uname = (getattr(user, "username", "") or "").lower().lstrip("@")
            if uname:
                VIP_USERS.add(uname)
                _save_vip_data()
    except Exception as e:
        print(f"[!] Ошибка pre_checkout: {e}")


@bot.message_handler(commands=["buy"])
def buy_command(message):
    try:
        bot.send_invoice(
            chat_id=message.chat.id,
            title="VIP Подписка",
            description="Поиск пачками по 10 ников и парсинг редких словарей",
            payload="vip_access_pack",
            amount_stars=100
        )
    except Exception as e:
        print(f"[!] Ошибка отправки команды buy: {e}")


def _is_admin(message) -> bool:
    username = (getattr(getattr(message, "from_user", None), "username", "") or "").lower().lstrip("@")
    return username in ADMIN_USERNAMES


def _admin_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("👑 Список VIP", callback_data="admin_vip_list"))
    kb.add(InlineKeyboardButton("➕ Выдать VIP", callback_data="admin_add_vip"))
    kb.add(InlineKeyboardButton("➖ Забрать VIP", callback_data="admin_del_vip"))
    kb.add(InlineKeyboardButton("🔄 Обновить", callback_data="admin_refresh"))
    return kb


@bot.message_handler(commands=["apanel"])
def admin_panel(message):
    if not _is_admin(message):
        safe_send_message(message.chat.id, "Нет доступа.")
        return
    ADMIN_STATE.pop(message.chat.id, None)
    _send_admin_panel(message.chat.id)


def _send_admin_panel(chat_id):
    all_vips = VIP_HISTORY | VIP_USERS
    if all_vips:
        lines = []
        for n in sorted(all_vips):
            status = "✅" if n in VIP_USERS else "🚫"
            lines.append(f"{status} @{n}")
        vip_text = "\n".join(lines)
    else:
        vip_text = "— пусто —"

    text = (
        f"<b>Админ-панель</b>\n\n"
        f"<b>Все VIP ({len(all_vips)}):</b>\n{vip_text}\n\n"
        f"✅ = активный  🚫 = был VIP\n\n"
        f"Напиши <b>@ник</b> чтобы выдать/забрать."
    )
    safe_send_message(chat_id, text, parse_mode="HTML", reply_markup=_admin_keyboard())


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def handle_admin_callback(call):
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    user = getattr(call, "from_user", None)
    uname = (getattr(user, "username", "") or "").lower().lstrip("@")
    if uname not in ADMIN_USERNAMES:
        safe_send_message(call.message.chat.id, "Нет доступа.")
        return

    if call.data == "admin_vip_list":
        _send_admin_panel(call.message.chat.id)

    elif call.data == "admin_add_vip":
        ADMIN_STATE[call.message.chat.id] = "add_vip"
        safe_send_message(
            call.message.chat.id,
            "✍️ Напиши <b>@ник</b> человека чтобы выдать VIP:",
            parse_mode="HTML"
        )

    elif call.data == "admin_del_vip":
        ADMIN_STATE[call.message.chat.id] = "del_vip"
        safe_send_message(
            call.message.chat.id,
            "✍️ Напиши <b>@ник</b> человека чтобы забрать VIP:",
            parse_mode="HTML"
        )

    elif call.data == "admin_refresh":
        ADMIN_STATE.pop(call.message.chat.id, None)
        _send_admin_panel(call.message.chat.id)


@bot.message_handler(func=lambda m: bool(m.body or m.text) and not (m.body or m.text).startswith("/"))
def check_single_handle(message):
    user = getattr(message, "from_user", None)
    if user:
        uid = getattr(user, "id", None)
        uname = getattr(user, "username", "") or ""
        fname = getattr(user, "first_name", "") or ""
        if uid:
            KNOWN_USERS[uid] = f"@{uname}" if uname else fname

    chat_id = message.chat.id
    state = ADMIN_STATE.get(chat_id)

    if state == "add_vip":
        ADMIN_STATE.pop(chat_id, None)
        nick = (message.body or message.text or "").strip().lstrip("@").lower()
        if not nick:
            safe_send_message(chat_id, "Пустой ник.")
            return
        VIP_USERS.add(nick)
        VIP_HISTORY.add(nick)
        _save_vip_data()
        safe_send_message(chat_id, f"✅ VIP выдан @{nick}", reply_markup=_admin_keyboard())
        return

    if state == "del_vip":
        ADMIN_STATE.pop(chat_id, None)
        nick = (message.body or message.text or "").strip().lstrip("@").lower()
        if not nick:
            safe_send_message(chat_id, "Пустой ник.")
            return
        VIP_USERS.discard(nick)
        _save_vip_data()
        safe_send_message(chat_id, f"❌ VIP убран у @{nick}", reply_markup=_admin_keyboard())
        return

    text = message.body or message.text
    username = text.strip().lstrip("@")

    if len(username) < 4:
        safe_send_message(message.chat.id, "Минимальная длина — 4 символа.")
        return

    if not all(c.isascii() and (c.isalnum() or c == "_") for c in username):
        safe_send_message(message.chat.id, "Только латинские буквы, цифры и _")
        return

    if username in BLOCKED_KEYWORDS:
        safe_send_message(message.chat.id, "Этот ник зарезервирован.")
        return

    is_free, reason = _fast_check(username)
    if not is_free:
        safe_send_message(
            message.chat.id,
            f"🔴 Ник @{username} занят ({reason}).",
        )
        return

    safe_send_message(
        message.chat.id,
        f"🟢 Ник @{username} свободен! ({reason})",
    )


if __name__ == "__main__":
    print("[+] Бот запущен и готов к работе.")
    while True:
        try:
            bot.polling(timeout=10, none_stop=True)
        except (NetworkError, ssl.SSLError, Exception) as err:
            print(f"[!] Переподключение после сетевого сбоя ({err})...")
            time.sleep(2)
