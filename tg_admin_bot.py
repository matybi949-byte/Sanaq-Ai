"""
tg_admin_bot.py -- Telegram-бот для владельцев бизнеса на aiogram 3.x.

Управление каталогом товаров и ценами напрямую из Telegram.

Команды:
  /start - Приветствие и справка
  /products - Просмотр всех товаров бизнеса
  /add [название] [цена] [остаток] - Добавление нового товара
  /update [ID] [новая_цена] - Обновление цены товара
  /stock [ID] [новый_остаток] - Обновление количества на складе
  /report [YYYY-MM-DD] - Дневная аналитика продаж (по умолчанию — сегодня)
  /resolve [client_id] - Снять эскалацию и вернуть клиента ИИ

Запуск:
  python tg_admin_bot.py
"""

import os
import asyncio
import logging
from typing import Optional, List, Callable, Dict, Any, Awaitable

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, BaseMiddleware, types, F
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from database import SessionLocal, engine, Base
from models import Business, Product, Order, OrderItem, Client
from analytics import get_daily_analytics, format_daily_report, send_daily_report_to_admin
from payments import confirm_manual_payment, reject_manual_payment

load_dotenv()


def db_get_orders(business_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """Получает список последних заказов бизнеса из БД."""
    with SessionLocal() as db:
        orders = (
            db.query(Order)
            .filter(Order.business_id == business_id)
            .order_by(Order.id.desc())
            .limit(limit)
            .all()
        )
        result = []
        for o in orders:
            items_cnt = db.query(OrderItem).filter(OrderItem.order_id == o.id).count()
            result.append({
                "id": o.id,
                "client_name": o.delivery_name or (o.client.name if o.client else "Клиент"),
                "phone": o.delivery_phone or (o.client.phone_number if o.client else "N/A"),
                "total_price": o.total_price,
                "status": o.status,
                "is_paid": o.is_paid,
                "delivery_time": o.delivery_time or "не указано",
                "delivery_address": o.delivery_address or "не указан",
                "items_count": items_cnt,
            })
        return result


# ──────────────────────────────────────────────
# Настройка логирования
# ──────────────────────────────────────────────

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logger = logging.getLogger("tg_admin_bot")


# ──────────────────────────────────────────────
# Загрузка конфигурации
# ──────────────────────────────────────────────

BOT_TOKEN: str = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()

# Поддержка одного или нескольких ADMIN_ID (через запятую)
RAW_ADMIN_IDS: str = os.getenv("ADMIN_ID", "").strip()
ADMIN_IDS: List[int] = []

if RAW_ADMIN_IDS:
    for item in RAW_ADMIN_IDS.split(","):
        item = item.strip()
        if item.isdigit():
            ADMIN_IDS.append(int(item))

# ID бизнеса по умолчанию (если у админа несколько)
DEFAULT_BUSINESS_ID: int = int(os.getenv("DEFAULT_BUSINESS_ID", "1"))


# ──────────────────────────────────────────────
# Middleware проверки прав доступа (Admin Guard)
# ──────────────────────────────────────────────

class AdminAccessMiddleware(BaseMiddleware):
    """
    Мидлварь безопасности.
    Проверяет, входит ли ID пользователя в список ADMIN_IDS.
    Если нет — вежливо отказывает в доступе.
    """

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user: Optional[types.User] = data.get("event_from_user")

        if not user:
            return await handler(event, data)

        if ADMIN_IDS and user.id not in ADMIN_IDS:
            logger.warning("Попытка несанкционированного доступа от user_id=%s (@%s)", user.id, user.username)

            refusal_text = (
                "⛔ <b>Доступ ограничен</b>\n\n"
                f"Ваш Telegram ID (<code>{user.id}</code>) не найден в списке администраторов.\n"
                "Обратитесь к владельцу системы Sanaq AI для получения доступа."
            )

            if isinstance(event, types.Message):
                await event.answer(refusal_text, parse_mode=ParseMode.HTML)
            elif isinstance(event, types.CallbackQuery):
                await event.answer("⛔ У вас нет прав доступа.", show_alert=True)
            return None

        return await handler(event, data)


# ──────────────────────────────────────────────
# Вспомогательные синхронные функции для БД
# ──────────────────────────────────────────────

def get_or_create_default_business(db_session) -> Business:
    """Возвращает существующий или создает тестовый бизнес."""
    b = db_session.query(Business).filter(Business.id == DEFAULT_BUSINESS_ID).first()
    if not b:
        b = Business(
            id=DEFAULT_BUSINESS_ID,
            name="Мой Бизнес (Sanaq AI)",
            phone="+77000000000",
        )
        db_session.add(b)
        db_session.commit()
        db_session.refresh(b)
    return b


def db_get_products(business_id: int) -> List[Dict[str, Any]]:
    """Получает список товаров бизнеса из БД."""
    with SessionLocal() as db:
        products = (
            db.query(Product)
            .filter(Product.business_id == business_id)
            .order_by(Product.id.asc())
            .all()
        )
        return [
            {
                "id": p.id,
                "article": p.article or f"Арт. #{p.id}",
                "name": p.name,
                "category": p.category or "Общее",
                "price": p.price,
                "discount_price": p.discount_price,
                "stock": p.stock,
                "description": p.description,
                "promotion_info": p.promotion_info,
            }
            for p in products
        ]


def db_add_product(business_id: int, name: str, price: float, stock: int) -> Product:
    """Добавляет новый товар в БД."""
    with SessionLocal() as db:
        get_or_create_default_business(db)
        new_product = Product(
            business_id=business_id,
            name=name,
            price=price,
            stock=stock,
        )
        db.add(new_product)
        db.commit()
        db.refresh(new_product)
        return new_product


def db_update_price(business_id: int, product_id: int, new_price: float) -> Optional[Product]:
    """Обновляет цену товара."""
    with SessionLocal() as db:
        p = db.query(Product).filter(Product.id == product_id, Product.business_id == business_id).first()
        if p:
            p.price = new_price
            db.commit()
            db.refresh(p)
        return p


def db_update_stock(business_id: int, product_id: int, new_stock: int) -> Optional[Product]:
    """Обновляет остаток товара на складе."""
    with SessionLocal() as db:
        p = db.query(Product).filter(Product.id == product_id, Product.business_id == business_id).first()
        if p:
            p.stock = new_stock
            db.commit()
            db.refresh(p)
        return p


# ──────────────────────────────────────────────
# Обработчики команд бота
# ──────────────────────────────────────────────

dp = Dispatcher()
dp.message.middleware(AdminAccessMiddleware())
dp.callback_query.middleware(AdminAccessMiddleware())


@dp.message(Command("start", "help"))
async def cmd_start(message: types.Message):
    """Приветственное сообщение и панель управления."""
    welcome_text = (
        "💼 <b>Панель Администратора Sanaq AI</b>\n\n"
        "Добро пожаловать! Вы можете управлять товарами, заказами и аналитикой вашего бизнеса.\n\n"
        "📋 <b>Доступные команды:</b>\n"
        "• /orders — Просмотреть список последних заказов\n"
        "• /products — Просмотреть список всех товаров\n"
        "• /add <i>[Название] [Цена] [Остаток]</i> — Добавить товар\n"
        "  <i>Пример: /add Красные Розы 1500 50</i>\n\n"
        "• /update <i>[ID_товара] [Новая_цена]</i> — Изменить цену\n"
        "  <i>Пример: /update 1 1800</i>\n\n"
        "• /stock <i>[ID_товара] [Новый_остаток]</i> — Изменить остаток\n"
        "  <i>Пример: /stock 1 30</i>\n\n"
        "📊 <b>Аналитика и менеджмент:</b>\n"
        "• /report — Дневной отчёт продаж (сегодня)\n"
        "• /report <i>[YYYY-MM-DD]</i> — Отчёт за конкретную дату\n"
        "  <i>Пример: /report 2026-08-17</i>\n\n"
        "• /resolve <i>[ID_клиента]</i> — Снять эскалацию с клиента\n"
        "  <i>Пример: /resolve 42</i>\n"
    )
    await message.answer(welcome_text, parse_mode=ParseMode.HTML)


@dp.message(Command("orders"))
async def cmd_orders(message: types.Message):
    """Вывод списка последних заказов бизнеса."""
    orders = await asyncio.to_thread(db_get_orders, DEFAULT_BUSINESS_ID)

    if not orders:
        await message.answer("🛍 <b>Список заказов пуст.</b>", parse_mode=ParseMode.HTML)
        return

    lines = ["🛍 <b>Последние заказы бизнеса:</b>\n"]
    for o in orders:
        paid_status = "✅ Оплачен" if o["is_paid"] else "⏳ Ожидает оплаты"
        lines.append(
            f"📦 <b>Заказ #{o['id']}</b> ({paid_status})\n"
            f"👤 Получатель: <b>{o['client_name']}</b> ({o['phone']})\n"
            f"📍 Адрес: {o['delivery_address']} | ⏰ Время: {o['delivery_time']}\n"
            f"💰 Сумма: <b>{o['total_price']} тг.</b> (Позиций: {o['items_count']})\n"
        )

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@dp.message(Command("products"))
async def cmd_products(message: types.Message):
    """Вывод списка товаров из базы данных."""
    products = await asyncio.to_thread(db_get_products, DEFAULT_BUSINESS_ID)

    if not products:
        await message.answer(
            "📦 <b>Каталог товаров пуст.</b>\nДобавьте первый товар командой:\n<code>/add Название Цена Остаток</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    text_lines = ["📦 <b>Список товаров бизнеса:</b>\n"]
    for p in products:
        stock_status = f"<b>{p['stock']} шт.</b>" if p['stock'] > 0 else "❌ <i>нет в наличии</i>"
        text_lines.append(
            f"🔹 <b>ID {p['id']}</b> | <b>{p['name']}</b>\n"
            f"   💰 Цена: <code>{p['price']} тг.</code> | 📊 Остаток: {stock_status}\n"
        )

    await message.answer("\n".join(text_lines), parse_mode=ParseMode.HTML)


@dp.message(Command("add"))
async def cmd_add_product(message: types.Message):
    """
    Быстрое добавление нового товара.
    Формат: /add Название товара Цена Остаток
    Пример: /add Букет тюльпанов 4500 15
    """
    args = message.text.split()[1:]
    if len(args) < 3:
        await message.answer(
            "⚠️ <b>Некорректный формат команды!</b>\n\n"
            "Использование: <code>/add [Название] [Цена] [Остаток]</code>\n"
            "<i>Пример: /add Букет Роз 12000 10</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        stock = int(args[-1])
        price = float(args[-2])
        name = " ".join(args[:-2]).strip()

        if price < 0 or stock < 0 or not name:
            raise ValueError("Негативные числа или пустое имя")

    except ValueError:
        await message.answer(
            "❌ <b>Ошибка валидации!</b>\n"
            "Убедитесь, что цена и остаток — неотрицательные числа.",
            parse_mode=ParseMode.HTML,
        )
        return

    new_prod = await asyncio.to_thread(db_add_product, DEFAULT_BUSINESS_ID, name, price, stock)

    await message.answer(
        f"✅ <b>Товар успешно добавлен!</b>\n\n"
        f"🆔 ID: <code>{new_prod.id}</code>\n"
        f"🏷 Наименование: <b>{new_prod.name}</b>\n"
        f"💰 Цена: <code>{new_prod.price} тг.</code>\n"
        f"📊 Остаток: <code>{new_prod.stock} шт.</code>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("update"))
async def cmd_update_price(message: types.Message):
    """
    Обновление цены товара.
    Формат: /update ID Новая_Цена
    Пример: /update 1 1800
    """
    args = message.text.split()[1:]
    if len(args) != 2:
        await message.answer(
            "⚠️ <b>Некорректный формат команды!</b>\n\n"
            "Использование: <code>/update [ID_товара] [Новая_цена]</code>\n"
            "<i>Пример: /update 1 1800</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        product_id = int(args[0])
        new_price = float(args[1])
        if new_price < 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ <b>ID и цена должны быть корректными положительными числами!</b>", parse_mode=ParseMode.HTML)
        return

    product = await asyncio.to_thread(db_update_price, DEFAULT_BUSINESS_ID, product_id, new_price)

    if not product:
        await message.answer(f"❌ Товар с ID <code>{product_id}</code> не найден.", parse_mode=ParseMode.HTML)
        return

    await message.answer(
        f"✅ <b>Цена товара обновлена!</b>\n\n"
        f"🏷 <b>{product.name}</b> (ID {product.id})\n"
        f"💰 Новая цена: <code>{product.price} тг.</code>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("stock"))
async def cmd_update_stock(message: types.Message):
    """
    Обновление количества товара на складе.
    Формат: /stock ID Новый_Остаток
    Пример: /stock 1 50
    """
    args = message.text.split()[1:]
    if len(args) != 2:
        await message.answer(
            "⚠️ <b>Некорректный формат команды!</b>\n\n"
            "Использование: <code>/stock [ID_товара] [Новый_остаток]</code>\n"
            "<i>Пример: /stock 1 50</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        product_id = int(args[0])
        new_stock = int(args[1])
        if new_stock < 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ <b>ID и остаток должны быть корректными положительными целыми числами!</b>", parse_mode=ParseMode.HTML)
        return

    product = await asyncio.to_thread(db_update_stock, DEFAULT_BUSINESS_ID, product_id, new_stock)

    if not product:
        await message.answer(f"❌ Товар с ID <code>{product_id}</code> не найден.", parse_mode=ParseMode.HTML)
        return

    await message.answer(
        f"✅ <b>Остаток товара обновлен!</b>\n\n"
        f"🏷 <b>{product.name}</b> (ID {product.id})\n"
        f"📊 Новый остаток: <code>{product.stock} шт.</code>",
        parse_mode=ParseMode.HTML,
    )


@dp.message(Command("report"))
async def cmd_report(message: types.Message):
    """
    Дневной отчёт аналитики продаж.
    Формат: /report [YYYY-MM-DD]  (по умолчанию — сегодня)
    """
    from datetime import date as date_type, datetime as dt_type

    args = message.text.split()[1:]
    target_date = None

    if args:
        try:
            target_date = dt_type.strptime(args[0], "%Y-%m-%d").date()
        except ValueError:
            await message.answer(
                "⚠️ <b>Некорректный формат даты!</b>\n\n"
                "Использование: <code>/report [YYYY-MM-DD]</code>\n"
                "<i>Пример: /report 2026-08-17</i>\n\n"
                "Без аргумента — отчёт за сегодня.",
                parse_mode=ParseMode.HTML,
            )
            return

    await message.answer("⏳ <i>Генерирую отчёт...</i>", parse_mode=ParseMode.HTML)

    def _build_report():
        with SessionLocal() as db:
            analytics = get_daily_analytics(db, DEFAULT_BUSINESS_ID, target_date)
        return format_daily_report(analytics)

    report_text = await asyncio.to_thread(_build_report)
    await message.answer(report_text, parse_mode=ParseMode.HTML)


@dp.message(Command("resolve"))
async def cmd_resolve(message: types.Message):
    """
    Снять флаг эскалации с клиента (вернуть его ИИ-менеджеру).
    Формат: /resolve [ID_клиента]
    """
    args = message.text.split()[1:]
    if not args:
        await message.answer(
            "⚠️ <b>Укажите ID клиента!</b>\n\n"
            "Использование: <code>/resolve [ID_клиента]</code>\n"
            "<i>Пример: /resolve 42</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        client_id = int(args[0])
    except ValueError:
        await message.answer("❌ <b>ID клиента должен быть числом!</b>", parse_mode=ParseMode.HTML)
        return

    def _resolve_client():
        with SessionLocal() as db:
            client = (
                db.query(Client)
                .filter(Client.id == client_id, Client.business_id == DEFAULT_BUSINESS_ID)
                .first()
            )
            if not client:
                return None, "not_found"
            if not client.needs_human:
                return client, "not_escalated"
            client.needs_human = False
            client.escalation_reason = None
            client.escalated_at = None
            db.commit()
            return client, "resolved"

    client, status = await asyncio.to_thread(_resolve_client)

    if status == "not_found":
        await message.answer(
            f"❌ Клиент с ID <code>{client_id}</code> не найден.",
            parse_mode=ParseMode.HTML,
        )
    elif status == "not_escalated":
        await message.answer(
            f"ℹ️ Клиент <b>{client.name}</b> (ID {client.id}) "
            f"не находится в режиме эскалации. Всё в порядке!",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.answer(
            f"✅ <b>Эскалация снята!</b>\n\n"
            f"👤 Клиент: <b>{client.name}</b> (ID {client.id})\n"
            f"📞 Телефон: <code>{client.phone_number}</code>\n\n"
            f"ИИ-менеджер снова обрабатывает сообщения этого клиента.",
            parse_mode=ParseMode.HTML,
        )


# ──────────────────────────────────────────────
# Инлайн-кнопки управления оплатой по реквизитам
# ──────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pay_confirm:"))
async def cb_confirm_payment(callback: types.CallbackQuery):
    """Обработка кнопки '✅ Подтвердить оплату' администратором."""
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("❌ Ошибка формата данных.", show_alert=True)
        return

    order_id = int(parts[1])
    shop_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else DEFAULT_BUSINESS_ID

    def _do_confirm():
        with SessionLocal() as db:
            return confirm_manual_payment(db, order_id=order_id, shop_id=shop_id)

    success, msg = await asyncio.to_thread(_do_confirm)

    if success:
        await callback.answer("✅ Оплата успешно подтверждена!", show_alert=True)
        # Обновляем текст сообщения админа
        new_text = (
            f"{callback.message.text}\n\n"
            f"✅ <b>ОПЛАТА ПОДТВЕРЖДЕНА АДМИНИСТРАТОРОМ</b>\n"
            f"<i>Клиент уведомлен: 'Оплата прошла, ваш заказ скоро будет готов.'</i>"
        )
        try:
            await callback.message.edit_text(new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Не удалось обновить сообщение callback админу: %s", e)
    else:
        await callback.answer(f"❌ {msg}", show_alert=True)


@dp.callback_query(F.data.startswith("pay_reject:"))
async def cb_reject_payment(callback: types.CallbackQuery):
    """Обработка кнопки '❌ Отклонить' администратором."""
    parts = callback.data.split(":")
    if len(parts) < 2:
        await callback.answer("❌ Ошибка формата данных.", show_alert=True)
        return

    order_id = int(parts[1])
    shop_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else DEFAULT_BUSINESS_ID

    def _do_reject():
        with SessionLocal() as db:
            return reject_manual_payment(db, order_id=order_id, shop_id=shop_id)

    success, msg = await asyncio.to_thread(_do_reject)

    if success:
        await callback.answer("❌ Оплата отклонена", show_alert=True)
        # Обновляем текст сообщения админа
        new_text = (
            f"{callback.message.text}\n\n"
            f"❌ <b>ОПЛАТА ОТКЛОНЕНА АДМИНИСТРАТОРОМ</b>\n"
            f"<i>Клиент уведомлен об отклонении перевода.</i>"
        )
        try:
            await callback.message.edit_text(new_text, reply_markup=None, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Не удалось обновить сообщение callback админу: %s", e)
    else:
        await callback.answer(f"❌ {msg}", show_alert=True)


# ──────────────────────────────────────────────
# Главная точка входа для запуска бота
# ──────────────────────────────────────────────

async def main():
    """Инициализирует БД и запускает поллинг Telegram-бота."""
    # Инициализация таблиц БД при необходимости
    Base.metadata.create_all(bind=engine)

    if not BOT_TOKEN or BOT_TOKEN in ("123456789:ABCdefGHIjklMNOpqrsTUVwxyz", "your_telegram_bot_token_here"):
        logger.error(
            "ОШИБКА: TELEGRAM_BOT_TOKEN / BOT_TOKEN не указан или содержит шаблонное значение в файле .env! "
            "Укажите ваш настоящий токен бота от @BotFather."
        )
        print("\n[!] TELEGRAM_BOT_TOKEN не настроен. Укажите реальный токен в файле .env.")
        return

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    logger.info("Запуск Telegram-бота администратора Sanaq AI...")
    if ADMIN_IDS:
        logger.info("Авторизованные ADMIN_ID: %s", ADMIN_IDS)
    else:
        logger.warning("ADMIN_ID не задан в .env! Доступ свободен для всех клиентов.")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот администратора остановлен.")
