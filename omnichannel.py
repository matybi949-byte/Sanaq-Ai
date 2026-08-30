"""
omnichannel.py -- Универсальный омниканальный роутер.

Принимает входящие вебхуки из WhatsApp (Cloud API / Business API),
Telegram Bot API и Instagram Direct (Graph API) и нормализует их
в единую внутреннюю структуру UnifiedMessage для дальнейшей обработки
через webhook_router.process_unified_message().

Архитектура:
  Мессенджер (WhatsApp/Telegram/Instagram)
        │
        ▼
  POST /omni/{business_id}/{channel}
        │
        ▼
  parse_{channel}_payload() → UnifiedMessage
        │
        ▼
  process_unified_message() → WebhookResponse
        │
        ▼
  Ответ отправляется обратно в соответствующий канал
"""

import logging
from typing import Optional, Dict, Any, List
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from rate_limiter import limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omni", tags=["Omnichannel"])


# ──────────────────────────────────────────────
# Enum каналов и унифицированная структура
# ──────────────────────────────────────────────

class Channel(str, Enum):
    """Поддерживаемые мессенджер-каналы."""
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    INSTAGRAM = "instagram"


class UnifiedMessage(BaseModel):
    """
    Унифицированная структура входящего сообщения.
    Любой канал (WhatsApp, Telegram, Instagram) нормализуется в этот формат.
    """
    channel: Channel = Field(..., description="Канал входящего сообщения")
    phone_number: str = Field(..., description="Уникальный идентификатор клиента (телефон или chat_id)")
    client_name: Optional[str] = Field(default=None, description="Имя клиента (если доступно)")
    message: Optional[str] = Field(default=None, description="Текстовое сообщение")
    image_url: Optional[str] = Field(default=None, description="URL изображения")
    audio_path: Optional[str] = Field(default=None, description="Путь / URL аудиофайла")
    raw_payload: Optional[Dict[str, Any]] = Field(default=None, description="Оригинальный payload мессенджера")


class OmniResponse(BaseModel):
    """Ответ омниканального роутера."""
    status: str = "success"
    channel: str
    business_id: int
    client_id: Optional[int] = None
    reply: str
    order_created: bool = False
    escalated: bool = False
    escalation_reason: Optional[str] = None


# ──────────────────────────────────────────────
# Парсеры входящих данных по каналам
# ──────────────────────────────────────────────

def parse_whatsapp_payload(body: Dict[str, Any]) -> Optional[UnifiedMessage]:
    """
    Парсит вебхук WhatsApp Cloud API (Meta Graph API).

    Ожидаемая структура:
    {
      "entry": [{
        "changes": [{
          "value": {
            "messages": [{
              "from": "77071112233",
              "type": "text",
              "text": {"body": "Привет!"}
            }],
            "contacts": [{"profile": {"name": "Клиент"}}]
          }
        }]
      }]
    }
    """
    try:
        entry = body.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return None

        msg = messages[0]
        phone = msg.get("from", "")
        msg_type = msg.get("type", "text")

        text = None
        image_url = None
        audio_path = None

        if msg_type == "text":
            text = msg.get("text", {}).get("body", "")
        elif msg_type == "image":
            image_url = msg.get("image", {}).get("url") or msg.get("image", {}).get("id", "")
        elif msg_type == "audio":
            audio_path = msg.get("audio", {}).get("url") or msg.get("audio", {}).get("id", "")

        contacts = value.get("contacts", [])
        client_name = None
        if contacts:
            client_name = contacts[0].get("profile", {}).get("name")

        return UnifiedMessage(
            channel=Channel.WHATSAPP,
            phone_number=phone,
            client_name=client_name,
            message=text,
            image_url=image_url,
            audio_path=audio_path,
            raw_payload=body,
        )
    except Exception as e:
        logger.error("Ошибка парсинга WhatsApp payload: %s", e)
        return None


def parse_telegram_payload(body: Dict[str, Any]) -> Optional[UnifiedMessage]:
    """
    Парсит вебхук Telegram Bot API.

    Ожидаемая структура:
    {
      "message": {
        "from": {"id": 123456, "first_name": "Иван", "last_name": "Петров"},
        "text": "Привет!"
      }
    }
    """
    try:
        message = body.get("message")
        if not message:
            return None

        from_user = message.get("from", {})
        chat_id = str(from_user.get("id", message.get("chat", {}).get("id", "")))
        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        client_name = f"{first_name} {last_name}".strip() or None

        text = message.get("text")
        image_url = None
        audio_path = None

        # Обработка фото (берём самое большое разрешение)
        if message.get("photo"):
            photos = message["photo"]
            image_url = photos[-1].get("file_id", "") if photos else None

        # Обработка голосового сообщения
        voice = message.get("voice") or message.get("audio")
        if voice:
            audio_path = voice.get("file_id", "")

        return UnifiedMessage(
            channel=Channel.TELEGRAM,
            phone_number=chat_id,
            client_name=client_name,
            message=text,
            image_url=image_url,
            audio_path=audio_path,
            raw_payload=body,
        )
    except Exception as e:
        logger.error("Ошибка парсинга Telegram payload: %s", e)
        return None


def parse_instagram_payload(body: Dict[str, Any]) -> Optional[UnifiedMessage]:
    """
    Парсит вебхук Instagram Direct Messages (Meta Graph API).

    Ожидаемая структура:
    {
      "entry": [{
        "messaging": [{
          "sender": {"id": "INSTAGRAM_SCOPED_ID"},
          "message": {"text": "Привет!", "attachments": [...]}
        }]
      }]
    }
    """
    try:
        entry = body.get("entry", [])
        if not entry:
            return None

        messaging = entry[0].get("messaging", [])
        if not messaging:
            return None

        event = messaging[0]
        sender_id = event.get("sender", {}).get("id", "")
        msg_data = event.get("message", {})

        text = msg_data.get("text")
        image_url = None
        audio_path = None

        attachments = msg_data.get("attachments", [])
        for att in attachments:
            att_type = att.get("type", "")
            payload_url = att.get("payload", {}).get("url", "")
            if att_type == "image" and payload_url:
                image_url = payload_url
            elif att_type in ("audio", "voice") and payload_url:
                audio_path = payload_url

        return UnifiedMessage(
            channel=Channel.INSTAGRAM,
            phone_number=sender_id,
            client_name=None,  # Instagram DM не передает имя напрямую в webhook
            message=text,
            image_url=image_url,
            audio_path=audio_path,
            raw_payload=body,
        )
    except Exception as e:
        logger.error("Ошибка парсинга Instagram payload: %s", e)
        return None


# ──────────────────────────────────────────────
# Маппинг каналов на парсеры
# ──────────────────────────────────────────────

_CHANNEL_PARSERS = {
    Channel.WHATSAPP: parse_whatsapp_payload,
    Channel.TELEGRAM: parse_telegram_payload,
    Channel.INSTAGRAM: parse_instagram_payload,
}


def normalize_incoming(channel: Channel, raw_body: Dict[str, Any]) -> Optional[UnifiedMessage]:
    """
    Нормализует сырой payload из любого мессенджера в UnifiedMessage.

    Args:
        channel:  Канал источника (whatsapp, telegram, instagram).
        raw_body: Сырой JSON-payload из вебхука.

    Returns:
        UnifiedMessage или None, если парсинг не удался.
    """
    parser = _CHANNEL_PARSERS.get(channel)
    if not parser:
        logger.error("Неподдерживаемый канал: %s", channel)
        return None
    return parser(raw_body)


# ──────────────────────────────────────────────
# Омниканальный FastAPI-эндпоинт
# ──────────────────────────────────────────────

@router.post("/{business_id}/{channel}", response_model=OmniResponse)
@limiter.limit("10/minute")
async def omnichannel_webhook(
    business_id: int,
    channel: Channel,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Универсальный омниканальный вебхук.

    Принимает входящие данные из WhatsApp, Telegram или Instagram Direct,
    нормализует их в единую структуру и передаёт на обработку через
    единую функцию process_unified_message() из webhook_router.

    URL: POST /omni/{business_id}/{whatsapp|telegram|instagram}
    """
    try:
        raw_body = await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Невалидный JSON в теле запроса.",
        )

    logger.info("Омниканальный вебхук: канал=%s, бизнес=%d", channel.value, business_id)

    # Нормализация payload в UnifiedMessage
    unified = normalize_incoming(channel, raw_body)
    if not unified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Не удалось распознать структуру входящего сообщения канала '{channel.value}'.",
        )

    if not unified.message and not unified.image_url and not unified.audio_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Входящее сообщение не содержит текста, изображения или аудио.",
        )

    # Вызываем единую функцию обработки из webhook_router
    from webhook_router import process_unified_message, WebhookIncomingMessage

    webhook_payload = WebhookIncomingMessage(
        phone_number=unified.phone_number,
        message=unified.message,
        client_name=unified.client_name,
        image_url=unified.image_url,
        audio_path=unified.audio_path,
        channel=channel.value,
    )

    result = process_unified_message(
        db=db,
        business_id=business_id,
        payload=webhook_payload,
    )

    return OmniResponse(
        status="success",
        channel=channel.value,
        business_id=business_id,
        client_id=result.client_id,
        reply=result.reply,
        order_created=result.order_created,
        escalated=result.escalated,
        escalation_reason=result.escalation_reason,
    )


# ──────────────────────────────────────────────
# Верификационный эндпоинт (WhatsApp / Instagram)
# ──────────────────────────────────────────────

@router.get("/{business_id}/{channel}")
async def omnichannel_verify(
    business_id: int,
    channel: Channel,
    request: Request,
):
    """
    Верификационный GET-запрос для WhatsApp Cloud API и Instagram Webhooks.
    Meta отправляет GET с параметрами hub.mode, hub.verify_token, hub.challenge.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    import os
    expected_token = os.getenv("WEBHOOK_VERIFY_TOKEN", "sanaq_ai_verify_token")

    if mode == "subscribe" and token == expected_token:
        logger.info("Верификация вебхука %s для бизнеса %d: УСПЕХ", channel.value, business_id)
        return int(challenge) if challenge else "OK"

    logger.warning("Неуспешная верификация вебхука %s: mode=%s, token=%s", channel.value, mode, token)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")
