"""Проверка всего пути без настоящего Telegram и ЮKassa: python -m unittest -v"""
import os
import tempfile
import unittest
from types import SimpleNamespace

os.environ.update(BOT_TOKEN="1:test", YOOKASSA_SHOP_ID="1", YOOKASSA_SECRET_KEY="s",
                  CHANNEL_ID="-1001", ADMIN_IDS="999")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app.config import load_config  # noqa: E402
from app.db import Database  # noqa: E402
from app.handlers import join_request, open_personal_link  # noqa: E402
from app.shop import Shop  # noqa: E402
from app.webhook import create_app  # noqa: E402
from app.yookassa import YooKassaError  # noqa: E402

BUYER, STRANGER = 111, 222


class FakeBot:
    id = 1

    def __init__(self):
        self.sent, self.members, self.links = [], set(), 0

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def create_chat_invite_link(self, chat_id, **kw):
        assert kw["creates_join_request"]
        self.links += 1
        return SimpleNamespace(invite_link=f"https://t.me/+link{self.links}")

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="member" if user_id in self.members else "left")

    async def get_chat(self, chat_id):
        return SimpleNamespace(username="buyer", full_name="Buyer")


class FakeYooKassa:
    def __init__(self):
        self.payments = {}

    async def create_payment(self, *, amount, order_token, email, **kw):
        pid = f"pay{len(self.payments) + 1}"
        self.payments[pid] = {"id": pid, "status": "pending", "paid": False,
                              "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                              "metadata": {"order_token": order_token}}
        return {"id": pid, "confirmation": {"confirmation_url": f"https://yoomoney.ru/{pid}"}}

    async def get_payment(self, pid):
        if pid not in self.payments:
            raise YooKassaError("404")
        return self.payments[pid]

    def pay(self, pid):
        self.payments[pid].update(status="succeeded", paid=True)


class FlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        await self.db.connect()
        self.bot, self.yk = FakeBot(), FakeYooKassa()
        self.shop = Shop(self.bot, self.db, self.yk, load_config())
        self.shop.bot_username = "guide_bot"
        self.client = TestClient(TestServer(create_app(self.shop)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()
        self.tmp.cleanup()

    async def webhook(self, pid):
        resp = await self.client.post("/yookassa/webhook",
                                      json={"event": "payment.succeeded", "object": {"id": pid}})
        return resp.status

    def join(self, user_id):
        calls = []

        async def approve():
            calls.append("approve")

        async def decline():
            calls.append("decline")

        req = SimpleNamespace(chat=SimpleNamespace(id=-1001), from_user=SimpleNamespace(id=user_id),
                              user_chat_id=user_id, approve=approve, decline=decline)
        return req, calls

    def message(self, user_id):
        answers = []

        async def answer(text, **kw):
            answers.append(text)

        return SimpleNamespace(from_user=SimpleNamespace(id=user_id), answer=answer), answers

    async def test_full_purchase(self):
        # Без оплаты в канал не пускает и предлагает купить
        req, calls = self.join(BUYER)
        await join_request(req, self.shop)
        self.assertEqual(calls, ["decline"])
        self.assertIn("490", self.bot.sent[-1][1])

        order = await self.shop.start_purchase(BUYER, "a@b.ru")
        self.assertEqual(order.status, "new")
        # Повторное нажатие «Купить» не плодит платежи
        self.assertEqual((await self.shop.start_purchase(BUYER, "a@b.ru")).id, order.id)

        # Уведомление пришло, а платёж в ЮKassa ещё не оплачен — доступа нет
        self.assertEqual(await self.webhook(order.payment_id), 200)
        self.assertEqual((await self.db.get(order.id)).status, "new")

        self.yk.pay(order.payment_id)
        self.assertEqual(await self.webhook(order.payment_id), 200)
        self.assertEqual(await self.webhook(order.payment_id), 200)  # дубль уведомления
        order = await self.db.get(order.id)
        self.assertEqual(order.status, "issued")
        access_msgs = [m for m in self.bot.sent if m[0] == BUYER and "личная ссылка" in m[1]]
        self.assertEqual(len(access_msgs), 1)
        self.assertTrue(access_msgs[0][2]["protect_content"])
        self.assertTrue(any(m[0] == 999 for m in self.bot.sent))  # админу пришло уведомление

        req, calls = self.join(BUYER)
        await join_request(req, self.shop)
        self.assertEqual(calls, ["approve"])

        # Чужой человек по той же ссылке не войдёт
        req, calls = self.join(STRANGER)
        await join_request(req, self.shop)
        self.assertEqual(calls, ["decline"])

        # Личная deep-link ссылка покупателя не сработает у другого
        msg, answers = self.message(STRANGER)
        await open_personal_link(msg, self.shop, order.token)
        self.assertIn("другому Telegram-аккаунту", answers[-1])

    async def test_fake_webhook_ignored(self):
        self.assertEqual(await self.webhook("forged"), 200)
        order = await self.shop.start_purchase(BUYER, "a@b.ru")
        self.yk.payments[order.payment_id]["amount"]["value"] = "1.00"
        self.yk.payments[order.payment_id].update(status="succeeded", paid=True)
        await self.webhook(order.payment_id)
        self.assertEqual((await self.db.get(order.id)).status, "new")  # сумма не та

    async def test_checker_without_webhook(self):
        order = await self.shop.start_purchase(BUYER, "a@b.ru")
        self.yk.pay(order.payment_id)
        for o in await self.db.pending_payments():
            await self.shop.refresh(o)
        self.assertEqual((await self.db.get(order.id)).status, "issued")

    async def test_one_time_grant_link(self):
        order = await self.db.create_order(None, "0", status="paid", source="manual")
        msg, _ = self.message(BUYER)
        await open_personal_link(msg, self.shop, order.token)
        self.assertEqual((await self.db.get(order.id)).telegram_id, BUYER)
        msg, answers = self.message(STRANGER)
        await open_personal_link(msg, self.shop, order.token)
        self.assertIn("другому Telegram-аккаунту", answers[-1])
        self.assertIsNone(await self.db.access_order(STRANGER))


    async def test_demo_mode_without_keys(self):
        from dataclasses import replace
        from app.handlers import send_payment
        self.shop.cfg = replace(self.shop.cfg, yookassa_shop_id="", yookassa_secret_key="")
        msg, answers = self.message(BUYER)
        await send_payment(msg, self.shop, BUYER, None)
        self.assertIn("подключается", answers[-1])
        self.assertIsNone(await self.db.open_order(BUYER))


if __name__ == "__main__":
    unittest.main()
