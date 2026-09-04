"""
kaspi_payment.py -- Модуль автоматической оплаты через Kaspi QR / Kaspi Pay.

Функции:
  1. generate_kaspi_pay_link() -- Генерация ссылки/данных Kaspi QR для мгновенной оплаты заказа.
  2. process_kaspi_webhook_payment() -- Обработка автоматического Callback/Webhook от Kaspi Pay при успешной оплате.
"""

import logging
from typing import Dict, Any, Tuple, Optional
from sqlalchemy.orm import Session

from models import Order, Business, Client
from payments import confirm_manual_payment

logger = logging.getLogger(__name__)


def generate_kaspi_pay_link(
    order_id: int,
    amount: float,
    shop_id: int = 1,
    service_name: str = "Оплата заказа Sanaq AI",
) -> Dict[str, Any]:
    """
    Генерирует ссылку на Kaspi Pay QR и метаданные платежа.

    Args:
        order_id: ID заказа.
        amount: Сумма к оплате.
        shop_id: ID магазина (бизнеса).
        service_name: Описание услуги/товара.

    Returns:
        Dict: Метаданные платежа с QR-ссылкой и кодом заказа.
    """
    # Kaspi Pay ссылка с диплинком для приложения Kaspi.kz
    pay_url = f"https://kaspi.kz/pay/sanaq_ai?order_id={order_id}&amount={int(amount)}&shop_id={shop_id}"
    qr_code_data = f"ST00012|Name=SanaqAI|PersonalAcc={order_id}|Sum={int(amount * 100)}|PaymPeriod=092026"

    return {
        "order_id": order_id,
        "amount": amount,
        "currency": "KZT",
        "kaspi_pay_url": pay_url,
        "qr_code_data": qr_code_data,
        "instruction": (
            f"📱 <b>Оплата через Kaspi.kz:</b>\n"
            f"1. Перейдите по ссылке: {pay_url}\n"
            f"2. Или отсканируйте QR-код в приложении Kaspi в разделе 'Kaspi QR'.\n"
            f"3. Сумма к оплате: <b>{amount:,.0f} тг.</b>"
        ),
    }


def process_kaspi_webhook_payment(
    db: Session,
    txn_id: str,
    order_id: int,
    amount: float,
    shop_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Обрабатывает входящий вебхук успешной оплаты от Kaspi Pay API.

    1. Находит заказ по order_id.
    2. Проверяет совпадение суммы.
    3. Подтверждает заказ (status = 'paid', is_paid = True).
    4. Возвращает статус обработки.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        logger.error("Kaspi Webhook: Заказ #%s не найден в БД", order_id)
        return False, f"Заказ #{order_id} не найден."

    if order.is_paid:
        logger.info("Kaspi Webhook: Заказ #%s уже отмечен как оплаченный.", order_id)
        return True, f"Заказ #{order_id} уже был оплачен ранее."

    if abs(order.total_price - amount) > 1.0:
        logger.warning(
            "Kaspi Webhook: Сумма оплаты (%s тг) не совпадает с суммой заказа #%s (%s тг)",
            amount, order_id, order.total_price,
        )
        return False, f"Сумма платежа ({amount} тг) не соответствует сумме заказа ({order.total_price} тг)."

    # Используем подтверждение из системы платежей
    target_shop_id = shop_id or order.shop_id or order.business_id
    success, msg = confirm_manual_payment(db, order_id=order_id, shop_id=target_shop_id)

    if success:
        logger.info("✅ Kaspi Pay: Заказ #%s успешно автоматически оплачен! TxnID: %s", order_id, txn_id)
        return True, f"Заказ #{order_id} успешно оплачен через Kaspi Pay (Транзакция: {txn_id})."
    else:
        logger.error("❌ Ошибка подтверждения платежа Kaspi для заказа #%s: %s", order_id, msg)
        return False, msg
