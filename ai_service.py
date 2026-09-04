"""
ai_service.py -- Модуль интеграции с ИИ (OpenAI-совместимый API).

Предоставляет функции:
  - build_system_prompt          -- формирование системного промпта с каталогом товаров
  - format_chat_history          -- преобразование истории из БД в формат API
  - detect_language               -- определение языка клиента (kk / ru / en)
  - get_ai_response              -- отправка запроса к ИИ и получение ответа
  - get_ai_response_with_intent  -- ответы с распознаванием намерения сделать заказ
  - analyze_image_for_article    -- извлечение артикула со скриншота Instagram Stories (OpenAI Vision)
  - transcribe_voice             -- перевод голосовых сообщений клиентов в текст (OpenAI Whisper)
  - prepare_incoming_message     -- единый обработчик мультимодальных входящих данных
"""

import os
import re
import json
import time
import logging
from typing import Optional, Tuple, List, Dict, Any, Union

import requests
from dotenv import load_dotenv
from sqlalchemy.orm import Session

from models import Business, Product

load_dotenv()

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Классы исключений OpenAI API (для отказоустойчивости)
# ──────────────────────────────────────────────

try:
    from openai import RateLimitError, APIConnectionError, APIError, OpenAIError
except ImportError:
    class OpenAIError(Exception):
        """Базовый класс исключений OpenAI."""
        pass

    class RateLimitError(OpenAIError):
        """Ошибка превышения лимитов запросов (Rate Limit / HTTP 429)."""
        pass

    class APIConnectionError(OpenAIError):
        """Ошибка соединения с OpenAI API."""
        pass

    class APIError(OpenAIError):
        """Общая ошибка OpenAI API."""
        pass


# ──────────────────────────────────────────────
# Конфигурация API
# ──────────────────────────────────────────────

API_BASE_URL: str = os.getenv("OPENAI_API_BASE", "https://api.openai.com/v1")
API_KEY: str = os.getenv("OPENAI_API_KEY", "")
DEFAULT_MODEL: str = os.getenv("AI_MODEL", "gpt-5.6-luna")

MAX_HISTORY_MESSAGES: int = int(os.getenv("MAX_HISTORY_MESSAGES", "20"))
MAX_RESPONSE_TOKENS: int = int(os.getenv("MAX_RESPONSE_TOKENS", "1024"))
API_TEMPERATURE: float = float(os.getenv("AI_TEMPERATURE", "0.7"))

# Максимальное количество повторных попыток при временных сбоях API
MAX_API_RETRIES: int = int(os.getenv("MAX_API_RETRIES", "2"))
RETRY_DELAY_SECONDS: float = float(os.getenv("RETRY_DELAY_SECONDS", "1.5"))


# ──────────────────────────────────────────────
# Фоллбэк-сообщения для клиентов (отказоустойчивость)
# ──────────────────────────────────────────────

_FALLBACK_MESSAGES = {
    "ru": {
        "timeout": "Небольшая техническая заминка, уже чиним! Попробуйте отправить сообщение через минуту. ⏳",
        "connection": "Небольшая техническая заминка, уже чиним! Временные неполадки со связью. Попробуйте ещё раз. 🔧",
        "rate_limit": "Небольшая техническая заминка, уже чиним! Сейчас очень много запросов, подождите минуту. 🙏",
        "server_error": "Небольшая техническая заминка, уже чиним! Пожалуйста, повторите запрос через пару минут. 🛠",
        "unknown": "Небольшая техническая заминка, уже чиним! Мы уже знаем и работаем над исправлением. 🙏",
    },
    "kk": {
        "timeout": "Шағын техникалық ақаулық, қазір жөндеп жатырмыз! Бір минуттан кейін қайталап көріңіз. ⏳",
        "connection": "Шағын техникалық ақаулық, қазір жөндеп жатырмыз! Байланыста уақытша ақау бар. 🔧",
        "rate_limit": "Шағын техникалық ақаулық, қазір жөндеп жатырмыз! Қазір сұраныстар өте көп. 🙏",
        "server_error": "Шағын техникалық ақаулық, қазір жөндеп жатырмыз! Бірнеше минуттан кейін қайталаңыз. 🛠",
        "unknown": "Шағын техникалық ақаулық, қазір жөндеп жатырмыз! Кейінірек қайталап көріңіз. 🙏",
    },
    "en": {
        "timeout": "A small technical hiccup, we're already fixing it! Please try again in a minute. ⏳",
        "connection": "A small technical hiccup, we're already fixing it! Temporary connection issues. 🔧",
        "rate_limit": "A small technical hiccup, we're already fixing it! Too many requests right now. 🙏",
        "server_error": "A small technical hiccup, we're already fixing it! Please try again in a couple of minutes. 🛠",
        "unknown": "A small technical hiccup, we're already fixing it! An unexpected error occurred. 🙏",
    },
}


def get_fallback_message(language: str, error_type: str) -> str:
    """
    Возвращает вежливое фоллбэк-сообщение для клиента на соответствующем языке.

    Args:
        language: Язык клиента ('ru', 'kk', 'en').
        error_type: Тип ошибки ('timeout', 'connection', 'rate_limit', 'server_error', 'unknown').

    Returns:
        str: Локализованное фоллбэк-сообщение.
    """
    lang_messages = _FALLBACK_MESSAGES.get(language, _FALLBACK_MESSAGES["ru"])
    return lang_messages.get(error_type, lang_messages["unknown"])


# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# Системный промпт для Цветочного Магазина (ИИ-флорист)
# ──────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE: str = """
Ты — вежливый, заботливый и высококлассный ИИ-флорист-консультант цветочного салона "{business_name}" (ID магазина: {business_id}).

## Твоя роль и стиль общения
- Ты общаешься как тёплый, эмпатичный, эстетичный и профессиональный флорист-консультант.
- Твоя цель — помочь клиенту подобрать идеально подходящий букет цветов или подарок, вызывать позитивные эмоции и быстро оформить заказ.
- Используй приветливый, уважительный тон и аккуратные цветочные эмодзи (🌸, 💐, 🌹, ✨, 🎁, 🚚, 💌).

## Языковые правила
- Ты свободно общаешься на русском и казахском языках.
- ВСЕГДА отвечай на том же языке, на котором обратился клиент.
- Если клиент пишет на казахском — отвечай на казахском (мың алғыс, гүл шоғы, жеткізу, құттықтау хаты и т.д.).
- Если клиент пишет на русском — отвечай на русском.
- Если язык неясен, по умолчанию отвечай на русском.

## Твои ключевые задачи
1. Приветствовать клиента и представляться консультантом цветочного салона "{business_name}".
2. Помогать подбирать букет под конкретный **повод** (День рождения, романтическое свидание, юбилей, 8 Марта, свадьба, просто порадовать без повода).
3. Подробно консультировать по каталогу букетов: сообщать **Название букета**, **Состав цветов** (какие именно цветы входят), **Размер** (S, M, L, XL), **Цену** (и акционную цену), **Наличие** и показывать **Ссылку на фото**.
4. **ОБЯЗАТЕЛЬНО собирать полные данные для заказа**:
   - 📅⏰ **Точная дата и время получения** (доставка или самовывоз — например, "сегодня к 18:00" или "завтра с 10:00 до 12:00").
   - 💌 **Бесплатная открытка / записка к букету**: вежливо спроси у клиента: *"Нужна ли записка или поздравительная открытка к букету?"*. Если да — попроси написать текст поздравления и сохранять его.
   - 👤 **Имя и телефон получателя**.
   - 📍 **Адрес доставки** (или уточнение о самовывозе).

## РАБОТА С РАСПРОДАННЫМИ БУКЕТАМИ И АЛЬТЕРНАТИВАМИ (КРИТИЧЕСКИ ВАЖНО)
- Если выбранный букет распродан (остаток `stock == 0`), НИКОГДА не отвечай сухо "Букета нет".
- Вместо этого ты ОБЯЗАН:
  1. Вежливо объяснить, что свежие цветы для этого букета сейчас в поставке или букет разобран.
  2. АВТОМАТИЧЕСКИ предложить до 3 альтернативных букетов в наличии из каталога ниже.
  3. Указать их Артикул, Название, Состав цветов, Размер и Актуальную цену.

## ОФОРМЛЕНИЕ ЗАКАЗА (КРИТИЧЕСКИ ВАЖНО)
Когда клиент ЯВНО определился с выбором и подтвердил намерение заказать букет (указал позицию и количество), в самом конце твоего ответа с новой строки обязательно добавь метку оформления заказа в строго таком формате JSON:
[[ORDER: {{"items": [{{"product_name": "Название букета", "quantity": 1}}]}}]

Пример:
Отличный выбор! Я с радостью забронировала для вас букет "Романтическая Нежность" (размер M). Давайте уточним желаемую дату и время получения, а также нужен ли текст для бесплатной поздравительной открытки! 🌸
[[ORDER: {{"items": [{{"product_name": "Букет Романтическая Нежность", "quantity": 1}}]}}]

Если клиент еще выбирает или просто задает вопросы — маркер [[ORDER: ...]] НЕ ДОБАВЛЯЙ.

## СТРОГИЕ ПРАВИЛА (НАРУШАТЬ ЗАПРЕЩЕНО)
- Используй для ответов о букетах, составе, размерах, ценах и наличии ТОЛЬКО данные из актуального каталога цветочного салона "{business_name}" ниже.
- НИКОГДА не выдумывай несуществующие букеты, состав или нереальные цены.

## АКТУАЛЬНЫЙ КАТАЛОГ ЦВЕТОЧНОГО САЛОНА (МАГАЗИН: {business_name}, ID: {business_id})
{product_catalog}

Если каталог пуст, вежливо сообщи клиенту, что свежая поставка цветов разгружается, и предложи оставить контакт для связи с флористом.
""".strip()


def find_alternative_products(
    db: Session,
    business_id: int,
    category: Optional[str] = None,
    exclude_product_id: Optional[int] = None,
    limit: int = 3,
    shop_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Ищет в БД до `limit` альтернативных товаров с наличием (stock > 0) из той же категории.
    Использует строгую фильтрацию по shop_id для обеспечения изоляции данных арендатора.
    """
    target_shop_id = shop_id if shop_id is not None else business_id

    query = db.query(Product).filter(
        Product.shop_id == target_shop_id,
        Product.stock > 0,
    )
    if exclude_product_id:
        query = query.filter(Product.id != exclude_product_id)

    # 1. Поиск по той же категории
    alternatives_same_category = []
    if category and category.strip():
        alternatives_same_category = (
            query.filter(Product.category == category.strip())
            .order_by(Product.stock.desc())
            .limit(limit)
            .all()
        )

    result_products = list(alternatives_same_category)

    # 2. Добор из других категорий бизнеса, если не набралось limit
    if len(result_products) < limit:
        existing_ids = {p.id for p in result_products}
        if exclude_product_id:
            existing_ids.add(exclude_product_id)

        other_products = (
            db.query(Product)
            .filter(
                Product.shop_id == target_shop_id,
                Product.stock > 0,
                Product.id.notin_(existing_ids),
            )
            .order_by(Product.stock.desc())
            .limit(limit - len(result_products))
            .all()
        )
        result_products.extend(other_products)

    return [
        {
            "id": p.id,
            "article": p.article or f"Арт. #{p.id}",
            "name": p.name,
            "category": p.category or "Букеты",
            "price": p.price,
            "discount_price": p.discount_price,
            "flower_composition": getattr(p, "flower_composition", None) or "",
            "size": getattr(p, "size", None) or "",
            "image_url": getattr(p, "image_url", None) or "",
            "stock": p.stock,
            "description": p.description or "",
            "promotion_info": p.promotion_info or "",
        }
        for p in result_products
    ]


def fetch_business_catalog(
    db: Session,
    business_id: int,
    shop_id: Optional[int] = None,
) -> Tuple[str, List[Dict[str, Any]], Optional[str]]:
    """
    Динамически подгружает информацию о бизнесе и его актуальном каталоге товаров из БД по shop_id / business_id.
    Гарантирует изоляцию данных по shop_id.

    Returns:
        Tuple[business_name, products_list, api_key_ai]
    """
    target_shop_id = shop_id if shop_id is not None else business_id

    business = db.query(Business).filter((Business.id == target_shop_id) | (Business.shop_id == target_shop_id)).first()
    if not business:
        raise ValueError(f"Магазин / бизнес с ID {target_shop_id} не найден в базе данных.")

    products_db = (
        db.query(Product)
        .filter(Product.shop_id == target_shop_id)
        .order_by(Product.id.asc())
        .all()
    )

    products_list: List[Dict[str, Any]] = []
    for p in products_db:
        p_dict = {
            "id": p.id,
            "article": p.article or f"Арт. #{p.id}",
            "name": p.name,
            "category": p.category or "Букеты",
            "price": p.price,
            "discount_price": p.discount_price,
            "flower_composition": getattr(p, "flower_composition", None) or "",
            "size": getattr(p, "size", None) or "",
            "image_url": getattr(p, "image_url", None) or "",
            "stock": p.stock,
            "description": p.description or "",
            "promotion_info": p.promotion_info or "",
            "alternatives": [],
        }

        # Если stock == 0, формируем до 3 альтернативных товаров из БД
        if p.stock <= 0:
            p_dict["alternatives"] = find_alternative_products(
                db=db,
                business_id=business_id,
                category=p.category,
                exclude_product_id=p.id,
                limit=3,
                shop_id=target_shop_id,
            )

        products_list.append(p_dict)

    return business.name, products_list, business.api_key_ai


def build_system_prompt(
    business_name: str,
    products: List[Dict[str, Any]],
    business_id: Optional[int] = None,
) -> str:
    """Формирует системный промпт с актуальным детальным каталогом товаров конкретного бизнеса."""
    if not products:
        catalog_text = "(Каталог пуст -- товары временно недоступны)"
    else:
        lines: List[str] = []
        for i, p in enumerate(products, start=1):
            p_id = p.get("id", i)
            article = p.get("article") or f"Арт. #{p_id}"
            category = p.get("category") or "Букеты"
            name = p.get("name", "Букет")
            flower_comp = p.get("flower_composition") or ""
            size_val = p.get("size") or ""
            image_val = p.get("image_url") or ""
            price = p.get("price", 0.0)
            discount_price = p.get("discount_price")
            stock = p.get("stock", 0)
            desc = p.get("description") or "без описания"
            promotion = p.get("promotion_info") or ""

            if discount_price and discount_price > 0 and discount_price < price:
                price_info = f"Базовая цена: {price} тг. | АКЦИОННАЯ ЦЕНА (СКИДКА): {discount_price} тг."
            else:
                price_info = f"Цена: {price} тг."

            if promotion:
                price_info += f" | Акция: {promotion}"

            details_lines = []
            if flower_comp:
                details_lines.append(f"   🌸 Состав цветов: {flower_comp}")
            if size_val:
                details_lines.append(f"   📏 Размер: {size_val}")
            if image_val:
                details_lines.append(f"   📸 Фото: {image_val}")

            details_part = ("\n" + "\n".join(details_lines)) if details_lines else ""

            if stock > 0:
                stock_status = f"В НАЛИЧИИ: {stock} шт."
                product_str = (
                    f"🔹 ID {p_id} | Артикул: {article} | Категория: {category} | 💐 {name}\n"
                    f"   💰 {price_info} | 📊 {stock_status}"
                    f"{details_part}\n"
                    f"   📝 Описание: {desc}"
                )
            else:
                stock_status = "❌ РАСПРОДАНО (0 шт.)"
                product_str = (
                    f"🔹 ID {p_id} | Артикул: {article} | Категория: {category} | 💐 {name}\n"
                    f"   💰 {price_info} | 📊 {stock_status}"
                    f"{details_part}\n"
                    f"   📝 Описание: {desc}"
                )

                alts = p.get("alternatives", [])
                if alts:
                    alt_lines = []
                    for alt in alts:
                        alt_art = alt.get("article") or f"Арт. #{alt['id']}"
                        alt_price = alt.get("discount_price") or alt.get("price")
                        alt_comp = alt.get("flower_composition") or ""
                        alt_size = alt.get("size") or ""
                        alt_extra = ""
                        if alt_comp:
                            alt_extra += f", состав: {alt_comp}"
                        if alt_size:
                            alt_extra += f", размер: {alt_size}"
                        alt_lines.append(
                            f"     -> Альтернатива: {alt['name']} (Артикул: {alt_art}) -- {alt_price} тг. (в наличии: {alt['stock']} шт.{alt_extra})"
                        )
                    product_str += "\n   💡 АЛЬТЕРНАТИВЫ ИЗ БД ДЛЯ ПРЕДЛОЖЕНИЯ КЛИЕНТУ:\n" + "\n".join(alt_lines)
                else:
                    product_str += "\n   💡 (Альтернативные товары в данной категории временно отсутствуют)"

            lines.append(product_str)

        catalog_text = "\n\n".join(lines)

    return SYSTEM_PROMPT_TEMPLATE.format(
        business_name=business_name,
        business_id=business_id or "N/A",
        product_catalog=catalog_text,
    )


# ──────────────────────────────────────────────
# Определение языка
# ──────────────────────────────────────────────

_KZ_UNIQUE_CHARS = re.compile(r"[әіңғүұқөһ]", re.IGNORECASE)
_CYRILLIC_CHARS = re.compile(r"[а-яА-ЯёЁ]")


def detect_language(text: str) -> str:
    """Определяет язык сообщения клиента ('kk', 'ru', 'en')."""
    if _KZ_UNIQUE_CHARS.search(text):
        return "kk"
    if _CYRILLIC_CHARS.search(text):
        return "ru"
    return "en"


# ──────────────────────────────────────────────
# Подготовка истории диалога
# ──────────────────────────────────────────────

def format_chat_history(
    chat_messages: List[Dict[str, Any]],
    limit: int = MAX_HISTORY_MESSAGES,
) -> List[Dict[str, str]]:
    """Преобразует историю сообщений из БД в формат OpenAI Chat API."""
    recent = chat_messages[-limit:] if len(chat_messages) > limit else chat_messages
    return [
        {"role": msg["role"], "content": msg["message"]}
        for msg in recent
    ]


# ──────────────────────────────────────────────
# Вспомогательный парсер намерения заказа
# ──────────────────────────────────────────────

ORDER_PATTERN = re.compile(r"\[\[ORDER:\s*({.*?})\]\]", re.DOTALL)


def parse_order_intent(raw_reply: str) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Извлекает блок [[ORDER: {...}]] из ответа ИИ, если он присутствует.

    Returns:
        Tuple[clean_reply, order_items]:
            clean_reply: Текст ответа ИИ без служебного маркера.
            order_items: Список товаров для заказа или None.
    """
    match = ORDER_PATTERN.search(raw_reply)
    if not match:
        return raw_reply.strip(), None

    json_str = match.group(1)
    clean_reply = ORDER_PATTERN.sub("", raw_reply).strip()

    try:
        data = json.loads(json_str)
        items = data.get("items", [])
        if isinstance(items, list) and len(items) > 0:
            return clean_reply, items
    except Exception as e:
        logger.warning("Не удалось распарсить JSON заказа из ответа ИИ: %s", e)

    return clean_reply, None


# ──────────────────────────────────────────────
# Отправка запроса к ИИ
# ──────────────────────────────────────────────

def get_ai_response(
    user_message: str,
    business_name: str,
    products: List[Dict[str, Any]],
    chat_history: List[Dict[str, Any]],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    business_id: Optional[int] = None,
) -> str:
    """Базовая функция получения ответа от ИИ."""
    reply, _ = get_ai_response_with_intent(
        user_message=user_message,
        business_name=business_name,
        products=products,
        chat_history=chat_history,
        api_key=api_key,
        model=model,
        business_id=business_id,
    )
    return reply


def get_ai_response_with_intent(
    user_message: str,
    business_name: str,
    products: List[Dict[str, Any]],
    chat_history: List[Dict[str, Any]],
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    business_id: Optional[int] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Отправляет запрос в ИИ и возвращает кортеж:
    (текст_ответа_клиенту, список_товаров_для_оформления_заказа_или_None).

    Отказоустойчивость:
      - Повторные попытки (retry) при таймаутах и сбоях соединения.
      - Обработка RateLimitError (HTTP 429) с понятным фоллбэком клиенту.
      - Обработка серверных ошибок (HTTP 5xx) без падения.
      - Полное логирование каждой ошибки в app.log.
    """
    effective_key = api_key or API_KEY
    if not effective_key:
        raise ValueError(
            "API-ключ не настроен. Укажите OPENAI_API_KEY в .env "
            "или передайте api_key напрямую."
        )
    effective_model = model or DEFAULT_MODEL

    lang = detect_language(user_message)
    logger.info("Язык сообщения: %s | Модель: %s | Бизнес/Shop ID: %s", lang, effective_model, business_id)

    system_prompt = build_system_prompt(business_name, products, business_id=business_id)
    history_messages = format_chat_history(chat_history)

    messages = [
        {"role": "system", "content": system_prompt},
        *history_messages,
        {"role": "user", "content": user_message},
    ]

    url = f"{API_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {effective_key}",
        "Content-Type": "application/json",
    }
    request_payload = {
        "model": effective_model,
        "messages": messages,
        "max_tokens": MAX_RESPONSE_TOKENS,
        "temperature": API_TEMPERATURE,
    }

    # ── Retry-цикл с экспоненциальной задержкой ──
    last_exception: Optional[Exception] = None
    for attempt in range(1, MAX_API_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=request_payload,
                timeout=60,
            )

            # ── HTTP 429: Rate Limit ──
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                logger.warning(
                    "[Попытка %d/%d] RateLimitError (HTTP 429) от API. Retry-After: %s | URL: %s",
                    attempt, MAX_API_RETRIES, retry_after or "не указано", url,
                )
                if attempt < MAX_API_RETRIES:
                    wait_time = float(retry_after) if retry_after else RETRY_DELAY_SECONDS * attempt
                    time.sleep(min(wait_time, 10.0))
                    continue
                # Все попытки исчерпаны
                logger.error(
                    "RATE LIMIT ИСЧЕРПАН: OpenAI API вернул 429 после %d попыток. "
                    "Бизнес ID: %s | Модель: %s | URL: %s",
                    MAX_API_RETRIES, business_id, effective_model, url,
                )
                return get_fallback_message(lang, "rate_limit"), None

            # ── HTTP 5xx: Серверная ошибка OpenAI ──
            if response.status_code >= 500:
                error_detail = response.text[:500]
                logger.warning(
                    "[Попытка %d/%d] Серверная ошибка OpenAI (HTTP %d): %s",
                    attempt, MAX_API_RETRIES, response.status_code, error_detail,
                )
                if attempt < MAX_API_RETRIES:
                    time.sleep(RETRY_DELAY_SECONDS * attempt)
                    continue
                logger.error(
                    "СЕРВЕРНАЯ ОШИБКА OpenAI: HTTP %d после %d попыток. "
                    "Бизнес ID: %s | Response: %s",
                    response.status_code, MAX_API_RETRIES, business_id, error_detail,
                )
                return get_fallback_message(lang, "server_error"), None

            # ── HTTP 401/403: Ошибка авторизации (не ретраим) ──
            if response.status_code in (401, 403):
                error_detail = response.text[:500]
                logger.error(
                    "ОШИБКА АВТОРИЗАЦИИ OpenAI (HTTP %d): %s | Бизнес ID: %s",
                    response.status_code, error_detail, business_id,
                )
                raise ValueError(
                    f"Ошибка авторизации API (HTTP {response.status_code}). "
                    "Проверьте OPENAI_API_KEY в настройках бизнеса."
                )

            # ── Прочие HTTP-ошибки (4xx) ──
            if response.status_code != 200:
                error_detail = response.text[:500]
                logger.error(
                    "API вернул ошибку HTTP %d (бизнес %s): %s",
                    response.status_code, business_id, error_detail,
                )
                return get_fallback_message(lang, "server_error"), None

            # ── Успешный ответ: парсинг ──
            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as json_err:
                logger.error(
                    "Не удалось распарсить JSON-ответ OpenAI: %s | Raw response: %s",
                    json_err, response.text[:300],
                )
                return get_fallback_message(lang, "server_error"), None

            try:
                raw_assistant_message = data["choices"][0]["message"]["content"].strip()
                logger.info("Успешный ответ от OpenAI (длина: %d символов)", len(raw_assistant_message))
            except (KeyError, IndexError, TypeError) as e:
                logger.error(
                    "Неожиданный формат ответа API (бизнес %s): %s | Data keys: %s",
                    business_id, e, list(data.keys()) if isinstance(data, dict) else type(data),
                )
                return get_fallback_message(lang, "server_error"), None

            # Распознаём намерение заказа
            clean_reply, order_items = parse_order_intent(raw_assistant_message)
            return clean_reply, order_items

        except RateLimitError as exc:
            last_exception = exc
            logger.error(
                "[Попытка %d/%d] RateLimitError от OpenAI API: %s | Бизнес ID: %s | URL: %s",
                attempt, MAX_API_RETRIES, exc, business_id, url, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            return get_fallback_message(lang, "rate_limit"), None

        except APIConnectionError as exc:
            last_exception = exc
            logger.error(
                "[Попытка %d/%d] APIConnectionError при подключении к OpenAI API: %s | Бизнес ID: %s",
                attempt, MAX_API_RETRIES, exc, business_id, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            return get_fallback_message(lang, "connection"), None

        except APIError as exc:
            last_exception = exc
            logger.error(
                "[Попытка %d/%d] APIError от OpenAI API: %s | Бизнес ID: %s",
                attempt, MAX_API_RETRIES, exc, business_id, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue
            return get_fallback_message(lang, "server_error"), None

        except requests.exceptions.Timeout as exc:
            last_exception = exc
            logger.warning(
                "[Попытка %d/%d] Таймаут запроса к OpenAI API (%s). Бизнес ID: %s",
                attempt, MAX_API_RETRIES, url, business_id, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue

        except requests.exceptions.ConnectionError as exc:
            last_exception = exc
            logger.warning(
                "[Попытка %d/%d] Ошибка соединения с OpenAI API (%s). Бизнес ID: %s | Ошибка: %s",
                attempt, MAX_API_RETRIES, url, business_id, exc, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue

        except requests.exceptions.RequestException as exc:
            last_exception = exc
            logger.error(
                "[Попытка %d/%d] Ошибка HTTP-запроса к OpenAI API: %s | Бизнес ID: %s",
                attempt, MAX_API_RETRIES, exc, business_id, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue

        except Exception as exc:
            last_exception = exc
            logger.error(
                "[Попытка %d/%d] Неожиданное исключение при вызове OpenAI API: %s | Бизнес ID: %s",
                attempt, MAX_API_RETRIES, exc, business_id, exc_info=True,
            )
            if attempt < MAX_API_RETRIES:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
                continue

    # Все retry-попытки исчерпаны
    error_type = "timeout"
    if last_exception and isinstance(last_exception, (requests.exceptions.ConnectionError, APIConnectionError)):
        error_type = "connection"
    elif last_exception and isinstance(last_exception, RateLimitError):
        error_type = "rate_limit"
    elif last_exception and not isinstance(last_exception, requests.exceptions.Timeout):
        error_type = "unknown"

    logger.error(
        "ВСЕ ПОПЫТКИ ИСЧЕРПАНЫ (%d): запрос к OpenAI API не удался. "
        "Бизнес ID: %s | Последняя ошибка: %s",
        MAX_API_RETRIES, business_id, last_exception, exc_info=True,
    )
    return get_fallback_message(lang, error_type), None


def get_ai_response_for_business(
    db: Session,
    business_id: int,
    user_message: str,
    chat_history: List[Dict[str, Any]],
    model: Optional[str] = None,
    shop_id: Optional[int] = None,
) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
    """
    Динамически подгружает каталог и данные бизнеса для shop_id/business_id и генерирует ответ ИИ.
    """
    target_shop_id = shop_id if shop_id is not None else business_id
    business_name, products, api_key_ai = fetch_business_catalog(db, business_id, shop_id=target_shop_id)

    return get_ai_response_with_intent(
        user_message=user_message,
        business_name=business_name,
        products=products,
        chat_history=chat_history,
        api_key=api_key_ai,
        model=model,
        business_id=target_shop_id,
    )


# ──────────────────────────────────────────────
# Мультимодальная обработка (Vision & Whisper)
# ──────────────────────────────────────────────

def analyze_image_for_article(
    image_url: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """
    Извлекает артикул или номер товара (например, 'Арт. 12', '#5')
    со скриншотов Instagram Stories с помощью OpenAI Vision API.

    Args:
        image_url: URL изображения или скриншота.
        api_key: API-ключ OpenAI (если не указан, берется из .env).
        model: Модель ИИ с поддержкой распознавания изображений (по умолчанию gpt-5.6-luna).

    Returns:
        Optional[str]: Извлеченный артикул/номер товара или None, если не найден.
    """
    effective_key = api_key or API_KEY
    if not effective_key:
        raise ValueError(
            "API-ключ не настроен. Укажите OPENAI_API_KEY в .env "
            "или передайте api_key напрямую."
        )

    effective_model = model or DEFAULT_MODEL

    url = f"{API_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {effective_key}",
        "Content-Type": "application/json",
    }

    prompt_text = (
        "Ты — узкоспециализированный ИИ-ассистент для извлечения артикулов товаров. "
        "Внимательно изучи скриншот из Instagram Stories и найди артикул или номер товара "
        "(например: 'Арт. 12', '#5', 'Артикул 101', 'Код 45'). "
        "Если артикул или номер товара присутствует, верни ТОЛЬКО найденный артикул "
        "(строго сам артикул, без лишних слов, вступлений и знаков препинания). "
        "Если артикула или номера товара на скриншоте нет, верни 'НЕ НАЙДЕНО'."
    )

    payload = {
        "model": effective_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            }
        ],
        "max_tokens": 100,
        "temperature": 0.1,
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)

        # Rate Limit (429)
        if response.status_code == 429:
            logger.warning(
                "Vision API: RateLimitError (HTTP 429). Image URL: %s",
                image_url[:100],
            )
            return None

        # Серверные ошибки (5xx)
        if response.status_code >= 500:
            logger.error(
                "Vision API: серверная ошибка (HTTP %d): %s",
                response.status_code, response.text[:300],
            )
            return None

        if response.status_code != 200:
            logger.error("Vision API вернул ошибку %d: %s", response.status_code, response.text[:300])
            return None

        try:
            data = response.json()
        except (ValueError, json.JSONDecodeError) as json_err:
            logger.error("Vision API: не удалось распарсить JSON-ответ: %s", json_err)
            return None

        raw_result = data["choices"][0]["message"]["content"].strip()

        if not raw_result or "НЕ НАЙДЕНО" in raw_result.upper() or "NOT FOUND" in raw_result.upper():
            logger.info("Артикул на скриншоте не обнаружен.")
            return None

        logger.info("Успешно извлечен артикул со скриншота: %s", raw_result)
        return raw_result

    except requests.exceptions.Timeout:
        logger.error("Vision API: таймаут запроса. Image URL: %s", image_url[:100])
        return None
    except requests.exceptions.ConnectionError as conn_err:
        logger.error("Vision API: ошибка соединения: %s", conn_err)
        return None
    except (KeyError, IndexError, TypeError) as parse_err:
        logger.error("Vision API: неожиданный формат ответа: %s", parse_err)
        return None
    except Exception as e:
        logger.error("Vision API: непредвиденная ошибка при анализе изображения: %s", e)
        return None


def transcribe_voice(
    audio_path: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """
    Переводит голосовое сообщение клиента из аудиофайла в текст
    с использованием OpenAI Whisper API.

    Args:
        audio_path: Путь к локальному аудиофайлу (mp3, ogg, wav, m4a и т.д.).
        api_key: API-ключ OpenAI (если не указан, берется из .env).
        model: Имя модели Whisper (по умолчанию 'whisper-1').

    Returns:
        str: Текст расшифрованного голосового сообщения.

    Raises:
        FileNotFoundError: Если аудиофайл не найден по указанному пути.
        RuntimeError: В случае ошибки сервиса Whisper API.
    """
    effective_key = api_key or API_KEY
    if not effective_key:
        raise ValueError(
            "API-ключ не настроен. Укажите OPENAI_API_KEY в .env "
            "или передайте api_key напрямую."
        )

    if not os.path.exists(audio_path):
        logger.error("Аудиофайл не найден по пути: %s", audio_path)
        raise FileNotFoundError(f"Аудиофайл не найден по пути: {audio_path}")

    effective_model = model or os.getenv("WHISPER_MODEL", "whisper-1")

    url = f"{API_BASE_URL.rstrip('/')}/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {effective_key}",
    }

    try:
        with open(audio_path, "rb") as audio_file:
            files = {"file": (os.path.basename(audio_path), audio_file)}
            data = {"model": effective_model}
            response = requests.post(url, headers=headers, files=files, data=data, timeout=60)

        # Rate Limit (429)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "неизвестно")
            logger.error(
                "Whisper API: RateLimitError (HTTP 429). Retry-After: %s | Файл: %s",
                retry_after, audio_path,
            )
            raise RuntimeError(
                "Слишком много запросов к сервису распознавания речи. "
                "Пожалуйста, подождите минуту и повторите."
            )

        # Серверные ошибки (5xx)
        if response.status_code >= 500:
            error_detail = response.text[:300]
            logger.error(
                "Whisper API: серверная ошибка (HTTP %d): %s | Файл: %s",
                response.status_code, error_detail, audio_path,
            )
            raise RuntimeError(
                f"Сервис распознавания речи временно недоступен (HTTP {response.status_code}). "
                "Попробуйте позже."
            )

        if response.status_code != 200:
            error_detail = response.text[:300]
            logger.error("Whisper API вернул ошибку %d: %s", response.status_code, error_detail)
            raise RuntimeError(f"Ошибка Whisper API (HTTP {response.status_code}): {error_detail}")

        try:
            res_json = response.json()
        except (ValueError, json.JSONDecodeError) as json_err:
            logger.error("Whisper API: не удалось распарсить JSON-ответ: %s", json_err)
            raise RuntimeError("Получен некорректный ответ от сервиса распознавания речи.")

        transcribed_text = res_json.get("text", "").strip()
        logger.info("Голосовое сообщение успешно расшифровано: '%s'", transcribed_text)
        return transcribed_text

    except requests.exceptions.Timeout:
        logger.error("Таймаут запроса к Whisper API. Файл: %s", audio_path)
        raise RuntimeError("Whisper API не ответил вовремя. Попробуйте позже.")
    except requests.exceptions.ConnectionError as conn_err:
        logger.error("Ошибка соединения с Whisper API: %s | Файл: %s", conn_err, audio_path)
        raise RuntimeError("Не удалось подключиться к сервису Whisper API.")
    except RuntimeError:
        raise  # Пробрасываем уже обработанные RuntimeError
    except Exception as e:
        logger.error("Непредвиденная ошибка транскрибации аудиофайла %s: %s", audio_path, e, exc_info=True)
        raise RuntimeError(f"Ошибка при распознавании голосового сообщения: {e}")


def prepare_incoming_message(
    message: Optional[str] = None,
    image_url: Optional[str] = None,
    audio_path: Optional[str] = None,
    api_key: Optional[str] = None,
) -> str:
    """
    Универсальная интеграционная функция для обработки входящих запросов.
    Объединяет текстовые сообщения, транскрибацию голосовых сообщений (Whisper API)
    и извлеченные артикулы со скриншотов (Vision API) в единый контекст.

    Args:
        message: Исходное текстовое сообщение клиента (если есть).
        image_url: URL изображения/скриншота Instagram Story.
        audio_path: Путь к файлу голосового сообщения.
        api_key: API-ключ ИИ.

    Returns:
        str: Подготовленный итоговый текст для ИИ-менеджера.
    """
    parts: List[str] = []

    text_clean = (message or "").strip()
    if text_clean:
        parts.append(text_clean)

    if audio_path:
        transcribed_text = transcribe_voice(audio_path, api_key=api_key)
        if transcribed_text:
            parts.append(f"[Голосовое сообщение]: {transcribed_text}")

    if image_url:
        article = analyze_image_for_article(image_url, api_key=api_key)
        if article:
            parts.append(f"[Артикул со скриншота: {article}]")
        elif not parts:
            parts.append("[Изображение получено, артикул не обнаружен]")

    return "\n".join(parts).strip()


