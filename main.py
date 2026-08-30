"""
main.py -- Точка входа FastAPI-приложения "Sanaq AI".

SaaS-платформа ИИ-менеджера для малого бизнеса.
Интеграции: Telegram, Instagram, WhatsApp (обходной мост), ABCP/каталог.

Оптимизировано для VPS (2 GB RAM):
  - SQLite + WAL-режим.
  - Легковесный httpx вместо тяжелой openai-библиотеки.
  - Connection pooling и lazy-инициализация.
"""

import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, date, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import settings
from database import engine, Base, get_db, init_db
from models import Business, Product, Client, ChatMessage
from rate_limiter import limiter
from ai_service import (
    get_ai_response,
    detect_language,
    analyze_image_for_article,
    transcribe_voice,
    prepare_incoming_message,
)
from orders import create_order, OrderCreateRequest, OrderResult
from webhook_router import router as webhook_router
from admin_router import router as admin_router
from omnichannel import router as omni_router
from analytics import get_daily_analytics, format_daily_report, send_daily_report_to_admin
from openai_service import openai_client

# Импортируем модели, чтобы SQLAlchemy знал обо всех таблицах
import models  # noqa: F401


# ──────────────────────────────────────────────
# Модуль 1: Детальное логирование в файл app.log
# ──────────────────────────────────────────────

log_formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Хэндлер для записи в файл app.log
file_handler = logging.FileHandler("app.log", mode="a", encoding="utf-8")
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)

# Хэндлер для вывода в консоль
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

# Конфигурация корневого логгера
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

logger = logging.getLogger("sanaq_ai")


# ──────────────────────────────────────────────
# Pydantic-схемы запроса и ответа для /chat
# ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    """Входящее сообщение от клиента."""
    business_id: int
    client_id: int
    message: Optional[str] = None
    image_url: Optional[str] = None
    audio_path: Optional[str] = None


class ChatResponse(BaseModel):
    """Ответ ИИ-менеджера."""
    reply: str
    language: str


# ──────────────────────────────────────────────
# Жизненный цикл приложения (Lifespan)
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения.
    При старте: создаёт все таблицы в базе данных.
    При завершении: закрывает соединения с БД и HTTP-клиенты.
    """
    # Startup
    init_db()
    logger.info("✅ База данных и миграции (shop_id) инициализированы в app.log.")
    logger.info("✅ Модель ИИ: %s | API: %s", settings.AI_MODEL, settings.OPENAI_API_BASE)

    if settings.OPENAI_API_KEY:
        logger.info("✅ OpenAI API-ключ настроен.")
    else:
        logger.warning("⚠️ OpenAI API-ключ НЕ настроен — ИИ-ответы будут недоступны!")

    yield

    # Shutdown
    await openai_client.close()
    engine.dispose()
    logger.info("🔌 Все соединения закрыты. Сервер остановлен.")


# ──────────────────────────────────────────────
# Инициализация приложения и Модули 2 & 4
# ──────────────────────────────────────────────

app = FastAPI(
    title="Sanaq AI",
    description=(
        "SaaS-платформа ИИ-менеджера для малого бизнеса. "
        "Интеграции: Telegram, Instagram, WhatsApp, ABCP/каталог."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# Модуль 4: Интеграция slowapi (Rate Limiter)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Модуль 2: Безопасная обработка ошибок (Глобальный перехватчик исклиний)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Глобальный обработчик всех необработанных исключений.
    Предотвращает падение сервера и возвращает клиенту понятный статус "Техническая пауза".
    Все детали ошибки сохраняются в app.log.
    """
    logger.error(
        "КРИТИЧЕСКАЯ ОШИБКА СЕРВЕРА [Path: %s %s]: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "detail": "Техническая пауза. Сервис временно недоступен, мы уже устраняем проблему.",
        },
    )

# CORS — разрешаем запросы из админ-панелей и фронтенда
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # В продакшене заменить на конкретные домены
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключаем роутеры
app.include_router(webhook_router)
app.include_router(admin_router)
app.include_router(omni_router)


# ──────────────────────────────────────────────
# Системные и стандартные эндпоинты
# ──────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health_check():
    """Проверка работоспособности сервиса."""
    return {
        "status": "ok",
        "service": "Sanaq AI",
        "version": "1.0.0",
        "model": settings.AI_MODEL,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/chat", response_model=ChatResponse, tags=["Chat"])
def chat_with_ai(request: ChatRequest, db: Session = Depends(get_db)):
    """Прямой эндпоинт общения с ИИ-менеджером."""
    target_shop_id = request.business_id
    business = db.query(Business).filter((Business.id == target_shop_id) | (Business.shop_id == target_shop_id)).first()
    if not business:
        raise HTTPException(status_code=404, detail="Бизнес не найден")

    client = db.query(Client).filter(
        Client.id == request.client_id,
        (Client.business_id == target_shop_id) | (Client.shop_id == target_shop_id),
    ).first()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    products_db = db.query(Product).filter(
        Product.shop_id == target_shop_id
    ).all()

    products_list = [
        {
            "id": p.id,
            "name": p.name,
            "price": p.price,
            "stock": p.stock,
            "description": p.description,
        }
        for p in products_db
    ]

    history_db = (
        db.query(ChatMessage)
        .filter(ChatMessage.shop_id == target_shop_id, ChatMessage.client_id == request.client_id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )

    chat_history = [
        {"role": msg.role, "message": msg.message}
        for msg in history_db
    ]

    try:
        full_user_message = prepare_incoming_message(
            message=request.message,
            image_url=request.image_url,
            audio_path=request.audio_path,
            api_key=business.api_key_ai,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not full_user_message:
        raise HTTPException(status_code=400, detail="Запрос должен содержать хотя бы один из параметров: message, image_url или audio_path.")

    language = detect_language(full_user_message)

    try:
        ai_reply = get_ai_response(
            user_message=full_user_message,
            business_name=business.name,
            products=products_list,
            chat_history=chat_history,
            api_key=business.api_key_ai,
            business_id=target_shop_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Сохранение истории (с изоляцией по shop_id)
    user_msg = ChatMessage(shop_id=target_shop_id, client_id=request.client_id, role="user", message=full_user_message)
    assistant_msg = ChatMessage(shop_id=target_shop_id, client_id=request.client_id, role="assistant", message=ai_reply)
    db.add(user_msg)
    db.add(assistant_msg)
    db.commit()

    return ChatResponse(reply=ai_reply, language=language)


@app.post("/orders", response_model=OrderResult, tags=["Orders"])
def api_create_order(request: OrderCreateRequest, db: Session = Depends(get_db)):
    """Эндпоинт прямого создания заказа вручную / из CRM."""
    items_dict = [item.model_dump() for item in request.items]
    result = create_order(
        db=db,
        business_id=request.business_id,
        phone_number=request.phone_number,
        items=items_dict,
        client_name=request.client_name,
    )
    if not result.success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=result.message,
        )
    return result


@app.get("/analytics/{business_id}", tags=["Analytics"])
def api_daily_analytics(
    business_id: int,
    target_date: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Эндпоинт дневной аналитики для REST API."""
    dt = None
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Формат даты: YYYY-MM-DD")
    return get_daily_analytics(db, business_id, dt)


@app.post("/analytics/{business_id}/send_report", tags=["Analytics"])
def api_send_daily_report(
    business_id: int,
    target_date: Optional[str] = None,
):
    """Отправляет дневной отчёт в Telegram-админ-бот."""
    dt = None
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Формат даты: YYYY-MM-DD")
    success = send_daily_report_to_admin(business_id, dt)
    return {
        "status": "success" if success else "error",
        "message": "Отчёт отправлен в Telegram" if success else "Не удалось отправить отчёт (проверьте TELEGRAM_BOT_TOKEN и ADMIN_ID)",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.DEBUG,
        workers=1,  # Для SQLite — строго 1 воркер
        log_level="debug" if settings.DEBUG else "info",
    )
