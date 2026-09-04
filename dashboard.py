"""
dashboard.py -- Веб-панель управления и аналитики Sanaq AI (HTML + REST API).

Эндпоинты:
  1. GET /dashboard -- Интерактивная веб-панель администратора.
  2. GET /api/dashboard/stats -- REST API метрик в формате JSON.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import get_db, SessionLocal
from models import Order, Product, Client, Business, OrderItem
from heartbeat import get_system_health_metrics, SERVER_START_TIME
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Web Dashboard"])


@router.get("/api/dashboard/stats", response_class=JSONResponse)
def get_dashboard_stats(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Возвращает полную статистику и список последних заказов в формате JSON.
    """
    system_metrics = get_system_health_metrics()

    # Сводка по заказам
    total_orders_count = db.query(func.count(Order.id)).scalar() or 0
    paid_orders_count = db.query(func.count(Order.id)).filter(Order.status.in_(("paid", "completed"))).scalar() or 0
    total_revenue = float(db.query(func.coalesce(func.sum(Order.total_price), 0.0)).filter(Order.status.in_(("paid", "completed"))).scalar() or 0.0)
    
    products_count = db.query(func.count(Product.id)).scalar() or 0

    # Последние 15 заказов
    recent_orders_db = db.query(Order).order_by(Order.id.desc()).limit(15).all()
    recent_orders = []
    for o in recent_orders_db:
        recent_orders.append({
            "id": o.id,
            "client_name": o.delivery_name or (o.client.name if o.client else "Не указано"),
            "phone": o.delivery_phone or (o.client.phone_number if o.client else "N/A"),
            "total_price": o.total_price,
            "status": o.status,
            "is_paid": o.is_paid,
            "delivery_address": o.delivery_address or "Не указан",
            "created_at": o.created_at.strftime("%Y-%m-%d %H:%M:%S") if o.created_at else "",
        })

    return {
        "status": "success",
        "system": system_metrics,
        "summary": {
            "total_orders": total_orders_count,
            "paid_orders": paid_orders_count,
            "total_revenue": total_revenue,
            "total_products": products_count,
        },
        "recent_orders": recent_orders,
    }


@router.get("/dashboard", response_class=HTMLResponse)
def render_dashboard():
    """
    Отрисовывает главную веб-панель управления Sanaq AI с визуализацией карточек и графиков.
    """
    html_content = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sanaq AI -- Панель Мониторинга и Аналитики</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #0f172a;
            --card-bg: rgba(30, 41, 59, 0.7);
            --card-border: rgba(255, 255, 255, 0.08);
            --accent-purple: #8b5cf6;
            --accent-blue: #3b82f6;
            --accent-green: #10b981;
            --accent-pink: #ec4899;
            --text-main: #f8fafc;
            --text-sub: #94a3b8;
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', sans-serif;
            background: radial-gradient(circle at top left, #1e1b4b, #0f172a 60%);
            color: var(--text-main);
            min-height: 100vh;
            padding: 2rem;
        }

        .container {
            max-width: 1280px;
            margin: 0 auto;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            padding-bottom: 1rem;
            border-bottom: 1px solid var(--card-border);
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }

        .brand-logo {
            width: 42px;
            height: 42px;
            background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue));
            border-radius: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 1.3rem;
            box-shadow: 0 0 20px rgba(139, 92, 246, 0.4);
        }

        .brand-title h1 {
            font-size: 1.5rem;
            font-weight: 700;
            letter-spacing: -0.5px;
        }

        .brand-title p {
            font-size: 0.85rem;
            color: var(--text-sub);
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            padding: 0.5rem 1rem;
            border-radius: 20px;
            border: 1px solid rgba(16, 185, 129, 0.3);
            font-size: 0.875rem;
            font-weight: 600;
        }

        .pulse-dot {
            width: 8px;
            height: 8px;
            background-color: var(--accent-green);
            border-radius: 50%;
            box-shadow: 0 0 10px var(--accent-green);
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 10px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        .grid-cards {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2.5rem;
        }

        .card {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 1.5rem;
            transition: transform 0.2s ease, border-color 0.2s ease;
        }

        .card:hover {
            transform: translateY(-4px);
            border-color: rgba(255, 255, 255, 0.2);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            color: var(--text-sub);
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 0.75rem;
        }

        .card-value {
            font-size: 2rem;
            font-weight: 800;
            letter-spacing: -0.5px;
        }

        .card-subtext {
            margin-top: 0.5rem;
            font-size: 0.8rem;
            color: var(--text-sub);
        }

        .section-title {
            font-size: 1.2rem;
            font-weight: 700;
            margin-bottom: 1rem;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .table-container {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            overflow: hidden;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
            font-size: 0.9rem;
        }

        th {
            background: rgba(15, 23, 42, 0.6);
            color: var(--text-sub);
            padding: 1rem 1.25rem;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 0.75rem;
            letter-spacing: 0.5px;
        }

        td {
            padding: 1rem 1.25rem;
            border-bottom: 1px solid var(--card-border);
            color: var(--text-main);
        }

        tr:last-child td {
            border-bottom: none;
        }

        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }

        .badge-paid {
            background: rgba(16, 185, 129, 0.2);
            color: #34d399;
            padding: 0.25rem 0.65rem;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .badge-pending {
            background: rgba(245, 158, 11, 0.2);
            color: #fbbf24;
            padding: 0.25rem 0.65rem;
            border-radius: 12px;
            font-size: 0.75rem;
            font-weight: 600;
        }

        .refresh-btn {
            background: linear-gradient(135deg, var(--accent-purple), var(--accent-blue));
            color: white;
            border: none;
            padding: 0.6rem 1.2rem;
            border-radius: 10px;
            font-weight: 600;
            cursor: pointer;
            transition: opacity 0.2s ease;
        }

        .refresh-btn:hover {
            opacity: 0.9;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="brand">
                <div class="brand-logo">S</div>
                <div class="brand-title">
                    <h1>Sanaq AI Dashboard</h1>
                    <p>Панель Аналитики & Мониторинга Сервера</p>
                </div>
            </div>
            <div style="display: flex; align-items: center; gap: 1rem;">
                <div class="status-badge">
                    <span class="pulse-dot"></span>
                    <span id="system-status">ONLINE</span>
                </div>
                <button class="refresh-btn" onclick="loadStats()">Обновить</button>
            </div>
        </header>

        <div class="grid-cards">
            <div class="card">
                <div class="card-header">Общая Выручка</div>
                <div class="card-value" id="val-revenue">0 ₸</div>
                <div class="card-subtext" id="sub-revenue">Оплаченные заказы</div>
            </div>
            <div class="card">
                <div class="card-header">Всего Заказов</div>
                <div class="card-value" id="val-orders">0</div>
                <div class="card-subtext" id="sub-orders">За весь период</div>
            </div>
            <div class="card">
                <div class="card-header">Клиенты в Базе</div>
                <div class="card-value" id="val-clients">0</div>
                <div class="card-subtext">Уникальные пользователи</div>
            </div>
            <div class="card">
                <div class="card-header">Время работы (Uptime)</div>
                <div class="card-value" id="val-uptime">0ч 0м</div>
                <div class="card-subtext" id="val-model">Модель: gpt-5.6-luna</div>
            </div>
        </div>

        <h2 class="section-title">📦 Последние Заказы</h2>
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>Клиент</th>
                        <th>Телефон</th>
                        <th>Сумма</th>
                        <th>Статус</th>
                        <th>Адрес Доставки</th>
                        <th>Дата</th>
                    </tr>
                </thead>
                <tbody id="orders-table-body">
                    <tr><td colspan="7" style="text-align: center; color: var(--text-sub);">Загрузка данных...</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <script>
        async function loadStats() {
            try {
                const res = await fetch('/api/dashboard/stats');
                const data = await res.json();

                if (data.status === 'success') {
                    document.getElementById('val-revenue').innerText = `${data.summary.total_revenue.toLocaleString()} ₸`;
                    document.getElementById('val-orders').innerText = data.summary.total_orders;
                    document.getElementById('val-clients').innerText = data.system.clients_count;
                    document.getElementById('val-uptime').innerText = data.system.uptime;
                    document.getElementById('val-model').innerText = `Модель: ${data.system.ai_model}`;
                    document.getElementById('system-status').innerText = data.system.status;

                    const tbody = document.getElementById('orders-table-body');
                    tbody.innerHTML = '';

                    if (data.recent_orders.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="7" style="text-align: center;">Заказов пока нет</td></tr>';
                        return;
                    }

                    data.recent_orders.forEach(o => {
                        const tr = document.createElement('tr');
                        const statusBadge = o.is_paid 
                            ? '<span class="badge-paid">✅ Оплачен</span>' 
                            : '<span class="badge-pending">⏳ В обработке</span>';

                        tr.innerHTML = `
                            <td><b>#${o.id}</b></td>
                            <td>${o.client_name}</td>
                            <td><code>${o.phone}</code></td>
                            <td><b>${o.total_price.toLocaleString()} ₸</b></td>
                            <td>${statusBadge}</td>
                            <td>${o.delivery_address}</td>
                            <td>${o.created_at}</td>
                        `;
                        tbody.appendChild(tr);
                    });
                }
            } catch (err) {
                console.error('Ошибка загрузки статистики:', err);
            }
        }

        // Загрузка при открытии и автообновление каждые 30 секунд
        loadStats();
        setInterval(loadStats, 30000);
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
