"""
heartbeat.py -- Модуль проверки доступности (Server Uptime Heartbeat & Health Check).

Периодическая отправка отчёта о состоянии сервера, базы данных и активности системы
прямо в Telegram-канал мониторинга.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
import requests
import httpx
from sqlalchemy import func

from config import settings
from database import SessionLocal
from models import Business, Order, Client, Product

logger = logging.getLogger(__name__)

# Запоминаем время запуска сервера
SERVER_START_TIME = datetime.now(timezone.utc)


def get_system_health_metrics() -> Dict[str, Any]:
    """
    Собирает метрики работоспособности системы: статус БД, количество бизнесов,
    заказов за сегодня, объем БД и аптайм.
    """
    uptime_seconds = int((datetime.now(timezone.utc) - SERVER_START_TIME).total_seconds())
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}ч {minutes}м {seconds}с"

    db_filename = "sanaq.db"
    db_size_mb = (os.path.getsize(db_filename) / (1024 * 1024)) if os.path.exists(db_filename) else 0.0

    businesses_count = 0
    orders_today_count = 0
    total_sales_today = 0.0
    clients_count = 0

    try:
        with SessionLocal() as db:
            businesses_count = db.query(func.count(Business.id)).scalar() or 0
            clients_count = db.query(func.count(Client.id)).scalar() or 0

            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            orders_today_count = db.query(func.count(Order.id)).filter(Order.created_at >= today_start).scalar() or 0

            sales_val = (
                db.query(func.coalesce(func.sum(Order.total_price), 0.0))
                .filter(Order.created_at >= today_start, Order.status.in_(("paid", "completed")))
                .scalar()
            )
            total_sales_today = float(sales_val or 0.0)
    except Exception as exc:
        logger.error("Ошибка сбора метрик БД для Heartbeat: %s", exc)

    return {
        "status": "🟢 ONLINE",
        "uptime": uptime_str,
        "ai_model": settings.AI_MODEL,
        "db_size_mb": round(db_size_mb, 2),
        "businesses_count": businesses_count,
        "clients_count": clients_count,
        "orders_today": orders_today_count,
        "sales_today": total_sales_today,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def send_uptime_heartbeat(target_chat_id: Optional[str] = None) -> bool:
    """
    Синхронная отправка отчёта о доступности (Heartbeat) в Telegram-канал мониторинга.
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()
    channel_id = (target_chat_id or settings.MONITORING_CHANNEL_ID or os.getenv("MONITORING_CHANNEL_ID") or "-4433809117").strip()

    if not bot_token or not channel_id:
        logger.warning("TELEGRAM_BOT_TOKEN или MONITORING_CHANNEL_ID не настроен. Heartbeat пропущен.")
        return False

    metrics = get_system_health_metrics()

    text = (
        f"💓 <b>SERVER UPTIME HEARTBEAT (СТАТУС СИСТЕМЫ)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Статус: <b>{metrics['status']}</b>\n"
        f"⏱ <b>Время работы (Uptime):</b> <code>{metrics['uptime']}</code>\n"
        f"🕒 <b>Время проверки:</b> <code>{metrics['timestamp']}</code>\n\n"
        f"🤖 <b>Модель ИИ:</b> <code>{metrics['ai_model']}</code>\n"
        f"💾 <b>Размер БД:</b> <code>{metrics['db_size_mb']} MB</code>\n"
        f"🏢 <b>Активных бизнесов:</b> <code>{metrics['businesses_count']}</code>\n"
        f"👥 <b>Всего клиентов в базе:</b> <code>{metrics['clients_count']}</code>\n"
        f"🛍 <b>Заказов за сегодня:</b> <code>{metrics['orders_today']}</code>\n"
        f"💰 <b>Выручка за сегодня:</b> <code>{metrics['sales_today']:,.0f} тг.</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Мониторинг доступности Sanaq AI</i>"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    channels_to_try = [channel_id]
    if channel_id.startswith("-") and not channel_id.startswith("-100"):
        channels_to_try.append(f"-100{channel_id[1:]}")

    for chat in channels_to_try:
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("Heartbeat успешно доставлен в канал %s", chat)
                return True
        except Exception as err:
            logger.error("Ошибка отправки Heartbeat в %s: %s", chat, err)

    return False


async def async_send_uptime_heartbeat(target_chat_id: Optional[str] = None) -> bool:
    """
    Асинхронная версия отправки отчёта о доступности (Heartbeat).
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()
    channel_id = (target_chat_id or settings.MONITORING_CHANNEL_ID or os.getenv("MONITORING_CHANNEL_ID") or "-4433809117").strip()

    if not bot_token or not channel_id:
        return False

    metrics = get_system_health_metrics()

    text = (
        f"💓 <b>SERVER UPTIME HEARTBEAT (СТАТУС СИСТЕМЫ)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Статус: <b>{metrics['status']}</b>\n"
        f"⏱ <b>Время работы (Uptime):</b> <code>{metrics['uptime']}</code>\n"
        f"🕒 <b>Время проверки:</b> <code>{metrics['timestamp']}</code>\n\n"
        f"🤖 <b>Модель ИИ:</b> <code>{metrics['ai_model']}</code>\n"
        f"💾 <b>Размер БД:</b> <code>{metrics['db_size_mb']} MB</code>\n"
        f"🏢 <b>Активных бизнесов:</b> <code>{metrics['businesses_count']}</code>\n"
        f"👥 <b>Всего клиентов в базе:</b> <code>{metrics['clients_count']}</code>\n"
        f"🛍 <b>Заказов за сегодня:</b> <code>{metrics['orders_today']}</code>\n"
        f"💰 <b>Выручка за сегодня:</b> <code>{metrics['sales_today']:,.0f} тг.</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Мониторинг доступности Sanaq AI</i>"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    channels_to_try = [channel_id]
    if channel_id.startswith("-") and not channel_id.startswith("-100"):
        channels_to_try.append(f"-100{channel_id[1:]}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for chat in channels_to_try:
            try:
                resp = await client.post(url, json={"chat_id": chat, "text": text, "parse_mode": "HTML"})
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Асинхронный Heartbeat доставлен в %s", chat)
                    return True
            except Exception as e:
                logger.error("Ошибка асинхронного Heartbeat в %s: %s", chat, e)

    return False
