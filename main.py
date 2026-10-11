"""Запуск: python main.py"""
import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

from app.config import load_config
from app.db import Database
from app.handlers import router
from app.shop import Shop
from app.tasks import payment_checker, reminders
from app.webhook import create_app
from app.yookassa import YooKassa

log = logging.getLogger("main")


async def check_channel(shop: Shop) -> None:
    """Проверяет, что бот — админ канала и может принимать заявки."""
    if not shop.cfg.channel_id:
        log.warning("CHANNEL_ID не задан. Перешлите боту любой пост из канала — он покажет ID.")
        return
    try:
        me = await shop.bot.get_chat_member(shop.cfg.channel_id, shop.bot.id)
    except TelegramAPIError as e:
        log.error("Бот не видит канал %s: %s. Добавьте бота в канал администратором.",
                  shop.cfg.channel_id, e)
        return
    if me.status != "administrator" or not getattr(me, "can_invite_users", False):
        log.error("Боту нужны права администратора канала с разрешением «Пригласительные ссылки».")
    else:
        log.info("Канал подключён, права в порядке")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    db = Database(cfg.db_path)
    await db.connect()
    yk = YooKassa(cfg.yookassa_shop_id, cfg.yookassa_secret_key)
    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shop = Shop(bot, db, yk, cfg)
    shop.bot_username = (await bot.get_me()).username
    log.info("Бот @%s запущен", shop.bot_username)
    await check_channel(shop)

    runner = web.AppRunner(create_app(shop))
    await runner.setup()
    await web.TCPSite(runner, cfg.webhook_host, cfg.webhook_port).start()
    log.info("Жду уведомления ЮKassa на порту %s, путь %s", cfg.webhook_port, cfg.webhook_path)

    background = [asyncio.create_task(payment_checker(shop)), asyncio.create_task(reminders(shop))]

    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, shop=shop, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in background:
            task.cancel()
        await runner.cleanup()
        await yk.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
