"""
test_multimodal.py -- Тестирование функций Vision, Whisper и единого потока обработки сообщений.
"""

import os
import tempfile
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from ai_service import (
    analyze_image_for_article,
    transcribe_voice,
    prepare_incoming_message,
)
from main import app
from database import Base, engine, SessionLocal
from models import Business, Client, Product


def test_analyze_image_for_article_success():
    """Тест извлечения артикула со скриншота через Vision API."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Арт. 12"
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_response):
        res = analyze_image_for_article("https://example.com/story_screenshot.jpg", api_key="sk-test")
        assert res == "Арт. 12"
    print("[OK] test_analyze_image_for_article_success пройден")


def test_analyze_image_for_article_not_found():
    """Тест сценария, когда артикул не найден на скриншоте."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "НЕ НАЙДЕНО"
                }
            }
        ]
    }

    with patch("requests.post", return_value=mock_response):
        res = analyze_image_for_article("https://example.com/story_screenshot.jpg", api_key="sk-test")
        assert res is None
    print("[OK] test_analyze_image_for_article_not_found пройден")


def test_transcribe_voice_success():
    """Тест расшифровки голосового сообщения через Whisper API."""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(b"fake audio bytes")
        tmp_path = tmp.name

    try:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Здравствуйте, хочу заказать роз 15 штук"}

        with patch("requests.post", return_value=mock_response):
            text = transcribe_voice(tmp_path, api_key="sk-test")
            assert text == "Здравствуйте, хочу заказать роз 15 штук"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    print("[OK] test_transcribe_voice_success пройден")


def test_prepare_incoming_message_combined():
    """Тест мультимодального объединения текста, голосового и изображения."""
    with patch("ai_service.transcribe_voice", return_value="Здравствуйте, есть в наличии?"):
        with patch("ai_service.analyze_image_for_article", return_value="Арт. 12"):
            combined = prepare_incoming_message(
                message="Привет!",
                image_url="https://example.com/img.jpg",
                audio_path="fake.mp3",
                api_key="sk-test",
            )
            assert "Привет!" in combined
            assert "[Голосовое сообщение]: Здравствуйте, есть в наличии?" in combined
            assert "[Артикул со скриншота: Арт. 12]" in combined
    print("[OK] test_prepare_incoming_message_combined пройден")


def test_webhook_integration_multimodal():
    """Тест полного потока вебхука с мультимодальными данными."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        b = db.query(Business).filter(Business.id == 99).first()
        if not b:
            b = Business(id=99, name="Test Flowers", phone="+77071112233", api_key_ai="sk-test")
            db.add(b)
            p = Product(business_id=99, name="Пионы", price=5000, stock=10)
            db.add(p)
            db.commit()

    client = TestClient(app)

    mock_ai_resp = MagicMock()
    mock_ai_resp.status_code = 200
    mock_ai_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Здравствуйте! Товар Арт. 12 (Пионы) есть в наличии по цене 5000 тг."
                }
            }
        ]
    }

    with patch("ai_service.analyze_image_for_article", return_value="Арт. 12"):
        with patch("requests.post", return_value=mock_ai_resp):
            resp = client.post(
                "/webhook/99",
                json={
                    "phone_number": "+77019998877",
                    "image_url": "https://example.com/story.png",
                    "client_name": "Анна",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "success"
            assert "Арт. 12" in data["reply"]
    print("[OK] test_webhook_integration_multimodal пройден")


if __name__ == "__main__":
    test_analyze_image_for_article_success()
    test_analyze_image_for_article_not_found()
    test_transcribe_voice_success()
    test_prepare_incoming_message_combined()
    test_webhook_integration_multimodal()
    print("\n[SUCCESS] Все мультимодальные тесты успешно пройдены!")
