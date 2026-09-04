"""
test_new_modules.py -- Юнит-тесты для 4 новых модулей Sanaq AI:
  1. db_backup.py (SQLite online backup & Telegram export)
  2. heartbeat.py (Server Uptime Heartbeat & Health metrics)
  3. kaspi_payment.py (Kaspi QR generation & Webhook confirmation)
  4. dashboard.py & main.py endpoints (Web Dashboard HTML & Stats API)
"""

import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from database import SessionLocal, engine, Base
from models import Business, Product, Order, Client, OrderItem
from main import app
from db_backup import create_db_backup, send_db_backup_to_telegram
from heartbeat import get_system_health_metrics, send_uptime_heartbeat
from kaspi_payment import generate_kaspi_pay_link, process_kaspi_webhook_payment


class TestNewModules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(bind=engine)
        cls.client = TestClient(app)

    def setUp(self):
        self.db = SessionLocal()
        # Создаем тестовый бизнес и заказ
        b = self.db.query(Business).filter(Business.id == 1).first()
        if not b:
            b = Business(id=1, name="Test Business", phone="+77000000000")
            self.db.add(b)
            self.db.commit()

        c = self.db.query(Client).filter(Client.id == 1).first()
        if not c:
            c = Client(id=1, business_id=1, shop_id=1, name="Тест Клиент", phone_number="+77071112233")
            self.db.add(c)
            self.db.commit()

        o = self.db.query(Order).filter(Order.id == 999).first()
        if not o:
            o = Order(
                id=999,
                business_id=1,
                shop_id=1,
                client_id=1,
                total_price=5000.0,
                status="new",
                is_paid=False,
                delivery_name="Тест Клиент",
                delivery_phone="+77071112233",
            )
            self.db.add(o)
            self.db.commit()

    def tearDown(self):
        self.db.close()

    # ── 1. Тесты db_backup.py ─────────────────────────────
    def test_create_db_backup(self):
        backup_path = create_db_backup(db_path="sanaq.db", backup_dir="test_backups")
        self.assertTrue(os.path.exists(backup_path))
        self.assertTrue(os.path.getsize(backup_path) > 0)
        # Очистка
        if os.path.exists(backup_path):
            os.remove(backup_path)

    @patch("db_backup.requests.post")
    def test_send_db_backup_to_telegram(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = send_db_backup_to_telegram(db_path="sanaq.db", target_chat_id="-4433809117")
        self.assertTrue(result)

    # ── 2. Тесты heartbeat.py ─────────────────────────────
    def test_get_system_health_metrics(self):
        metrics = get_system_health_metrics()
        self.assertEqual(metrics["status"], "🟢 ONLINE")
        self.assertIn("uptime", metrics)
        self.assertGreaterEqual(metrics["businesses_count"], 1)

    @patch("heartbeat.requests.post")
    def test_send_uptime_heartbeat(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ok": True}
        mock_post.return_value = mock_resp

        result = send_uptime_heartbeat(target_chat_id="-4433809117")
        self.assertTrue(result)

    # ── 3. Тесты kaspi_payment.py ─────────────────────────
    def test_generate_kaspi_pay_link(self):
        res = generate_kaspi_pay_link(order_id=999, amount=5000.0, shop_id=1)
        self.assertEqual(res["order_id"], 999)
        self.assertEqual(res["amount"], 5000.0)
        self.assertIn("https://kaspi.kz/pay/", res["kaspi_pay_url"])

    def test_process_kaspi_webhook_payment(self):
        success, msg = process_kaspi_webhook_payment(
            db=self.db,
            txn_id="KASPI_TXN_12345",
            order_id=999,
            amount=5000.0,
            shop_id=1,
        )
        self.assertTrue(success)
        # Проверяем, что заказ переведен в статус paid
        o = self.db.query(Order).filter(Order.id == 999).first()
        self.assertTrue(o.is_paid)
        self.assertEqual(o.status, "paid")

    # ── 4. Тесты Dashboard HTML & Stats API ───────────────
    def test_dashboard_html_endpoint(self):
        res = self.client.get("/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Sanaq AI Dashboard", res.text)
        self.assertIn("text/html", res.headers["content-type"])

    def test_dashboard_stats_api(self):
        res = self.client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("summary", data)
        self.assertIn("recent_orders", data)

    # ── 5. Тесты эндпоинтов main.py ───────────────────────
    @patch("main.send_db_backup_to_telegram")
    def test_admin_backup_endpoint(self, mock_backup):
        res = self.client.post("/admin/backup")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "success")

    @patch("main.async_send_uptime_heartbeat")
    def test_health_heartbeat_endpoint(self, mock_heartbeat):
        res = self.client.post("/health/heartbeat")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "success")

    def test_kaspi_pay_details_endpoint(self):
        res = self.client.get("/payments/kaspi/999")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["order_id"], 999)

    def test_kaspi_pay_webhook_endpoint(self):
        # Создаем заказ 998
        o = self.db.query(Order).filter(Order.id == 998).first()
        if not o:
            o = Order(id=998, business_id=1, shop_id=1, client_id=1, total_price=3500.0, status="new", is_paid=False)
            self.db.add(o)
            self.db.commit()

        res = self.client.post("/webhook/kaspi", json={"txn_id": "TXN_777", "order_id": 998, "amount": 3500.0})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "success")


if __name__ == "__main__":
    unittest.main()
