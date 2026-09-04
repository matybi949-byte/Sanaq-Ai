"""
config.py -- Централизованная конфигурация приложения Sanaq AI.

Все переменные окружения загружаются один раз через pydantic-settings
и доступны как атрибуты единого объекта `settings`.

Использование:
    from config import settings
    print(settings.OPENAI_API_KEY)
"""

import os
from typing import List, Optional
try:
    from pydantic_settings import BaseSettings
except ImportError:
    from pydantic import BaseSettings  # type: ignore
from pydantic import Field


class Settings(BaseSettings):
    """
    Конфигурация Sanaq AI.
    Автоматически читает переменные из файла .env и окружения.
    """

    # ── OpenAI API ────────────────────────────
    OPENAI_API_KEY: str = Field(default="", description="API-ключ OpenAI")
    OPENAI_API_BASE: str = Field(
        default="https://api.openai.com/v1",
        description="Базовый URL OpenAI-совместимого API",
    )
    AI_MODEL: str = Field(default="gpt-5.6-luna", description="Модель ИИ для чата")
    WHISPER_MODEL: str = Field(default="whisper-1", description="Модель Whisper для транскрибации")
    AI_TEMPERATURE: float = Field(default=0.7, ge=0.0, le=2.0, description="Температура генерации")
    MAX_RESPONSE_TOKENS: int = Field(default=1024, ge=64, le=4096, description="Макс. токенов в ответе")
    MAX_HISTORY_MESSAGES: int = Field(default=20, ge=1, le=100, description="Макс. сообщений истории для контекста")

    # ── База данных ───────────────────────────
    DATABASE_URL: str = Field(
        default="sqlite:///./sanaq.db",
        description="Строка подключения к БД (SQLite по умолчанию)",
    )

    # ── Telegram Admin Bot ────────────────────
    TELEGRAM_BOT_TOKEN: str = Field(default="", alias="BOT_TOKEN", description="Токен Telegram-бота")
    ADMIN_ID: str = Field(default="", description="ID админов через запятую")
    DEFAULT_BUSINESS_ID: int = Field(default=1, description="ID бизнеса по умолчанию")
    MONITORING_CHANNEL_ID: str = Field(
        default="-4433809117",
        description="ID Telegram-канала для алертов о критических системных ошибках",
    )

    # ── Интеграция ABCP / Каталог ─────────────
    ABCP_API_URL: str = Field(default="", description="URL API ABCP (если используется)")
    ABCP_API_KEY: str = Field(default="", description="Ключ API ABCP")
    ABCP_USER_LOGIN: str = Field(default="", description="Логин пользователя ABCP")
    ABCP_USER_PASSWORD: str = Field(default="", description="Пароль пользователя ABCP")

    # ── WhatsApp мост ─────────────────────────
    WHATSAPP_BRIDGE_URL: str = Field(
        default="",
        description="URL обходного моста WhatsApp (whatsapp-web.js / Baileys / Evolution API)",
    )
    WHATSAPP_BRIDGE_TOKEN: str = Field(default="", description="Токен авторизации моста")

    # ── Instagram ─────────────────────────────
    INSTAGRAM_PAGE_ACCESS_TOKEN: str = Field(default="", description="Page Access Token для Instagram Graph API")

    # ── Webhooks ──────────────────────────────
    WEBHOOK_VERIFY_TOKEN: str = Field(
        default="sanaq_ai_verify_token",
        description="Токен верификации вебхуков Meta (WhatsApp/Instagram)",
    )
    WEBHOOK_URL: str = Field(
        default="",
        description="Внешний публичный URL сервера для регистрации Telegram setWebhook",
    )

    # ── Сервер ────────────────────────────────
    HOST: str = Field(default="0.0.0.0", description="Хост сервера")
    PORT: int = Field(default=8000, description="Порт сервера")
    DEBUG: bool = Field(default=False, description="Режим отладки")

    @property
    def admin_ids_list(self) -> List[int]:
        """Парсит ADMIN_ID (через запятую) в список int."""
        raw = self.ADMIN_ID.strip()
        if not raw:
            return []
        return [int(i.strip()) for i in raw.split(",") if i.strip().isdigit()]

    @property
    def telegram_bot_token(self) -> str:
        """Токен Telegram-бота (совместимость с legacy-кодом)."""
        return self.TELEGRAM_BOT_TOKEN

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "case_sensitive": True,
    }


# Единый глобальный объект конфигурации
settings = Settings()
