"""
rate_limiter.py -- Модуль ограничения частоты запросов (Rate Limiting).
Используется slowapi для защиты API и вебхуков от злоупотреблений и DDoS.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
