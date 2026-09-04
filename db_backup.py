"""
db_backup.py -- Модуль автоматического резервного копирования SQLite БД и отправки в Telegram.

Функционал:
  1. create_db_backup() -- Безопасное копирование SQLite БД (работает в WAL-режиме под нагрузкой).
  2. send_db_backup_to_telegram() -- Отправка сформированного файла бэкапа в Telegram-канал мониторинга.
"""

import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional
import requests

from config import settings

logger = logging.getLogger(__name__)


def create_db_backup(db_path: str = "sanaq.db", backup_dir: str = "backups") -> str:
    """
    Создает горячую резервную копию базы данных SQLite через встроенное API backup.
    Гарантирует консистентность данных даже при активной записи в WAL-режиме.

    Args:
        db_path: Путь к исходному файлу БД SQLite.
        backup_dir: Папка для сохранения резервных копий.

    Returns:
        str: Полный путь к созданному файлу бэкапа.
    """
    # Определяем корректный путь, если передана строка соединения типа sqlite:///./sanaq.db
    clean_path = db_path.replace("sqlite:///", "").replace("./", "")
    if not os.path.exists(clean_path):
        clean_path = "sanaq.db"

    if not os.path.exists(clean_path):
        raise FileNotFoundError(f"Файл базы данных '{clean_path}' не найден!")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_filename = f"sanaq_backup_{timestamp}.db"
    backup_file_path = os.path.join(backup_dir, backup_filename)

    src_conn = sqlite3.connect(clean_path)
    dst_conn = sqlite3.connect(backup_file_path)

    try:
        with dst_conn:
            src_conn.backup(dst_conn)
        logger.info("Горячий бэкап SQLite БД создан успешно: %s", backup_file_path)
    finally:
        dst_conn.close()
        src_conn.close()

    return backup_file_path


def send_db_backup_to_telegram(db_path: str = "sanaq.db", target_chat_id: Optional[str] = None) -> bool:
    """
    Создает резервную копию базы данных и отправляет .db файл в Telegram-канал мониторинга.

    Args:
        db_path: Путь к файлу БД.
        target_chat_id: Канал/чат получателя (по умолчанию из настроек MONITORING_CHANNEL_ID).

    Returns:
        bool: True при успехе, иначе False.
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or os.getenv("BOT_TOKEN") or "").strip()
    channel_id = (target_chat_id or settings.MONITORING_CHANNEL_ID or os.getenv("MONITORING_CHANNEL_ID") or "-4433809117").strip()

    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN не задан. Отправка бэкапа в Telegram отменена.")
        return False

    if not channel_id:
        logger.warning("MONITORING_CHANNEL_ID не задан. Отправка бэкапа в Telegram отменена.")
        return False

    try:
        backup_file = create_db_backup(db_path=db_path)
        file_size_bytes = os.path.getsize(backup_file)
        file_size_mb = file_size_bytes / (1024 * 1024)

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        caption = (
            f"💾 <b>РЕЗЕРВНАЯ КОПИЯ БАЗЫ ДАННЫХ (БЭКАП)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 <b>Дата:</b> <code>{now_str}</code>\n"
            f"📁 <b>Размер файла:</b> <code>{file_size_mb:.2f} MB</code>\n"
            f"⚙️ <b>Режим:</b> SQLite WAL Online Backup\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Система авто-бэкапов Sanaq AI</i>"
        )

        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"

        channels_to_try = [channel_id]
        if channel_id.startswith("-") and not channel_id.startswith("-100"):
            channels_to_try.append(f"-100{channel_id[1:]}")

        for chat in channels_to_try:
            with open(backup_file, "rb") as doc_file:
                files = {"document": doc_file}
                data = {"chat_id": chat, "caption": caption, "parse_mode": "HTML"}
                resp = requests.post(url, data=data, files=files, timeout=60)

                if resp.status_code == 200 and resp.json().get("ok"):
                    logger.info("Резервная копия БД успешно отправлена в Telegram (chat_id=%s)", chat)
                    return True
                else:
                    logger.error("Ошибка отправки документа бэкапа в Telegram (%d): %s", resp.status_code, resp.text)

        return False

    except Exception as exc:
        logger.error("Ошибка при генерации или отправке бэкапа БД: %s", exc, exc_info=True)
        return False
