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

from json import JSONDecodeError
from fastapi import FastAPI, Depends, HTTPException, Request, status, BackgroundTasks
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import settings
from database import engine, Base, get_db, init_db
from models import (
    Business,
    Product,
    Client,
    Order,
    ChatMessage,
    WebhookPayloadValidation,
    OrderDataValidation,
    OrderItemValidation,
)
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
from messengers_router import router as messengers_router, register_telegram_webhook
from analytics import get_daily_analytics, format_daily_report, send_daily_report_to_admin
from openai_service import openai_client
from error_notifier import send_critical_error, async_send_critical_error, TelegramErrorLoggingHandler
from dashboard import router as dashboard_router
from db_backup import send_db_backup_to_telegram, create_db_backup
from heartbeat import async_send_uptime_heartbeat, send_uptime_heartbeat
from kaspi_payment import generate_kaspi_pay_link, process_kaspi_webhook_payment

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

# Хэндлер для отправки критических ошибок в Telegram-канал мониторинга
telegram_error_handler = TelegramErrorLoggingHandler(level=logging.ERROR)
telegram_error_handler.setFormatter(log_formatter)

# Конфигурация корневого логгера
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)
root_logger.addHandler(telegram_error_handler)

logger = logging.getLogger("sanaq_ai")


# ──────────────────────────────────────────────
# Pydantic-схемы запроса и ответа для /chat
# ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    """
    Строгая схема входящего сообщения от клиента (/chat).

    Валидация:
      - business_id: целое число > 0.
      - client_id: целое число > 0.
      - Хотя бы одно из полей message / image_url / audio_path обязательно.
    """
    business_id: int = Field(..., ge=1, description="ID бизнеса (целое > 0)")
    client_id: int = Field(..., ge=1, description="ID клиента (целое > 0)")
    message: Optional[str] = Field(default=None, max_length=10000, description="Текст сообщения")
    image_url: Optional[str] = Field(default=None, max_length=2048, description="URL изображения")
    audio_path: Optional[str] = Field(default=None, max_length=2048, description="Путь к аудиофайлу")

    @model_validator(mode="after")
    def at_least_one_content_field(self) -> "ChatRequest":
        """Хотя бы одно поле контента обязательно."""
        if not self.message and not self.image_url and not self.audio_path:
            raise ValueError(
                "Запрос должен содержать хотя бы одно: "
                "message, image_url или audio_path."
            )
        return self


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

    # Автоматическая регистрация Telegram setWebhook при старте приложения
    try:
        await register_telegram_webhook()
    except Exception as tg_err:
        logger.error("Ошибка при авто-регистрации Telegram setWebhook: %s", tg_err)

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


# ──────────────────────────────────────────────
# Автоматические обработчики 422 (Битый JSON и ошибки Pydantic)
# ──────────────────────────────────────────────

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Автоматический перехват ошибок валидации Pydantic и возврат статуса 422 Unprocessable Entity.
    """
    logger.warning(
        "Ошибка валидации входящего JSON [HTTP 422] (%s %s): %s",
        request.method, request.url.path, exc,
    )
    raw_errors = exc.errors()
    safe_errors = []
    for err in raw_errors:
        err_copy = dict(err)
        if "ctx" in err_copy and isinstance(err_copy["ctx"], dict):
            err_copy["ctx"] = {
                k: str(v) if isinstance(v, Exception) else v
                for k, v in err_copy["ctx"].items()
            }
        safe_errors.append(err_copy)

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "status": "error",
            "detail": "Некорректный формат данных или невалидный JSON (Validation Error).",
            "errors": safe_errors,
        },
    )


@app.exception_handler(JSONDecodeError)
async def json_decode_exception_handler(request: Request, exc: JSONDecodeError):
    """
    Автоматический перехват синтаксически битого JSON в теле запроса и возврат 422 Unprocessable Entity.
    """
    logger.warning(
        "Битый JSON в теле запроса [HTTP 422] (%s %s): %s",
        request.method, request.url.path, exc,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "status": "error",
            "detail": "Битый или некорректный JSON в теле запроса (JSONDecodeError).",
        },
    )


# Модуль 2: Безопасная обработка ошибок (Глобальный перехватчик исключений)
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
    # Автоматическая отправка текста ошибки в Telegram-канал мониторинга
    await async_send_critical_error(
        error=exc,
        context=f"FastAPI [{request.method} {request.url.path}]",
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

# ──────────────────────────────────────────────
# Эндпоинты Мониторинга, Бэкапов и Kaspi Pay
# ──────────────────────────────────────────────

@app.post("/admin/backup", tags=["System"])
def api_trigger_db_backup(background_tasks: BackgroundTasks):
    """Принудительно создает бэкап БД и отправляет его в Telegram в фоновом режиме."""
    background_tasks.add_task(send_db_backup_to_telegram)
    return {"status": "success", "message": "Бэкап базы данных запущен и будет отправлен в Telegram."}


@app.post("/health/heartbeat", tags=["System"])
async def api_trigger_uptime_heartbeat(background_tasks: BackgroundTasks):
    """Отправляет актуальный статус Uptime Heartbeat в канал мониторинга."""
    background_tasks.add_task(async_send_uptime_heartbeat)
    return {"status": "success", "message": "Отправка Heartbeat статуса запущена."}


@app.get("/payments/kaspi/{order_id}", tags=["Payments"])
def api_get_kaspi_pay_details(order_id: int, db: Session = Depends(get_db)):
    """Получает данные Kaspi QR / ссылки на оплату для заказа."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail=f"Заказ #{order_id} не найден")
    return generate_kaspi_pay_link(order_id=order.id, amount=order.total_price, shop_id=order.shop_id or 1)


class KaspiWebhookPayload(BaseModel):
    txn_id: str = Field(..., description="ID транзакции Kaspi")
    order_id: int = Field(..., description="ID заказа")
    amount: float = Field(..., description="Сумма оплаты")


@app.post("/webhook/kaspi", tags=["Payments"])
@app.post("/payments/kaspi/webhook", tags=["Payments"])
def api_kaspi_pay_webhook(payload: KaspiWebhookPayload, db: Session = Depends(get_db)):
    """Принимает автоматически уведомления об успешной оплате от Kaspi Pay API."""
    success, msg = process_kaspi_webhook_payment(
        db=db,
        txn_id=payload.txn_id,
        order_id=payload.order_id,
        amount=payload.amount,
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}


# Подключаем роутеры (конкретные пути messengers_router должны быть раньше параметризованного webhook_router)
app.include_router(dashboard_router)
app.include_router(messengers_router)
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
    background_tasks: BackgroundTasks,
    target_date: Optional[str] = None,
):
    """Отправляет дневной отчёт в Telegram-админ-бот в фоновом режиме."""
    dt = None
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Формат даты: YYYY-MM-DD")
    background_tasks.add_task(send_daily_report_to_admin, business_id, dt)
    return {
        "status": "success",
        "message": "Отправка отчета запущена в фоновом режиме (BackgroundTasks).",
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
