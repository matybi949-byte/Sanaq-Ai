"""
error_notifier.py -- Модуль отправки уведомления о критических системных ошибках в Telegram-канал мониторинга.

Обеспечивает:
  1. send_critical_error() -- Синхронная отправка информации об ошибке прямо в Telegram-канал (-4433809117).
  2. async_send_critical_error() -- Асинхронная версия функции отправки ошибок.
  3. TelegramErrorLoggingHandler -- Кастомный хэндлер для logging.Logger, автоматически перехватывающий ERROR и CRITICAL логи.
"""

import os
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, Union
import httpx
import requests

from config import settings

logger = logging.getLogger(__name__)


def send_critical_error(
    error: Union[Exception, str],
    context: Optional[str] = None,
    trace: Optional[str] = None,
) -> bool:
    """
    Отправляет детализированный текст критической ошибки в Telegram-канал мониторинга.

    Args:
        error: Объект исключения Exception или текст ошибки.
        context: Описание модуля/эндпоинта, где произошел сбой.
        trace: Стек-трейс исключения (если не передан, формируется автоматически).

    Returns:
        bool: True, если сообщение успешно доставлено в канал, иначе False.
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()
    channel_id = (settings.MONITORING_CHANNEL_ID or os.getenv("MONITORING_CHANNEL_ID") or "-4433809117").strip()

    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN не настроен. Отправка ошибки в канал мониторинга пропущена.")
        return False

    if not channel_id:
        logger.warning("MONITORING_CHANNEL_ID не настроен. Отправка ошибки в канал мониторинга пропущена.")
        return False

    # Формирование детального описания ошибки
    if isinstance(error, Exception):
        error_msg = f"{type(error).__name__}: {str(error)}"
        if not trace:
            trace = traceback.format_exc()
    else:
        error_msg = str(error)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ctx_str = context or "Системный сбой"

    formatted_text = (
        f"🚨 <b>КРИТИЧЕСКАЯ СИСТЕМНАЯ ОШИБКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Время:</b> <code>{now_str}</code>\n"
        f"📍 <b>Контекст:</b> <b>{ctx_str}</b>\n\n"
        f"❌ <b>Ошибка:</b>\n<code>{error_msg[:1000]}</code>\n"
    )

    if trace and "NoneType: None" not in trace and trace.strip() != "None":
        formatted_text += f"\n📜 <b>Traceback:</b>\n<code>{trace[:1500]}</code>"

    formatted_text += f"\n━━━━━━━━━━━━━━━━━━━━━━\n<i>Автоматическая система мониторинга Sanaq AI</i>"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Telegram каналы/супергруппы могут использовать -4433809117 или -1004433809117
    channels_to_try = [channel_id]
    if channel_id.startswith("-") and not channel_id.startswith("-100"):
        channels_to_try.append(f"-100{channel_id[1:]}")

    for target_chat in channels_to_try:
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": target_chat,
                    "text": formatted_text,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                logger.info("Уведомление о критической ошибке доставлено в канал %s", target_chat)
                return True
            else:
                # Резервная попытка без HTML разметки при конфликте тегов
                fallback_text = (
                    f"🚨 КРИТИЧЕСКАЯ ОШИБКА [{now_str}]\n"
                    f"Контекст: {ctx_str}\n"
                    f"Ошибка: {error_msg}\n\n"
                    f"{trace[:1000] if trace else ''}"
                )
                retry_resp = requests.post(
                    url,
                    json={
                        "chat_id": target_chat,
                        "text": fallback_text,
                    },
                    timeout=10,
                )
                if retry_resp.status_code == 200 and retry_resp.json().get("ok"):
                    logger.info("Уведомление об ошибке доставлено в канал %s (без HTML)", target_chat)
                    return True
                logger.error("Ошибка Telegram API при отправке алерта (%d): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("Не удалось отправить алерт об ошибке в канал %s: %s", target_chat, e)

    return False


async def async_send_critical_error(
    error: Union[Exception, str],
    context: Optional[str] = None,
    trace: Optional[str] = None,
) -> bool:
    """
    Асинхронная отправка критической ошибки в Telegram-канал мониторинга.
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()
    channel_id = (settings.MONITORING_CHANNEL_ID or os.getenv("MONITORING_CHANNEL_ID") or "-4433809117").strip()

    if not bot_token or not channel_id:
        return False

    if isinstance(error, Exception):
        error_msg = f"{type(error).__name__}: {str(error)}"
        if not trace:
            trace = traceback.format_exc()
    else:
        error_msg = str(error)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ctx_str = context or "Системный сбой"

    formatted_text = (
        f"🚨 <b>КРИТИЧЕСКАЯ СИСТЕМНАЯ ОШИБКА</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 <b>Время:</b> <code>{now_str}</code>\n"
        f"📍 <b>Контекст:</b> <b>{ctx_str}</b>\n\n"
        f"❌ <b>Ошибка:</b>\n<code>{error_msg[:1000]}</code>\n"
    )

    if trace and "NoneType: None" not in trace and trace.strip() != "None":
        formatted_text += f"\n📜 <b>Traceback:</b>\n<code>{trace[:1500]}</code>"

    formatted_text += f"\n━━━━━━━━━━━━━━━━━━━━━━\n<i>Автоматическая система мониторинга Sanaq AI</i>"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    channels_to_try = [channel_id]
    if channel_id.startswith("-") and not channel_id.startswith("-100"):
        channels_to_try.append(f"-100{channel_id[1:]}")

    async with httpx.AsyncClient(timeout=10.0) as client:
        for target_chat in channels_to_try:
            try:
                resp = await client.post(
                    url,
                    json={
                        "chat_id": target_chat,
                        "text": formatted_text,
                        "parse_mode": "HTML",
                    },
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Асинхронный алерт о критической ошибке доставлен в %s", target_chat)
                    return True
                else:
                    fallback_text = (
                        f"🚨 КРИТИЧЕСКАЯ ОШИБКА [{now_str}]\n"
                        f"Контекст: {ctx_str}\n"
                        f"Ошибка: {error_msg}\n\n"
                        f"{trace[:1000] if trace else ''}"
                    )
                    retry_resp = await client.post(
                        url,
                        json={
                            "chat_id": target_chat,
                            "text": fallback_text,
                        },
                    )
                    if retry_resp.status_code == 200 and retry_resp.json().get("ok"):
                        return True
            except Exception as e:
                logger.error("Ошибка асинхронной отправки алерта в канал %s: %s", target_chat, e)

    return False


class TelegramErrorLoggingHandler(logging.Handler):
    """
    Хэндлер логирования Python, перехватывающий ERROR и CRITICAL события
    и автоматически отправляющий их в канал мониторинга.
    """
    def __init__(self, level=logging.ERROR):
        super().__init__(level=level)

    def emit(self, record: logging.LogRecord):
        # Предотвращаем бесконечную рекурсию
        if record.name == __name__ or "api.telegram.org" in record.getMessage():
            return

        try:
            exc_text = None
            if record.exc_info:
                exc_text = "".join(traceback.format_exception(*record.exc_info))

            ctx = f"Logger [{record.name}] ({record.levelname})"
            send_critical_error(
                error=record.getMessage(),
                context=ctx,
                trace=exc_text,
            )
        except Exception:
            self.handleError(record)
