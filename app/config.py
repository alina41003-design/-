"""Читает настройки из файла .env. Сами пароли и ключи в коде не хранятся."""
import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"В файле .env не заполнена настройка {name}")
    return value


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in ("1", "true", "yes", "да")


@dataclass(frozen=True)
class Config:
    bot_token: str
    channel_id: int
    admin_ids: tuple[int, ...]

    product_name: str
    price: Decimal

    yookassa_shop_id: str
    yookassa_secret_key: str
    receipt_enabled: bool
    vat_code: int

    webhook_host: str
    webhook_port: int
    webhook_path: str

    db_path: str
    check_interval_sec: int
    reminder_minutes: int


def load_config() -> Config:
    admin_ids = tuple(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=_required("BOT_TOKEN"),
        channel_id=int(os.getenv("CHANNEL_ID", "").strip() or 0),
        admin_ids=admin_ids,
        product_name=os.getenv(
            "PRODUCT_NAME",
            "Гайд «Не баночки, а логика: как выбирать уход за волосами по составу»",
        ),
        price=Decimal(os.getenv("PRICE", "490")).quantize(Decimal("0.01")),
        yookassa_shop_id=_required("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_required("YOOKASSA_SECRET_KEY"),
        receipt_enabled=_bool("RECEIPT_ENABLED", True),
        vat_code=int(os.getenv("VAT_CODE", "1")),
        webhook_host=os.getenv("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=int(os.getenv("WEBHOOK_PORT", "8080")),
        webhook_path=os.getenv("WEBHOOK_PATH", "/yookassa/webhook"),
        db_path=os.getenv("DB_PATH", "data/shop.db"),
        check_interval_sec=int(os.getenv("CHECK_INTERVAL_SEC", "30")),
        reminder_minutes=int(os.getenv("REMINDER_MINUTES", "60")),
    )
