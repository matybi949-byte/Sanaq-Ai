"""
analytics.py -- Модуль аналитики для Sanaq AI.

Функции:
  - get_daily_analytics()  — Подсчет дневной аналитики за указанную дату:
      • Количество уникальных клиентов (по таблице заказов).
      • Общая сумма продаж (total_price заказов со статусом paid/completed).
      • Средний чек.
  - format_daily_report() — Форматирование отчёта для отправки в Telegram.
  - send_daily_report_to_admin() — Отправка готового отчёта в Telegram-админ-бот.
"""

import os
import logging
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, Any

import requests
from sqlalchemy import func, distinct, case
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Order, Client, Business

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Подсчёт дневной аналитики
# ──────────────────────────────────────────────

def get_daily_analytics(
    db: Session,
    business_id: int,
    target_date: Optional[date] = None,
    shop_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Подсчитывает дневную аналитику для указанного бизнеса/магазина.

    Считает по заказам со статусом 'paid' или 'completed' за указанную дату.
    Применяет строгую фильтрацию по shop_id для изоляции данных арендатора.

    Args:
        db:           Сессия SQLAlchemy.
        business_id:  ID бизнеса.
        target_date:  Дата для подсчёта (по умолчанию — сегодня).
        shop_id:      ID магазина (арендатора).

    Returns:
        Словарь со статистикой бизнеса.
    """
    target_shop_id = shop_id if shop_id is not None else business_id

    if target_date is None:
        target_date = date.today()

    # Диапазон дня: от 00:00:00 до 23:59:59
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date, datetime.max.time())

    # Название бизнеса
    business = db.query(Business).filter((Business.id == target_shop_id) | (Business.shop_id == target_shop_id)).first()
    business_name = business.name if business else f"Бизнес #{target_shop_id}"

    # Оплаченные/завершённые заказы за день
    paid_statuses = ("paid", "completed")

    paid_query = (
        db.query(
            func.count(Order.id).label("total_orders"),
            func.count(distinct(Order.client_id)).label("unique_clients"),
            func.coalesce(func.sum(Order.total_price), 0.0).label("total_sales"),
        )
        .filter(
            Order.shop_id == target_shop_id,
            Order.status.in_(paid_statuses),
            Order.created_at >= day_start,
            Order.created_at <= day_end,
        )
        .first()
    )

    total_orders = paid_query.total_orders or 0
    unique_clients = paid_query.unique_clients or 0
    total_sales = float(paid_query.total_sales or 0.0)
    average_check = round(total_sales / total_orders, 2) if total_orders > 0 else 0.0

    # Новые (неоплаченные) заказы за день
    new_orders = (
        db.query(func.count(Order.id))
        .filter(
            Order.shop_id == target_shop_id,
            Order.status.in_(("new", "pending_checkout")),
            Order.created_at >= day_start,
            Order.created_at <= day_end,
        )
        .scalar() or 0
    )

    # Отменённые заказы за день
    cancelled_orders = (
        db.query(func.count(Order.id))
        .filter(
            Order.shop_id == target_shop_id,
            Order.status == "cancelled",
            Order.created_at >= day_start,
            Order.created_at <= day_end,
        )
        .scalar() or 0
    )

    # Количество эскалированных клиентов за день (needs_human=True)
    escalated_clients = (
        db.query(func.count(Client.id))
        .filter(
            Client.shop_id == target_shop_id,
            Client.needs_human == True,
            Client.escalated_at >= day_start,
            Client.escalated_at <= day_end,
        )
        .scalar() or 0
    )

    return {
        "date": target_date.isoformat(),
        "business_id": target_shop_id,
        "shop_id": target_shop_id,
        "business_name": business_name,
        "unique_clients": unique_clients,
        "total_sales": total_sales,
        "average_check": average_check,
        "total_orders": total_orders,
        "new_orders": new_orders,
        "cancelled_orders": cancelled_orders,
        "escalated_clients": escalated_clients,
    }


# ──────────────────────────────────────────────
# Форматирование отчёта для Telegram
# ──────────────────────────────────────────────

def format_daily_report(analytics: Dict[str, Any]) -> str:
    """
    Форматирует аналитику в красивое HTML-сообщение для Telegram.

    Args:
        analytics: Результат get_daily_analytics().

    Returns:
        Строка с HTML-разметкой для Telegram Bot API.
    """
    # Определяем дату в красивом формате
    try:
        dt = datetime.fromisoformat(analytics["date"])
        date_str = dt.strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        date_str = analytics["date"]

    # Определяем «настроение» дня по сумме продаж
    total = analytics["total_sales"]
    if total >= 100000:
        mood = "🔥"
    elif total >= 50000:
        mood = "📈"
    elif total >= 10000:
        mood = "👍"
    elif total > 0:
        mood = "📊"
    else:
        mood = "😴"

    text = (
        f"{mood} <b>ДНЕВНОЙ ОТЧЁТ — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🏢 <b>Бизнес:</b> {analytics['business_name']} (ID: {analytics['business_id']})\n\n"
        f"👥 <b>Уникальных клиентов:</b> <code>{analytics['unique_clients']}</code>\n"
        f"🛍 <b>Оплаченных заказов:</b> <code>{analytics['total_orders']}</code>\n"
        f"📝 <b>Новых заказов (в работе):</b> <code>{analytics['new_orders']}</code>\n"
        f"❌ <b>Отменённых заказов:</b> <code>{analytics['cancelled_orders']}</code>\n\n"
        f"💰 <b>Общая сумма продаж:</b> <code>{analytics['total_sales']:,.0f} тг.</code>\n"
        f"🧮 <b>Средний чек:</b> <code>{analytics['average_check']:,.0f} тг.</code>\n\n"
        f"🚨 <b>Эскалировано клиентов:</b> <code>{analytics['escalated_clients']}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Отчёт сгенерирован автоматически Sanaq AI</i>"
    )
    return text


# ──────────────────────────────────────────────
# Отправка отчёта в Telegram-админ-бот
# ──────────────────────────────────────────────

def send_daily_report_to_admin(
    business_id: int,
    target_date: Optional[date] = None,
) -> bool:
    """
    Генерирует дневную аналитику и отправляет готовый отчёт в Telegram-админ-бот.

    Args:
        business_id: ID бизнеса.
        target_date:  Дата для подсчёта (по умолчанию — сегодня).

    Returns:
        True, если хотя бы одному админу отчёт отправлен успешно.
    """
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    raw_admin_ids = os.getenv("ADMIN_ID", "").strip()
    admin_ids = [int(i.strip()) for i in raw_admin_ids.split(",") if i.strip().isdigit()]

    if not bot_token or not admin_ids:
        logger.warning(
            "TELEGRAM_BOT_TOKEN или ADMIN_ID не настроены в .env. "
            "Отчёт не отправлен."
        )
        return False

    # Подсчитываем аналитику
    with SessionLocal() as db:
        analytics = get_daily_analytics(db, business_id, target_date)

    # Форматируем
    report_text = format_daily_report(analytics)

    # Отправляем
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    success_count = 0

    for admin_id in admin_ids:
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": admin_id,
                    "text": report_text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                success_count += 1
                logger.info(
                    "Дневной отчёт за %s отправлен admin_id=%d (бизнес #%d)",
                    analytics["date"], admin_id, business_id,
                )
            else:
                logger.error(
                    "Ошибка отправки отчёта в Telegram (HTTP %d): %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as e:
            logger.error("Не удалось отправить отчёт admin_id=%d: %s", admin_id, e)

    return success_count > 0
