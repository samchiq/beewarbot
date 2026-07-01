import os
import time
import logging
import asyncio
from aiohttp import web
from telegram import Update, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import Application, CommandHandler, InlineQueryHandler, ContextTypes
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

# ── Конфигурация ──────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
WEBHOOK_URL        = os.getenv('WEBHOOK_URL')
PORT               = int(os.getenv('PORT', 10000))
TARGET_LIKES       = int(os.getenv('TARGET_LIKES', 400_000))
VIDEO_URL          = os.getenv('VIDEO_URL', 'https://www.youtube.com/watch?v=MddwBrh-9lU')

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Кэш ──────────────────────────────────────────────────────────────────────

_cache: dict = {'likes': None, 'title': None, 'err': None, 'ts': 0.0}
_CACHE_TTL = 3600  # секунд (1 час)

# ── Получение лайков через yt-dlp ─────────────────────────────────────────────

def _fetch_sync() -> tuple[int | None, str | None, str | None]:
    """
    Синхронная выборка — запускается в отдельном потоке.

    Используем клиент android_vr для Innertube API YouTube: у него нет
    проверки на бота, которая обычно блокирует запросы с серверных IP
    (Render, Heroku и т.д.) при обычном web-клиенте. Куки не нужны.
    """
    try:
        opts = {
            'quiet':         True,
            'no_warnings':   True,
            'skip_download': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android_vr'],
                }
            },
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(VIDEO_URL, download=False)

        title = info.get('title', 'Видео')
        likes = info.get('like_count')

        if likes is None:
            return None, title, "YouTube скрыл количество лайков для этого видео."
        return int(likes), title, None

    except yt_dlp.utils.DownloadError as e:
        logger.error("yt-dlp DownloadError: %s", e)
        return None, None, "Не удалось получить данные о видео."
    except Exception:
        logger.exception("_fetch_sync: неожиданная ошибка")
        return None, None, "Внутренняя ошибка бота."


async def get_video_stats() -> tuple[int | None, str | None, str | None]:
    """Возвращает (likes, title, error). Использует кэш 1 час."""
    now = time.monotonic()
    if _cache['ts'] and now - _cache['ts'] < _CACHE_TTL:
        return _cache['likes'], _cache['title'], _cache['err']

    loop = asyncio.get_event_loop()
    likes, title, err = await loop.run_in_executor(None, _fetch_sync)
    _cache.update({'likes': likes, 'title': title, 'err': err, 'ts': time.monotonic()})
    return likes, title, err


# ── Форматирование ────────────────────────────────────────────────────────────

def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", "\u202f")


def build_message(likes: int, title: str) -> str:
    remaining = TARGET_LIKES - likes
    pct       = min(likes / TARGET_LIKES * 100, 100.0)
    filled    = round(pct / 5)
    bar       = "█" * filled + "░" * (20 - filled)

    if remaining <= 0:
        return (
            f"🎉 *ЦЕЛЬ ДОСТИГНУТА!*\n\n"
            f"📹 {title}\n"
            f"❤️ Лайков: {_fmt(likes)}\n"
            f"🏆 {_fmt(TARGET_LIKES)} — выполнено!"
        )

    return (
        f"📹 {title}\n\n"
        f"❤️ Сейчас:   *{_fmt(likes)}* лайков\n"
        f"🎯 Цель:     {_fmt(TARGET_LIKES)} лайков\n"
        f"⏳ Осталось: *{_fmt(remaining)}* лайков\n\n"
        f"`[{bar}]` {pct:.1f}%"
    )


# ── Обработчики бота ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"❤️ Привет! Слежу за лайками до {_fmt(TARGET_LIKES)}.\n\n"
        "Используй меня в inline-режиме:\n"
        "напиши *@имя\\_бота* в любом чате и нажми на результат.",
        parse_mode="Markdown",
    )


async def inline_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    likes, title, err = await get_video_stats()

    if err:
        results = [InlineQueryResultArticle(
            id="err",
            title=f"❌ {err}",
            input_message_content=InputTextMessageContent(f"❌ {err}"),
        )]
    else:
        remaining = max(0, TARGET_LIKES - likes)
        results = [InlineQueryResultArticle(
            id="likes",
            title=f"❤️ Осталось {_fmt(remaining)} лайков до {_fmt(TARGET_LIKES)}",
            description=f"Сейчас {_fmt(likes)} · нажми, чтобы поделиться",
            input_message_content=InputTextMessageContent(
                build_message(likes, title),
                parse_mode="Markdown",
            ),
        )]

    await update.inline_query.answer(results, cache_time=3600)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update %s вызвал ошибку: %s", update, context.error)


# ── Веб-сервер (aiohttp) ──────────────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    """GET /health — для UptimeRobot, пингует каждые 10–14 минут."""
    return web.Response(text="ok", status=200)


async def webhook(request: web.Request) -> web.Response:
    """POST /webhook — входящие апдейты от Telegram."""
    try:
        bot_app = request.app['bot_app']
        data    = await request.json()
        update  = Update.de_json(data, bot_app.bot)
        await bot_app.process_update(update)
        return web.Response()
    except Exception:
        logger.exception("webhook: ошибка при обработке апдейта")
        return web.Response(status=500)


# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(InlineQueryHandler(inline_handler))
    application.add_error_handler(error_handler)

    if not WEBHOOK_URL:
        logger.critical("WEBHOOK_URL не задан — выход.")
        return

    await application.initialize()
    await application.start()

    wh = f"{WEBHOOK_URL}/webhook"
    logger.info("Устанавливаю webhook → %s", wh)
    await application.bot.set_webhook(url=wh, drop_pending_updates=True)

    web_app = web.Application()
    web_app['bot_app'] = application
    web_app.router.add_get('/health',   health)
    web_app.router.add_post('/webhook', webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    logger.info("Сервер слушает порт %d", PORT)

    await asyncio.Event().wait()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
