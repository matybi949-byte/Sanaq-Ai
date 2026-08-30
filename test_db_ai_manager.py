"""
test_db_ai_manager.py -- Тесты работы ИИ-менеджера с моделями БД, остатками, ценами, акциями и подбором альтернатив.
"""

import os
from unittest.mock import patch, MagicMock

from database import Base, engine, SessionLocal, init_db
from models import Business, Product, Client, ChatMessage
from ai_service import (
    fetch_business_catalog,
    build_system_prompt,
    find_alternative_products,
    get_ai_response_with_intent,
)


def setup_db_data():
    """Создаёт тестовый бизнес и товары различных категорий с разным наличием и акциями."""
    init_db()
    with SessionLocal() as db:
        # Очищаем или подготавливаем тестовый бизнес #100
        b = db.query(Business).filter(Business.id == 100).first()
        if not b:
            b = Business(id=100, name="Flower Boutique 100", phone="+77001002030", api_key_ai="sk-test-100")
            db.add(b)
            db.commit()

        # Удаляем существующие товары бизнеса #100
        db.query(Product).filter(Product.business_id == 100).delete()
        db.commit()

        # Добавляем 5 товаров
        # 1. Товар не в наличии (stock == 0) - Категория "Букеты"
        p1 = Product(
            business_id=100,
            article="Арт. 12",
            name="Букет 101 Красная Роза",
            category="Букеты",
            price=25000.0,
            discount_price=None,
            stock=0,
            description="Шикарный большой букет красных роз",
            promotion_info=None,
        )
        # 2. Альтернатива 1 в наличии - Категория "Букеты" (со скидкой)
        p2 = Product(
            business_id=100,
            article="Арт. 15",
            name="Букет 51 Белая Роза",
            category="Букеты",
            price=18000.0,
            discount_price=15000.0,
            stock=5,
            description="Нежный букет белых роз",
            promotion_info="Скидка 16% до воскресенья",
        )
        # 3. Альтернатива 2 в наличии - Категория "Букеты"
        p3 = Product(
            business_id=100,
            article="Арт. 18",
            name="Букет Микс Тюльпанов",
            category="Букеты",
            price=12000.0,
            discount_price=None,
            stock=10,
            description="Яркие весенние тюльпаны",
            promotion_info=None,
        )
        # 4. Альтернатива 3 в наличии - Категория "Букеты"
        p4 = Product(
            business_id=100,
            article="Арт. 20",
            name="Букет Пионов",
            category="Букеты",
            price=22000.0,
            discount_price=20000.0,
            stock=3,
            description="Ароматные розовые пионы",
            promotion_info="Акция месяца!",
        )
        # 5. Товар в другой категории "Подарки"
        p5 = Product(
            business_id=100,
            article="Арт. 99",
            name="Мягкая Игрушка Мишка",
            category="Подарки",
            price=8000.0,
            discount_price=None,
            stock=8,
            description="Большой плюшевый мишка",
            promotion_info=None,
        )

        db.add_all([p1, p2, p3, p4, p5])
        db.commit()


def test_find_alternative_products():
    """Проверяет поиск до 3 альтернативных товаров из той же категории."""
    setup_db_data()
    with SessionLocal() as db:
        alts = find_alternative_products(
            db=db,
            business_id=100,
            category="Букеты",
            exclude_product_id=None,
            limit=3,
        )
        assert len(alts) == 3
        # Все 3 должны иметь stock > 0
        for alt in alts:
            assert alt["stock"] > 0
            assert alt["category"] == "Букеты"

        print("[OK] test_find_alternative_products пройден")


def test_fetch_business_catalog_with_alternatives():
    """Проверяет подгрузку каталога бизнеса с автоматической привзякой альтернатив к распроданному товару."""
    setup_db_data()
    with SessionLocal() as db:
        b_name, products, api_key = fetch_business_catalog(db, 100)
        assert b_name == "Flower Boutique 100"
        assert len(products) == 5

        # Товар p1 (stock == 0) должен содержать ровно 3 альтернативы
        p1_data = next(p for p in products if p["article"] == "Арт. 12")
        assert p1_data["stock"] == 0
        assert len(p1_data["alternatives"]) == 3
        # Ни одна из альтернатив не должна быть p1
        alt_articles = [alt["article"] for alt in p1_data["alternatives"]]
        assert "Арт. 12" not in alt_articles
        assert "Арт. 15" in alt_articles
        assert "Арт. 18" in alt_articles
        assert "Арт. 20" in alt_articles

        print("[OK] test_fetch_business_catalog_with_alternatives пройден")


def test_build_system_prompt_structure():
    """Проверяет формирование системного промпта с ценами, акциями и блоком альтернатив."""
    setup_db_data()
    with SessionLocal() as db:
        b_name, products, _ = fetch_business_catalog(db, 100)
        prompt = build_system_prompt(b_name, products, business_id=100)

        # Проверяем ключевые фрагменты в промпте
        assert "Арт. 12" in prompt
        assert "❌ РАСПРОДАНО (0 шт.)" in prompt
        assert "АКЦИОННАЯ ЦЕНА (СКИДКА): 15000.0 тг." in prompt
        assert "Скидка 16% до воскресенья" in prompt
        assert "💡 АЛЬТЕРНАТИВЫ ИЗ БД ДЛЯ ПРЕДЛОЖЕНИЯ КЛИЕНТУ:" in prompt
        assert "РАБОТА С РАСПРОДАННЫМИ БУКЕТАМИ И АЛЬТЕРНАТИВАМИ" in prompt

        print("[OK] test_build_system_prompt_structure пройден")


def test_ai_response_with_intent_multimodal():
    """Тест взаимодействия ИИ с моком сетевого запроса к OpenAI API."""
    setup_db_data()

    mock_ai_resp = MagicMock()
    mock_ai_resp.status_code = 200
    mock_ai_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        "К сожалению, Букет 101 Красная Роза (Арт. 12) временно распродан.\n"
                        "Предлагаю отличные альтернативы из категории 'Букеты':\n"
                        "1) Букет 51 Белая Роза (Арт. 15) — 15000 тг (акция!)\n"
                        "2) Букет Микс Тюльпанов (Арт. 18) — 12000 тг\n"
                        "3) Букет Пионов (Арт. 20) — 20000 тг"
                    )
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_ai_resp):
        with SessionLocal() as db:
            b_name, products, api_key = fetch_business_catalog(db, 100)
            reply, order_items = get_ai_response_with_intent(
                user_message="Хочу заказать Арт. 12 (Красные розы)",
                business_name=b_name,
                products=products,
                chat_history=[],
                api_key=api_key,
                business_id=100,
            )

            assert "распродан" in reply or "альтернативы" in reply.lower()
            assert "Арт. 15" in reply
            assert "Арт. 18" in reply
            assert "Арт. 20" in reply
            assert order_items is None

    print("[OK] test_ai_response_with_intent_multimodal пройден")


if __name__ == "__main__":
    test_find_alternative_products()
    test_fetch_business_catalog_with_alternatives()
    test_build_system_prompt_structure()
    test_ai_response_with_intent_multimodal()
    print("\n[SUCCESS] Все тесты ИИ-менеджера и альтернатив БД успешно пройдены!")
