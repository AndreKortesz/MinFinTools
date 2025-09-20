import os
import re
import json
import time
import html
import random
import hashlib
import logging
import threading
from io import BytesIO
from time import mktime
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import pytz
import httpx
import feedparser
import telegram
from telegram.error import BadRequest
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request

# ─── Настройки ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID не задан в переменных окружения")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не задан в переменных окружения")

# ➕ Новое: базовый каталог для персистентных файлов (Railway Volume)
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Максимальная длина подписи к фото в Telegram
CAPTION_LIMIT = int(os.getenv("TG_CAPTION_LIMIT", "1024"))

# анти-повторы новостей — можно переопределять через ENV
SEEN_NEWS_FILE = os.getenv("SEEN_NEWS_FILE", os.path.join(DATA_DIR, "seen_news.json"))
SEEN_MAX_DAYS = int(os.getenv("SEEN_MAX_DAYS", "7"))
SEEN_MAX_ITEMS = int(os.getenv("SEEN_MAX_ITEMS", "1000"))

# ➕ Новое: куда класть состояние ротации (на Volume)
ROTATION_STATE_FILE = os.getenv("ROTATION_STATE_FILE", os.path.join(DATA_DIR, "rotation_state.json"))

client = OpenAI(api_key=OPENAI_API_KEY)
bot = telegram.Bot(token=TELEGRAM_TOKEN)
scheduler = BackgroundScheduler(timezone=pytz.timezone("Europe/Moscow"))

# блокировка на случай одновременных вызовов (scheduler + /test)
ROT_LOCK = threading.Lock()

NEGATIVE_SUFFIX = (
    "Строго БЕЗ текста, букв, цифр и логотипов. "
    "Запрет: плоская векторная графика, иконки, комикс, 2D-иллюстрация, клипарт."
)

# ─── Маршруты здоровья / ручной триггер ───────────────────────────────────────
@app.route("/")
def home():
    return "Бот работает ✅", 200

@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/test")
def manual_test():
    # опциональная защита токеном: задайте TEST_TOKEN в ENV
    token = request.args.get("token")
    expected = os.getenv("TEST_TOKEN")
    if expected and token != expected:
        return "Forbidden", 403

    kind = request.args.get("type", "news")  # "news" | "rubric" | "history"
    try:
        if kind == "rubric":
            scheduled_rubric_post()
            return "✅ Отправлен следующий рубричный пост по ротации", 200
        elif kind == "history":
            scheduled_history_post()
            return "✅ Отправлен исторический пост «В этот день в финансах»", 200
        else:
            scheduled_news_post()
            return "✅ Отправлен следующий новостной пост по ротации", 200
    except Exception as e:
        return f"❌ Ошибка: {e}", 500

# ─── Утилиты ──────────────────────────────────────────────────────────────────
def clean_html(raw_html: str) -> str:
    return re.sub(re.compile('<.*?>'), '', raw_html or "")

def _canonical_link(link: str) -> str:
    if not link:
        return ""
    try:
        u = urlsplit(link)
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
             if not k.lower().startswith("utm_")]
        return urlunsplit((u.scheme, u.netloc.lower(), u.path, urlencode(sorted(q)), ""))
    except Exception:
        return link

def _story_id(title: str, link: str) -> str:
    canon = _canonical_link(link)
    base = (canon or title or "").lower()
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"[^\w\s/.\-]+", "", base)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

def _load_seen() -> dict:
    if os.path.exists(SEEN_NEWS_FILE):
        try:
            with open(SEEN_NEWS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_seen(seen: dict):
    try:
        os.makedirs(os.path.dirname(SEEN_NEWS_FILE) or DATA_DIR, exist_ok=True)  # ➕ ensure dir
        with open(SEEN_NEWS_FILE, "w", encoding="utf-8") as f:
            json.dump(seen, f, ensure_ascii=False)
    except Exception:
        pass

def _prune_seen(seen: dict):
    now = time.time()
    cutoff = now - SEEN_MAX_DAYS * 86400
    # по времени
    for k in list(seen.keys()):
        if seen[k] < cutoff:
            del seen[k]
    # по размеру
    if len(seen) > SEEN_MAX_ITEMS:
        keep = dict(sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:SEEN_MAX_ITEMS])
        seen.clear()
        seen.update(keep)

def _mark_seen(story_id: str):
    seen = _load_seen()
    seen[story_id] = time.time()
    _prune_seen(seen)
    _save_seen(seen)

def _is_seen(story_id: str) -> bool:
    return story_id in _load_seen()

# ➕ Новое: персистентная ротация индексов
def _load_rotation_state() -> dict:
    try:
        with open(ROTATION_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_rotation_state(state: dict):
    try:
        os.makedirs(os.path.dirname(ROTATION_STATE_FILE) or DATA_DIR, exist_ok=True)  # ➕ ensure dir
        tmp = ROTATION_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, ROTATION_STATE_FILE)  # атомарная запись
    except Exception as e:
        logger.warning(f"Не удалось сохранить состояние ротации: {e}")

def _next_index(kind: str, total: int) -> int:
    """
    Возвращает ТЕКУЩИЙ индекс для kind ('rubric'|'news') и сразу
    продвигает его на +1 по кольцу. Потокобезопасно и переживает перезапуски.
    """
    with ROT_LOCK:
        state = _load_rotation_state()
        key = f"{kind}_index"
        idx = int(state.get(key, 0))
        state[key] = (idx + 1) % total
        _save_rotation_state(state)
        # держим глобалки в синхроне (если где-то читаются)
        if kind == "rubric":
            globals()["rubric_index"] = state[key]
        elif kind == "news":
            globals()["news_index"] = state[key]
        return idx

def _polish_and_to_html(text: str) -> str:
    """
    1) убирает '— Подсчёт: ...'
    2) нормализует подзаголовки (эмодзи + жирный)
    3) вставляет пустые строки перед подзаголовками
    4) конвертирует **...** → <b>...</b> и экранирует HTML
    """
    t = (text or "").strip()
    # убрать строку '— Подсчёт: ...'
    t = re.sub(r'(?im)^\s*[—\-–]\s*Подсч[её]т:.*$', '', t)

    # заголовки
    t = re.sub(r'(?im)^\s*Аналитика\s*:?\s*$', '**📊 Аналитика:**', t, flags=re.MULTILINE)
    t = re.sub(r'(?im)^\s*Прогноз\s*:?\s*$',   '**📈 Прогноз:**',   t, flags=re.MULTILINE)
    t = re.sub(r'(?im)^\s*Вывод\s*:?\s*$',     '**🧭 Вывод:**',     t, flags=re.MULTILINE)
    t = re.sub(r'(?im)^\s*Шаги\s*:?\s*$',                 '**🧩 Шаги:**',                 t, flags=re.MULTILINE)
    t = re.sub(r'(?im)^\s*Что делать инвестору\s*:?\s*$', '**🧭 Что делать инвестору:**', t, flags=re.MULTILINE)

    # пустая строка перед подзаголовками
    t = re.sub(r'(?m)([^\n])\n(\*\*[^\n]*\*\*)', r'\1\n\n\2', t)
    # и перенос после
    t = re.sub(r'(?m)(\*\*[^\n]*\*\*)\n(?!\n)', r'\1\n', t)

    # временно скрываем **...**
    placeholders = []
    def _keep_bold(m):
        placeholders.append(m.group(1))
        return f"@@B{len(placeholders)-1}@@"

    t = re.sub(r"\*\*(.+?)\*\*", _keep_bold, t, flags=re.DOTALL)

    # экранируем HTML
    t = html.escape(t)

    # возвращаем <b>...</b>
    for i, content in enumerate(placeholders):
        t = t.replace(f"@@B{i}@@", f"<b>{html.escape(content)}</b>")

    return t.strip()

def _regenerate_to_fit(original_text: str, target_limits=(940, 900, 860)) -> str:
    """
    Перегенерирует пост короче, чтобы уместиться в лимит подписи Telegram после HTML.
    Не добавляет «…», не обрезает — просит LLM написать компактнее.
    """
    base = (original_text or "").strip()
    for tgt in target_limits:
        try:
            prompt = (
                "Перепиши этот пост КОРОЧЕ, сохранив структуру и смысл: "
                "заголовок с эмодзи, подзаголовок-зацеп, краткое вступление, "
                "жирные подзаголовки, аналитика/прогноз, вывод, вопрос в конце. "
                "Без хештегов. Без искусственного многоточия в конце. "
                f"СТРОГО: общий объём не более {tgt} символов в чистом тексте.\n\n"
                f"Текст:\n{base}"
            )
            new_text = generate_post_text(prompt)
            if not new_text:
                continue
            html_ver = _polish_and_to_html(new_text)
            if len(html_ver) <= CAPTION_LIMIT:
                return new_text.strip()
        except Exception:
            continue
    return base  # если не уложились после нескольких попыток — вернём исходник

# ─── Контентные настройки (оставлены как были) ────────────────────────────────
rubrics = [
    "Финсовет дня", "Финликбез", "Личный финменеджмент", "Деньги в цифрах",
    "Кейс / Разбор", "Психология денег", "Финансовая ошибка", "Продукт недели",
    "Инвест-горизонт", "Миф недели", "Путь к 1 млн", "Финансовая привычка",
    "Вопрос — ответ", "Excel / Таблица", "Финансовая цитата", "Инструмент недели"
]

news_themes = [
    "Финансовые новости России",
    "Новости криптовалют",
    "Новости фондовых рынков (Россия и США)",
    "Финансовые новости США и мира",
]

rss_sources = {
    "Финансовые новости России": [
        "https://rssexport.rbc.ru/rbcnews/news/20/full.rss",
        "https://tass.ru/rss/v2.xml?rubric=ekonomika",
        "https://www.interfax.ru/rss.asp",
        "https://www.forbes.ru/newrss.xml",
        "https://www.moex.com/export/news.aspx?cat=news&fmt=rss",
        "https://www.vedomosti.ru/rss/news",
        "https://www.коммерсант.ru/RSS/news.xml".replace("коммерсант", "kommersant"),
    ],
    "Новости криптовалют": [
        "https://forklog.com/feed/",
        "https://bitnovosti.com/feed/",
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://www.theblock.co/rss",
        "https://bits.media/rss/",
        "https://decrypt.co/feed",
    ],
    "Новости фондовых рынков (Россия и США)": [
        "https://rssexport.rbc.ru/rbcnews/news/21/full.rss",
        "https://www.finam.ru/rss/news.rss",
    ],
    "Финансовые новости США и мира": [
        "https://www.ft.com/markets?format=rss",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "https://feeds.reuters.com/reuters/businessNews",
        "https://apnews.com/apf-business?output=rss",
    ],
}

SYSTEM_PROMPT = (
    "Ты — финансовый редактор Telegram-канала. Пиши живо, структурно и современно. "
    "Пост обязательно должен включать следующие блоки: "
    "1) заголовок с эмодзи, "
    "2) подзаголовок-зацеп с эмодзи — интригующий крючок (вопрос или фраза, до 50 символов), "
    "3) краткое вступление, "
    "4) подзаголовки с эмодзи и жирным шрифтом, "
    "5) аналитика и прогноз, "
    "6) итоговый вывод. "
    "В конце — ненавязчивый, естественно встроенный вопрос к подписчику. "
    "Не используй решётки #. Используй только жирный шрифт для подзаголовков. "
    "Не используй эмодзи в теле текста, только в заголовках. "
    "СТРОГО: Ответ не должен превышать 990 символов. Перед финальным ответом подсчитай длину и убедись, что она <=990. Если больше — сократи. "
)

SYSTEM_PROMPT += (
    "\n\nКОНКРЕТИКА И ПРАКТИКА:\n"
    "— Когда возможно, приводи числа/диапазоны (%, ₽, сроки).\n"
    "— Добавляй 1 короткий пример-расчёт (каждый раз новый, без повторений).\n"
    "— Если уместно, вставляй блок **🧩 Шаги:** (3–5 нумерованных пунктов по 1 строке).\n"
    "— Для новостей — блок **🧭 Что делать инвестору:** 2–3 лаконичных пункта.\n"
    "— Не выдумывай фактов; если нет данных — используй ориентиры и диапазоны.\n"
    "\n\nЯЗЫК И ПЕРЕВОД:\n"
    "Пиши весь пост на русском. Если фрагмент новости на английском — переведи ключевые формулировки на русский без искажений. "
    "Имена собственные и тикеры (Fed, ECB, S&P 500, Apple, BTC) оставляй в оригинале. "
    "Числа и проценты не выдумывай, бери только из входного фрагмента; валюты — $, €, ₽; даты — в русском формате."
    "\n\nДИСЦИПЛИНА ФАКТОВ: Используй только сведения, явно присутствующие во входном блоке "
    "«Содержание новости». Не добавляй геополитику/города/цифры/котировки, если их там нет. "
    "Прогноз формулируй без новых числовых значений."
)

CONCRETE_HINT_RUBRIC = (
    "Добавь конкретики: 1 пример-расчёт в ₽ и блок **🧩 Шаги:** (3–5 пунктов по 1 строке)."
)
CONCRETE_HINT_NEWS = (
    "Пиши ТОЛЬКО на основе блока «Содержание новости» ниже. "
    "Запрещено добавлять факты, города/страны, курсы валют, уровни индексов и цены активов, "
    "если их НЕТ в «Содержание новости». "
    "Если исходник на английском — переведи формулировки. Имена/тикеры не переводить. "
    "В конце добавь блок **🧭 Что делать инвестору:** 2–3 пункта, основанные на этих фактах (без новых цифр)."
)

# ➕ Новое: подсказка для исторической рубрики
HISTORY_HINT = (
    "Это рубрика «В этот день в финансах». Начни с заголовка «📅 В этот день в финансах». "
    "Обязательно назови год в первой строке фактов. Коротко опиши событие и почему оно важно для экономики/рынков. "
    "Добавь блок **📊 Контекст:** 1–2 предложения. "
    "Добавь блок **🧭 Урок инвестору:** 2–3 пункта. Никаких выдуманных фактов или цифр — только из справки. "
    "Если исходник на английском — переведи аккуратно на русский."
)

# ─── Генерация текста/картинок ────────────────────────────────────────────────
def generate_post_text(user_prompt, system_prompt=None):
    try:
        for _ in range(5):
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt}
                ]
            )
            content = response.choices[0].message.content.strip().replace("###", "")
            if len(content) <= 1015:
                return content
        logger.warning("⚠️ GPT не смог уложиться в лимит. Возвращаем None.")
        return None
    except Exception as e:
        logger.error(f"Ошибка генерации текста: {e}")
        return None

def generate_image(title_line, style="news"):
    try:
        stripped_title = title_line.strip('📊📈📉💰🏦💸🧠📌📅').strip()

        # вариативность, чтобы картинки не повторялись
        lenses  = ["85mm lens", "50mm prime", "35mm wide", "telephoto compression"]
        cameras = ["three-quarters view", "low-angle hero shot", "top-down minimal", "isometric semi-orthographic"]
        lights  = ["global illumination", "soft studio lighting", "volumetric light", "rim light, subtle bloom"]
        moods   = ["optimistic growth", "tense volatility", "calm stability", "cautious uncertainty", "analytical, high-tech"]
        envs    = [
            "sleek trading desk with glass surfaces",
            "abstract city skyline of a financial district",
            "clean studio with floating glass charts",
            "minimal architectural space with columns",
            "macro world of money props (paper, metal, glass) without text"
        ]
        devices = [
            "subtle wavy glass charts (volatility)",
            "gradually rising geometric bars (growth)",
            "balancing scales or counterweights (risk)",
            "interlocking blocks (diversification)",
            "flowing liquid glass shapes (liquidity)",
            "soft bokeh particles and thin market grid lines"
        ]

        # общая формулировка — сюжетная сцена по смыслу заголовка
        base_prompt = f"""
            Create a premium, photorealistic 3D narrative scene (not a single centered emblem) that visually conveys
            the meaning of the headline: “{stripped_title}”. Use believable PBR materials, ray-traced reflections,
            depth of field and cinematic contrast. Environment: {random.choice(envs)}.
            Camera: {random.choice(cameras)} with {random.choice(lenses)}.
            Lighting: {random.choice(lights)}. Mood: {random.choice(moods)}.
            Include 2–3 subtle visual metaphors appropriate to the headline, such as {', '.join(random.sample(devices, 3))}.
            No people. Square 1:1. Clean composition, premium finance aesthetics.
            Strictly no text, numbers or logos.
        """

        if style == "rubric":
            # рубричные — немного светлее и с аккуратным дизайн-акцентом
            style_hint = (
                "Slightly brighter neutral background, gentle studio feel. "
                "Optionally a very subtle design accent (faint dotted grid or thin soft border), not distracting."
            )
        else:
            # новости — темнее, динамичнее
            style_hint = (
                "Darker premium background and a more dynamic overall composition. "
                "Keep it tasteful and realistic."
            )

        prompt = base_prompt + "\n" + style_hint + "\n" + NEGATIVE_SUFFIX

        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="hd",
            n=1
        )
        return response.data[0].url

    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        return None

def publish_post(content, image_url):
    """Сначала пытаемся отправить по URL, при неудаче — скачиваем и шлём как файл.
       Текст отправляем как HTML. Если превышен лимит Telegram — ПЕРЕГЕНЕРИРУЕМ, а не обрезаем."""
    try:
        plain = (content or "").strip()

        # 1) первичная сборка HTML
        caption_html = _polish_and_to_html(plain)

        # 2) если выходим за лимит — просим модель написать компактнее и пересобираем
        if len(caption_html) > CAPTION_LIMIT:
            compact_plain = _regenerate_to_fit(plain)
            caption_html = _polish_and_to_html(compact_plain)
            # дополнительная страховка: если вдруг всё ещё длинно — ещё одна попытка
            if len(caption_html) > CAPTION_LIMIT:
                compact_plain = _regenerate_to_fit(compact_plain, target_limits=(880, 840, 800))
                caption_html = _polish_and_to_html(compact_plain)

        # Попытка 1: URL
        try:
            bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=caption_html,
                parse_mode=telegram.ParseMode.HTML
            )
            logger.info("✅ Пост опубликован по URL")
            return
        except BadRequest as e:
            msg = str(e)
            if ("Failed to get http url content" in msg
                or "wrong type of the web page content" in msg
                or "URL host is empty" in msg):
                logger.warning("⚠️ TG не смог скачать изображение по URL, шлём как файл…")
            else:
                raise

        # Попытка 2: файл
        with httpx.Client(timeout=30.0, follow_redirects=True) as c:
            resp = c.get(image_url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            image_bytes = resp.content

        file_obj = telegram.InputFile(BytesIO(image_bytes), filename="cover.png")
        bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_obj,
            caption=caption_html,
            parse_mode=telegram.ParseMode.HTML
        )
        logger.info("✅ Пост опубликован (отправлено как файл)")
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")

# ─── Ротация постов ───────────────────────────────────────────────────────────
rubric_index = 0
news_index = 0

def _pick_title_line(text: str) -> str:
    """Берём строку-заголовок (учитываем 📅 для истории)."""
    return next(
        (line for line in (text or "").split('\n')
         if line.strip().startswith(('📊','📈','📉','💰','🏦','💸','🧠','📌','📅'))),
        (text or "").split('\n')[0]
    )

def scheduled_rubric_post():
    # 🔁 теперь индексы устойчивы к перезапуску
    idx = _next_index("rubric", len(rubrics))
    rubric = rubrics[idx]
    logger.info(f"⏳ Генерация рубричного поста: {rubric}")

    attempts = 0
    text = None
    while attempts < 5:
        text = generate_post_text(
            f"Создай структурированный и интересный Telegram-пост по рубрике: {rubric}. {CONCRETE_HINT_RUBRIC}",
            system_prompt=SYSTEM_PROMPT
        )
        if text and len(text) <= 1015:
            break
        attempts += 1
    else:
        logger.warning("⚠️ GPT не смог уложиться в лимит. Возвращаем None.")
        return

    title_line = _pick_title_line(text)
    image_url = generate_image(title_line, style="news")  # одинаковый стиль
    if image_url:
        publish_post(text, image_url)

# ── NEW: «В этот день в финансах» ─────────────────────────────────────────────
_FIN_KW_RU = [
    "банк", "банков", "бирж", "акци", "облигац", "валют", "доллар", "евро", "рубл",
    "финанс", "налог", "бюджет", "дефолт", "кризис", "инфляц", "ипотек", "золото",
    "золотой стандарт", "бреттон-вудс", "центробанк", "фрс", "ецб", "казначейств",
    "рынок", "торгов", "санкц", "эмисси", "банкротств"
]
_FIN_KW_EN = [
    "bank", "banking", "stock", "exchange", "bond", "currency", "dollar", "euro", "ruble",
    "finance", "tax", "budget", "default", "crisis", "inflation", "mortgage", "gold",
    "gold standard", "bretton woods", "central bank", "federal reserve", "ecb",
    "treasury", "market", "trade", "sanction", "emission", "bankruptcy", "panic",
    "great depression", "oil crisis", "dot-com", "credit"
]

def _score_fin_event(txt: str) -> int:
    t = (txt or "").lower()
    score = 0
    for w in _FIN_KW_RU:
        if w in t: score += 2
    for w in _FIN_KW_EN:
        if w in t: score += 2
    # сильные маркеры
    for w in ["кризис", "default", "panic", "бреттон", "bretton", "gold standard", "great depression", "bankruptcy"]:
        if w in t: score += 4
    return score

def fetch_finance_event_today():
    """Берём событие этого дня из Wikipedia (ru → en fallback), фильтруем по финансам."""
    now = datetime.now(pytz.timezone("Europe/Moscow"))
    m, d = now.month, now.day
    urls = [
        f"https://ru.wikipedia.org/api/rest_v1/feed/onthisday/events/{m}/{d}",
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{m}/{d}",
    ]
    headers = {"User-Agent": "MinFinToolsBot/1.0 (+telegram)"}

    candidates = []
    for url in urls:
        try:
            r = httpx.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            for ev in data.get("events", []):
                year = ev.get("year")
                text = ev.get("text", "")  # краткое описание
                pages = ev.get("pages") or []
                title = pages[0].get("normalizedtitle") if pages else ""
                extract = pages[0].get("extract") if pages else ""
                link = ""
                try:
                    link = pages[0]["content_urls"]["desktop"]["page"]
                except Exception:
                    pass

                blob = " ".join([str(year or ""), title or "", text or "", extract or ""])
                score = _score_fin_event(blob)
                if score > 0:  # считаем финансовым
                    candidates.append({
                        "year": year,
                        "title": title or text[:120],
                        "summary": text or extract or "",
                        "link": link,
                        "lang": "ru" if "ru.wikipedia.org" in url else "en",
                        "score": score
                    })
        except Exception as ex:
            logger.warning("OnThisDay fetch fail %s: %s", url, ex)

    if not candidates:
        return None

    # берём самый «финансовый»
    pick = sorted(candidates, key=lambda x: (x["score"], x["year"] or 0), reverse=True)[0]
    return pick

def scheduled_history_post():
    """Пост «В этот день в финансах» — 08:30 ежедневно."""
    evt = fetch_finance_event_today()
    if not evt:
        logger.info("⏭️ Историческое событие не найдено — пропуск.")
        return

    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    facts = f"{evt.get('year','?')}: {evt.get('title','')} — {evt.get('summary','')}"
    # страхуем длину фактов, чтобы не раздувать prompt
    facts = facts.strip()
    if len(facts) > 600:
        facts = facts[:600].rstrip() + "…"
    src = evt.get("link") or ""

    user_prompt = (
        f"Сделай пост для рубрики «В этот день в финансах». Дата: {today}. "
        f"ФАКТЫ (без домыслов): {facts}. Источник: {src}. "
        f"{HISTORY_HINT}"
    )

    text = generate_post_text(user_prompt)
    if not text:
        return
    title_line = _pick_title_line(text)
    # для исторической рубрики используем чуть «светлее» оформление
    image_url = generate_image(title_line, style="rubric")
    if image_url:
        publish_post(text, image_url)

# ─── Новости (как было) ───────────────────────────────────────────────────────
def fetch_buzzy_rss_news(topic, per_feed=5, lookback_hours=48):
    feeds = rss_sources.get(topic, [])
    entries = []

    for url in feeds:
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            for e in feed.entries[:per_feed]:
                # published/updated fallback
                if getattr(e, "published_parsed", None):
                    published = datetime.fromtimestamp(mktime(e.published_parsed), tz=pytz.UTC)
                elif getattr(e, "updated_parsed", None):
                    published = datetime.fromtimestamp(mktime(e.updated_parsed), tz=pytz.UTC)
                else:
                    published = datetime.utcnow().replace(tzinfo=pytz.UTC)

                # summary/description/content fallback
                raw = e.get("summary") or e.get("description")
                if not raw and e.get("content"):
                    try:
                        raw = e.content[0].value
                    except Exception:
                        raw = ""
                summary = clean_html(raw).strip()

                title = e.get("title", "").strip()
                link = e.get("link", "")

                if title:
                    entries.append({
                        "title": title,
                        "summary": summary,
                        "link": link,
                        "published": published.isoformat()
                    })
        except Exception as ex:
            logger.warning(f"RSS parse error {url}: {ex}")

    if not entries:
        return "Нет актуальных новостей по теме."

    cutoff = datetime.utcnow().replace(tzinfo=pytz.UTC) - timedelta(hours=lookback_hours)
    fresh = [x for x in entries if datetime.fromisoformat(x["published"]) >= cutoff]
    items = fresh or entries

    # выбор «самой нашумевшей» через LLM
    try:
        headlines = "\n".join([f"{i+1}. {x['title']}" for i, x in enumerate(items[:30])])
        prompt = (
            "Ниже список заголовков по одной теме. Выбери РОВНО ОДНУ «самую нашумевшую» "
            "с учётом повторяемости сюжета в разных источниках, свежести (в приоритете последние 24–48ч), "
            "значимости источника и масштаба последствий. "
            "Ответ верни в JSON с полями: best_index (int, начиная с 1) и reason (1 короткая фраза). "
            f"\n\nСписок заголовков:\n{headlines}"
        )
        # ✔ фикс: используем тот же метод, что и в остальных местах
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        data = json.loads(resp.choices[0].message.content)
        idx = int(data.get("best_index", 1)) - 1
        pick = items[max(0, min(idx, len(items)-1))]
    except Exception as ex:
        logger.warning(f"LLM ranking failed, fallback to latest: {ex}")
        pick = sorted(items, key=lambda x: x["published"], reverse=True)[0]

    # анти-повторы: если уже было, берем ближайшую свежую альтернативу
    sid = _story_id(pick["title"], pick.get("link", ""))
    if _is_seen(sid):
        for x in sorted(items, key=lambda y: y["published"], reverse=True):
            alt_sid = _story_id(x["title"], x.get("link", ""))
            if not _is_seen(alt_sid):
                pick, sid = x, alt_sid
                break
    _mark_seen(sid)

    summary = pick["summary"] or ""
    if len(summary) > 300:
        summary = summary[:300] + "..."
    logger.info("📰 Источник: %s | %s", pick.get("title",""), pick.get("link",""))
    return f"{pick['title']}: {summary}"

def scheduled_news_post():
    # 🔁 теперь индексы устойчивы к перезапуску
    idx = _next_index("news", len(news_themes))
    topic = news_themes[idx]
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    logger.info(f"⏳ Генерация новостного поста: {topic}")

    rss_news = fetch_buzzy_rss_news(topic)
    if not rss_news or rss_news.startswith("Нет актуальных новостей"):
        logger.info("⏭️ Пропуск: нет свежих новостей по теме %s", topic)
        return
    if len(rss_news) > 500:
        rss_news = rss_news[:500] + "..."

    user_prompt = (
        f"Составь актуальный Telegram-пост по теме: {topic}. "
        f"Дата: {today}. ФАКТЫ (не добавляй ничего сверх): {rss_news}. "
        f"Сделай пост живым, структурным, не более 990 символов. В конце добавь вопрос подписчику. "
        f"{CONCRETE_HINT_NEWS}"
    )

    text = generate_post_text(user_prompt)
    if text:
        title_line = _pick_title_line(text)
        image_url = generate_image(title_line, style="news")
        if image_url:
            publish_post(text, image_url)

# ─── Ручные тесты (как были) ──────────────────────────────────────────────────
def test_rubric_post(rubric_name):
    logger.info(f"⏳ Ручная генерация рубричного поста: {rubric_name}")
    attempts, text = 0, None
    while attempts < 5:
        text = generate_post_text(
            f"Создай структурированный и интересный Telegram-пост по рубрике: {rubric_name}.",
            system_prompt=SYSTEM_PROMPT
        )
        if text and len(text) <= 1015:
            break
        attempts += 1
    else:
        logger.warning("⚠️ GPT не смог уложиться в лимит. Возвращаем None.")
        return
    title_line = _pick_title_line(text)
    image_url = generate_image(title_line, style="news")
    if image_url:
        publish_post(text, image_url)

def test_news_post(rubric_name):
    logger.info(f"⏳ Ручная генерация новостного поста: {rubric_name}")
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    rss_news = fetch_buzzy_rss_news(rubric_name)
    if len(rss_news) > 500:
        rss_news = rss_news[:500] + "..."
    user_prompt = (
        f"Составь актуальный Telegram-пост по теме: {rubric_name}. "
        f"Дата: {today}. Содержание новости: {rss_news}. "
        f"Сделай пост живым, структурным, не более 990 символов. Вставь подзаголовок-зацеп. В конце — вопрос подписчику. "
        f"{CONCRETE_HINT_NEWS}"
    )
    text = generate_post_text(user_prompt)
    if text:
        title_line = _pick_title_line(text)
        image_url = generate_image(title_line, style="news")
        if image_url:
            publish_post(text, image_url)

# ─── Расписание (МСК) ─────────────────────────────────────────────────────────
# новый пост истории — САМЫЙ ПЕРВЫЙ
scheduler.add_job(scheduled_history_post, 'cron', hour=8,  minute=30)

scheduler.add_job(scheduled_news_post,   'cron', hour=9,  minute=26)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=11, minute=42)
scheduler.add_job(scheduled_news_post,   'cron', hour=13, minute=24)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=16, minute=5)
scheduler.add_job(scheduled_news_post,   'cron', hour=18, minute=47)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=19, minute=47)

# ─── Запуск под Railway ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading

    def run_scheduler():
        scheduler.start()
        logger.info("🗓️ APScheduler запущен")

    threading.Thread(target=run_scheduler, daemon=True).start()

    port = int(os.getenv("PORT", "8080"))
    logger.info(f"🌐 Flask слушает порт {port}")
    app.run(host="0.0.0.0", port=port)
