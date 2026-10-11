"""Приём уведомлений (webhook) от ЮKassa об оплате.

Самому уведомлению не доверяем: по ID платежа заново спрашиваем статус в API ЮKassa
с нашим секретным ключом. Подделать такое подтверждение нельзя.
"""
import json
import logging

from aiohttp import web

from .shop import Shop
from .yookassa import YooKassaError

log = logging.getLogger(__name__)


def create_app(shop: Shop) -> web.Application:
    async def yookassa_webhook(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            payment_id = data["object"]["id"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return web.Response(status=400, text="bad request")

        log.info("Уведомление ЮKassa: %s, платёж %s", data.get("event"), payment_id)
        try:
            payment = await shop.yk.get_payment(payment_id)
        except YooKassaError as e:
            # Платёж не найден в нашем магазине — значит, уведомление поддельное или чужое.
            log.warning("Отклонено уведомление о платеже %s: %s", payment_id, e)
            return web.Response(status=200, text="ignored")
        except OSError:
            log.exception("ЮKassa недоступна, она повторит уведомление позже")
            return web.Response(status=503, text="retry")

        await shop.process_payment(payment)
        return web.Response(status=200, text="ok")

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_post(shop.cfg.webhook_path, yookassa_webhook)
    app.router.add_get("/health", health)
    return app
