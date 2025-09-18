import os
import json
import logging
import telegram
from openai import OpenAI
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import pytz
import feedparser
import re
from dotenv import load_dotenv

# Загрузка env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация клиентов
client = OpenAI(api_key=OPENAI_API_KEY)
bot = telegram.Bot(token=TELEGRAM_TOKEN)
scheduler = BackgroundScheduler(timezone=pytz.timezone("Europe/Moscow"))

# Список рубрик и новостных тем
rubrics = [
    "Финсовет дня", "Финликбез", "Личный финменеджмент", "Деньги в цифрах",
    "Кейс / Разбор", "Психология денег", "Финансовая ошибка", "Продукт недели",
    "Инвест-горизонт", "Миф недели", "Путь к 1 млн", "Финансовая привычка",
    "Вопрос — ответ", "Excel / Таблица", "Финансовая цитата", "Инструмент недели"
]
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
        "https://forklog.com/feed/",
        "https://bitnovosti.com/feed/"
    ],
    "Новости фондовых рынков (Россия и США)": [
        "https://rssexport.rbc.ru/rbcnews/news/21/full.rss",
        "https://www.finam.ru/rss/news.rss"
    ]
}

# Сохранение/загрузка состояния ротации
STATE_FILE = "state.json"

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"rubric_index": 0, "news_index": 0}

def save_state(rubric_index, news_index):
    with open(STATE_FILE, 'w') as f:
        json.dump({"rubric_index": rubric_index, "news_index": news_index}, f)

# Очистка HTML
def clean_html(raw_html):
    cleanr = re.compile('<.*?>')
    return re.sub(cleanr, '', raw_html)

# Промпт для GPT
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
    "Ответ не должен превышать 990 символов."
)

def generate_post_text(user_prompt):
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
        logger.warning("⚠️ GPT не смог уложиться в лимит.")
        return None
    except Exception as e:
        logger.error(f"Ошибка генерации текста: {e}")
        return None

# Генерация изображений
def generate_image(title_line, style="news"):
    try:
        stripped_title = title_line.strip('📊📈📉💰🏦💸🧠📌').strip()
        if style == "rubric":
            prompt = (
                f"Минималистичная иллюстрация в деловом стиле на тему: «{stripped_title}». "
                "Без текста. Только визуальные элементы: иконки, схемы, графики, стрелки роста, деньги, монеты, инвестиционные символы. "
                "Цветовая палитра — тёмно-зелёный, светло-зелёный и нейтральные светлые оттенки. "
                "Стиль — чистый, современный, как в финансовом Telegram-канале."
            )
        else:
            prompt = (
                f"Современная иллюстрация в стиле постера для делового Telegram-канала. Тема: «{stripped_title}». "
                "Без текста. Визуальная метафора: рост, ракета, стрелка вверх, стабильность, деньги, экономика, биржа. "
                "Цветовая гамма — мягкие тени, глубокий фон, зелёные и нейтральные оттенки. "
                "Стиль — иллюстративный, чистый, как обложка к новостной статье."
            )
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

# Публикация поста
def publish_post(content, image_url):
    try:
        if len(content) > 1024:
            content = content[:1020] + "..."
        bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=image_url,
            caption=content,
            parse_mode=telegram.constants.ParseMode.MARKDOWN_V2  # Исправлено
        )
        logger.info("✅ Пост опубликован!")
    except Exception as e:
        logger.error(f"Ошибка публикации: {e}")

# Рубричный пост
def scheduled_rubric_post():
    state = load_state()
    rubric_index = state["rubric_index"]
    rubric = rubrics[rubric_index]
    rubric_index = (rubric_index + 1) % len(rubrics)
    state["rubric_index"] = rubric_index
    save_state(rubric_index, state["news_index"])
    logger.info(f"⏳ Генерация рубричного поста: {rubric}")

    text = generate_post_text(f"Создай структурированный и интересный Telegram-пост по рубрике: {rubric}.")
    if text:
        title_line = next(
            (line for line in text.split('\n') if line.strip().startswith(('📊', '📈', '📉', '💰', '🏦', '💸', '🧠', '📌'))),
            text.split('\n')[0]
        )
        image_url = generate_image(title_line, style="rubric")
        if image_url:
            publish_post(text, image_url)

# Новостной пост
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
    state = load_state()
    news_index = state["news_index"]
    topic = news_themes[news_index]
    news_index = (news_index + 1) % len(news_themes)
    state["news_index"] = news_index
    save_state(state["rubric_index"], news_index)
    today = datetime.now(pytz.timezone("Europe/Moscow")).strftime("%-d %B %Y")
    logger.info(f"⏳ Генерация новостного поста: {topic}")

    rss_news = fetch_top_rss_news(topic)
    if len(rss_news) > 500:
        rss_news = rss_news[:500] + "..."
    user_prompt = (
        f"Составь актуальный Telegram-пост по теме: {topic}. "
        f"Дата: {today}. Содержание новости: {rss_news}. "
        f"Сделай пост живым, структурным, не более 990 символов. Вставь подзаголовок-зацеп. В конце — вопрос подписчику."
    )
    text = generate_post_text(user_prompt)
    if text:
        title_line = next(
            (line for line in text.split('\n') if line.strip().startswith(('📊', '📈', '📉', '💰', '🏦', '💸', '🧠', '📌'))),
            text.split('\n')[0]
        )
        image_url = generate_image(title_line, style="news")
        if image_url:
            publish_post(text, image_url)

# Расписание (МСК)
scheduler.add_job(scheduled_news_post, 'cron', hour=9, minute=16)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=11, minute=42)
scheduler.add_job(scheduled_news_post, 'cron', hour=13, minute=24)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=16, minute=5)
scheduler.add_job(scheduled_news_post, 'cron', hour=18, minute=47)
scheduler.add_job(scheduled_rubric_post, 'cron', hour=19, minute=47)

if __name__ == "__main__":
    logger.info("Запуск бота...")
    scheduled_rubric_post()
    scheduler.start()
    try:
        while True:
            pass  # Держим процесс активным
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

# Тестовые посты. Выше - scheduler.start() нужно вставить: Тестовый рубричный пост: scheduled_rubric_post()   Тестовый новостной пост: scheduled_news_post()
