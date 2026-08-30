"""
webhook_router.py -- Единый роутер вебхуков для приема сообщений из мессенджеров.

Объединяет прием сообщений из разных каналов (WhatsApp, Telegram, Instagram)
в общую обработку с интеграцией модуля безопасности (Human-in-the-loop).

Логика эскалации:
  1. Если ИИ сомневается (маркеры неуверенности) или клиент агрессивен --
     проставляется флаг needs_human=True в таблице clients.
  2. Отправляется алерт в Telegram-админ-бот.
  3. Клиенту возвращается вежливый шаблон о подключении сотрудника.
  4. Все последующие сообщения клиента с needs_human=True маршрутизируются
     напрямую на менеджера (ИИ не отвечает).

Эндпоинт POST /webhook/{business_id}:
  1. Принимает входящее сообщение от клиента (телефон, имя, текст).
  2. Создаёт или находит клиента в БД.
  3. Проверяет флаг needs_human — если True, не вызывает ИИ.
  4. Сохраняет входящее сообщение в ChatMessage (role='user').
  5. Загружает список товаров бизнеса и историю сообщений.
  6. Генерирует ответ ИИ с помощью модуля ai_service.
  7. Проверяет ответ ИИ и сообщение клиента через safety.check_safety_escalation().
  8. Если эскалация — проставляет флаг в БД, шлёт алерт, отвечает шаблоном.
  9. Если ИИ определил намерение заказа — запускает пошаговое оформление.
  10. Сохраняет ответ ИИ в ChatMessage (role='assistant').
"""

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from database import get_db
from models import Business, Product, Client, ChatMessage, Order
from rate_limiter import limiter
from ai_service import (
    get_ai_response_with_intent,
    get_ai_response_for_business,
    fetch_business_catalog,
    detect_language,
    analyze_image_for_article,
    transcribe_voice,
    prepare_incoming_message,
)
from orders import (
    create_order,
    OrderResult,
    initiate_step_by_step_checkout,
    process_checkout_step,
    confirm_order_payment,
)
from safety import (
    check_safety_escalation,
    EscalationReason,
    get_escalation_reply,
    send_escalation_notification,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhook", tags=["Webhooks"])


# ──────────────────────────────────────────────
# Pydantic-схемы входящих и исходящих данных
# ──────────────────────────────────────────────

class WebhookIncomingMessage(BaseModel):
    """
    Строгая схема валидации входящего сообщения от вебхука мессенджера.

    Валидация:
      - phone_number: обязательно, минимум 5 символов, только цифры/+.
      - Хотя бы одно из полей message / image_url / audio_path должно быть заполнено.
      - channel: если указан, должен быть из разрешённых.
    """
    phone_number: str = Field(
        ...,
        min_length=5,
        max_length=30,
        description="Номер телефона клиента в международном формате или ID отправителя",
    )
    message: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Текст сообщения клиента",
    )
    client_name: Optional[str] = Field(
        default=None,
        max_length=255,
        description="Имя клиента (если передано мессенджером)",
    )
    image_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="URL изображения/скриншота",
    )
    audio_path: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Путь к аудиофайлу голосового сообщения",
    )
    channel: Optional[str] = Field(
        default=None,
        max_length=20,
        description="Канал связи: whatsapp / telegram / instagram / api",
    )

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v: str) -> str:
        """Проверка формата номера телефона / идентификатора."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Номер телефона / ID отправителя не может быть пустым.")
        return cleaned

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, v: Optional[str]) -> Optional[str]:
        """Проверка канала связи."""
        if v is None:
            return v
        allowed = {"whatsapp", "telegram", "instagram", "api"}
        v_lower = v.strip().lower()
        if v_lower not in allowed:
            raise ValueError(
                f"Недопустимый канал связи: '{v}'. "
                f"Допустимые: {', '.join(sorted(allowed))}"
            )
        return v_lower

    @field_validator("message")
    @classmethod
    def validate_message_not_blank(cls, v: Optional[str]) -> Optional[str]:
        """Отсекаем пустые строки / пробелы."""
        if v is not None:
            v = v.strip()
            if not v:
                return None
        return v

    @model_validator(mode="after")
    def at_least_one_content_field(self) -> "WebhookIncomingMessage":
        """Верификация: хотя бы одно поле содержимого обязательно."""
        if not self.message and not self.image_url and not self.audio_path:
            raise ValueError(
                "Входящее сообщение должно содержать хотя бы одно: "
                "message (текст), image_url (изображение) или audio_path (аудио)."
            )
        return self


class WebhookResponse(BaseModel):
    """Ответ клиенту / мессенджеру."""
    status: str = "success"
    business_id: int
    client_id: int
    channel: Optional[str] = None
    language: str
    reply: str
    order_created: bool = False
    order_details: Optional[OrderResult] = None
    escalated: bool = False
    escalation_reason: Optional[str] = None


# ──────────────────────────────────────────────
# Шаблон ответа для клиента, ожидающего менеджера
# ──────────────────────────────────────────────

_WAITING_FOR_HUMAN_REPLY = (
    "Ваш диалог передан живому менеджеру. "
    "Специалист свяжется с вами в самое ближайшее время! "
    "Спасибо за ожидание. 🙏"
)


# ──────────────────────────────────────────────
# Вспомогательная функция: проставить флаг эскалации
# ──────────────────────────────────────────────

def _mark_client_for_escalation(
    db: Session,
    client: Client,
    reason: EscalationReason,
) -> None:
    """Проставляет флаг needs_human=True в БД и сохраняет причину эскалации."""
    client.needs_human = True
    client.escalation_reason = reason.value
    client.escalated_at = datetime.now(timezone.utc)
    db.commit()
    logger.warning(
        "Клиент #%d (%s) помечен для эскалации: reason=%s",
        client.id, client.phone_number, reason.value,
    )


def _reset_client_escalation(db: Session, client: Client) -> None:
    """Сбрасывает флаг needs_human (вызывается менеджером, когда он закрыл тикет)."""
    client.needs_human = False
    client.escalation_reason = None
    client.escalated_at = None
    db.commit()
    logger.info("Эскалация клиента #%d (%s) сброшена.", client.id, client.phone_number)


# ──────────────────────────────────────────────
# Единая функция обработки сообщений из любого канала
# ──────────────────────────────────────────────

def process_unified_message(
    db: Session,
    business_id: int,
    payload: WebhookIncomingMessage,
) -> WebhookResponse:
    """
    Единая точка обработки входящих сообщений из любого канала.

    Вызывается напрямую из POST /webhook/{business_id} и из
    omnichannel.py → POST /omni/{business_id}/{channel}.
    """
    channel = payload.channel or "api"
    shop_id = business_id

    # 1. Подгружаем информацию о бизнесе и динамический каталог товаров
    try:
        business_name, products_list, api_key_ai = fetch_business_catalog(db, business_id, shop_id=shop_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    # 2. Поиск или создание клиента (строгая фильтрация по shop_id)
    client = (
        db.query(Client)
        .filter(
            Client.shop_id == shop_id,
            Client.phone_number == payload.phone_number,
        )
        .first()
    )

    if not client:
        client = Client(
            shop_id=shop_id,
            business_id=business_id,
            phone_number=payload.phone_number,
            name=payload.client_name or "Клиент",
            channel=channel,
        )
        db.add(client)
        db.commit()
        db.refresh(client)
    else:
        # Обновляем имя, если пришло более конкретное
        if payload.client_name and (not client.name or client.name == "Клиент"):
            client.name = payload.client_name
        # Обновляем канал связи
        if channel and channel != "api":
            client.channel = channel
        if not client.shop_id:
            client.shop_id = shop_id
        db.commit()

    # 3. Проверяем: клиент уже отмечен для передачи менеджеру?
    if client.needs_human:
        logger.info(
            "Клиент #%d (%s, shop_id=%d) уже эскалирован (reason=%s). Сообщение переадресовано менеджеру.",
            client.id, client.phone_number, shop_id, client.escalation_reason,
        )
        # Сохраняем входящее сообщение в историю (для менеджера)
        if payload.message:
            user_msg = ChatMessage(
                shop_id=shop_id,
                client_id=client.id,
                role="user",
                message=payload.message,
            )
            db.add(user_msg)
            db.commit()

        # Повторно уведомляем админа о новом сообщении от эскалированного клиента
        send_escalation_notification(
            reason=EscalationReason(client.escalation_reason) if client.escalation_reason else EscalationReason.EXPLICIT_REQUEST,
            trigger_detail=f"Повторное сообщение от клиента, ожидающего менеджера",
            client_phone=client.phone_number,
            client_name=client.name or "Клиент",
            business_name=business_name,
            business_id=business_id,
            user_message=payload.message or "[медиа-сообщение]",
        )

        return WebhookResponse(
            status="success",
            business_id=business_id,
            client_id=client.id,
            channel=channel,
            language="ru",
            reply=_WAITING_FOR_HUMAN_REPLY,
            escalated=True,
            escalation_reason=client.escalation_reason,
        )

    # 4. Обработка входящих данных (текст, голосовое сообщение Whisper, скриншот Vision)
    try:
        full_user_message = prepare_incoming_message(
            message=payload.message,
            image_url=payload.image_url,
            audio_path=payload.audio_path,
            api_key=api_key_ai,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as e:
        logger.error("Ошибка обработки мультимедиа бизнеса %d: %s", business_id, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ошибка обработки медиа-сообщения: {e}",
        )

    if not full_user_message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Входящий запрос должен содержать хотя бы один из параметров: message, image_url или audio_path.",
        )

    # 5. Сохраняем обработанное сообщение клиента в историю
    user_msg = ChatMessage(
        shop_id=shop_id,
        client_id=client.id,
        role="user",
        message=full_user_message,
    )
    db.add(user_msg)
    db.commit()

    language = detect_language(full_user_message)

    # 6. ПРОВЕРКА: Находится ли клиент в процессе пошагового оформления заказа?
    active_order = (
        db.query(Order)
        .filter(
            Order.shop_id == shop_id,
            Order.client_id == client.id,
            Order.status == "pending_checkout",
            Order.checkout_step != "completed",
        )
        .order_by(Order.created_at.desc())
        .first()
    )

    if active_order:
        logger.info(
            "Клиент %s продолжает пошаговое оформление заказа #%d: step=%s",
            client.phone_number,
            active_order.id,
            active_order.checkout_step,
        )
        checkout_reply = process_checkout_step(db, active_order, full_user_message)

        # Сохраняем ответ в историю сообщений
        assistant_msg = ChatMessage(
            shop_id=shop_id,
            client_id=client.id,
            role="assistant",
            message=checkout_reply,
        )
        db.add(assistant_msg)
        db.commit()

        order_res = OrderResult(
            success=True,
            message=f"Заказ #{active_order.id} на этапе '{active_order.checkout_step}'",
            order_id=active_order.id,
            total_price=active_order.total_price,
            payment_link=active_order.payment_link,
        )

        return WebhookResponse(
            status="success",
            business_id=business_id,
            client_id=client.id,
            channel=channel,
            language=language,
            reply=checkout_reply,
            order_created=True,
            order_details=order_res,
        )

    # 7. Загружаем историю переписки и вызываем ИИ
    history_db = (
        db.query(ChatMessage)
        .filter(ChatMessage.shop_id == shop_id, ChatMessage.client_id == client.id)
        .order_by(ChatMessage.timestamp.asc())
        .all()
    )

    chat_history: List[Dict[str, Any]] = [
        {"role": msg.role, "message": msg.message}
        for msg in history_db
    ]

    try:
        ai_reply, order_items = get_ai_response_with_intent(
            user_message=full_user_message,
            business_name=business_name,
            products=products_list,
            chat_history=chat_history,
            api_key=api_key_ai,
            business_id=business_id,
        )
    except ValueError as e:
        logger.error("Ошибка конфигурации API бизнеса %d: %s", business_id, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except RuntimeError as e:
        logger.error("Ошибка ИИ-сервиса бизнеса %d: %s", business_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )

    # ─────────────────────────────────────────
    # 8. ПРОВЕРКА БЕЗОПАСНОСТИ (Human-in-the-loop)
    # ─────────────────────────────────────────
    escalated, safe_reply = check_safety_escalation(
        user_message=full_user_message,
        ai_response=ai_reply,
        client_phone=client.phone_number,
        client_name=client.name or "Клиент",
        business_name=business_name,
        business_id=business_id,
        language=language,
    )

    escalation_reason_str: Optional[str] = None

    if escalated:
        from safety import detect_escalation_trigger
        reason, trigger_detail = detect_escalation_trigger(full_user_message, ai_reply)

        _mark_client_for_escalation(db, client, reason)
        escalation_reason_str = reason.value
        ai_reply = safe_reply

        logger.warning(
            "ЭСКАЛАЦИЯ: Клиент #%d (%s), бизнес '%s' #%d | Причина: %s",
            client.id, client.phone_number, business_name, business_id, reason.value,
        )

    # 9. Автоматический запуск пошагового оформления заказа при распознании намерений
    order_result: Optional[OrderResult] = None
    order_created = False

    if order_items and not escalated:
        logger.info(
            "Распознано намерение покупки для клиента %s: %s",
            client.phone_number,
            order_items,
        )
        new_order, prompt_msg = initiate_step_by_step_checkout(
            db=db,
            business_id=business_id,
            phone_number=client.phone_number,
            items=order_items,
            client_name=client.name,
            shop_id=shop_id,
        )

        if new_order:
            order_created = True
            ai_reply += f"\n\n{prompt_msg}"
            order_result = OrderResult(
                success=True,
                message=f"Начато пошаговое оформление заказа #{new_order.id}.",
                order_id=new_order.id,
                total_price=new_order.total_price,
                payment_link=new_order.payment_link,
            )
        else:
            ai_reply += f"\n\n[Не удалось начать пошаговое оформление заказа: {prompt_msg}]"

    # 10. Сохраняем ответ ассистента в историю
    assistant_msg = ChatMessage(
        shop_id=shop_id,
        client_id=client.id,
        role="assistant",
        message=ai_reply,
    )
    db.add(assistant_msg)
    db.commit()

    return WebhookResponse(
        status="success",
        business_id=business_id,
        client_id=client.id,
        channel=channel,
        language=language,
        reply=ai_reply,
        order_created=order_created,
        order_details=order_result,
        escalated=escalated,
        escalation_reason=escalation_reason_str,
    )


# ──────────────────────────────────────────────
# Эндпоинты вебхуков
# ──────────────────────────────────────────────

@router.post("/{business_id}", response_model=WebhookResponse)
@limiter.limit("10/minute")
def handle_incoming_webhook(
    request: Request,
    business_id: int,
    payload: WebhookIncomingMessage,
    db: Session = Depends(get_db),
):
    """
    Универсальный вебхук приема сообщений клиентов конкретного бизнеса.
    Защищен ограничением частоты запросов slowapi (10 запросов в минуту).
    """
    return process_unified_message(db=db, business_id=business_id, payload=payload)


@router.post("/{business_id}/orders/{order_id}/confirm_payment", response_model=OrderResult)
@limiter.limit("10/minute")
def webhook_confirm_payment(
    request: Request,
    business_id: int,
    order_id: int,
    db: Session = Depends(get_db),
):
    """
    Эндпоинт верификации / вебхука оплаты (Kaspi Pay / Эквайринг).
    Переводит заказ в статус 'paid', уменьшает остаток товара и отправляет уведомление в Telegram-админ-бот.
    """
    return confirm_order_payment(db=db, order_id=order_id, shop_id=business_id)


@router.post("/{business_id}/resolve/{client_id}")
def resolve_escalation(
    business_id: int,
    client_id: int,
    db: Session = Depends(get_db),
):
    """
    Сброс флага эскалации (вызывается менеджером после ручной обработки).
    После вызова ИИ снова может отвечать данному клиенту.
    """
    client = (
        db.query(Client)
        .filter(Client.id == client_id, (Client.business_id == business_id) | (Client.shop_id == business_id))
        .first()
    )
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден.")

    _reset_client_escalation(db, client)

    return {
        "status": "success",
        "message": f"Эскалация клиента #{client.id} ({client.phone_number}) снята. ИИ снова активен.",
    }
