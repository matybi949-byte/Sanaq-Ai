"""
test_bot.py -- Тест работоспособности функций и логики Telegram-бота.
"""

from database import engine, Base, SessionLocal
from models import Business, Product
from tg_admin_bot import db_add_product, db_get_products, db_update_price, db_update_stock, DEFAULT_BUSINESS_ID

def test_bot_functions():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    # 1. Добавление товара
    p1 = db_add_product(DEFAULT_BUSINESS_ID, "Букет Пионов", 15000.0, 10)
    assert p1.id is not None
    assert p1.name == "Букет Пионов"
    print(f"[1] Добавлен товар #{p1.id}: {p1.name} - {p1.price} тг (остаток {p1.stock})")

    # 2. Получение списка
    prods = db_get_products(DEFAULT_BUSINESS_ID)
    assert len(prods) == 1
    print(f"[2] Получен список товаров (всего {len(prods)})")

    # 3. Обновление цены
    updated_p1 = db_update_price(DEFAULT_BUSINESS_ID, p1.id, 17500.0)
    assert updated_p1.price == 17500.0
    print(f"[3] Обновлена цена товара #{p1.id}: новая цена {updated_p1.price} тг")

    # 4. Обновление остатка
    updated_stock = db_update_stock(DEFAULT_BUSINESS_ID, p1.id, 25)
    assert updated_stock.stock == 25
    print(f"[4] Обновлен остаток товара #{p1.id}: новый остаток {updated_stock.stock} шт")

    print("\n[OK] Все модульные функции бота работают отлично!")

if __name__ == "__main__":
    test_bot_functions()
