"""Работа с ЮKassa: создать платёж и узнать его настоящий статус."""
from decimal import Decimal

import aiohttp

API_URL = "https://api.yookassa.ru/v3"


class YooKassaError(Exception):
    pass


class YooKassa:
    def __init__(self, shop_id: str, secret_key: str):
        self._auth = aiohttp.BasicAuth(shop_id, secret_key)
        self._session: aiohttp.ClientSession | None = None

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                auth=self._auth, timeout=aiohttp.ClientTimeout(total=30)
            )
        async with self._session.request(method, API_URL + path, **kwargs) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise YooKassaError(f"ЮKassa ответила {resp.status}: {data}")
            return data

    async def create_payment(
        self, *, amount: Decimal, description: str, order_token: str, return_url: str,
        email: str | None, receipt_enabled: bool, vat_code: int,
    ) -> dict:
        value = f"{amount:.2f}"
        body = {
            "amount": {"value": value, "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description[:128],
            "metadata": {"order_token": order_token},
        }
        if receipt_enabled:
            body["receipt"] = {
                "customer": {"email": email},
                "items": [{
                    "description": description[:128],
                    "quantity": "1.00",
                    "amount": {"value": value, "currency": "RUB"},
                    "vat_code": vat_code,
                    "payment_mode": "full_payment",
                    "payment_subject": "service",
                }],
            }
        # Ключ идемпотентности: при повторе запроса ЮKassa не создаст второй платёж.
        headers = {"Idempotence-Key": order_token}
        return await self._request("POST", "/payments", json=body, headers=headers)

    async def get_payment(self, payment_id: str) -> dict:
        return await self._request("GET", f"/payments/{payment_id}")

    async def close(self) -> None:
        if self._session:
            await self._session.close()
