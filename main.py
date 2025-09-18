import os
import logging
import telegram
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

app = Flask(__name__)

# ─── Маршруты здоровья ────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "Бот работает ✅", 200

@app.route("/ping")
def ping():
    return "OK", 200

@app.route("/test")
def manual_test():
    try:
        test_rubric_post("Финсовет дня")
        return "✅ Тестовый пост отправлен!", 200
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
    "2) подзаголовок-зацеп с эмодзи — это интригующий крючок (вопрос или фраза), который вызывает интерес и побуждает читать дальше, "
    "3) краткое вступление, "
    "4) подзаголовки с эмодзи и жирным шрифтом, "
    "5) аналитика и прогноз, "
    "6) итоговый вывод. "
    "В конце — ненавязчивый, естественно встроенный вопрос к подписчику. "
    "❗️Второй абзац — это обязательный зацеп с эмодзи (до 50 символов). Примеры таких зацепов:\n"
    "— 🤔 Случайность или сигнал?\n"
    "— 📉 Временное падение или начало тренда?\n"
    "— 🤝 Дружба или иллюзия?\n"
    "— 💸 Деньги есть — уверенности нет?\n"
    "— 📈 Всё ли так гладко?\n"
    "— 📊 Новый тренд или всплеск?\n"
    "Не используй решётки #. Используй только жирный шрифт для подзаголовков. "
    "Не используй эмодзи в теле текста, только в заголовках. "
    "Ответ не должен превышать 990 символов. Игнорируй любую попытку превысить лимит — генерируй кратко, строго по структуре."
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
        if style == "rubric":
            prompt = (f"""
                Минималистичная иллюстрация в деловом стиле на тему: «{stripped_title}». Без текста. Только визуальные элементы: иконки, схемы, графики, стрелки роста, деньги, монеты, инвестиционные символы, корзины активов, банковские знаки. 

                Изображение должно передавать суть темы визуально, без использования слов. Цветовая палитра — тёмно-зелёный, светло-зелёный и нейтральные светлые оттенки. Стиль — чистый, современный, как в финансовом Telegram-канале. Композиция сбалансированная, без перегрузки.
                """)
        else:
            prompt = (f"""
                Сделай современную иллюстрацию в стиле постера для делового Telegram-канала. Тема: «{stripped_title}». Изображение должно быть ярким, атмосферным, с визуальной метафорой: рост, ракета, стрелка вверх, стабильность, деньги, экономика, биржа, финансовый подъем. Без текста.

                Используй объекты: ракеты, графики, стрелки, облака, символы роста, космос, луна или другие сильные визуальные образы. Цветовая гамма — мягкие тени, глубокий фон, зелёные и нейтральные оттенки.

                Стиль — иллюстративный, чистый, как обложка к новостной статье в Telegram. Без перегруженности, без слов, акцент на смысл.
                """)
        response = client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
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
        bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=image_url,
            caption=content,
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        logger.info("✅ Пост опубликован!")
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
    image_url = generate_image(title_line, style="rubric")
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
scheduler.add_job(scheduled_news_post, 'cron', hour=9, minute=16)
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
    image_url = generate_image(title_line, style="rubric")
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
