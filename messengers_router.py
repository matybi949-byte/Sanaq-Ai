"""
messengers_router.py -- Единый модуль интеграции мессенджеров (WhatsApp & Instagram Direct).

Объединяет:
1. Вебхук WhatsApp (/webhook/whatsapp) -- прием сообщений, извлечение телефона и текста.
2. Вебхук Instagram (/webhook/instagram) -- верификация Meta токена (GET) и прием сообщений (POST).
3. Единый обработчик ИИ -- маршрутизация сообщений в общий ИИ-модуль с учетом каталога и истории.
4. Универсальная отправка -- функция отправки ответов в WhatsApp (через API шлюза) и Instagram (через Graph API Meta).
"""

import os
import logging
from typing import Optional, Dict, Any, Tuple, List
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from rate_limiter import limiter
from webhook_router import process_unified_message, WebhookIncomingMessage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Messengers Integration"])



# ──────────────────────────────────────────────
# Pydantic Схемы
# ──────────────────────────────────────────────

class IncomingMessageRequest(BaseModel):
    """Схема для прямых POST-запросов входящих сообщений (если payload не в формате Meta)."""
    phone_number: Optional[str] = Field(default=None, description="Номер телефона отправителя (WhatsApp) или sender_id (Instagram)")
    sender_id: Optional[str] = Field(default=None, description="ID отправителя в Instagram Direct")
    text: Optional[str] = Field(default=None, description="Текст входящего сообщения")
    message: Optional[str] = Field(default=None, description="Альтернативное поле текста сообщения")
    image_url: Optional[str] = Field(default=None, description="URL прикрепленного изображения")
    audio_path: Optional[str] = Field(default=None, description="Путь/URL голосового сообщения")


class MessengerWebhookResponse(BaseModel):
    """Единая схема ответа вебхука мессенджеров."""
    status: str = "success"
    channel: str
    business_id: int
    sender_id: str
    reply: str
    sent_to_client: bool
    order_created: bool = False
    escalated: bool = False


# ──────────────────────────────────────────────
# 4. Универсальная функция отправки сообщений
# ──────────────────────────────────────────────

async def send_reply_to_messenger(
    channel: str,
    recipient_id: str,
    text: str,
    business_id: Optional[int] = None,
) -> bool:
    """
    Универсальная отправка ответа обратно клиенту в мессенджер:
    - WhatsApp: через API шлюза / bridge.
    - Instagram Direct: через Meta Graph API.

    Args:
        channel: 'whatsapp' или 'instagram'.
        recipient_id: Номер телефона (WhatsApp) или sender_id (Instagram).
        text: Текст ответа для клиента.
        business_id: ID бизнеса (опционально).

    Returns:
        bool: True, если сообщение успешно отправлено, иначе False.
    """
    ch = channel.lower()
    
    if ch == "whatsapp":
        bridge_url = settings.WHATSAPP_BRIDGE_URL or "http://localhost:3000/send-message"
        headers = {"Content-Type": "application/json"}
        if settings.WHATSAPP_BRIDGE_TOKEN:
            headers["Authorization"] = f"Bearer {settings.WHATSAPP_BRIDGE_TOKEN}"

        payload = {
            "phone": recipient_id,
            "message": text,
            "business_id": business_id or settings.DEFAULT_BUSINESS_ID,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(bridge_url, json=payload, headers=headers)
                if res.status_code in (200, 201):
                    logger.info("WhatsApp сообщение успешно отправлено на %s", recipient_id)
                    return True
                else:
                    logger.error("Ошибка отправки WhatsApp (%d): %s", res.status_code, res.text)
                    return False
        except Exception as e:
            logger.error("Ошибка подключения к WhatsApp шлюзу (%s): %s", bridge_url, e)
            return False

    elif ch == "instagram":
        access_token = settings.INSTAGRAM_PAGE_ACCESS_TOKEN
        if not access_token:
            logger.warning("INSTAGRAM_PAGE_ACCESS_TOKEN не настроен в .env! Пропуск отправки через Graph API.")
            return False

        graph_url = f"https://graph.facebook.com/v18.0/me/messages?access_token={access_token}"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
            "messaging_type": "RESPONSE",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(graph_url, json=payload)
                if res.status_code in (200, 201):
                    logger.info("Instagram DM успешно отправлено на sender_id=%s", recipient_id)
                    return True
                else:
                    logger.error("Ошибка Meta Graph API (%d): %s", res.status_code, res.text)
                    return False
        except Exception as e:
            logger.error("Ошибка подключения к Meta Graph API: %s", e)
            return False

    elif ch == "telegram":
        bot_token = (settings.TELEGRAM_BOT_TOKEN or settings.BOT_TOKEN or "").strip()
        if not bot_token:
            logger.warning("TELEGRAM_BOT_TOKEN не настроен в .env! Пропуск отправки в Telegram.")
            return False

        tg_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": recipient_id,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(tg_url, json=payload)
                if res.status_code == 200:
                    logger.info("Telegram сообщение успешно отправлено на chat_id=%s", recipient_id)
                    return True
                else:
                    # Запасная попытка без HTML разметки при ошибке форматирования тегов
                    payload.pop("parse_mode", None)
                    res_retry = await client.post(tg_url, json=payload)
                    if res_retry.status_code == 200:
                        logger.info("Telegram сообщение отправлено без HTML на chat_id=%s", recipient_id)
                        return True
                    logger.error("Ошибка Telegram Bot API (%d): %s", res_retry.status_code, res_retry.text)
                    return False
        except Exception as e:
            logger.error("Ошибка подключения к Telegram Bot API (chat_id=%s): %s", recipient_id, e)
            return False

    else:
        logger.warning("Неизвестный канал для отправки: %s", channel)
        return False



# ──────────────────────────────────────────────
# Парсеры payloads для WhatsApp и Instagram
# ──────────────────────────────────────────────

def parse_whatsapp_payload(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Извлекает (phone_number, text, image_url, audio_path) из WhatsApp payload.
    Поддерживает варианты:
    1. Direct Gateway API: {"phone": "...", "text": "..."} / {"phone_number": "...", "message": "..."}
    2. Meta WhatsApp Cloud API: entry[0].changes[0].value.messages[0]
    """
    # Direct Gateway payload
    phone = body.get("phone") or body.get("phone_number") or body.get("from") or body.get("sender")
    text = body.get("text") or body.get("message") or body.get("body")
    image_url = body.get("image_url")
    audio_path = body.get("audio_path")

    if phone and (text or image_url or audio_path):
        return str(phone), text, image_url, audio_path

    # Meta Cloud API payload
    try:
        entry = body.get("entry", [])
        if entry:
            changes = entry[0].get("changes", [])
            if changes:
                value = changes[0].get("value", {})
                messages = value.get("messages", [])
                if messages:
                    msg = messages[0]
                    phone_val = str(msg.get("from", ""))
                    msg_type = msg.get("type", "text")
                    text_val = None
                    img_val = None
                    audio_val = None

                    if msg_type == "text":
                        text_val = msg.get("text", {}).get("body")
                    elif msg_type == "image":
                        img_val = msg.get("image", {}).get("url") or msg.get("image", {}).get("id")
                    elif msg_type == "audio":
                        audio_val = msg.get("audio", {}).get("url") or msg.get("audio", {}).get("id")

                    return phone_val, text_val, img_val, audio_val
    except Exception as e:
        logger.error("Ошибка разбора Meta WhatsApp Cloud API payload: %s", e)

    return phone, text, image_url, audio_path


def parse_instagram_payload(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Извлекает (sender_id, text, image_url, audio_path) из Instagram Direct payload.
    Поддерживает варианты:
    1. Direct API payload: {"sender_id": "...", "text": "..."}
    2. Meta Graph API Webhook: entry[0].messaging[0].sender.id & entry[0].messaging[0].message
    """
    sender_id = body.get("sender_id") or body.get("sender") or body.get("from")
    text = body.get("text") or body.get("message")
    image_url = body.get("image_url")
    audio_path = body.get("audio_path")

    if sender_id and (text or image_url or audio_path):
        return str(sender_id), text, image_url, audio_path

    try:
        entry = body.get("entry", [])
        if entry:
            messaging = entry[0].get("messaging", [])
            if messaging:
                event = messaging[0]
                sender_val = str(event.get("sender", {}).get("id", ""))
                msg_data = event.get("message", {})
                text_val = msg_data.get("text")
                img_val = None
                audio_val = None

                attachments = msg_data.get("attachments", [])
                for att in attachments:
                    att_type = att.get("type", "")
                    payload_url = att.get("payload", {}).get("url", "")
                    if att_type == "image" and payload_url:
                        img_val = payload_url
                    elif att_type in ("audio", "voice") and payload_url:
                        audio_val = payload_url

                return sender_val, text_val, img_val, audio_val
    except Exception as e:
        logger.error("Ошибка разбора Instagram Direct payload: %s", e)

    return sender_id, text, image_url, audio_path


async def download_telegram_file(file_id: str, bot_token: str) -> Optional[str]:
    """
    Загружает файл (голосовое сообщение или фото) с серверов Telegram API по file_id
    и сохраняет во временную папку temp_media.

    Returns:
        Optional[str]: Путь к локально сохранённому файлу или None.
    """
    if not file_id or not bot_token:
        return None

    get_file_url = f"https://api.telegram.org/bot{bot_token}/getFile"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(get_file_url, params={"file_id": file_id})
            if res.status_code != 200:
                logger.error("Telegram getFile вернул ошибку (%d): %s", res.status_code, res.text)
                return None

            data = res.json()
            if not data.get("ok"):
                logger.error("Telegram getFile статус ok=False: %s", data)
                return None

            file_path_remote = data.get("result", {}).get("file_path")
            if not file_path_remote:
                return None

            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path_remote}"
            file_res = await client.get(download_url)
            if file_res.status_code != 200:
                logger.error("Ошибка скачивания файла с Telegram сервера (%d)", file_res.status_code)
                return None

            os.makedirs("temp_media", exist_ok=True)
            filename = f"tg_{file_id}_{os.path.basename(file_path_remote)}"
            local_path = os.path.join("temp_media", filename)

            with open(local_path, "wb") as f:
                f.write(file_res.content)

            logger.info("Файл Telegram успешно скачан локально: %s", local_path)
            return local_path

    except Exception as e:
        logger.error("Исключение при скачивании файла Telegram (file_id=%s): %s", file_id, e)
        return None


async def parse_telegram_payload(
    body: Dict[str, Any],
    bot_token: str,
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], List[str]]:
    """
    Извлекает (sender_id, client_name, text, image_url, audio_path, temp_files) из Telegram Update JSON.
    """
    temp_files: List[str] = []

    msg = body.get("message") or body.get("edited_message")
    if not isinstance(msg, dict):
        return None, None, None, None, None, temp_files

    chat = msg.get("chat", {})
    sender_id = str(chat.get("id") or "")
    if not sender_id:
        from_user = msg.get("from", {})
        sender_id = str(from_user.get("id") or "")

    if not sender_id:
        return None, None, None, None, None, temp_files

    from_data = msg.get("from", {})
    first_name = from_data.get("first_name", "")
    last_name = from_data.get("last_name", "")
    client_name = f"{first_name} {last_name}".strip() or "Клиент Telegram"

    text = msg.get("text") or msg.get("caption")

    # Голосовое сообщение (voice / audio)
    audio_path = None
    voice_info = msg.get("voice") or msg.get("audio")
    if voice_info and isinstance(voice_info, dict) and voice_info.get("file_id"):
        file_id = voice_info["file_id"]
        downloaded = await download_telegram_file(file_id, bot_token)
        if downloaded:
            audio_path = downloaded
            temp_files.append(downloaded)

    # Изображение (photo)
    image_url = None
    photo_list = msg.get("photo")
    if photo_list and isinstance(photo_list, list) and len(photo_list) > 0:
        file_id = photo_list[-1].get("file_id")
        if file_id:
            downloaded = await download_telegram_file(file_id, bot_token)
            if downloaded:
                image_url = downloaded
                temp_files.append(downloaded)

    return sender_id, client_name, text, image_url, audio_path, temp_files


# ──────────────────────────────────────────────
# 3. Единый обработчик ИИ
# ──────────────────────────────────────────────


async def process_incoming_channel_message(
    db: Session,
    business_id: int,
    channel: str,
    sender_id: str,
    text: Optional[str],
    image_url: Optional[str] = None,
    audio_path: Optional[str] = None,
    auto_send: bool = True,
    background_tasks: Optional[BackgroundTasks] = None,
) -> MessengerWebhookResponse:
    """
    Единый обработчик ИИ:
    1. Направляет текст/медиа из любого канала в общий ИИ-менеджер.
    2. Загружает каталог товаров бизнеса и историю сообщений.
    3. Получает ответ от ИИ.
    4. Отправляет ответ обратно клиенту через универсальную функцию отправки (синхронно или в фоновом режиме через BackgroundTasks).
    """
    if not sender_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не указан идентификатор отправителя (phone_number или sender_id).",
        )

    if not text and not image_url and not audio_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Сообщение должно содержать текст, изображение или аудио.",
        )

    # 1. Формируем единый входящий payload
    payload = WebhookIncomingMessage(
        phone_number=sender_id,
        message=text,
        image_url=image_url,
        audio_path=audio_path,
        channel=channel,
    )

    # 2. Передаем в единый модуль обработки с контекстом и каталогом БД
    result = process_unified_message(
        db=db,
        business_id=business_id,
        payload=payload,
    )

    # 3. Универсальная отправка ответа обратно в мессенджер
    sent_to_client = False
    if auto_send and result.reply:
        if background_tasks:
            background_tasks.add_task(
                send_reply_to_messenger,
                channel=channel,
                recipient_id=sender_id,
                text=result.reply,
                business_id=business_id,
            )
            sent_to_client = True
        else:
            sent_to_client = await send_reply_to_messenger(
                channel=channel,
                recipient_id=sender_id,
                text=result.reply,
                business_id=business_id,
            )

    return MessengerWebhookResponse(
        status="success",
        channel=channel,
        business_id=business_id,
        sender_id=sender_id,
        reply=result.reply,
        sent_to_client=sent_to_client,
        order_created=result.order_created,
        escalated=result.escalated,
    )


# ──────────────────────────────────────────────
# 1. Вебхук для WhatsApp (/webhook/whatsapp)
# ──────────────────────────────────────────────

@router.get("/webhook/whatsapp")
@router.get("/webhook/whatsapp/{business_id}")
async def whatsapp_verify(
    business_id: Optional[int] = None,
    request: Request = None,
):
    """
    GET-эндпоинт верификации токена Meta для WhatsApp (или системного хэндшейка).
    """
    params = request.query_params if request else {}
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected_token = settings.WEBHOOK_VERIFY_TOKEN

    if mode == "subscribe" and token == expected_token:
        logger.info("Верификация WhatsApp вебхука успешно пройдена.")
        return Response(content=str(challenge), media_type="text/plain")

    if not mode and not token:
        return {"status": "ok", "service": "WhatsApp Webhook Active"}

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")


@router.post("/webhook/whatsapp", response_model=MessengerWebhookResponse)
@router.post("/webhook/whatsapp/{business_id}", response_model=MessengerWebhookResponse)
@limiter.limit("10/minute")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    business_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    POST-эндпоинт приема входящих сообщений WhatsApp.
    Принимает входящее сообщение от клиента WhatsApp, извлекает номер телефона и текст,
    направляет в ИИ и автоматически отправляет ответ через шлюз.
    """
    target_business_id = business_id or settings.DEFAULT_BUSINESS_ID
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON.")

    phone_number, text, image_url, audio_path = parse_whatsapp_payload(body)

    if not phone_number:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не удалось извлечь номер телефона отправителя WhatsApp.",
        )

    return await process_incoming_channel_message(
        db=db,
        business_id=target_business_id,
        channel="whatsapp",
        sender_id=phone_number,
        text=text,
        image_url=image_url,
        audio_path=audio_path,
        auto_send=True,
        background_tasks=background_tasks,
    )


# ──────────────────────────────────────────────
# 2. Вебхук для Instagram (/webhook/instagram)
# ──────────────────────────────────────────────

@router.get("/webhook/instagram")
@router.get("/webhook/instagram/{business_id}")
async def instagram_verify(
    business_id: Optional[int] = None,
    request: Request = None,
):
    """
    GET-эндпоинт верификации токена Meta для Instagram Direct.
    Реализует проверку hub.mode, hub.verify_token и возвращает hub.challenge.
    """
    params = request.query_params if request else {}
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    expected_token = settings.WEBHOOK_VERIFY_TOKEN

    if mode == "subscribe" and token == expected_token:
        logger.info("Верификация Meta токена для Instagram Direct: УСПЕХ")
        return Response(content=str(challenge), media_type="text/plain")

    if not mode and not token:
        return {"status": "ok", "service": "Instagram Direct Webhook Active"}

    logger.warning("Неудачная попытка верификации Instagram токена Meta: mode=%s, token=%s", mode, token)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")


@router.post("/webhook/instagram", response_model=MessengerWebhookResponse)
@router.post("/webhook/instagram/{business_id}", response_model=MessengerWebhookResponse)
@limiter.limit("10/minute")
async def instagram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    business_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    POST-эндпоинт приема входящих сообщений Instagram Direct.
    Принимает входящие сообщения из Instagram Direct, извлекает текст и sender_id,
    обрабатывает в ИИ-модуле и отправляет ответ в Директ через Meta Graph API.
    """
    target_business_id = business_id or settings.DEFAULT_BUSINESS_ID
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Невалидный JSON.")

    sender_id, text, image_url, audio_path = parse_instagram_payload(body)

    if not sender_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Не удалось извлечь sender_id из входящего запроса Instagram Direct.",
        )

    return await process_incoming_channel_message(
        db=db,
        business_id=target_business_id,
        channel="instagram",
        sender_id=sender_id,
        text=text,
        image_url=image_url,
        audio_path=audio_path,
        auto_send=True,
        background_tasks=background_tasks,
    )


# ──────────────────────────────────────────────
# 6. Авто-регистрация и эндпоинты Telegram Webhook
# ──────────────────────────────────────────────

async def register_telegram_webhook() -> bool:
    """
    Автоматическая регистрация вебхука в Telegram Bot API (setWebhook).
    Использует settings.TELEGRAM_BOT_TOKEN и settings.WEBHOOK_URL.
    """
    bot_token = (settings.TELEGRAM_BOT_TOKEN or settings.BOT_TOKEN or "").strip()
    webhook_url_setting = (settings.WEBHOOK_URL or "").strip()

    if not bot_token or bot_token in ("your_telegram_bot_token_here", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"):
        logger.warning("⚠️ TELEGRAM_BOT_TOKEN / BOT_TOKEN не настроен -- пропуск авто-регистрации Telegram setWebhook.")
        return False

    if not webhook_url_setting:
        logger.info("ℹ️ WEBHOOK_URL не задан в .env -- пропуск авто-регистрации Telegram setWebhook.")
        return False

    if "/webhook/telegram" in webhook_url_setting:
        target_webhook = webhook_url_setting
    else:
        target_webhook = f"{webhook_url_setting.rstrip('/')}/webhook/telegram"

    set_webhook_api_url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload = {"url": target_webhook}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(set_webhook_api_url, json=payload)
            if res.status_code == 200 and res.json().get("ok"):
                logger.info("✅ Telegram setWebhook успешно зарегистрирован: %s", target_webhook)
                return True
            else:
                logger.error("❌ Ошибка при регистрации Telegram setWebhook (%d): %s", res.status_code, res.text)
                return False
    except Exception as e:
        logger.error("❌ Исключение при вызове Telegram setWebhook (%s): %s", set_webhook_api_url, e)
        return False


@router.get("/webhook/telegram/setup_webhook")
@router.post("/webhook/telegram/setup_webhook")
async def api_setup_telegram_webhook():
    """Эндпоинт ручной вызова функции setWebhook для Telegram."""
    success = await register_telegram_webhook()
    if success:
        return {"status": "success", "message": "Telegram setWebhook успешно выполнен."}
    return {
        "status": "error",
        "message": "Не удалось зарегистрировать setWebhook (проверьте TELEGRAM_BOT_TOKEN и WEBHOOK_URL в .env).",
    }


@router.post("/webhook/telegram", response_model=MessengerWebhookResponse)
@router.post("/webhook/telegram/{business_id}", response_model=MessengerWebhookResponse)
@limiter.limit("30/minute")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    business_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """
    POST-эндпоинт приема входящих вебхуков (Update) от Telegram-бота в формате JSON.
    Поддерживает текстовые сообщения, голосовые сообщения (Whisper API) и изображения (Vision API).
    """
    target_business_id = business_id or settings.DEFAULT_BUSINESS_ID
    bot_token = (settings.TELEGRAM_BOT_TOKEN or settings.BOT_TOKEN or "").strip()

    try:
        body = await request.json()
    except Exception as e:
        logger.warning("Невалидный JSON во входящем вебхуке Telegram: %s", e)
        raise HTTPException(status_code=400, detail="Невалидный JSON.")

    if not isinstance(body, dict):
        logger.warning("Некорректный формат Telegram Update (ожидался словарь): %s", type(body))
        return JSONResponse(status_code=200, content={"status": "ok", "detail": "Invalid update format"})

    sender_id, client_name, text, image_url, audio_path, temp_files = await parse_telegram_payload(body, bot_token)

    if not sender_id:
        logger.info("Игнорирование Telegram Update без сообщения или chat_id (update_id=%s)", body.get("update_id"))
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "detail": "Update ignored (no message or chat_id)"},
        )

    try:
        response = await process_incoming_channel_message(
            db=db,
            business_id=target_business_id,
            channel="telegram",
            sender_id=sender_id,
            text=text,
            image_url=image_url,
            audio_path=audio_path,
            auto_send=True,
            background_tasks=background_tasks,
        )
        return response
    finally:
        for tf in temp_files:
            if tf and os.path.exists(tf):
                try:
                    os.remove(tf)
                    logger.debug("Временный файл Telegram удален: %s", tf)
                except Exception as clean_err:
                    logger.warning("Не удалось удалить временный файл %s: %s", tf, clean_err)

