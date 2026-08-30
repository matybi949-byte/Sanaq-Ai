"""
test_payments.py -- Тестирование модуля ручной оплаты по реквизитам.
"""

import os
import unittest
from unittest.mock import patch, MagicMock

from database import SessionLocal, init_db
from models import Business, Product, Client, Order, OrderItem, ChatMessage
from payments import (
    get_shop_payment_details,
    format_client_requisites_text,
    send_admin_payment_card,
    process_manual_payment_request,
    confirm_manual_payment,
    reject_manual_payment,
    UNIVERSAL_PAYMENT_SUCCESS_REPLY,
    UNIVERSAL_PAYMENT_REJECTED_REPLY,
)


class TestPaymentsModule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()

        # Очищаем тестовые данные
        self.db.query(ChatMessage).delete()
        self.db.query(OrderItem).delete()
        self.db.query(Order).delete()
        self.db.query(Client).delete()
        self.db.query(Product).delete()
        self.db.query(Business).delete()
        self.db.commit()

        # Создаем тестовый бизнес
        self.business = Business(
            id=1,
            name="Тестовый Салон Цветов",
            phone="+77001112233",
            settings='{"payment_requisites": {"bank_name": "Halyk Bank", "card_or_phone": "+7 (777) 999-88-77", "recipient_name": "ИП Цветы"}}',
        )
        self.db.add(self.business)

        # Создаем клиента
        self.client = Client(
            id=10,
            shop_id=1,
            business_id=1,
            phone_number="+77071234567",
            name="Анара",
        )
        self.db.add(self.client)

        # Создаем товар
        self.product = Product(
            id=100,
            shop_id=1,
            business_id=1,
            name="Букет 25 Роз",
            price=12000.0,
            stock=15,
        )
        self.db.add(self.product)
        self.db.commit()

        # Создаем тестовый заказ
        self.order = Order(
            id=50,
            shop_id=1,
            business_id=1,
            client_id=10,
            total_price=12000.0,
            status="pending_checkout",
            checkout_step="awaiting_address",
            delivery_name="Анара",
            delivery_phone="+77071234567",
            delivery_time="Завтра в 15:00",
            delivery_address="ул. Достык 10",
        )
        self.db.add(self.order)
        self.db.commit()

        self.order_item = OrderItem(
            id=500,
            shop_id=1,
            order_id=50,
            product_id=100,
            product_name="Букет 25 Роз",
            quantity=1,
            unit_price=12000.0,
            line_total=12000.0,
        )
        self.db.add(self.order_item)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_1_get_shop_payment_details(self):
        """Проверка извлечения банковских реквизитов из business.settings."""
        reqs = get_shop_payment_details(self.db, shop_id=1)
        self.assertEqual(reqs["bank_name"], "Halyk Bank")
        self.assertEqual(reqs["card_or_phone"], "+7 (777) 999-88-77")
        self.assertEqual(reqs["recipient_name"], "ИП Цветы")

    def test_2_format_client_requisites_text(self):
        """Проверка форматирования текста реквизитов для клиента."""
        reqs = get_shop_payment_details(self.db, shop_id=1)
        text = format_client_requisites_text(self.order, reqs)
        self.assertIn("Halyk Bank", text)
        self.assertIn("+7 (777) 999-88-77", text)
        self.assertIn("12000.0 тг.", text)

    @patch("payments.requests.post")
    def test_3_send_admin_payment_card_with_inline_buttons(self, mock_post):
        """Проверка формирования и отправки карточки с инлайн-кнопками в Telegram-бот админа."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "123456:TestToken", "ADMIN_ID": "777888"}):
            sent = send_admin_payment_card(self.order, self.db)

        self.assertTrue(sent)
        mock_post.assert_called_once()

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], 777888)
        self.assertIn("ЗАПРОС РУЧНОЙ ОПЛАТЫ ПО РЕКВИЗИТАМ", payload["text"])

        # Проверяем наличие инлайн-кнопок
        reply_markup = payload["reply_markup"]
        self.assertIn("inline_keyboard", reply_markup)
        buttons = reply_markup["inline_keyboard"][0]
        self.assertEqual(buttons[0]["text"], "✅ Подтвердить оплату")
        self.assertEqual(buttons[0]["callback_data"], "pay_confirm:50:1")
        self.assertEqual(buttons[1]["text"], "❌ Отклонить")
        self.assertEqual(buttons[1]["callback_data"], "pay_reject:50:1")

    @patch("payments.requests.post")
    def test_4_confirm_manual_payment_and_universal_notification(self, mock_post):
        """Проверка подтверждения оплаты администратором, списания товара и отправки сообщения клиенту."""
        success, msg = confirm_manual_payment(self.db, order_id=50, shop_id=1)

        self.assertTrue(success)
        self.assertTrue(self.order.is_paid)
        self.assertEqual(self.order.status, "paid")

        # Проверяем уменьшение остатка
        updated_prod = self.db.query(Product).filter(Product.id == 100).first()
        self.assertEqual(updated_prod.stock, 14)  # 15 - 1 = 14

        # Проверяем сохранение универсального уведомления клиенту в ChatMessage
        client_msg = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.client_id == 10, ChatMessage.role == "assistant")
            .order_by(ChatMessage.id.desc())
            .first()
        )
        self.assertIsNotNone(client_msg)
        self.assertEqual(client_msg.message, UNIVERSAL_PAYMENT_SUCCESS_REPLY)
        self.assertIn("Оплата прошла, ваш заказ скоро будет готов.", client_msg.message)

    def test_5_reject_manual_payment(self):
        """Проверка отклонения оплаты администратором."""
        success, msg = reject_manual_payment(self.db, order_id=50, shop_id=1)

        self.assertTrue(success)
        self.assertEqual(self.order.status, "cancelled")

        # Проверяем сообщение клиенту об отклонении
        client_msg = (
            self.db.query(ChatMessage)
            .filter(ChatMessage.client_id == 10, ChatMessage.role == "assistant")
            .order_by(ChatMessage.id.desc())
            .first()
        )
        self.assertIsNotNone(client_msg)
        self.assertEqual(client_msg.message, UNIVERSAL_PAYMENT_REJECTED_REPLY)


if __name__ == "__main__":
    unittest.main()
