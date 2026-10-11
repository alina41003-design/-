"""Что бот отвечает на сообщения, кнопки и заявки на вступление в канал."""
import html
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, ChatJoinRequest, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from . import texts
from .db import HAS_ACCESS, Order
from .shop import Shop
from .yookassa import YooKassaError

log = logging.getLogger(__name__)
router = Router()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class Checkout(StatesGroup):
    waiting_email = State()


def pay_keyboard(shop: Shop, order: Order) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=shop.fmt(texts.PAY_BUTTON), url=order.confirmation_url)],
        [InlineKeyboardButton(text=texts.CHECK_BUTTON, callback_data=f"check:{order.token}")],
    ])


async def send_payment(message: Message, shop: Shop, telegram_id: int, email: str | None) -> None:
    if not shop.cfg.payments_enabled:
        await message.answer(shop.fmt(texts.PAYMENTS_SOON))
        return
    try:
        order = await shop.start_purchase(telegram_id, email)
    except (YooKassaError, OSError) as e:
        log.exception("Ошибка создания платежа: %s", e)
        await message.answer(texts.PAYMENT_ERROR)
        return
    await message.answer(texts.PAY, reply_markup=pay_keyboard(shop, order))


# ---------- /start, в том числе личные ссылки acc_... ----------

@router.message(CommandStart())
async def start(message: Message, command: CommandObject, shop: Shop, state: FSMContext):
    await state.clear()
    user = message.from_user
    await shop.db.save_user(user.id, user.username, user.first_name)
    payload = command.args or ""

    if payload.startswith("acc_"):
        await open_personal_link(message, shop, payload[4:])
        return

    order = await shop.db.access_order(user.id)
    if order:
        await shop.deliver(order)
        return
    await message.answer(shop.fmt(texts.WELCOME), reply_markup=shop.buy_keyboard())


async def open_personal_link(message: Message, shop: Shop, token: str) -> None:
    user_id = message.from_user.id
    order = await shop.db.get_by_token(token)
    if not order or order.status == "revoked":
        await message.answer(texts.UNKNOWN_LINK, reply_markup=shop.buy_keyboard())
        return

    # Ссылка привязывается к первому, кто её открыл. Переслать её другому уже не выйдет.
    if order.telegram_id is None:
        await shop.db.bind(order.id, user_id)
        order = await shop.db.get(order.id)
    if order.telegram_id != user_id:
        await message.answer(texts.FOREIGN_LINK, reply_markup=shop.buy_keyboard())
        return

    try:
        order = await shop.refresh(order)
    except (YooKassaError, OSError):
        log.exception("Не удалось проверить платёж заказа №%s", order.id)
    await reply_by_status(message, shop, order)


async def reply_by_status(message: Message, shop: Shop, order: Order) -> None:
    if order.status in HAS_ACCESS:
        await shop.deliver(order)
    elif order.status == "new" and order.confirmation_url:
        await message.answer(texts.NOT_PAID_YET, reply_markup=pay_keyboard(shop, order))
    else:
        await message.answer(texts.PAYMENT_CANCELED, reply_markup=shop.buy_keyboard())


# ---------- покупка ----------

@router.callback_query(F.data == "buy")
async def buy(call: CallbackQuery, shop: Shop, state: FSMContext):
    await call.answer()
    user = call.from_user
    await shop.db.save_user(user.id, user.username, user.first_name)

    order = await shop.db.access_order(user.id)
    if order:
        await shop.deliver(order)
        return

    email = await shop.db.get_email(user.id)
    if shop.cfg.payments_enabled and shop.cfg.receipt_enabled and not email:
        await state.set_state(Checkout.waiting_email)
        await call.message.answer(texts.ASK_EMAIL)
        return
    await send_payment(call.message, shop, user.id, email)


@router.message(Checkout.waiting_email, F.text)
async def got_email(message: Message, shop: Shop, state: FSMContext):
    email = message.text.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer(texts.BAD_EMAIL)
        return
    await state.clear()
    await shop.db.set_email(message.from_user.id, email)
    await send_payment(message, shop, message.from_user.id, email)


@router.callback_query(F.data.startswith("check:"))
async def check(call: CallbackQuery, shop: Shop):
    order = await shop.db.get_by_token(call.data.split(":", 1)[1])
    if not order or order.telegram_id != call.from_user.id:
        await call.answer()
        return
    try:
        order = await shop.refresh(order)
    except (YooKassaError, OSError):
        log.exception("Не удалось проверить платёж заказа №%s", order.id)
    if order.status == "new":
        await call.answer(texts.NOT_PAID_YET, show_alert=True)
        return
    await call.answer()
    await reply_by_status(call.message, shop, order)


# ---------- заявки на вступление в канал ----------

@router.chat_join_request()
async def join_request(request: ChatJoinRequest, shop: Shop):
    if request.chat.id != shop.cfg.channel_id:
        return
    user_id = request.from_user.id
    order = await shop.db.access_order(user_id)

    if order:
        await request.approve()
        await shop.db.mark_issued(order.id)
        await shop.db.log_join(user_id, approved=True)
        log.info("Впустил в канал %s (заказ №%s)", user_id, order.id)
        try:
            await shop.bot.send_message(request.user_chat_id, texts.JOIN_APPROVED)
        except TelegramAPIError:
            pass
        return

    # Сначала пишем человеку (Telegram разрешает это, пока заявка не обработана), потом отклоняем.
    try:
        await shop.bot.send_message(
            request.user_chat_id,
            shop.fmt(texts.JOIN_DECLINED),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                text=shop.fmt(texts.BUY_BUTTON),
                url=f"https://t.me/{shop.bot_username}?start=buy",
            )]]),
        )
    except TelegramAPIError:
        log.warning("Не смог написать %s после отклонения заявки", user_id)
    await request.decline()
    await shop.db.log_join(user_id, approved=False)
    log.info("Отклонил заявку %s: оплаты нет", user_id)


# ---------- команды администратора ----------

def is_admin(message: Message, shop: Shop) -> bool:
    return message.from_user.id in shop.cfg.admin_ids


@router.message(Command("stats"))
async def stats(message: Message, shop: Shop):
    if not is_admin(message, shop):
        return
    s = await shop.db.stats()
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Запустили бота: {s['users']}\n"
        f"Ждут оплату: {s.get('new', 0)}\n"
        f"Оплачено, ссылка ещё не выдана: {s.get('paid', 0)}\n"
        f"Оплачено и выдано: {s.get('issued', 0)}\n"
        f"Отменённые платежи: {s.get('canceled', 0)}\n"
        f"Доступ отозван: {s.get('revoked', 0)}\n"
        f"Выручка через ЮKassa: {s['revenue']:g} ₽\n"
        f"Пытались зайти без оплаты: {s['rejected']} чел."
    )


@router.message(Command("grant"))
async def grant(message: Message, command: CommandObject, shop: Shop):
    """/grant — одноразовая ссылка-доступ (например, для оплаты переводом или подарка).
    /grant 123456789 — сразу выдать доступ человеку с этим Telegram-ID."""
    if not is_admin(message, shop):
        return
    arg = (command.args or "").strip()
    if arg:
        if not arg.isdigit():
            await message.answer("Пример: /grant 123456789")
            return
        order = await shop.db.create_order(int(arg), "0", status="paid", source="manual")
        await shop.deliver(order)
        await message.answer(f"Готово: доступ выдан пользователю {arg} (заказ №{order.id}).")
        return
    order = await shop.db.create_order(None, "0", status="paid", source="manual")
    await message.answer(
        "Одноразовая ссылка-доступ (заказ №{id}). Отправьте её человеку. Она привяжется "
        "к первому Telegram-аккаунту, который её откроет:\n\n{link}".format(
            id=order.id, link=shop.personal_link(order)),
    )


@router.message(Command("revoke"))
async def revoke(message: Message, command: CommandObject, shop: Shop):
    """/revoke 123456789 — забрать доступ и удалить человека из канала."""
    if not is_admin(message, shop):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Пример: /revoke 123456789")
        return
    user_id = int(arg)
    count = await shop.db.revoke(user_id)
    try:
        await shop.bot.ban_chat_member(shop.cfg.channel_id, user_id)
        await shop.bot.unban_chat_member(shop.cfg.channel_id, user_id)  # убрать из канала без вечного бана
    except TelegramAPIError as e:
        log.warning("Не смог удалить %s из канала: %s", user_id, e)
    await message.answer(f"Доступ отозван (заказов: {count}), пользователь удалён из канала.")


@router.message(F.forward_origin.type == "channel")
async def channel_id(message: Message, shop: Shop):
    """Перешлите боту любой пост из канала, и он покажет ID канала для файла .env."""
    if shop.cfg.channel_id and not is_admin(message, shop):
        return
    chat = message.forward_origin.chat
    await message.answer(
        f"ID канала «{html.escape(chat.title or '')}»: <code>{chat.id}</code>\n"
        "Впишите его в .env в строку CHANNEL_ID и перезапустите бота."
    )


@router.message(Command("id"))
async def my_id(message: Message):
    await message.answer(f"Ваш Telegram-ID: <code>{message.from_user.id}</code>")
