"""
test_fault_tolerance_validation.py -- Комплексный тест отказоустойчивости и валидации.

Проверяет:
 1. ai_service.py: обработку RateLimitError, APIConnectionError, Timeout с возвратом фоллбэка на ru/kk и логированием в app.log.
 2. models.py / main.py / orders.py: строгие Pydantic-модели (user_id, message_text, phone, items, quantity > 0).
 3. main.py: автоматический возврат ошибки HTTP 422 при битом JSON и невалидных данных.
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from main import app
from models import (
    OrderItemValidation,
    WebhookPayloadValidation,
    OrderDataValidation,
)
from ai_service import (
    get_ai_response_with_intent,
    get_ai_response,
    get_fallback_message,
    RateLimitError,
    APIConnectionError,
)
from pydantic import ValidationError


class TestFaultToleranceAndValidation(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    # ──────────────────────────────────────────────
    # Тест 1: AI Service Fault Tolerance (RateLimitError, APIConnectionError, Timeout)
    # ──────────────────────────────────────────────

    @patch("ai_service.requests.post")
    def test_ai_service_ratelimit_error_fallback(self, mock_post):
        """Проверяет обработку RateLimitError (HTTP 429) с вежливым фоллбэком."""
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "1"}
        mock_post.return_value = mock_response

        # Вызов функции
        reply, items = get_ai_response_with_intent(
            user_message="Привет, есть букеты?",
            business_name="Цветочный Рай",
            products=[],
            chat_history=[],
            api_key="sk-fake-key",
            business_id=1,
        )

        self.assertIsNone(items)
        self.assertIn("Небольшая техническая заминка, уже чиним!", reply)

    @patch("ai_service.requests.post")
    def test_ai_service_connection_error_fallback(self, mock_post):
        """Проверяет обработку ошибки соединения APIConnectionError."""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")

        reply, items = get_ai_response_with_intent(
            user_message="Сәлеметсіз бе, гүлдер бар ма?",
            business_name="Гүлдер әлемі",
            products=[],
            chat_history=[],
            api_key="sk-fake-key",
            business_id=1,
        )

        self.assertIsNone(items)
        self.assertIn("Шағын техникалық ақаулық, қазір жөндеп жатырмыз!", reply)

    @patch("ai_service.requests.post")
    def test_ai_service_timeout_fallback(self, mock_post):
        """Проверяет обработку таймаута запроса к OpenAI."""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("Read timeout")

        reply = get_ai_response(
            user_message="Есть ли роза?",
            business_name="Цветочный Рай",
            products=[],
            chat_history=[],
            api_key="sk-fake-key",
            business_id=1,
        )

        self.assertIn("Небольшая техническая заминка, уже чиним!", reply)

    # ──────────────────────────────────────────────
    # Тест 2: Pydantic Validation Models (quantity > 0, aliases, non-empty)
    # ──────────────────────────────────────────────

    def test_order_item_validation_valid(self):
        """Проверяет успешную валидацию корректной позиции заказа."""
        item = OrderItemValidation(product_name="Красные розы", quantity=3)
        self.assertEqual(item.product_name, "Красные розы")
        self.assertEqual(item.quantity, 3)

    def test_order_item_validation_zero_quantity_raises(self):
        """Проверяет, что quantity = 0 вызывает ошибку валидации."""
        with self.assertRaises(ValidationError):
            OrderItemValidation(product_name="Розы", quantity=0)

    def test_order_item_validation_negative_quantity_raises(self):
        """Проверяет, что negative quantity вызывает ошибку валидации."""
        with self.assertRaises(ValidationError):
            OrderItemValidation(product_name="Розы", quantity=-5)

    def test_order_item_validation_bool_quantity_raises(self):
        """Проверяет, что boolean quantity (True/False) вызывает ошибку валидации."""
        with self.assertRaises(ValidationError):
            OrderItemValidation(product_name="Розы", quantity=True)

    def test_webhook_payload_validation_aliases(self):
        """Проверяет поддержку алиасов в WebhookPayloadValidation."""
        payload_data = {
            "client_id": 42,
            "message": "Хочу заказать тюльпаны",
            "phone_number": "+77011234567",
            "items": [{"product_name": "Тюльпаны", "quantity": 10}],
        }
        model = WebhookPayloadValidation(**payload_data)
        self.assertEqual(model.user_id, 42)
        self.assertEqual(model.message_text, "Хочу заказать тюльпаны")
        self.assertEqual(model.phone, "+77011234567")
        self.assertEqual(len(model.items), 1)
        self.assertEqual(model.items[0].quantity, 10)

    def test_order_data_validation_valid(self):
        """Проверяет модель OrderDataValidation."""
        order_data = {
            "user_id": 10,
            "phone": "+77771234567",
            "message_text": "Срочная доставка",
            "items": [{"product_name": "Букет 101 роза", "quantity": 1}],
        }
        order = OrderDataValidation(**order_data)
        self.assertEqual(order.user_id, 10)
        self.assertEqual(order.phone, "+77771234567")
        self.assertEqual(order.items[0].quantity, 1)

    # ──────────────────────────────────────────────
    # Тест 3: FastAPI Automated 422 Response (Malformed JSON / Validation Error)
    # ──────────────────────────────────────────────

    def test_http_422_on_malformed_json(self):
        """Проверяет возврат HTTP 422 при синтаксически битом JSON."""
        response = self.client.post(
            "/chat",
            content='{"user_id": 1, "message": "Привет", ',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data.get("status"), "error")
        self.assertIn("detail", data)

    def test_http_422_on_invalid_quantity(self):
        """Проверяет возврат HTTP 422 при передаче quantity <= 0 в эндпоинт создания заказа."""
        invalid_payload = {
            "business_id": 1,
            "phone_number": "+77011234567",
            "client_name": "Иван",
            "items": [
                {"product_name": "Розы", "quantity": 0}
            ],
        }
        response = self.client.post("/orders", json=invalid_payload)
        self.assertEqual(response.status_code, 422)
        data = response.json()
        self.assertEqual(data.get("status"), "error")


if __name__ == "__main__":
    unittest.main()
