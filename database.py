"""
database.py — Модуль подключения к базе данных SQLite.

Оптимизирован для продакшена на VPS (2 GB RAM):
  - WAL-режим журналирования (параллельные чтение/запись без блокировок).
  - Connection pooling через QueuePool (размер пула = 5 соединений).
  - Автоматические миграции (ALTER TABLE) при старте.
  - Генератор get_db() для использования как зависимость FastAPI.
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, declarative_base

from config import settings


# ──────────────────────────────────────────────
# Конфигурация подключения
# ──────────────────────────────────────────────

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=settings.DEBUG,
    pool_size=5,           # Макс. число соединений в пуле
    max_overflow=3,        # Доп. соединения сверх пула
    pool_timeout=10,       # Таймаут ожидания свободного соединения
    pool_recycle=600,      # Переподключение каждые 10 минут
    pool_pre_ping=True,    # Проверка жизнеспособности перед выдачей
)


# ──────────────────────────────────────────────
# Оптимизация SQLite: WAL + PRAGMA настройки
# ──────────────────────────────────────────────

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    """
    Устанавливает PRAGMA-параметры для каждого нового соединения SQLite.

    WAL (Write-Ahead Logging):
      - Позволяет одновременное чтение и запись.
      - Критически важно для FastAPI, где несколько потоков обращаются к БД.

    journal_size_limit:
      - Ограничивает размер WAL-журнала (64 MB) для экономии диска.

    synchronous = NORMAL:
      - Баланс между производительностью и надёжностью.
      - FULL = максимально надёжно, но медленнее.
      - NORMAL = безопасно для WAL-режима + быстрее.

    cache_size = -32000:
      - 32 MB кеша страниц в памяти (минус означает КБ).
      - Ускоряет повторные чтения на слабом VPS.

    busy_timeout = 5000:
      - 5 секунд ожидания при блокировке другим процессом.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA journal_size_limit=67108864;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA cache_size=-32000;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


# ──────────────────────────────────────────────
# Фабрика сессий и Base
# ──────────────────────────────────────────────

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

# Базовый класс для декларативных моделей
Base = declarative_base()


# ──────────────────────────────────────────────
# Инициализация и автомиграция
# ──────────────────────────────────────────────

def init_db():
    """
    Инициализирует БД: создаёт таблицы и применяет ALTER TABLE миграции.

    Все ALTER TABLE обернуты в try/except, т.к. SQLite выбросит ошибку
    при попытке добавить уже существующую колонку.
    """
    Base.metadata.create_all(bind=engine)

    # Миграции: безопасное добавление новых колонок (включая shop_id для мультиарендаторности)
    _migrations = {
        "businesses": [
            "ADD COLUMN shop_id INTEGER",
        ],
        "products": [
            "ADD COLUMN shop_id INTEGER DEFAULT 1",
            "ADD COLUMN article VARCHAR(50)",
            "ADD COLUMN category VARCHAR(100) DEFAULT 'Букеты'",
            "ADD COLUMN discount_price FLOAT",
            "ADD COLUMN promotion_info VARCHAR(255)",
            "ADD COLUMN flower_composition TEXT",
            "ADD COLUMN size VARCHAR(50)",
            "ADD COLUMN image_url VARCHAR(512)",
        ],
        "orders": [
            "ADD COLUMN shop_id INTEGER DEFAULT 1",
            "ADD COLUMN delivery_name VARCHAR(255)",
            "ADD COLUMN delivery_phone VARCHAR(20)",
            "ADD COLUMN delivery_time VARCHAR(100)",
            "ADD COLUMN card_text TEXT",
            "ADD COLUMN delivery_address TEXT",
            "ADD COLUMN payment_link VARCHAR(512)",
            "ADD COLUMN is_paid BOOLEAN DEFAULT 0",
            "ADD COLUMN checkout_step VARCHAR(50)",
        ],
        "clients": [
            "ADD COLUMN shop_id INTEGER DEFAULT 1",
            "ADD COLUMN channel VARCHAR(30)",
            "ADD COLUMN needs_human BOOLEAN DEFAULT 0",
            "ADD COLUMN escalation_reason VARCHAR(100)",
            "ADD COLUMN escalated_at DATETIME",
        ],
        "chat_messages": [
            "ADD COLUMN shop_id INTEGER DEFAULT 1",
        ],
        "order_items": [
            "ADD COLUMN shop_id INTEGER DEFAULT 1",
        ],
    }

    with engine.connect() as conn:
        for table_name, columns in _migrations.items():
            for col_def in columns:
                try:
                    conn.execute(text(f"ALTER TABLE {table_name} {col_def};"))
                    conn.commit()
                except Exception:
                    pass  # Колонка уже существует — игнорируем

        # Заполнение / синхронизация shop_id для существующих записей
        _backfills = [
            "UPDATE businesses SET shop_id = id WHERE shop_id IS NULL OR shop_id = 0;",
            "UPDATE products SET shop_id = business_id WHERE (shop_id IS NULL OR shop_id = 1) AND business_id IS NOT NULL;",
            "UPDATE clients SET shop_id = business_id WHERE (shop_id IS NULL OR shop_id = 1) AND business_id IS NOT NULL;",
            "UPDATE orders SET shop_id = business_id WHERE (shop_id IS NULL OR shop_id = 1) AND business_id IS NOT NULL;",
            "UPDATE chat_messages SET shop_id = (SELECT shop_id FROM clients WHERE clients.id = chat_messages.client_id) WHERE shop_id IS NULL OR shop_id = 1;",
            "UPDATE order_items SET shop_id = (SELECT shop_id FROM orders WHERE orders.id = order_items.order_id) WHERE shop_id IS NULL OR shop_id = 1;",
        ]
        for bf_sql in _backfills:
            try:
                conn.execute(text(bf_sql))
                conn.commit()
            except Exception:
                pass

    _seed_flower_shop_catalog()


def _seed_flower_shop_catalog():
    """
    Заполняет базу данных первоначальным каталогом букетов для цветочного магазина,
    если таблица products пуста.
    """
    db = SessionLocal()
    try:
        from models import Business, Product

        # Проверяем или создаем базовый цветочный бизнес
        biz = db.query(Business).filter(Business.id == 1).first()
        if not biz:
            biz = Business(
                id=1,
                shop_id=1,
                name="Sanaq Flowers (Цветочный Салон)",
                api_key_ai="sk-sanaq-flowers-demo-key",
            )
            db.add(biz)
            db.commit()
            db.refresh(biz)

        # Проверяем наличие букетов
        count = db.query(Product).filter(Product.shop_id == 1).count()
        if count == 0:
            flower_bouquets = [
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-101",
                    name="Букет 'Романтическая Нежность'",
                    category="Букеты роз",
                    flower_composition="15 нежно-розовых роз Эквадор, 3 ветки эвкалипта, матовая упаковка, атласная лента",
                    size="M (высота 50 см)",
                    price=18500.0,
                    discount_price=16900.0,
                    image_url="https://images.unsplash.com/photo-1582794543139-8ac9cb0f7b11",
                    stock=10,
                    description="Изысканный романтичный букет из пышных розовых роз с освежающим ароматом эвкалипта. Идеален для признания в любви и подарка любимой.",
                    promotion_info="Бесплатная открытка с вашим текстом + подкормка Chrysal в подарок!",
                ),
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-102",
                    name="Букет 'Гранд Пионы & Гортензии'",
                    category="Авторские букеты",
                    flower_composition="3 нежно-голубые гортензии, 5 пионов Сара Бернар, диантусы, оксипеталум, зелень",
                    size="L (авторский пышный)",
                    price=29000.0,
                    discount_price=None,
                    image_url="https://images.unsplash.com/photo-1563245372-f21724e3856d",
                    stock=5,
                    description="Роскошный авторский букет премиум-класса. Воздушное сочетание голубых гортензий и невероятных ароматных пионов.",
                    promotion_info="Бесплатная доставка курьером при заказе сегодня!",
                ),
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-103",
                    name="Коробка 'Алые Розы Премиум'",
                    category="Цветы в коробках",
                    flower_composition="25 премиальных алых роз Эквадор в шляпной коробке со специальной флористической оазис-губкой",
                    size="L (диаметр 30 см)",
                    price=24000.0,
                    discount_price=21900.0,
                    image_url="https://images.unsplash.com/photo-1526047932273-341f2a7631f9",
                    stock=8,
                    description="Страстный и незабываемый подарок. Цветы в шляпной коробке пропитаны влагой и не требуют вазы — стоят очень долго.",
                    promotion_info="Скидка 2 100 тг при заказе через бот!",
                ),
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-104",
                    name="Букет 'Весенний Бриз'",
                    category="Сезонные букеты",
                    flower_composition="19 разноцветных голландских тюльпанов, свежая зелень, дизайнерский крафт",
                    size="S (компактный)",
                    price=14500.0,
                    discount_price=12900.0,
                    image_url="https://images.unsplash.com/photo-1520763185298-1b434c919102",
                    stock=12,
                    description="Яркий, свежий и сочный букет тюльпанов, подарит весеннее настроение в любой день.",
                    promotion_info="Акция недели: свежая поставка из Голландии!",
                ),
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-105",
                    name="Композиция 'Солнечный Микс'",
                    category="Авторские букеты",
                    flower_composition="3 подсолнуха, кустовые желтые розы, ромашки, солидаго, упаковка крафт",
                    size="M (средний)",
                    price=21000.0,
                    discount_price=None,
                    image_url="https://images.unsplash.com/photo-1591886960571-74d43a9d4166",
                    stock=0,  # Распродан для проверки авто-предложения альтернатив ИИ
                    description="Тёплая и жизнерадостная композиция. Отлично подходит для выражения искренней благодарности и поздравлений с Днем рождения.",
                    promotion_info="Временно распродан — спросите у флориста про аналоги!",
                ),
                Product(
                    shop_id=1,
                    business_id=1,
                    article="FL-106",
                    name="Букет 'Белоснежное Облако'",
                    category="Букеты роз",
                    flower_composition="21 белоснежная роза Avalanche, пышная гипсофила, атласная лента",
                    size="L (пышный)",
                    price=22000.0,
                    discount_price=19900.0,
                    image_url="https://images.unsplash.com/photo-1561181286-d3fee7d55364",
                    stock=7,
                    description="Чистый и утонченный букет белоснежных роз с нежной облачной гипсофилой. Идеален на свадьбу, юбилей и для извинений.",
                    promotion_info="Бесплатная открытка в подарок!",
                ),
            ]
            db.bulk_save_objects(flower_bouquets)
            db.commit()
    finally:
        db.close()


def get_db():
    """
    Генератор сессии базы данных.

    Используется как зависимость FastAPI (Depends(get_db)).
    Гарантирует закрытие сессии после завершения запроса.

    Yields:
        Session: Активная сессия SQLAlchemy.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
