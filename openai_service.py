"""
openai_service.py -- Асинхронный клиент OpenAI API (httpx-based).

Легковесный модуль для прямого взаимодействия с OpenAI-совместимыми API:
  - chat_completion()      — текстовая генерация (gpt-5.6-luna)
  - vision_analyze()       — анализ изображений (Vision API)
  - whisper_transcribe()   — транскрибация аудио (Whisper API)

Оптимизирован для работы на слабых VPS (2 GB RAM):
  - Единый httpx.AsyncClient с пулом соединений (переиспользование TCP).
  - Автоматический retry (экспоненциальный backoff) при 429/5xx.
  - Таймауты для защиты от зависания event loop.
  - Минимальный расход памяти: нет библиотеки openai (80+ MB), только httpx (~5 MB).

Использование:
    from openai_service import openai_client

    # В асинхронном контексте:
    response = await openai_client.chat_completion(messages=[...])

    # При shutdown:
    await openai_client.close()
"""

import os
import asyncio
import logging
from typing import Optional, List, Dict, Any

import httpx
from config import settings

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Конфигурация
# ──────────────────────────────────────────────

_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=60.0,
    write=10.0,
    pool=10.0,
)

# Максимальное число одновременных соединений к API
_CONNECTION_LIMITS = httpx.Limits(
    max_connections=10,
    max_keepalive_connections=5,
    keepalive_expiry=30.0,
)

# Коды ошибок, при которых делаем retry
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Максимальное число попыток
_MAX_RETRIES = 3

# Базовая задержка между попытками (экспоненциальный backoff)
_BASE_RETRY_DELAY = 1.0


# ──────────────────────────────────────────────
# Асинхронный клиент OpenAI
# ──────────────────────────────────────────────

class OpenAIClient:
    """
    Асинхронный HTTP-клиент для OpenAI-совместимых API.

    Преимущества перед synchronous `requests`:
      - Не блокирует event loop FastAPI.
      - Переиспользует TCP-соединения (connection pooling).
      - Автоматический retry с экспоненциальным backoff.
      - Корректное закрытие при shutdown.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._base_url = (base_url or settings.OPENAI_API_BASE).rstrip("/")
        self._model = model or settings.AI_MODEL
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Lazy-инициализация httpx.AsyncClient (создается при первом вызове)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=_DEFAULT_TIMEOUT,
                limits=_CONNECTION_LIMITS,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self):
        """Корректно закрывает HTTP-клиент (вызывается при shutdown FastAPI)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("OpenAI HTTP-клиент закрыт.")

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        """
        Выполняет HTTP-запрос с автоматическим retry при transient-ошибках.

        Реализует экспоненциальный backoff:
          - Попытка 1: 1 сек
          - Попытка 2: 2 сек
          - Попытка 3: 4 сек
        """
        client = self._get_client()
        last_exc: Optional[Exception] = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = await client.request(method, url, **kwargs)

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    # Для 429 используем Retry-After, если есть
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else _BASE_RETRY_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        "API вернул %d, попытка %d/%d, ожидание %.1f сек...",
                        response.status_code, attempt, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue

                return response

            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as e:
                last_exc = e
                delay = _BASE_RETRY_DELAY * (2 ** (attempt - 1))
                logger.warning(
                    "Ошибка соединения (%s), попытка %d/%d, ожидание %.1f сек...",
                    type(e).__name__, attempt, _MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(
            f"API недоступен после {_MAX_RETRIES} попыток: {last_exc}"
        )

    # ──────────────────────────────────────────
    # Chat Completion (gpt-5.6-luna)
    # ──────────────────────────────────────────

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        api_key: Optional[str] = None,
    ) -> str:
        """
        Отправляет запрос к Chat Completions API и возвращает текст ответа.

        Args:
            messages:    Список сообщений в формате OpenAI Chat API.
            model:       Модель (по умолчанию из settings).
            temperature: Температура генерации.
            max_tokens:  Максимальное число токенов ответа.
            api_key:     Переопределенный API-ключ (для multi-tenant).

        Returns:
            str: Текст ответа ассистента.

        Raises:
            RuntimeError: При ошибке API или некорректном ответе.
        """
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": model or self._model,
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.AI_TEMPERATURE,
            "max_tokens": max_tokens or settings.MAX_RESPONSE_TOKENS,
        }

        # Если передан кастомный api_key — используем отдельный запрос с другим заголовком
        extra_headers = {}
        if api_key and api_key != self._api_key:
            extra_headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await self._request_with_retry(
                "POST", url, json=payload, headers=extra_headers,
            )

            if response.status_code != 200:
                error_text = response.text[:500]
                logger.error("Chat API ошибка %d: %s", response.status_code, error_text)
                raise RuntimeError(f"Ошибка ИИ-сервиса (HTTP {response.status_code}).")

            data = response.json()
            content = data["choices"][0]["message"]["content"].strip()
            logger.info("Успешный запрос к OpenAI Chat API (модель %s, длина ответа %d)", payload["model"], len(content))
            return content
        except (KeyError, IndexError) as e:
            logger.error("Некорректный формат ответа Chat API: %s", e)
            raise RuntimeError("Получен некорректный ответ от ИИ-сервиса.")

    # ──────────────────────────────────────────
    # Vision API (анализ изображений)
    # ──────────────────────────────────────────

    async def vision_analyze(
        self,
        image_url: str,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 200,
        api_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Анализирует изображение через Vision API.

        Args:
            image_url: URL изображения для анализа.
            prompt:    Текстовый промпт (что искать на изображении).
            model:     Модель с поддержкой Vision.
            max_tokens: Максимум токенов ответа.
            api_key:   API-ключ (для multi-tenant).

        Returns:
            Optional[str]: Результат анализа или None при ошибке.
        """
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": model or self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }

        extra_headers = {}
        if api_key and api_key != self._api_key:
            extra_headers["Authorization"] = f"Bearer {api_key}"

        try:
            response = await self._request_with_retry(
                "POST", url, json=payload, headers=extra_headers,
            )
            if response.status_code != 200:
                logger.error("Vision API ошибка %d: %s", response.status_code, response.text[:300])
                return None

            data = response.json()
            result = data["choices"][0]["message"]["content"].strip()

            if "НЕ НАЙДЕНО" in result.upper() or "NOT FOUND" in result.upper():
                logger.info("Vision API: артикул не обнаружен на изображении.")
                return None

            logger.info("Vision API: успешно извлечено — '%s'", result[:100])
            return result

        except Exception as e:
            logger.error("Ошибка Vision API: %s", e)
            return None

    # ──────────────────────────────────────────
    # Whisper API (транскрибация аудио)
    # ──────────────────────────────────────────

    async def whisper_transcribe(
        self,
        audio_path: str,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> str:
        """
        Транскрибирует аудиофайл в текст через Whisper API.

        Args:
            audio_path: Путь к локальному аудиофайлу.
            model:      Модель Whisper (по умолчанию 'whisper-1').
            api_key:    API-ключ (для multi-tenant).

        Returns:
            str: Распознанный текст.

        Raises:
            FileNotFoundError: Если аудиофайл не найден.
            RuntimeError: При ошибке Whisper API.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудиофайл не найден: {audio_path}")

        url = f"{self._base_url}/audio/transcriptions"
        effective_model = model or settings.WHISPER_MODEL

        extra_headers = {"Authorization": f"Bearer {api_key or self._api_key}"}

        # Для multipart/form-data не устанавливаем Content-Type (httpx сделает сам)
        client = self._get_client()

        try:
            with open(audio_path, "rb") as f:
                files = {"file": (os.path.basename(audio_path), f)}
                data = {"model": effective_model}

                response = await client.post(
                    url,
                    files=files,
                    data=data,
                    headers=extra_headers,
                    timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
                )

            if response.status_code != 200:
                error_detail = response.text[:300]
                logger.error("Whisper API ошибка %d: %s", response.status_code, error_detail)
                raise RuntimeError(f"Ошибка Whisper API (HTTP {response.status_code}).")

            result = response.json()
            text = result.get("text", "").strip()
            logger.info("Whisper: транскрибация успешна — '%s'", text[:100])
            return text

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.error("Ошибка соединения с Whisper API: %s", e)
            raise RuntimeError(f"Whisper API недоступен: {e}")


# ──────────────────────────────────────────────
# Глобальный экземпляр клиента (singleton)
# ──────────────────────────────────────────────

openai_client = OpenAIClient()
