"""
orders.py -- Логика обработки и оформления заказов.

Содержит функции для пошагового сбора данных (Имя, Телефон, Время, Адрес),
генерации ссылок на оплату Kaspi Pay, проверки остатков товаров на складе,
уменьшения stock, формирования записи заказа (Order) и уведомления в Telegram-админ-бот.
"""

import os
import logging
import requests
from typing import List, Dict, Any, Optional, Tuple
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from models import Business, Product, Client, Order, OrderItem
from payments import process_manual_payment_request

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Pydantic-схемы для заказов
# ──────────────────────────────────────────────

class OrderItemInput(BaseModel):
    """
    Строгая схема одной позиции заказа в запросе.

    Валидация:
      - quantity: обязательно целое число > 0 (int, ge=1).
      - Хотя бы одно из полей product_id / product_name обязательно.
      - product_name: не может быть пустой строкой.
    """
    product_id: Optional[int] = Field(default=None, ge=1, description="ID товара в БД (целое число > 0)")
    product_name: Optional[str] = Field(default=None, min_length=1, max_length=255, description="Название товара (если ID не указан)")
    quantity: int = Field(default=1, ge=1, le=9999, description="Количество товара (целое число от 1 до 9999)")

    @field_validator("quantity", mode="before")
    @classmethod
    def validate_quantity_is_positive_int(cls, v: Any) -> int:
        """Quantity must be a positive integer (strictly > 0)."""
        if isinstance(v, bool):
            raise ValueError("Количество (quantity) должно быть целым числом, а не boolean.")
        if isinstance(v, float):
            if not v.is_integer():
                raise ValueError(f"Количество (quantity) должно быть целым числом, получено дробное: {v}")
            v = int(v)
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"Количество (quantity) должно быть целым числом, получено: {type(v).__name__}")
        if val <= 0:
            raise ValueError(f"Количество (quantity) должно быть больше нуля, получено: {val}")
        return val

    @field_validator("product_name")
    @classmethod
    def validate_product_name_not_blank(cls, v: Optional[str]) -> Optional[str]:
        """Product name cannot be blank whitespace."""
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Название товара (product_name) не может быть пустой строкой.")
        return v

    @model_validator(mode="after")
    def at_least_one_product_reference(self) -> "OrderItemInput":
        """At least one of product_id or product_name must be provided."""
        if self.product_id is None and not self.product_name:
            raise ValueError(
                "Для позиции заказа нужно указать хотя бы одно: "
                "product_id (ID товара) или product_name (название товара)."
            )
        return self


class OrderCreateRequest(BaseModel):
    """
    Строгая схема запроса на создание заказа.

    Валидация:
      - business_id: целое число > 0.
      - phone_number: обязательно, минимум 5 символов.
      - items: список позиций, не может быть пустым.
    """
    business_id: int = Field(..., ge=1, description="ID бизнеса (целое число > 0)")
    phone_number: str = Field(..., min_length=5, max_length=30, description="Номер телефона клиента")
    client_name: Optional[str] = Field(default=None, max_length=255, description="Имя клиента")
    items: List[OrderItemInput] = Field(..., min_length=1, description="Список позиций заказа (минимум 1)")

    @field_validator("phone_number")
    @classmethod
    def validate_phone_format(cls, v: str) -> str:
        """Phone number must not be blank."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Номер телефона не может быть пустым.")
        return cleaned

    @field_validator("items")
    @classmethod
    def validate_items_not_empty(cls, v: List[OrderItemInput]) -> List[OrderItemInput]:
        """Items list must contain at least one item."""
        if not v:
            raise ValueError("Список позиций заказа (items) не может быть пустым.")
        return v


class OrderItemDetail(BaseModel):
    """Информация об одной позиции созданного заказа."""
    product_id: Optional[int]
    product_name: str
    quantity: int
    unit_price: float
    line_total: float


class OrderResult(BaseModel):
    """Результат оформления заказа."""
    success: bool
    message: str
    order_id: Optional[int] = None
    total_price: float = 0.0
    payment_link: Optional[str] = None
    items: List[OrderItemDetail] = []


# ──────────────────────────────────────────────
# Интеграция с Kaspi Pay / Эквайрингом
# ──────────────────────────────────────────────

def generate_payment_link(order_id: int, total_price: float, business_name: str) -> str:
    """
    Генерирует ссылку на оплату Kaspi Pay / Эквайринг.
    В реальном продакшене вызывается API Kaspi Pay / Halyk Merchant.
    """
    clean_name = business_name.replace(" ", "_").lower()
    return f"https://pay.kaspi.kz/pay/{clean_name}?order_id={order_id}&amount={int(total_price)}"


# ──────────────────────────────────────────────
# Уведомления в Telegram-бот администратора
# ──────────────────────────────────────────────

def send_admin_order_notification(order: Order, db: Session) -> bool:
    """
    Отправляет красивое уведомление о новом оплаченном заказе администратору в Telegram.
    """
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    raw_admin_ids = os.getenv("ADMIN_ID", "").strip()
    admin_ids = [int(i.strip()) for i in raw_admin_ids.split(",") if i.strip().isdigit()]

    if not bot_token or not admin_ids:
        logger.warning("TELEGRAM_BOT_TOKEN или ADMIN_ID не настроены в .env. Уведомление в Telegram не отправлено.")
        return False

    business = db.query(Business).filter(Business.id == order.business_id).first()
    business_name = business.name if business else f"Бизнес #{order.business_id}"

    # Формируем список позиций
    items = db.query(OrderItem).filter(OrderItem.order_id == order.id).all()
    items_text_list = []
    for item in items:
        items_text_list.append(f"  • <b>{item.product_name}</b> x {item.quantity} — <code>{item.line_total} тг.</code>")
    items_formatted = "\n".join(items_text_list) if items_text_list else "  • Данные о позициях отсутствуют"

    card_info = order.card_text or 'Без открытки'

    text = (
        f"🛍 <b>НОВЫЙ ОПЛАЧЕННЫЙ ЗАКАЗ #{order.id}</b>\n\n"
        f"🏢 <b>Бизнес:</b> {business_name} (ID: {order.business_id})\n"
        f"👤 <b>Получатель:</b> {order.delivery_name or 'Не указано'}\n"
        f"📞 <b>Телефон:</b> <code>{order.delivery_phone or (order.client.phone_number if order.client else 'Не указано')}</code>\n"
        f"📅 <b>Дата и время:</b> {order.delivery_time or 'Не указано'}\n"
        f"💌 <b>Открытка:</b> {card_info}\n"
        f"📍 <b>Адрес / Самовывоз:</b> {order.delivery_address or 'Не указано'}\n\n"
        f"📦 <b>Состав заказа:</b>\n{items_formatted}\n\n"
        f"💰 <b>Итого оплачено:</b> <b>{order.total_price} тг.</b>\n"
        f"✅ <b>Статус:</b> Оплачен и оформлен!"
    )

    success_count = 0
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for admin_id in admin_ids:
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": admin_id,
                    "text": text,
                    "parse_mode": "HTML",
                },
                timeout=5,
            )
            if resp.status_code == 200:
                success_count += 1
                logger.info("Уведомление о заказе #%d отправлено в Telegram admin_id=%d", order.id, admin_id)
            else:
                logger.error("Ошибка отправки в Telegram (status %d): %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("Не удалось отправить уведомление админу %d: %s", admin_id, e)

    return success_count > 0


# ──────────────────────────────────────────────
# Пошаговое оформление заказа
# ──────────────────────────────────────────────

def initiate_step_by_step_checkout(
    db: Session,
    business_id: int,
    phone_number: str,
    items: List[Dict[str, Any]],
    client_name: Optional[str] = None,
    shop_id: Optional[int] = None,
) -> Tuple[Optional[Order], str]:
    """
    Инициализирует пошаговое оформление заказа (первый шаг — запрос имени).
    Обеспечивает строгую изоляцию данных по shop_id.
    """
    target_shop_id = shop_id if shop_id is not None else business_id

    business = db.query(Business).filter((Business.id == target_shop_id) | (Business.shop_id == target_shop_id)).first()
    if not business:
        return None, "Бизнес не найден."

    client = (
        db.query(Client)
        .filter(Client.shop_id == target_shop_id, Client.phone_number == phone_number)
        .first()
    )
    if not client:
        client = Client(
            shop_id=target_shop_id,
            business_id=target_shop_id,
            phone_number=phone_number,
            name=client_name or "Клиент",
        )
        db.add(client)
        db.flush()

    resolved_items = []
    total_price = 0.0

    for item_data in items:
        p_id = item_data.get("product_id")
        p_name = item_data.get("product_name")
        qty = int(item_data.get("quantity", 1))

        if qty <= 0:
            return None, "Некорректное количество товара."

        product = None
        if p_id is not None:
            product = db.query(Product).filter(Product.id == p_id, Product.shop_id == target_shop_id).first()
        elif p_name:
            product = db.query(Product).filter(
                Product.shop_id == target_shop_id,
                Product.name.ilike(f"%{p_name.strip()}%"),
            ).first()

        if not product:
            target = f"ID {p_id}" if p_id else f"названием '{p_name}'"
            return None, f"Товар с {target} не найден в каталоге."

        if product.stock < qty:
            return None, f"Недостаточно товара '{product.name}' на складе (в наличии: {product.stock} шт.)."

        effective_price = (
            product.discount_price
            if (product.discount_price is not None and product.discount_price > 0)
            else product.price
        )
        line_total = round(effective_price * qty, 2)
        total_price += line_total

        resolved_items.append({
            "product": product,
            "quantity": qty,
            "unit_price": effective_price,
            "line_total": line_total,
        })

    # Создаём незавершенный заказ в статусе pending_checkout
    new_order = Order(
        shop_id=target_shop_id,
        business_id=target_shop_id,
        client_id=client.id,
        total_price=round(total_price, 2),
        status="pending_checkout",
        checkout_step="awaiting_name",
        is_paid=False,
    )
    db.add(new_order)
    db.flush()

    for res in resolved_items:
        order_item = OrderItem(
            shop_id=target_shop_id,
            order_id=new_order.id,
            product_id=res["product"].id,
            product_name=res["product"].name,
            quantity=res["quantity"],
            unit_price=res["unit_price"],
            line_total=res["line_total"],
        )
        db.add(order_item)

    db.commit()
    db.refresh(new_order)

    prompt_msg = (
        f"Замечательно! 🌸 Начинаем оформление вашего заказа (Заказ #{new_order.id}) на сумму {new_order.total_price} тг.\n\n"
        "Шаг 1 из 5: Укажите, пожалуйста, **Имя получателя** (как флористу и курьеру обращаться при вручении букета)?"
    )
    return new_order, prompt_msg


def process_checkout_step(
    db: Session,
    order: Order,
    user_message: str,
) -> str:
    """
    Обрабатывает очередной шаг пошагового сбора данных для доставки цветов.
    Шаги: awaiting_name -> awaiting_phone -> awaiting_time -> awaiting_card_text -> awaiting_address -> awaiting_payment -> completed
    """
    text = user_message.strip()
    step = order.checkout_step or "awaiting_name"

    if step == "awaiting_name":
        order.delivery_name = text
        order.checkout_step = "awaiting_phone"
        db.commit()
        return (
            f"Принято, имя получателя: {text}! ✨\n\n"
            "Шаг 2 из 5: Укажите контактный **номер телефона** для согласования доставки курьером:"
        )

    elif step == "awaiting_phone":
        order.delivery_phone = text
        order.checkout_step = "awaiting_time"
        db.commit()
        return (
            "Номер телефона сохранен! 📞\n\n"
            "Шаг 3 из 5: Напишите желаемую **дату и время получения** "
            "(доставка или самовывоз, например: 'Сегодня к 18:00' или 'Завтра с 10:00 до 12:00'):"
        )

    elif step == "awaiting_time":
        order.delivery_time = text
        order.checkout_step = "awaiting_card_text"
        db.commit()
        return (
            f"Дата и время зафиксированы: {text}. ⏰\n\n"
            "Шаг 4 из 5: 💌 Хотите добавить **бесплатную поздравительную открытку / записку** к букету?\n"
            "Напишите текст поздравления или 'Нет', если открытка не нужна:"
        )

    elif step == "awaiting_card_text":
        if text.lower() in ("нет", "не надо", "без открытки", "нет спасибо", "no", "-"):
            order.card_text = None
        else:
            order.card_text = text
        order.checkout_step = "awaiting_address"
        db.commit()
        card_status = f"'{order.card_text}'" if order.card_text else "без открытки"
        return (
            f"Записка к букету: {card_status}. 💌\n\n"
            "Шаг 5 из 5: Напишите точный **адрес доставки** (город, улица, дом, квартира/офис) или 'Самовывоз':"
        )

    elif step == "awaiting_address":
        order.delivery_address = text
        business = db.query(Business).filter(Business.id == order.business_id).first()
        b_name = business.name if business else "Sanaq Flowers"
        link = generate_payment_link(order.id, order.total_price, b_name)
        order.payment_link = link

        # Отправляем банковские реквизиты клиенту и карточку оплаты с кнопками админу
        requisites_text = process_manual_payment_request(db, order)

        card_info = f"💌 **Текст открытки:** {order.card_text}\n" if order.card_text else ""
        return (
            f"💐 **Заказ #{order.id} на доставку цветов успешно сформирован!**\n\n"
            f"📋 **Детали заказа и доставки:**\n"
            f"👤 **Получатель:** {order.delivery_name}\n"
            f"📞 **Телефон:** {order.delivery_phone}\n"
            f"📅 **Дата и время:** {order.delivery_time}\n"
            f"{card_info}"
            f"📍 **Адрес:** {order.delivery_address}\n\n"
            f"{requisites_text}"
        )

    elif step in ("awaiting_payment", "awaiting_payment_confirmation"):
        return (
            f"Ваш заказ #{order.id} на сумму {order.total_price} тг. ожидает подтверждения оплаты.\n"
            f"Администратор проверяет поступивший перевод. После подтверждения статус обновится автоматически! ⏳"
        )

    elif step == "completed":
        return f"Заказ #{order.id} уже полностью оформлен и оплачен!"

    return "Оформление заказа продолжается..."


def confirm_order_payment(db: Session, order_id: int, shop_id: Optional[int] = None) -> OrderResult:
    """
    Подтверждает оплату заказа, списывает товары со склада и отправляет уведомление администратору в Telegram.
    Использует строгую фильтрацию по shop_id.
    """
    query = db.query(Order).filter(Order.id == order_id)
    if shop_id is not None:
        query = query.filter(Order.shop_id == shop_id)
    order = query.first()

    if not order:
        return OrderResult(success=False, message=f"Заказ с ID #{order_id} не найден.")

    if order.is_paid or order.status in ("confirmed", "paid", "completed"):
        return OrderResult(
            success=True,
            message=f"Заказ #{order.id} уже был ранее оплачен.",
            order_id=order.id,
            total_price=order.total_price,
            payment_link=order.payment_link,
        )

    # 1. Уменьшаем остатки товаров на складе (изоляция по shop_id)
    items = db.query(OrderItem).filter(OrderItem.shop_id == order.shop_id, OrderItem.order_id == order.id).all()
    item_details = []

    for item in items:
        if item.product_id:
            product = db.query(Product).filter(Product.shop_id == order.shop_id, Product.id == item.product_id).first()
            if product:
                product.stock = max(0, product.stock - item.quantity)

        item_details.append(
            OrderItemDetail(
                product_id=item.product_id,
                product_name=item.product_name,
                quantity=item.quantity,
                unit_price=item.unit_price,
                line_total=item.line_total,
            )
        )

    # 2. Переводим в статус оплаченного
    order.is_paid = True
    order.status = "paid"
    order.checkout_step = "completed"

    db.commit()
    db.refresh(order)

    # 3. Отправляем уведомление в Telegram-админ-бот
    send_admin_order_notification(order, db)

    logger.info("Заказ #%d (shop_id=%d) успешно оплачен и подтвержден. Уведомление отправлено в Telegram.", order.id, order.shop_id)

    return OrderResult(
        success=True,
        message=f"Заказ #{order.id} успешно оплачен!",
        order_id=order.id,
        total_price=order.total_price,
        payment_link=order.payment_link,
        items=item_details,
    )


def create_order(
    db: Session,
    business_id: int,
    phone_number: str,
    items: List[Dict[str, Any]],
    client_name: Optional[str] = None,
    shop_id: Optional[int] = None,
) -> OrderResult:
    """Прямой вызов создания заказа (для обратной совместимости или API)."""
    target_shop_id = shop_id if shop_id is not None else business_id
    order, prompt_msg = initiate_step_by_step_checkout(
        db=db,
        business_id=business_id,
        phone_number=phone_number,
        items=items,
        client_name=client_name,
        shop_id=target_shop_id,
    )
    if not order:
        return OrderResult(success=False, message=prompt_msg)

    # Подгружаем детали для ответа
    items_db = db.query(OrderItem).filter(OrderItem.shop_id == target_shop_id, OrderItem.order_id == order.id).all()
    item_details = [
        OrderItemDetail(
            product_id=i.product_id,
            product_name=i.product_name,
            quantity=i.quantity,
            unit_price=i.unit_price,
            line_total=i.line_total,
        )
        for i in items_db
    ]

    return OrderResult(
        success=True,
        message=f"Заказ #{order.id} сформирован. Ожидаются данные для доставки.",
        order_id=order.id,
        total_price=order.total_price,
        payment_link=order.payment_link,
        items=item_details,
    )

