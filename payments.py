"""
payments.py -- Модуль ручной оплаты по реквизитам с подтверждением через администратора.

Обеспечивает полный цикл ручной оплаты:
1. Получение реквизитов: отправка реквизитов магазина (номер карты/телефона и банк) текстом клиенту в чат по shop_id.
2. Мгновенная отправка админу: формирование карточки заказа со статусом ожидания оплаты и отправка в tg_admin_bot.py.
3. Кнопки управления у админа: инлайн-кнопки "✅ Подтвердить оплату" и "❌ Отклонить".
4. Универсальное уведомление клиента: изменение статуса заказа в БД на "paid" и отправка клиенту сообщения:
   "Оплата прошла, ваш заказ скоро будет готов."
"""

import os
import json
import logging
import requests
from typing import Optional, Dict, Any, Tuple
from sqlalchemy.orm import Session

from models import Business, Product, Client, Order, OrderItem, ChatMessage

logger = logging.getLogger(__name__)

# Сообщение клиенту после подтверждения оплаты администратором
UNIVERSAL_PAYMENT_SUCCESS_REPLY = "Оплата прошла, ваш заказ скоро будет готов."

# Сообщение клиенту после отклонения оплаты администратором
UNIVERSAL_PAYMENT_REJECTED_REPLY = (
    "К сожалению, ваш платеж не был подтвержден администратором. "
    "Пожалуйста, проверьте чек об оплате или свяжитесь с менеджером."
)


# ──────────────────────────────────────────────
# 1. Получение и форматирование реквизитов магазина
# ──────────────────────────────────────────────

def get_shop_payment_details(db: Session, shop_id: int) -> Dict[str, str]:
    """
    Возвращает банковские реквизиты магазина (название банка, номер карты/телефона, ФИО получателя).
    Данные извлекаются из поля business.settings (JSON) или из настроек по умолчанию.
    """
    business = db.query(Business).filter(Business.id == shop_id).first()
    business_name = business.name if business else f"Магазин #{shop_id}"

    # Значения по умолчанию
    requisites = {
        "bank_name": "Kaspi Bank",
        "card_or_phone": "+7 (707) 111-22-33",
        "recipient_name": f"ИП {business_name}",
    }

    if business and business.settings:
        try:
            settings_data = json.loads(business.settings)
            payment_cfg = settings_data.get("payment_requisites", {})
            if payment_cfg.get("bank_name"):
                requisites["bank_name"] = payment_cfg["bank_name"]
            if payment_cfg.get("card_or_phone"):
                requisites["card_or_phone"] = payment_cfg["card_or_phone"]
            if payment_cfg.get("recipient_name"):
                requisites["recipient_name"] = payment_cfg["recipient_name"]
        except Exception as e:
            logger.warning("Ошибка парсинга business.settings для shop_id=%d: %s", shop_id, e)

    return requisites


def format_client_requisites_text(order: Order, requisites: Dict[str, str]) -> str:
    """
    Формирует текст сообщения с реквизитами для отправки клиенту в чат.
    """
    return (
        f"💳 **Реквизиты для оплаты заказа #{order.id}:**\n\n"
        f"🏦 **Банк:** {requisites['bank_name']}\n"
        f"🔢 **Номер карты / телефона:** `{requisites['card_or_phone']}`\n"
        f"👤 **Получатель:** {requisites['recipient_name']}\n"
        f"💰 **Сумма к оплате:** **{order.total_price} тг.**\n\n"
        f"⚠️ *После совершения перевода, пожалуйста, отправьте скриншот/чек в этот чат или напишите 'Оплачено'. "
        f"Наш администратор сразу подтвердит поступление средств!* ✨"
    )


# ──────────────────────────────────────────────
# 2. Карточка заказа и кнопка для Telegram-админ-бота
# ──────────────────────────────────────────────

def send_admin_payment_card(order: Order, db: Session) -> bool:
    """
    Формирует карточку заказа со статусом ожидания оплаты и отправляет её в Telegram-админ-бот
    с инлайн-кнопками '✅ Подтвердить оплату' и '❌ Отклонить'.
    """
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    raw_admin_ids = os.getenv("ADMIN_ID", "").strip()
    admin_ids = [int(i.strip()) for i in raw_admin_ids.split(",") if i.strip().isdigit()]

    if not bot_token or not admin_ids:
        logger.warning("TELEGRAM_BOT_TOKEN или ADMIN_ID не заданы в .env. Карточка оплаты админу не отправлена.")
        return False

    business = db.query(Business).filter(Business.id == order.business_id).first()
    business_name = business.name if business else f"Бизнес #{order.business_id}"

    client = db.query(Client).filter(Client.id == order.client_id).first()
    client_phone = order.delivery_phone or (client.phone_number if client else "Не указан")
    client_name = order.delivery_name or (client.name if client else "Клиент")

    # Формируем список позиций заказа
    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    items_lines = []
    for item in items:
        items_lines.append(f"  • <b>{item.product_name}</b> x {item.quantity} — <code>{item.line_total} тг.</code>")
    items_formatted = "\n".join(items_lines) if items_lines else "  • (Состав не указан)"

    text = (
        f"💳 <b>ЗАПРОС РУЧНОЙ ОПЛАТЫ ПО РЕКВИЗИТАМ</b>\n\n"
        f"📦 <b>Заказ:</b> #{order.id}\n"
        f"🏢 <b>Магазин:</b> {business_name} (ID: {order.shop_id})\n"
        f"👤 <b>Получатель:</b> {client_name}\n"
        f"📞 <b>Телефон:</b> <code>{client_phone}</code>\n"
        f"⏰ <b>Время доставки:</b> {order.delivery_time or 'Не указано'}\n"
        f"📍 <b>Адрес доставки:</b> {order.delivery_address or 'Не указан'}\n\n"
        f"🛍 <b>Состав заказа:</b>\n{items_formatted}\n\n"
        f"💰 <b>Сумма к оплате:</b> <b>{order.total_price} тг.</b>\n"
        f"⏳ <b>Статус:</b> <i>Ожидает подтверждения перевода по реквизитам</i>"
    )

    # Инлайн-кнопки управления для администратора
    inline_keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "✅ Подтвердить оплату",
                    "callback_data": f"pay_confirm:{order.id}:{order.shop_id}",
                },
                {
                    "text": "❌ Отклонить",
                    "callback_data": f"pay_reject:{order.id}:{order.shop_id}",
                },
            ]
        ]
    }

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    success_count = 0

    for admin_id in admin_ids:
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": admin_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": inline_keyboard,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                success_count += 1
                logger.info("Карточка оплаты заказа #%d успешно отправлена админу %d", order.id, admin_id)
            else:
                logger.error("Ошибка отправки карточки оплаты админу %d (status %d): %s", admin_id, resp.status_code, resp.text)
        except Exception as e:
            logger.error("Не удалось отправить карточку оплаты админу %d: %s", admin_id, e)

    return success_count > 0


# ──────────────────────────────────────────────
# 3. Полный процесс формирования запроса реквизитов
# ──────────────────────────────────────────────

def process_manual_payment_request(db: Session, order: Order) -> str:
    """
    Выполняет комплексный сценарий при запросе ручной оплаты:
    1. Переводит статус заказа в pending_payment / awaiting_payment.
    2. Получает реквизиты магазина по shop_id.
    3. Отправляет карточку заказа в Telegram-бот админа.
    4. Возвращает текст с реквизитами для клиента.
    """
    order.status = "pending_payment"
    order.checkout_step = "awaiting_payment_confirmation"
    db.commit()

    # 1. Получаем реквизиты
    requisites = get_shop_payment_details(db, order.shop_id)

    # 2. Формируем текст клиенту
    client_text = format_client_requisites_text(order, requisites)

    # 3. Отправляем карточку админу с инлайн-кнопками
    send_admin_payment_card(order, db)

    return client_text


# ──────────────────────────────────────────────
# 4. Обработка действий администратора (Подтвердить / Отклонить)
# ──────────────────────────────────────────────

def send_client_notification(db: Session, order: Order, text: str) -> bool:
    """
    Отправляет уведомление клиенту:
    - Добавляет запись в ChatMessage (role='assistant') для истории диалога.
    """
    user_msg = ChatMessage(
        shop_id=order.shop_id,
        client_id=order.client_id,
        role="assistant",
        message=text,
    )
    db.add(user_msg)
    db.commit()
    logger.info("Уведомление клиенту #%d по заказу #%d сохранено в БД: '%s'", order.client_id, order.id, text)
    return True


def confirm_manual_payment(db: Session, order_id: int, shop_id: Optional[int] = None) -> Tuple[bool, str]:
    """
    Вызывается при нажатии админом кнопки '✅ Подтвердить оплату':
    1. Проверяет и меняет статус заказа на 'paid', is_paid=True.
    2. Уменьшает остатки товаров на складе (stock).
    3. Отправляет клиенту универсальное сообщение: 'Оплата прошла, ваш заказ скоро будет готов.'
    """
    query = db.query(Order).filter(Order.id == order_id)
    if shop_id is not None:
        query = query.filter(Order.shop_id == shop_id)
    order = query.first()

    if not order:
        return False, f"Заказ #{order_id} не найден."

    if order.is_paid or order.status in ("paid", "completed"):
        return True, f"Заказ #{order.id} уже подтвержден ранее."

    # 1. Списание товара со склада
    items = db.query(OrderItem).filter(OrderItem.shop_id == order.shop_id, OrderItem.order_id == order.id).all()
    for item in items:
        if item.product_id:
            product = db.query(Product).filter(Product.shop_id == order.shop_id, Product.id == item.product_id).first()
            if product:
                product.stock = max(0, product.stock - item.quantity)

    # 2. Обновление статуса заказа
    order.is_paid = True
    order.status = "paid"
    order.checkout_step = "completed"
    db.commit()
    db.refresh(order)

    # 3. Отправка универсального уведомления клиенту
    send_client_notification(db, order, UNIVERSAL_PAYMENT_SUCCESS_REPLY)

    logger.info("Ручная оплата заказа #%d подтверждена админом. Статус обновлен на paid.", order.id)
    return True, f"Оплата заказа #{order.id} успешно подтверждена! Клиент уведомлен."


def reject_manual_payment(db: Session, order_id: int, shop_id: Optional[int] = None) -> Tuple[bool, str]:
    """
    Вызывается при нажатии админом кнопки '❌ Отклонить':
    1. Переводит статус заказа в 'cancelled'.
    2. Отправляет клиенту уведомление об отклонении.
    """
    query = db.query(Order).filter(Order.id == order_id)
    if shop_id is not None:
        query = query.filter(Order.shop_id == shop_id)
    order = query.first()

    if not order:
        return False, f"Заказ #{order_id} не найден."

    order.status = "cancelled"
    order.checkout_step = "cancelled"
    db.commit()
    db.refresh(order)

    # Отправка уведомления клиенту
    send_client_notification(db, order, UNIVERSAL_PAYMENT_REJECTED_REPLY)

    logger.info("Ручная оплата заказа #%d отклонена админом. Статус обновлен на cancelled.", order.id)
    return True, f"Оплата заказа #{order.id} отклонена. Клиент уведомлен."
