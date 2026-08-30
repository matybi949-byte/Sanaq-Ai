"""
safety.py -- Модуль безопасности и Human-in-the-loop.

Реализует:
  1. Детекцию агрессии, оскорблений и токсичного поведения клиентов.
  2. Детекцию нестандартных/невалидных запросов, выходящих за рамки компетенции ИИ.
  3. Детекцию неуверенности ИИ (фразы-маркеры галлюцинаций в ответе).
  4. Автоматический перевод чата в режим ожидания менеджера (escalation).
  5. Отправку уведомления администратору в Telegram о необходимости подключения.

Использование в webhook_router.py:
  - Перед отправкой ответа ИИ клиенту вызывается check_safety_escalation().
  - Если сработал триггер, ИИ-ответ заменяется вежливым сообщением о подключении менеджера.
"""

import os
import re
import logging
from typing import Optional, Tuple, List
from enum import Enum

import requests

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Типы эскалации
# ──────────────────────────────────────────────

class EscalationReason(str, Enum):
    """Причина эскалации диалога на живого менеджера."""
    AGGRESSION = "aggression"           # Агрессия / оскорбление / нецензурная лексика
    OFF_TOPIC = "off_topic"             # Нестандартный запрос вне компетенции бота
    AI_UNCERTAIN = "ai_uncertain"       # ИИ не уверен в ответе (маркеры галлюцинаций)
    EXPLICIT_REQUEST = "explicit_request"  # Клиент явно просит живого менеджера
    NONE = "none"


# ──────────────────────────────────────────────
# Паттерны для детекции (русский + казахский)
# ──────────────────────────────────────────────

# Нецензурная / оскорбительная лексика (русский)
_AGGRESSION_PATTERNS_RU = re.compile(
    r"\b("
    r"бл[яеи]д[ьи]?|сук[аи]|пид[оа]р|на\s*х[уюе][йя]|х[уюе][йяе]|"
    r"еб[аоуёл]|мраз[ьи]|тв[аоа]рь|идиот|дебил|уро[дн]|козёл|козел|"
    r"дур[аоеи][кч]?|гавно|гнид[аы]|скотин[аы]|отстой|жаловат|"
    r"ублюд[ок]|пош[её]?л?\s*(на|в|ты)|убей\s*себя|"
    r"заткни|закрой\s*рот|чмо|лох|отвали|задолбал[аи]?|бесиш"
    r")\b",
    re.IGNORECASE,
)

# Оскорбления (казахский)
_AGGRESSION_PATTERNS_KZ = re.compile(
    r"\b("
    r"масқара|намыссыз|сатқын|ит[тк]?ен|обал|надан|"
    r"ақымақ|жынды|тентек|сорлы|арсыз"
    r")\b",
    re.IGNORECASE,
)

# Запросы вне компетенции бота (явные off-topic)
_OFF_TOPIC_PATTERNS = re.compile(
    r"\b("
    r"полити[кч]|выбор[аы]|президент|война|религи[яю]|ислам|христиан|"
    r"депрессия|суицид|убить|наркотик|рецепт\s*(на\s*)?лекарств|"
    r"секс|интим|эротик|порно|"
    r"взлом|хакер|пароль\s*(от|к)|обман|мошен|"
    r"казино|ставк[иа]|лотере[яю]|"
    r"юридическ|адвокат|суд\b|налог\s*(на|ов)|закон\s*(о|ов)"
    r")\b",
    re.IGNORECASE,
)

# Явная просьба «позовите менеджера»
_HUMAN_REQUEST_PATTERNS = re.compile(
    r"("
    r"(позов|подключ|переключ|соедин|свяж)\w*\s*(менеджер|оператор|человек|админ|консультант)|"
    r"(хочу|можно|дай)\s*(живо[йг]о?|реальн\w*)\s*(менеджер|оператор|человек|консультант)|"
    r"(менеджер\w*|оператор\w*)\s*(позов|подключ|нужен|нужна|где)|"
    r"адам\w*\s*(қосыңыз|шақыр|керек|байланыс)|"  # казахский: «подключите человека»
    r"менеджер\w*\s*(қосыңыз|шақыр|керек)"
    r")",
    re.IGNORECASE,
)

# Маркеры неуверенности ИИ (признаки галлюцинации в ответе)
_AI_UNCERTAINTY_MARKERS = re.compile(
    r"("
    r"я\s*не\s*(уверен|знаю|могу\s*подтвердить)|"
    r"к\s*сожалению,?\s*у\s*меня\s*нет\s*(точн|достоверн)|"
    r"возможно,?\s*я\s*(ошиба|не\s*прав)|"
    r"мне\s*сложно\s*(ответить|сказать)|"
    r"рекомендую\s*(уточнить|связаться|обратиться)|"
    r"точных\s*данных\s*(у\s*меня\s*)?нет|"
    r"не\s*располагаю\s*(данн|информац)|"
    r"это\s*за\s*пределами\s*(моих|мо[её]й)"
    r")",
    re.IGNORECASE,
)


# ──────────────────────────────────────────────
# Основной механизм детекции
# ──────────────────────────────────────────────

def detect_escalation_trigger(
    user_message: str,
    ai_response: Optional[str] = None,
) -> Tuple[EscalationReason, str]:
    """
    Анализирует сообщение клиента и (опционально) ответ ИИ на предмет необходимости эскалации.

    Returns:
        Tuple[EscalationReason, str]:
            - Причина эскалации (NONE, если эскалации не нужно).
            - Описание сработавшего триггера (для уведомления админа).
    """
    text = user_message.strip()

    # 1. Проверка: клиент явно просит менеджера
    match = _HUMAN_REQUEST_PATTERNS.search(text)
    if match:
        return EscalationReason.EXPLICIT_REQUEST, f"Клиент явно запросил живого менеджера: «{match.group()[:60]}»"

    # 2. Проверка: агрессия / оскорбления (русский)
    match = _AGGRESSION_PATTERNS_RU.search(text)
    if match:
        return EscalationReason.AGGRESSION, f"Обнаружена агрессия/оскорбление (RU): «{match.group()[:40]}»"

    # 3. Проверка: агрессия / оскорбления (казахский)
    match = _AGGRESSION_PATTERNS_KZ.search(text)
    if match:
        return EscalationReason.AGGRESSION, f"Обнаружена агрессия/оскорбление (KZ): «{match.group()[:40]}»"

    # 4. Проверка: запрос вне компетенции бота
    match = _OFF_TOPIC_PATTERNS.search(text)
    if match:
        return EscalationReason.OFF_TOPIC, f"Запрос вне компетенции бота: «{match.group()[:50]}»"

    # 5. Проверка ответа ИИ на маркеры неуверенности / галлюцинации
    if ai_response:
        match = _AI_UNCERTAINTY_MARKERS.search(ai_response)
        if match:
            return EscalationReason.AI_UNCERTAIN, f"ИИ не уверен в ответе: «{match.group()[:60]}»"

    return EscalationReason.NONE, ""


# ──────────────────────────────────────────────
# Шаблоны вежливых ответов при эскалации
# ──────────────────────────────────────────────

_ESCALATION_REPLIES = {
    EscalationReason.AGGRESSION: (
        "Я понимаю, что ситуация может вызывать эмоции. "
        "Мне важно помочь вам, поэтому я подключаю живого менеджера, "
        "который разберется в вашем вопросе лично. "
        "Пожалуйста, ожидайте — специалист свяжется с вами в ближайшее время! 🙏"
    ),
    EscalationReason.OFF_TOPIC: (
        "Ваш вопрос выходит за рамки моей компетенции, и я не хочу давать вам неточную информацию. "
        "Я перевожу ваш запрос живому менеджеру — он сможет помочь вам гораздо лучше! "
        "Ожидайте, пожалуйста. 🙏"
    ),
    EscalationReason.AI_UNCERTAIN: (
        "Я хочу предоставить вам только проверенную информацию, "
        "поэтому подключаю живого специалиста для точного ответа на ваш вопрос. "
        "Менеджер свяжется с вами в самое ближайшее время! 🙏"
    ),
    EscalationReason.EXPLICIT_REQUEST: (
        "Конечно! Перевожу вас на живого менеджера. "
        "Специалист подключится в ближайшее время. Спасибо за ожидание! 🙏"
    ),
}


def get_escalation_reply(reason: EscalationReason, language: str = "ru") -> str:
    """Возвращает вежливый ответ клиенту при эскалации на живого менеджера."""
    return _ESCALATION_REPLIES.get(reason, _ESCALATION_REPLIES[EscalationReason.AI_UNCERTAIN])


# ──────────────────────────────────────────────
# Отправка уведомления админу в Telegram
# ──────────────────────────────────────────────

def send_escalation_notification(
    reason: EscalationReason,
    trigger_detail: str,
    client_phone: str,
    client_name: str,
    business_name: str,
    business_id: int,
    user_message: str,
) -> bool:
    """
    Отправляет уведомление о необходимости подключения менеджера в Telegram-админ-бот.
    """
    bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
    raw_admin_ids = os.getenv("ADMIN_ID", "").strip()
    admin_ids = [int(i.strip()) for i in raw_admin_ids.split(",") if i.strip().isdigit()]

    if not bot_token or not admin_ids:
        logger.warning("TELEGRAM_BOT_TOKEN / ADMIN_ID не настроены. Уведомление об эскалации не отправлено.")
        return False

    reason_emoji = {
        EscalationReason.AGGRESSION: "🔴",
        EscalationReason.OFF_TOPIC: "🟡",
        EscalationReason.AI_UNCERTAIN: "🟠",
        EscalationReason.EXPLICIT_REQUEST: "🔵",
    }

    reason_label = {
        EscalationReason.AGGRESSION: "Агрессия / Оскорбление",
        EscalationReason.OFF_TOPIC: "Запрос вне компетенции ИИ",
        EscalationReason.AI_UNCERTAIN: "ИИ не уверен в данных",
        EscalationReason.EXPLICIT_REQUEST: "Клиент запросил менеджера",
    }

    emoji = reason_emoji.get(reason, "⚠️")
    label = reason_label.get(reason, "Неизвестная причина")
    truncated_msg = user_message[:300] + ("…" if len(user_message) > 300 else "")

    text = (
        f"{emoji} <b>ТРЕБУЕТСЯ ПОДКЛЮЧЕНИЕ МЕНЕДЖЕРА</b>\n\n"
        f"🏢 <b>Бизнес:</b> {business_name} (ID: {business_id})\n"
        f"👤 <b>Клиент:</b> {client_name}\n"
        f"📞 <b>Телефон:</b> <code>{client_phone}</code>\n\n"
        f"⚠️ <b>Причина:</b> {label}\n"
        f"📎 <b>Триггер:</b> {trigger_detail}\n\n"
        f"💬 <b>Сообщение клиента:</b>\n"
        f"<i>{truncated_msg}</i>\n\n"
        f"👉 Пожалуйста, свяжитесь с клиентом и ответьте вручную!"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    success_count = 0

    for admin_id in admin_ids:
        try:
            resp = requests.post(
                url,
                json={"chat_id": admin_id, "text": text, "parse_mode": "HTML"},
                timeout=5,
            )
            if resp.status_code == 200:
                success_count += 1
                logger.info("Уведомление об эскалации отправлено admin_id=%d (причина: %s)", admin_id, reason.value)
            else:
                logger.error("Ошибка отправки уведомления эскалации (HTTP %d): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logger.error("Не удалось отправить уведомление эскалации admin_id=%d: %s", admin_id, e)

    return success_count > 0


# ──────────────────────────────────────────────
# Главная функция безопасности (вызывается из webhook_router)
# ──────────────────────────────────────────────

def check_safety_escalation(
    user_message: str,
    ai_response: str,
    client_phone: str,
    client_name: str,
    business_name: str,
    business_id: int,
    language: str = "ru",
) -> Tuple[bool, str]:
    """
    Комплексная проверка безопасности.

    1. Анализирует входящее сообщение клиента на агрессию, off-topic, запрос менеджера.
    2. Анализирует ответ ИИ на маркеры неуверенности / галлюцинаций.
    3. Если триггер сработал — отправляет уведомление в Telegram и возвращает вежливый ответ.

    Args:
        user_message:   Текст сообщения клиента.
        ai_response:    Ответ ИИ (для проверки на галлюцинации).
        client_phone:   Номер телефона клиента.
        client_name:    Имя клиента.
        business_name:  Название бизнеса.
        business_id:    ID бизнеса.
        language:       Язык диалога.

    Returns:
        Tuple[bool, str]:
            - True + вежливый ответ клиенту (если нужна эскалация).
            - False + оригинальный ai_response (если всё в порядке).
    """
    reason, trigger_detail = detect_escalation_trigger(user_message, ai_response)

    if reason == EscalationReason.NONE:
        return False, ai_response

    logger.warning(
        "ЭСКАЛАЦИЯ [%s] Бизнес #%d | Клиент %s (%s) | Триггер: %s",
        reason.value, business_id, client_name, client_phone, trigger_detail,
    )

    # Отправляем уведомление админу
    send_escalation_notification(
        reason=reason,
        trigger_detail=trigger_detail,
        client_phone=client_phone,
        client_name=client_name,
        business_name=business_name,
        business_id=business_id,
        user_message=user_message,
    )

    # Возвращаем вежливый ответ вместо ИИ-ответа
    safe_reply = get_escalation_reply(reason, language)
    return True, safe_reply
