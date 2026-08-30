"""
test_messengers_router.py -- Тестирование модуля messengers_router (WhatsApp & Instagram Direct).
"""

import asyncio
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from config import settings
from database import init_db, SessionLocal
from models import Business, Product, Client, ChatMessage
from messengers_router import send_reply_to_messenger, parse_whatsapp_payload, parse_instagram_payload

client = TestClient(app)


def setup_test_database():
    """Подготовка тестовых данных в БД."""
    init_db()
    with SessionLocal() as db:
        b = db.query(Business).filter(Business.id == 1).first()
        if not b:
            b = Business(id=1, name="Sanaq Flower Shop", phone="+77071112233", api_key_ai="sk-test-key")
            db.add(b)
            db.commit()

        # Очистим старые товары бизнеса 1 для чистых тестов
        db.query(Product).filter((Product.business_id == 1) | (Product.shop_id == 1)).delete()
        db.commit()

        p1 = Product(
            shop_id=1,
            business_id=1,
            name="Красные Розы 101",
            price=25000.0,
            stock=10,
            description="Букет из 101 свежей красной розы",
        )
        db.add(p1)
        db.commit()


def test_instagram_meta_verification():
    """1. Тест верификации токена Meta для Instagram Direct (GET /webhook/instagram)."""
    verify_token = settings.WEBHOOK_VERIFY_TOKEN
    response = client.get(
        "/webhook/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": verify_token,
            "hub.challenge": "123456789",
        },
    )
    assert response.status_code == 200
    assert response.text == "123456789"
    print("[OK] Верификация токена Meta для Instagram пройдена!")


def test_whatsapp_meta_verification():
    """2. Тест верификации токена Meta для WhatsApp (GET /webhook/whatsapp)."""
    verify_token = settings.WEBHOOK_VERIFY_TOKEN
    response = client.get(
        "/webhook/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": verify_token,
            "hub.challenge": "987654321",
        },
    )
    assert response.status_code == 200
    assert response.text == "987654321"
    print("[OK] Верификация токена Meta для WhatsApp пройдена!")


def test_whatsapp_incoming_webhook():
    """3. Тест приема сообщения из WhatsApp (POST /webhook/whatsapp) и обработки ИИ."""
    setup_test_database()

    mock_ai = MagicMock()
    mock_ai.status_code = 200
    mock_ai.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Здравствуйте! Букет Красные Розы 101 стоит 25000 тг и есть в наличии! 🌸"
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_ai), patch("messengers_router.send_reply_to_messenger", return_value=True):
        payload = {
            "phone": "77071112233",
            "message": "Здравствуйте! Сколько стоит 101 красная роза?",
        }
        response = client.post("/webhook/whatsapp", json=payload)
        if response.status_code != 200:
            print("WA Error 422 detail:", response.json())
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["channel"] == "whatsapp"
        assert data["sender_id"] == "77071112233"
        assert "25000 тг" in data["reply"]
        assert data["sent_to_client"] is True

    print("[OK] Вебхук WhatsApp успешно обработан и ответ сгенерирован ИИ!")


def test_instagram_incoming_webhook():
    """4. Тест приема сообщения из Instagram Direct (POST /webhook/instagram) и обработки ИИ."""
    setup_test_database()

    mock_ai = MagicMock()
    mock_ai.status_code = 200
    mock_ai.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Приветствуем в Instagram Direct! 🌹 Букет в наличии."
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_ai), patch("messengers_router.send_reply_to_messenger", return_value=True):
        # Тестируем Meta Graph API payload формат
        payload = {
            "entry": [
                {
                    "messaging": [
                        {
                            "sender": {"id": "INSTA_USER_999"},
                            "message": {"text": "Привет! Букет роза бар?", "attachments": []},
                        }
                    ]
                }
            ]
        }
        response = client.post("/webhook/instagram", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["channel"] == "instagram"
        assert data["sender_id"] == "INSTA_USER_999"
        assert "Instagram Direct" in data["reply"]
        assert data["sent_to_client"] is True

    print("[OK] Вебхук Instagram Direct успешно распарсен, обработан ИИ и отправлен!")


def test_universal_sending_function():
    """5. Тест универсальной функции отправки send_reply_to_messenger."""
    async def run_sending_test():
        # Мокаем httpx запрос для WhatsApp
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            res_wa = await send_reply_to_messenger("whatsapp", "77071112233", "Тестовый ответ в WA")
            assert res_wa is True

        # Мокаем httpx запрос для Instagram Direct
        with patch.object(settings, "INSTAGRAM_PAGE_ACCESS_TOKEN", "mock_token_123"):
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                res_ig = await send_reply_to_messenger("instagram", "INSTA_123", "Тестовый ответ в IG")
                assert res_ig is True

        # Мокаем httpx запрос для Telegram
        with patch.object(settings, "TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"):
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                res_tg = await send_reply_to_messenger("telegram", "888777666", "Тестовый ответ в Telegram")
                assert res_tg is True

    asyncio.run(run_sending_test())
    print("[OK] Универсальная функция отправки сообщений (WA, IG, TG) работает корректно!")


def test_telegram_incoming_webhook():
    """6. Тест приема входящего Update от Telegram (POST /webhook/telegram) и ИИ-ответа."""
    setup_test_database()

    mock_ai = MagicMock()
    mock_ai.status_code = 200
    mock_ai.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Здравствуйте, Арман! Букет Красные Розы 101 стоит 25000 тг 🌸"
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_ai), patch("messengers_router.send_reply_to_messenger", return_value=True):
        tg_update = {
            "update_id": 100001,
            "message": {
                "message_id": 500,
                "from": {"id": 888777666, "first_name": "Арман", "last_name": "Каримов"},
                "chat": {"id": 888777666, "type": "private"},
                "date": 1690000000,
                "text": "Здравствуйте! Сколько стоят красные розы?",
            },
        }

        response = client.post("/webhook/telegram", json=tg_update)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["channel"] == "telegram"
        assert data["sender_id"] == "888777666"
        assert "Красные Розы 101" in data["reply"]
        assert data["sent_to_client"] is True

    print("[OK] Вебхук Telegram (Update JSON) успешно принят, обработан ИИ и отправлен!")


def test_telegram_set_webhook_registration():
    """7. Тест авто-регистрации Telegram setWebhook."""
    from messengers_router import register_telegram_webhook

    async def run_set_webhook_test():
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "result": True, "description": "Webhook was set"}

        with patch.object(settings, "TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"), \
             patch.object(settings, "WEBHOOK_URL", "https://sanaq-ai.example.com"), \
             patch("httpx.AsyncClient.post", return_value=mock_response):

            res = await register_telegram_webhook()
            assert res is True

    asyncio.run(run_set_webhook_test())
    print("[OK] Автоматическая регистрация Telegram setWebhook прошла успешно!")


if __name__ == "__main__":
    test_instagram_meta_verification()
    test_whatsapp_meta_verification()
    test_whatsapp_incoming_webhook()
    test_instagram_incoming_webhook()
    test_universal_sending_function()
    test_telegram_incoming_webhook()
    test_telegram_set_webhook_registration()
    print("\n[SUCCESS] Все 7 тестов messengers_router.py успешно пройдены!")

