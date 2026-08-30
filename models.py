"""
models.py -- Модели базы данных (SQLAlchemy ORM).

Определяет шесть таблиц:
  - Business    -- бизнес-аккаунт (компания)
  - Product     -- товар / услуга
  - Client      -- клиент компании
  - ChatMessage -- сообщение в чате (диалог с ИИ)
  - Order       -- заказ клиента
  - OrderItem   -- позиция (строка) заказа
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    String,
    Float,
    Text,
    ForeignKey,
    DateTime,
    Date,
)
from sqlalchemy.orm import relationship

from database import Base


# ──────────────────────────────────────────────
# Бизнес-аккаунт
# ──────────────────────────────────────────────

class Business(Base):
    """
    Модель бизнес-аккаунта (магазина).

    Attributes:
        id:          Уникальный идентификатор.
        shop_id:     ID магазина (арендатора).
        name:        Название компании.
        api_key_ai:  API-ключ для доступа к ИИ-сервису.
        phone:       Контактный телефон бизнеса.
        settings:    Произвольные настройки (JSON-строка).
    """
    __tablename__ = "businesses"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, index=True, nullable=True, comment="ID магазина/арендатора (синхронизирован с id)")
    name = Column(String(255), nullable=False, comment="Название компании")
    api_key_ai = Column(String(512), nullable=True, comment="API-ключ ИИ-сервиса")
    phone = Column(String(20), nullable=True, comment="Контактный телефон")
    settings = Column(Text, nullable=True, comment="Настройки (JSON)")

    # Связи
    products = relationship("Product", back_populates="business", cascade="all, delete-orphan", foreign_keys="[Product.business_id]")
    clients = relationship("Client", back_populates="business", cascade="all, delete-orphan", foreign_keys="[Client.business_id]")
    orders = relationship("Order", back_populates="business", cascade="all, delete-orphan", foreign_keys="[Order.business_id]")

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "id" in kwargs and kwargs["id"] is not None:
                kwargs["shop_id"] = kwargs["id"]
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<Business(id={self.id}, shop_id={self.shop_id}, name='{self.name}')>"


# ──────────────────────────────────────────────
# Товар / Услуга
# ──────────────────────────────────────────────

class Product(Base):
    """
    Модель товара или услуги.

    Attributes:
        id:              Уникальный идентификатор.
        shop_id:         Обязательный ID магазина/арендатора для изоляции данных.
        business_id:     FK -> businesses.id.
        article:         Артикул товара (например, 'Арт. 12', '#5').
        name:            Название товара.
        category:        Категория товара (например, 'Цветы', 'Букеты', 'Одежда').
        price:           Базовая цена.
        discount_price:  Акционная цена / цена со скидкой.
        stock:           Остаток на складе.
        description:     Описание товара.
        promotion_info:  Информация об акции или скидке.
    """
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        default=1,
        index=True,
        comment="Обязательное поле: ID магазина/арендатора для изоляции данных",
    )
    business_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID бизнеса-владельца",
    )
    article = Column(String(50), nullable=True, index=True, comment="Артикул товара (например, 'FL-101')")
    name = Column(String(255), nullable=False, comment="Название букета / товара")
    category = Column(String(100), nullable=True, default="Букеты", index=True, comment="Категория товара")
    flower_composition = Column(Text, nullable=True, comment="Состав цветов в букете (например, '15 роз, 3 эвкалипта')")
    size = Column(String(50), nullable=True, comment="Размер букета (S, M, L, XL)")
    price = Column(Float, nullable=False, default=0.0, comment="Базовая цена букета")
    discount_price = Column(Float, nullable=True, comment="Акционная цена / цена со скидкой")
    image_url = Column(String(512), nullable=True, comment="Ссылка на фото букета")
    stock = Column(Integer, nullable=False, default=0, comment="Остаток на складе")
    description = Column(Text, nullable=True, comment="Описание букета")
    promotion_info = Column(String(255), nullable=True, comment="Информация об акции/скидке")

    # Связь
    business = relationship("Business", back_populates="products", foreign_keys=[business_id])

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "business_id" in kwargs and kwargs["business_id"] is not None:
                kwargs["shop_id"] = kwargs["business_id"]
        super().__init__(**kwargs)

    @property
    def photo_url(self) -> Optional[str]:
        """Удобный псевдоним для доступа к фото букета."""
        return self.image_url

    def __repr__(self) -> str:
        return f"<Product(id={self.id}, shop_id={self.shop_id}, article='{self.article}', name='{self.name}', size='{self.size}', price={self.price}, stock={self.stock})>"



# ──────────────────────────────────────────────
# Клиент
# ──────────────────────────────────────────────

class Client(Base):
    """
    Модель клиента.

    Attributes:
        id:           Уникальный идентификатор.
        shop_id:      Обязательный ID магазина/арендатора для изоляции данных.
        business_id:  FK -> businesses.id.
        phone_number: Номер телефона клиента.
        name:         Имя клиента.
    """
    __tablename__ = "clients"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        default=1,
        index=True,
        comment="Обязательное поле: ID магазина/арендатора для изоляции данных",
    )
    business_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID бизнеса",
    )
    phone_number = Column(String(20), nullable=False, comment="Телефон клиента")
    name = Column(String(255), nullable=True, comment="Имя клиента")
    channel = Column(String(30), nullable=True, comment="Канал связи: whatsapp / telegram / instagram / api")
    needs_human = Column(Boolean, default=False, nullable=False, comment="Флаг: требуется живой менеджер")
    escalation_reason = Column(String(100), nullable=True, comment="Причина эскалации")
    escalated_at = Column(DateTime, nullable=True, comment="Дата/время последней эскалации")

    # Связи
    business = relationship("Business", back_populates="clients", foreign_keys=[business_id])
    messages = relationship("ChatMessage", back_populates="client", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="client", cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "business_id" in kwargs and kwargs["business_id"] is not None:
                kwargs["shop_id"] = kwargs["business_id"]
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<Client(id={self.id}, shop_id={self.shop_id}, name='{self.name}', phone='{self.phone_number}', needs_human={self.needs_human})>"


# ──────────────────────────────────────────────
# Сообщение чата
# ──────────────────────────────────────────────

class ChatMessage(Base):
    """
    Модель сообщения в чате (диалоги).

    Attributes:
        id:        Уникальный идентификатор.
        shop_id:   Обязательный ID магазина/арендатора для изоляции данных.
        client_id: FK -> clients.id.
        role:      Роль отправителя: 'user' или 'assistant'.
        message:   Текст сообщения.
        timestamp: Дата и время отправки.
    """
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        default=1,
        index=True,
        comment="Обязательное поле: ID магазина/арендатора для изоляции данных",
    )
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID клиента",
    )
    role = Column(
        String(20),
        nullable=False,
        comment="Роль: 'user' или 'assistant'",
    )
    message = Column(Text, nullable=False, comment="Текст сообщения")
    timestamp = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="Время отправки",
    )

    # Связь
    client = relationship("Client", back_populates="messages")

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "client" in kwargs and getattr(kwargs["client"], "shop_id", None):
                kwargs["shop_id"] = kwargs["client"].shop_id
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<ChatMessage(id={self.id}, shop_id={self.shop_id}, role='{self.role}', time='{self.timestamp}')>"


# ──────────────────────────────────────────────
# Заказ
# ──────────────────────────────────────────────

class Order(Base):
    """
    Модель заказа.

    Attributes:
        id:          Уникальный идентификатор.
        shop_id:     Обязательный ID магазина/арендатора для изоляции данных.
        business_id: FK -> businesses.id.
        client_id:   FK -> clients.id.
        total_price: Итоговая сумма заказа.
        status:      Статус заказа (new / confirmed / completed / cancelled).
        created_at:  Дата и время создания.
    """
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        default=1,
        index=True,
        comment="Обязательное поле: ID магазина/арендатора для изоляции данных",
    )
    business_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID бизнеса",
    )
    client_id = Column(
        Integer,
        ForeignKey("clients.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID клиента",
    )
    total_price = Column(Float, nullable=False, default=0.0, comment="Итоговая сумма")
    status = Column(
        String(20),
        nullable=False,
        default="new",
        comment="Статус: new / pending_checkout / confirmed / paid / completed / cancelled",
    )
    delivery_name = Column(String(255), nullable=True, comment="Имя получателя для доставки")
    delivery_phone = Column(String(20), nullable=True, comment="Телефон получателя для доставки")
    delivery_time = Column(String(100), nullable=True, comment="Время/дата доставки")
    card_text = Column(Text, nullable=True, comment="Текст открытки/записки к букету")
    delivery_address = Column(Text, nullable=True, comment="Адрес доставки")
    payment_link = Column(String(512), nullable=True, comment="Ссылка на оплату (Kaspi Pay)")
    is_paid = Column(Boolean, default=False, comment="Флаг оплаты")
    checkout_step = Column(String(50), nullable=True, comment="Текущий этап пошагового оформления: awaiting_name, awaiting_phone, awaiting_time, awaiting_card_text, awaiting_address, awaiting_payment, completed")
    created_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        comment="Дата создания",
    )

    # Связи
    business = relationship("Business", back_populates="orders", foreign_keys=[business_id])
    client = relationship("Client", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "business_id" in kwargs and kwargs["business_id"] is not None:
                kwargs["shop_id"] = kwargs["business_id"]
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<Order(id={self.id}, shop_id={self.shop_id}, total={self.total_price}, status='{self.status}')>"


# ──────────────────────────────────────────────
# Позиция заказа
# ──────────────────────────────────────────────

class OrderItem(Base):
    """
    Модель позиции (строки) заказа.

    Attributes:
        id:           Уникальный идентификатор.
        shop_id:      Обязательный ID магазина/арендатора для изоляции данных.
        order_id:     FK -> orders.id.
        product_id:   FK -> products.id.
        product_name: Название товара (снимок на момент заказа).
        quantity:     Количество.
        unit_price:   Цена за единицу (снимок на момент заказа).
        line_total:   Сумма по позиции (quantity * unit_price).
    """
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(
        Integer,
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
        default=1,
        index=True,
        comment="Обязательное поле: ID магазина/арендатора для изоляции данных",
    )
    order_id = Column(
        Integer,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID заказа",
    )
    product_id = Column(
        Integer,
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID товара",
    )
    product_name = Column(String(255), nullable=False, comment="Название товара (снимок)")
    quantity = Column(Integer, nullable=False, default=1, comment="Количество")
    unit_price = Column(Float, nullable=False, default=0.0, comment="Цена за единицу")
    line_total = Column(Float, nullable=False, default=0.0, comment="Сумма по позиции")

    # Связь
    order = relationship("Order", back_populates="items")

    def __init__(self, **kwargs):
        if "shop_id" not in kwargs or kwargs["shop_id"] is None:
            if "order" in kwargs and getattr(kwargs["order"], "shop_id", None):
                kwargs["shop_id"] = kwargs["order"].shop_id
        super().__init__(**kwargs)

    def __repr__(self) -> str:
        return f"<OrderItem(id={self.id}, shop_id={self.shop_id}, product='{self.product_name}', qty={self.quantity})>"
