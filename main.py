import os
import logging
import telegram
import httpx
import random
from io import BytesIO
import telegram
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

NEGATIVE_SUFFIX = (
    "Строго БЕЗ текста, букв, цифр и логотипов. "
    "Запрет: ракеты, шаттлы, выхлопное пламя, стартовые площадки, космос/звёзды как фон, "
    "конусный нос и килевые стабилизаторы, эмблемы NASA/SpaceX. "
    "Запрет: плоская векторная графика, иконки, комикс, 2D-иллюстрация, клипарт."
)

app = Flask(__name__)

# ─── Маршруты здоровья ────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "Бот работает ✅", 200

@app.route("/ping")
def ping():
    return "OK", 200

from flask import request

@app.route("/test")
def manual_test():

    kind = request.args.get("type", "news")   # "news" или "rubric"
    try:
        if kind == "rubric":
            # публикует СЛЕДУЮЩУЮ рубрику по ротации (двигает rubric_index)
            scheduled_rubric_post()
            return "✅ Отправлен следующий рубричный пост по ротации", 200
        else:
            # публикует СЛЕДУЮЩУЮ новостную тему по ротации (двигает news_index)
            scheduled_news_post()
            return "✅ Отправлен следующий новостной пост по ротации", 200
    except Exception as e:
        return f"❌ Ошибка: {e}", 500

from datetime import datetime
import pytz
import feedparser
import re

def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

# ─── Логирование ───────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── ENV ───────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN не задан в переменных окружения")
if not CHANNEL_ID:
    raise ValueError("CHANNEL_ID не задан в переменных окружения")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY не задан в переменных окружения")

client = OpenAI(api_key=OPENAI_API_KEY)
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# PT: Railway — часовой пояс тут норм (МСК)
scheduler = BackgroundScheduler(timezone=pytz.timezone("Europe/Moscow"))

# ─── Контентные настройки (НЕ ТРОГАЛ) ─────────────────────────────────────────
rubrics = [
    "Финсовет дня", "Финликбез", "Личный финменеджмент", "Деньги в цифрах",
    "Кейс / Разбор", "Психология денег", "Финансовая ошибка", "Продукт недели",
    "Инвест-горизонт", "Миф недели", "Путь к 1 млн", "Финансовая привычка",
    "Вопрос — ответ", "Excel / Таблица", "Финансовая цитата",
    "Инструмент недели"
]
rubric_index = 0
news_index = 0

news_themes = [
    "Финансовые новости России", "Новости криптовалют",
    "Новости фондовых рынков (Россия и США)"
]
rss_sources = {
    "Финансовые новости России": [
        "https://rssexport.rbc.ru/rbcnews/news/20/full.rss",
        "https://tass.ru/rss/v2.xml?rubric=ekonomika",
        "https://www.interfax.ru/rss.asp"
    ],
    "Новости криптовалют": [
        "https://forklog.com/feed/", "https://bitnovosti.com/feed/"
    ],
    "Новости фондовых рынков (Россия и США)": [
        "https://rssexport.rbc.ru/rbcnews/news/21/full.rss",
        "https://www.finam.ru/rss/news.rss"
    ]
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
    "Примеры зацепов:\n"
    "— 🤔 Случайность или сигнал?\n"
    "— 📉 Временное падение или начало тренда?\n"
    "— 🤝 Дружба или иллюзия?\n"
    "— 💸 Деньги есть — уверенности нет?\n"
    "— 📈 Всё ли так гладко?\n"
    "— 📊 Новый тренд или всплеск?\n"
    "Не используй решётки #. Используй только жирный шрифт для подзаголовков. "
    "Не используй эмодзи в теле текста, только в заголовках. "
    "СТРОГО: Ответ не должен превышать 990 символов. Перед финальным ответом подсчитай длину и убедись, что она <=990. Если больше — сократи. "
    "Пример поста (длина 750 символов):\n"
    "💰 Финсовет дня\n"
    "🤔 Готовы к росту?\n"
    "В мире инвестиций дисциплина — ключ к успеху.\n"
    "**📊 Аналитика:** Рынок показывает волатильность, но диверсификация снижает риски.\n"
    "**📈 Прогноз:** В ближайший месяц ожидается подъём на 5-7%.\n"
    "Вывод: Начните с малого, но регулярно.\n"
    "А вы пробовали диверсифицировать портфель?\n"
    "— Подсчёт: 750 символов."
)

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
        stripped_title = title_line.strip('📊📈📉💰🏦💸🧠📌').strip()

        # общие вариации для реализма
        lenses = ["85mm lens", "50mm prime", "35mm documentary", "macro close-up"]
        lights = ["soft studio lighting", "global illumination", "volumetric light", "rim light"]
        looks  = ["photorealistic PBR materials", "cinematic grade", "ray-traced reflections", "high microdetail"]

        # ── RUBRIC: тот же семейный стиль, но с парой 'подписей' и чуть светлее
        if style == "rubric":
            rubric_marks = [
                "thin light border around the frame",
                "subtle bookmark ribbon in the top-left (no text)",
                "delicate dotted grid background pattern",
                "soft halo ring behind the central object"
            ]
            prompt = f"""
                Ultra-detailed 3D photorealistic CG render, {random.choice(lenses)}, {random.choice(lights)},
                {random.choice(looks)}, clean studio composition, shallow depth of field.
                Theme from the headline: “{stripped_title}”.
                One clear central object that conveys the finance topic; realistic materials (metal, glass, paper, fabric).
                Add rubric signatures: {', '.join(random.sample(rubric_marks, k=2))}.
                Background slightly brighter than news covers. Balanced, minimal, premium.
                Square 1:1. No people.
            """
            prompt += "\n" + NEGATIVE_SUFFIX

        # ── NEWS: такой же реализм, но без 'подписей', фон темнее, композиция динамичнее
        else:
            title_lc = stripped_title.lower()
            central_by_kw = [
                (("биткоин","bitcoin","btc","крипт"), "large realistic bitcoin coin"),
                (("эфир","eth","ethereum"), "crystal-like ethereum symbol"),
                (("нефть","brent","брент","wti","oil","баррель"), "metal oil barrel"),
                (("золото","gold","xau"), "gold bullion bar"),
                (("рубль","rub","₽"), "ruble sign sculpted in metal"),
                (("доллар","usd","$","фрс","ставка фрс"), "dollar sign sculpted in metal"),
                (("евро","eur","€","ецб"), "euro sign sculpted in metal"),
                (("облигац","офз","доходност","купон","yields"), "real bond coupon sheet"),
                (("акци","ipo","etf","индекс","s&p","моэкс","nasdaq","dow"), "glass candlestick chart sculpture"),
                (("инфляц","cpi","pce","цен","ставк","ключев"), "pressure gauge instrument"),
                (("банк","кредит","депозит","ипотек"), "bank facade with columns (miniature)"),
                (("санкц","экспорт","импорт","торгов"), "cargo containers stack"),
            ]
            central = "premium abstract financial sculpture"
            for keys, obj in central_by_kw:
                if any(k in title_lc for k in keys):
                    central = obj
                    break

            compositions = [
                "dynamic diagonal composition",
                "rule-of-thirds composition",
                "isometric product shot",
                "low-angle hero shot",
            ]
            details = random.sample([
                "tiny scattered coins", "thin market grid lines", "soft bokeh particles",
                "mini tickers as abstract bars", "glass shards like price bars"
            ], k=2)

            prompt = f"""
                Ultra-detailed 3D photorealistic CG render, {random.choice(lenses)}, {random.choice(lights)},
                {random.choice(looks)}, {random.choice(compositions)}, cinematic contrast.
                Central object: {central}. Around it: {', '.join(details)}.
                Darker premium background; clean, realistic, no noise. Square 1:1. No people.
            """
            prompt += "\n" + NEGATIVE_SUFFIX

        # сам вызов — только качество ставим HD
        response = client.images.generate(
            model="dall-e-3",          # можно оставить; см. примечание ниже
            prompt=prompt,
            size="1024x1024",
            quality="hd",              # <— было "standard"
            n=1
        )
        return response.data[0].url

    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        return None

def publish_post(content, image_url):
    try:
        if len(content) > 1024:
            content = content[:1020] + "..."

        # Сначала пробуем как URL (дешевле и быстрее)
        try:
            bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=content,
                parse_mode=telegram.ParseMode.MARKDOWN
            )
            logger.info("✅ Пост опубликован по URL")
            return
        except BadRequest as e:
            # Классические тексты ошибок от Telegram при недоступном URL
            msg = str(e)
            if ("Failed to get http url content" in msg
                or "wrong type of the web page content" in msg
                or "URL host is empty" in msg):
                logger.warning("⚠️ TG не смог скачать изображение по URL, шлём как файл...")
            else:
                raise

        # Скачиваем и шлём как файл (устойчивый путь)
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(image_url)
            resp.raise_for_status()
            image_bytes = resp.content

        file_obj = telegram.InputFile(BytesIO(image_bytes), filename="cover.png")
        bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_obj,
            caption=content,
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        logger.info("✅ Пост опубликован (отправлено как файл)")
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")

def scheduled_rubric_post():
    global rubric_index
    rubric = rubrics[rubric_index]
    rubric_index = (rubric_index + 1) % len(rubrics)
    logger.info(f"⏳ Генерация рубричного поста: {rubric}")

    attempts = 0
    text = None
    while attempts < 5:
        text = generate_post_text(
            f"Создай структурированный и интересный Telegram-пост по рубрике: {rubric}.",
            system_prompt=SYSTEM_PROMPT
        )
        if text and len(text) <= 1015:
            break
        attempts += 1
    else:
        logger.warning("⚠️ GPT не смог уложиться в лимит. Возвращаем None.")
        return

    title_line = next(
        (line for line in text.split('\n')
         if line.strip().startswith(('📊','📈','📉','💰','🏦','💸','🧠','📌'))),
        text.split('\n')[0]
    )
    image_url = generate_image(title_line, style="news")
    if image_url:
        publish_post(text, image_url)

def fetch_top_rss_news(rubric_name):
    feeds = rss_sources.get(rubric_name, [])
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                title = entry.title
                summary = clean_html(entry.get("summary", ""))
                if len(summary) > 50:
                    return f"{title}: {summary}"
        except Exception as e:
            logger.error(f"Ошибка при парсинге RSS {url}: {e}")
    return "Нет актуальных новостей по теме."

def scheduled_news_post():
    global news_index
    topic = news_themes[news_index]
    news_index = (news_index + 1) % len(news_themes)
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    logger.info(f"⏳ Генерация новостного поста: {topic}")

    rss_news = fetch_top_rss_news(topic)
    if len(rss_news) > 500:
        rss_news = rss_news[:500] + "..."

    user_prompt = (
        f"Составь актуальный Telegram-пост по теме: {topic}. "
        f"Дата: {today}. Содержание новости: {rss_news}. "
        f"Сделай пост живым, структурным, не более 990 символов. В конце добавь вопрос подписчику."
    )

    text = generate_post_text(user_prompt)
    if text:
        title_line = next(
            (line for line in text.split('\n')
             if line.strip().startswith(('📊','📈','📉','💰','🏦','💸','🧠','📌'))),
            text.split('\n')[0]
        )
        image_url = generate_image(title_line, style="news")
        if image_url:
            publish_post(text, image_url)

# ─── Расписание (МСК) — оставил твоё ──────────────────────────────────────────
scheduler.add_job(scheduled_news_post, 'cron', hour=9, minute=26)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=11, minute=42)
scheduler.add_job(scheduled_news_post, 'cron', hour=13, minute=24)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=16, minute=5)
scheduler.add_job(scheduled_news_post, 'cron', hour=18, minute=47)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=19, minute=47)

# ─── Ручные тесты (оставил как есть) ──────────────────────────────────────────
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
    title_line = next(
        (line for line in text.split('\n')
         if line.strip().startswith(('📊','📈','📉','💰','🏦','💸','🧠','📌'))),
        text.split('\n')[0]
    )
    image_url = generate_image(title_line, style="news")
    if image_url:
        publish_post(text, image_url)

def test_news_post(rubric_name):
    logger.info(f"⏳ Ручная генерация новостного поста: {rubric_name}")
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    rss_news = fetch_top_rss_news(rubric_name)
    if len(rss_news) > 500:
        rss_news = rss_news[:500] + "..."
    user_prompt = (
        f"Составь актуальный Telegram-пост по теме: {rubric_name}. "
        f"Дата: {today}. Содержание новости: {rss_news}. "
        f"Сделай пост живым, структурным, не более 990 символов. Вставь подзаголовок-зацеп. В конце — вопрос подписчику."
    )
    text = generate_post_text(user_prompt)
    if text:
        title_line = next(
            (line for line in text.split('\n')
             if line.strip().startswith(('📊','📈','📉','💰','🏦','💸','🧠','📌'))),
            text.split('\n')[0]
        )
        image_url = generate_image(title_line, style="news")
        if image_url:
            publish_post(text, image_url)

# ─── Запуск под Railway ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import threading
    # 1) стартуем планировщик
    def run_scheduler():
        scheduler.start()
        logger.info("🗓️ APScheduler запущен")

    threading.Thread(target=run_scheduler, daemon=True).start()

    # 2) Flask-сервер должен слушать PORT, который задаёт Railway
    port = int(os.getenv("PORT", "8080"))
    logger.info(f"🌐 Flask слушает порт {port}")
    app.run(host="0.0.0.0", port=port)
