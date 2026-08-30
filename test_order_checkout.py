"""
test_order_checkout.py -- Комплексный тест пошагового оформления заказа и оплаты.

Проверяет:
1. Автоматический запуск пошагового оформления при намерении купить.
2. Последовательный сбор данных: Имя -> Телефон -> Время -> Адрес.
3. Генерацию ссылки на оплату (Kaspi Pay).
4. Эндпоинт/функцию подтверждения оплаты, списание stock и отправку Telegram-уведомления.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

from database import SessionLocal, init_db, engine, Base
from models import Business, Product, Client, Order, OrderItem, ChatMessage
from orders import (
    initiate_step_by_step_checkout,
    process_checkout_step,
    confirm_order_payment,
    generate_payment_link,
    send_admin_order_notification,
)
from webhook_router import handle_incoming_webhook, WebhookIncomingMessage, webhook_confirm_payment


class TestOrderCheckoutFlow(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        # Очищаем тестовые данные
        self.db.query(OrderItem).delete()
        self.db.query(Order).delete()
        self.db.query(ChatMessage).delete()
        self.db.query(Client).delete()
        self.db.query(Product).delete()
        self.db.query(Business).delete()
        self.db.commit()

        # Создаем тестовый бизнес
        self.business = Business(
            id=1,
            name="Тестовый Салон Цветов",
            api_key_ai="mock_key",
        )
        self.db.add(self.business)

        # Создаем товар
        self.product = Product(
            id=10,
            business_id=1,
            article="FL-01",
            name="Букет 51 Белая Роза",
            category="Букеты",
            price=15000.0,
            discount_price=13500.0,
            stock=10,
        )
        self.db.add(self.product)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_payment_link_generation(self):
        link = generate_payment_link(order_id=42, total_price=13500.0, business_name="Тестовый Салон Цветов")
        self.assertIn("kaspi.kz", link)
        self.assertIn("order_id=42", link)
        self.assertIn("amount=13500", link)

    def test_step_by_step_checkout_direct(self):
        # 1. Инициализация заказа
        items = [{"product_id": 10, "quantity": 1}]
        order, prompt = initiate_step_by_step_checkout(
            db=self.db,
            business_id=1,
            phone_number="+77071112233",
            items=items,
        )
        self.assertIsNotNone(order)
        self.assertEqual(order.status, "pending_checkout")
        self.assertEqual(order.checkout_step, "awaiting_name")
        self.assertEqual(order.total_price, 13500.0)
        self.assertIn("Шаг 1 из 4", prompt)

        # 2. Шаг 1: Ввод Имя
        res_name = process_checkout_step(self.db, order, "Нурсултан")
        self.assertEqual(order.delivery_name, "Нурсултан")
        self.assertEqual(order.checkout_step, "awaiting_phone")
        self.assertIn("Шаг 2 из 4", res_name)

        # 3. Шаг 2: Ввод Телефона
        res_phone = process_checkout_step(self.db, order, "+77071112233")
        self.assertEqual(order.delivery_phone, "+77071112233")
        self.assertEqual(order.checkout_step, "awaiting_time")
        self.assertIn("Шаг 3 из 4", res_phone)

        # 4. Шаг 3: Ввод Времени
        res_time = process_checkout_step(self.db, order, "Сегодня к 19:00")
        self.assertEqual(order.delivery_time, "Сегодня к 19:00")
        self.assertEqual(order.checkout_step, "awaiting_address")
        self.assertIn("Шаг 4 из 4", res_time)

        # 5. Шаг 4: Ввод Адреса
        res_addr = process_checkout_step(self.db, order, "г. Алматы, пр. Абая 150")
        self.assertEqual(order.delivery_address, "г. Алматы, пр. Абая 150")
        self.assertIn(order.checkout_step, ("awaiting_payment", "awaiting_payment_confirmation"))
        self.assertIsNotNone(order.payment_link)
        self.assertIn("успешно сформирован", res_addr)

    @patch("orders.requests.post")
    def test_payment_confirmation_and_stock_reduction(self, mock_requests_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_requests_post.return_value = mock_response

        # Имитируем оформление
        items = [{"product_id": 10, "quantity": 2}]
        order, _ = initiate_step_by_step_checkout(
            db=self.db,
            business_id=1,
            phone_number="+77071112233",
            items=items,
        )
        process_checkout_step(self.db, order, "Алексей")
        process_checkout_step(self.db, order, "+77071112233")
        process_checkout_step(self.db, order, "Завтра в 12:00")
        process_checkout_step(self.db, order, "ул. Достык 50, кв 12")

        self.assertEqual(self.product.stock, 10)  # Списание происходит при подтверждении оплаты

        # Подтверждение оплаты
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123:ABC", "ADMIN_ID": "999888"}):
            pay_result = confirm_order_payment(self.db, order.id)

        self.assertTrue(pay_result.success)
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, "paid")
        self.assertEqual(order.checkout_step, "completed")
        self.assertEqual(self.product.stock, 8)  # 10 - 2 = 8

        # Проверяем вызов отправки уведомления в Telegram
        self.assertTrue(mock_requests_post.called)

    @patch("webhook_router.get_ai_response_with_intent")
    def test_webhook_step_by_step_integration(self, mock_ai_intent):
        from webhook_router import process_unified_message

        # Имитируем определение ИИ намерения покупки
        mock_ai_intent.return_value = (
            "Конечно, я оформлю ваш заказ!",
            [{"product_id": 10, "quantity": 1}],
        )

        phone = "+77019998877"

        # 1. Первый запрос вебхука (клиент хочет купить)
        payload1 = WebhookIncomingMessage(phone_number=phone, message="Хочу купить 51 белую розу")
        resp1 = process_unified_message(business_id=1, payload=payload1, db=self.db)

        self.assertTrue(resp1.order_created)
        self.assertIn("Шаг 1 из 4", resp1.reply)
        order_id = resp1.order_details.order_id

        # 2. Второй запрос (клиент вводит имя)
        payload2 = WebhookIncomingMessage(phone_number=phone, message="Динара")
        resp2 = process_unified_message(business_id=1, payload=payload2, db=self.db)
        self.assertIn("Шаг 2 из 4", resp2.reply)
        self.assertIn("Динара", resp2.reply)

        # 3. Третий запрос (клиент вводит телефон)
        payload3 = WebhookIncomingMessage(phone_number=phone, message="+77019998877")
        resp3 = process_unified_message(business_id=1, payload=payload3, db=self.db)
        self.assertIn("Шаг 3 из 4", resp3.reply)

        # 4. Четвертый запрос (время)
        payload4 = WebhookIncomingMessage(phone_number=phone, message="Сегодня к 15:00")
        resp4 = process_unified_message(business_id=1, payload=payload4, db=self.db)
        self.assertIn("Шаг 4 из 4", resp4.reply)

        # 5. Пятый запрос (адрес)
        payload5 = WebhookIncomingMessage(phone_number=phone, message="мкр. Самал-2, д 15")
        resp5 = process_unified_message(business_id=1, payload=payload5, db=self.db)
        self.assertIn("Реквизиты для оплаты", resp5.reply)
        self.assertIn("успешно сформирован", resp5.reply)

        # 6. Проверяем состояние заказа в БД
        order_db = self.db.query(Order).filter(Order.id == order_id).first()
        self.assertEqual(order_db.delivery_name, "Динара")
        self.assertEqual(order_db.delivery_phone, "+77019998877")
        self.assertEqual(order_db.delivery_time, "Сегодня к 15:00")
        self.assertEqual(order_db.delivery_address, "мкр. Самал-2, д 15")
        self.assertFalse(order_db.is_paid)

        # 7. Подтверждаем оплату через функцию подтверждения оплаты
        pay_res = confirm_order_payment(db=self.db, order_id=order_id, shop_id=1)
        self.assertTrue(pay_res.success)
        self.assertTrue(order_db.is_paid)
        self.assertEqual(order_db.status, "paid")


if __name__ == "__main__":
    unittest.main()
