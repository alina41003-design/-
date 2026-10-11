"""Фоновые задачи: подстраховка webhook и напоминания тем, кто не оплатил."""
import asyncio
import logging

from aiogram.exceptions import TelegramAPIError

from . import texts
from .shop import Shop
from .yookassa import YooKassaError

log = logging.getLogger(__name__)


async def payment_checker(shop: Shop) -> None:
    """Раз в N секунд перепроверяет неоплаченные заказы в ЮKassa.
    Если уведомление (webhook) потерялось, покупатель всё равно получит доступ."""
    while True:
        await asyncio.sleep(shop.cfg.check_interval_sec)
        try:
            for order in await shop.db.pending_payments():
                try:
                    await shop.refresh(order)
                except (YooKassaError, OSError) as e:
                    log.warning("Проверка заказа №%s не удалась: %s", order.id, e)
        except Exception:
            log.exception("Ошибка фоновой проверки платежей")


async def reminders(shop: Shop) -> None:
    """Один раз напоминает тем, кто начал покупку, но не оплатил."""
    if shop.cfg.reminder_minutes <= 0:
        return
    while True:
        await asyncio.sleep(300)
        try:
            for order in await shop.db.need_reminder(shop.cfg.reminder_minutes):
                await shop.db.set_reminded(order.id)
                try:
                    await shop.bot.send_message(
                        order.telegram_id, shop.fmt(texts.REMINDER),
                        reply_markup=shop.buy_keyboard(),
                    )
                except TelegramAPIError:
                    pass
        except Exception:
            log.exception("Ошибка отправки напоминаний")
