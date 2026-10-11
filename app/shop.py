"""Главная логика: создать заказ, подтвердить оплату, выдать доступ."""
import html
import logging
from decimal import Decimal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import texts
from .config import Config
from .db import HAS_ACCESS, Database, Order
from .yookassa import YooKassa

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


class Shop:
    def __init__(self, bot: Bot, db: Database, yk: YooKassa, cfg: Config):
        self.bot = bot
        self.db = db
        self.yk = yk
        self.cfg = cfg
        self.bot_username = ""

    # ---------- общее ----------

    @property
    def price(self) -> str:
        return f"{self.cfg.price:g}"

    def fmt(self, text: str, **extra) -> str:
        return text.format(product=html.escape(self.cfg.product_name), price=self.price, **extra)

    def buy_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=self.fmt(texts.BUY_BUTTON), callback_data="buy",
        )]])

    def personal_link(self, order: Order) -> str:
        """Личная одноразовая deep-link ссылка «Старт» для заказа."""
        return f"https://t.me/{self.bot_username}?start=acc_{order.token}"

    async def is_member(self, telegram_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(self.cfg.channel_id, telegram_id)
        except TelegramAPIError:
            return False
        return member.status in MEMBER_STATUSES

    # ---------- покупка ----------

    async def start_purchase(self, telegram_id: int, email: str | None) -> Order:
        """Создаёт заказ и платёж в ЮKassa (или возвращает ещё действующий)."""
        order = await self.db.open_order(telegram_id)
        if order:
            return order
        order = await self.db.create_order(telegram_id, str(self.cfg.price), email)
        payment = await self.yk.create_payment(
            amount=self.cfg.price,
            description=self.cfg.product_name,
            order_token=order.token,
            return_url=self.personal_link(order),
            email=email,
            receipt_enabled=self.cfg.receipt_enabled,
            vat_code=self.cfg.vat_code,
        )
        await self.db.set_payment(order.id, payment["id"], payment["confirmation"]["confirmation_url"])
        log.info("Заказ №%s создан, платёж %s", order.id, payment["id"])
        return await self.db.get(order.id)

    async def process_payment(self, payment: dict) -> Order | None:
        """Принимает платёж, полученный НАПРЯМУЮ из API ЮKassa, и обновляет заказ."""
        token = (payment.get("metadata") or {}).get("order_token")
        order = await self.db.get_by_token(token) if token else None
        if not order or order.payment_id != payment.get("id"):
            log.warning("Платёж %s не относится ни к одному заказу", payment.get("id"))
            return None

        status = payment.get("status")
        if status == "succeeded" and payment.get("paid"):
            amount = payment.get("amount") or {}
            if amount.get("currency") != "RUB" or Decimal(amount.get("value", "0")) != Decimal(order.amount):
                log.error("Сумма платежа %s не совпадает с заказом №%s", payment["id"], order.id)
                return order
            if await self.db.mark_paid(order.id):
                log.info("Заказ №%s оплачен", order.id)
                order = await self.db.get(order.id)
                await self.notify_admins(order)
                await self.deliver(order)
        elif status == "canceled":
            await self.db.mark_canceled(order.id)
        return await self.db.get(order.id)

    async def refresh(self, order: Order) -> Order:
        """Спрашивает у ЮKassa актуальный статус платежа по заказу."""
        if order.status != "new" or not order.payment_id:
            return order
        payment = await self.yk.get_payment(order.payment_id)
        return await self.process_payment(payment) or order

    # ---------- выдача доступа ----------

    async def deliver(self, order: Order) -> None:
        """Присылает покупателю личную ссылку в канал."""
        if order.status not in HAS_ACCESS or not order.telegram_id:
            return
        try:
            if await self.is_member(order.telegram_id):
                await self.db.mark_issued(order.id)
                await self.bot.send_message(order.telegram_id, texts.ALREADY_MEMBER)
                return
            link = order.invite_link
            if not link:
                invite = await self.bot.create_chat_invite_link(
                    self.cfg.channel_id,
                    name=f"Заказ №{order.id}",
                    creates_join_request=True,  # каждого входящего проверяет бот
                )
                link = invite.invite_link
            await self.bot.send_message(
                order.telegram_id,
                texts.ACCESS,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=texts.ACCESS_BUTTON, url=link),
                ]]),
                protect_content=True,  # сообщение нельзя переслать или сохранить
            )
            await self.db.mark_issued(order.id, link)
            log.info("Заказ №%s: ссылка выдана", order.id)
        except TelegramForbiddenError:
            log.warning("Покупатель %s заблокировал бота — выдам ссылку, когда он нажмёт /start",
                        order.telegram_id)

    async def notify_admins(self, order: Order) -> None:
        user = "—"
        try:
            chat = await self.bot.get_chat(order.telegram_id)
            user = f"@{chat.username}" if chat.username else html.escape(chat.full_name or "")
        except TelegramAPIError:
            pass
        text = texts.ADMIN_NEW_SALE.format(
            amount=f"{Decimal(order.amount):g}", user=user,
            tg_id=order.telegram_id, order_id=order.id,
        )
        for admin_id in self.cfg.admin_ids:
            try:
                await self.bot.send_message(admin_id, text)
            except TelegramAPIError:
                pass
