"""
admin_router.py -- Модуль администрирования товаров и цен для владельцев бизнеса.

Предоставляет защищенные API-эндпоинты:
  - GET /admin/products             -- список всех товаров бизнеса
  - POST /admin/products            -- добавление нового товара
  - PUT /admin/products/{product_id} -- обновление цены/остатка товара
  - DELETE /admin/products/{product_id} -- удаление товара

Авторизация:
  Выполняется проверка API-ключа бизнеса через заголовок 'X-API-Key'
  или через стандартный 'Authorization: Bearer <token>'.
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import Business, Product

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin Products"])


# ──────────────────────────────────────────────
# Pydantic-схемы валидации товаров
# ──────────────────────────────────────────────

class ProductCreate(BaseModel):
    """Схема создания нового товара / букета."""
    article: Optional[str] = Field(default=None, description="Артикул товара (например, 'FL-101')")
    name: str = Field(..., min_length=1, max_length=255, description="Название букета")
    category: Optional[str] = Field(default="Букеты", description="Категория товара")
    flower_composition: Optional[str] = Field(default=None, description="Состав цветов в букете")
    size: Optional[str] = Field(default=None, description="Размер букета (S, M, L, XL)")
    price: float = Field(..., ge=0.0, description="Базовая цена букета (должна быть >= 0)")
    discount_price: Optional[float] = Field(default=None, ge=0.0, description="Акционная цена букета (>= 0)")
    image_url: Optional[str] = Field(default=None, description="Ссылка на фото букета")
    stock: int = Field(default=0, ge=0, description="Остаток на складе (должен быть >= 0)")
    description: Optional[str] = Field(default=None, description="Описание товара")
    promotion_info: Optional[str] = Field(default=None, description="Информация об акции или скидке")


class ProductUpdate(BaseModel):
    """Схема обновления имеющегося товара / букета."""
    article: Optional[str] = Field(default=None, description="Новый артикул товара")
    name: Optional[str] = Field(default=None, min_length=1, max_length=255, description="Новое название товара")
    category: Optional[str] = Field(default=None, description="Новая категория товара")
    flower_composition: Optional[str] = Field(default=None, description="Новый состав цветов")
    size: Optional[str] = Field(default=None, description="Новый размер букета")
    price: Optional[float] = Field(default=None, ge=0.0, description="Новая базовая цена букета (>= 0)")
    discount_price: Optional[float] = Field(default=None, ge=0.0, description="Новая акционная цена букета (>= 0)")
    image_url: Optional[str] = Field(default=None, description="Новая ссылка на фото букета")
    stock: Optional[int] = Field(default=None, ge=0, description="Новый остаток на складе (>= 0)")
    description: Optional[str] = Field(default=None, description="Новое описание товара")
    promotion_info: Optional[str] = Field(default=None, description="Новая информация об акции")


class ProductResponse(BaseModel):
    """Схема ответа с данными товара / букета."""
    id: int
    business_id: int
    article: Optional[str] = None
    name: str
    category: Optional[str] = "Букеты"
    flower_composition: Optional[str] = None
    size: Optional[str] = None
    price: float
    discount_price: Optional[float] = None
    image_url: Optional[str] = None
    stock: int
    description: Optional[str] = None
    promotion_info: Optional[str] = None

    class Config:
        from_attributes = True


# ──────────────────────────────────────────────
# Функция проверки авторизации владельца бизнеса
# ──────────────────────────────────────────────

def get_current_business(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> Business:
    """
    Проверяет API-ключ владельца бизнеса в заголовках запроса.

    Поддерживает:
      1. Заголовок 'X-API-Key: <key>'
      2. Заголовок 'Authorization: Bearer <key>'

    Returns:
        Business: Объект бизнеса из БД.

    Raises:
        HTTPException(401): Если токен не передан или бизнес не найден.
    """
    token = x_api_key

    if not token and authorization:
        if authorization.startswith("Bearer "):
            token = authorization.split(" ", 1)[1].strip()
        else:
            token = authorization.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Необходим API-ключ авторизации (заголовок X-API-Key или Authorization: Bearer <key>).",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Ищем бизнес с указанным API-ключом
    business = db.query(Business).filter(Business.api_key_ai == token).first()

    if not business:
        # Для удобства разработки/тестирования: если введен токен формата 'business_id_1', ищем по ID
        if token.startswith("business_id_"):
            try:
                b_id = int(token.replace("business_id_", ""))
                business = db.query(Business).filter(Business.id == b_id).first()
            except ValueError:
                pass

    if not business:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Недействительный API-ключ владельца бизнеса.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return business


# ──────────────────────────────────────────────
# Эндпоинты управления товарами (CRUD)
# ──────────────────────────────────────────────

@router.get("/products", response_model=List[ProductResponse])
def get_products(
    business: Business = Depends(get_current_business),
    db: Session = Depends(get_db),
):
    """
    Получить список всех товаров текущего бизнеса/магазина (изоляция по shop_id).
    """
    target_shop_id = business.shop_id or business.id
    products = (
        db.query(Product)
        .filter(Product.shop_id == target_shop_id)
        .order_by(Product.id.asc())
        .all()
    )
    return products


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    business: Business = Depends(get_current_business),
    db: Session = Depends(get_db),
):
    """
    Добавить новый товар в базу данных текущего бизнеса (shop_id).
    """
    target_shop_id = business.shop_id or business.id
    new_product = Product(
        shop_id=target_shop_id,
        business_id=business.id,
        article=payload.article,
        name=payload.name,
        category=payload.category or "Букеты",
        flower_composition=payload.flower_composition,
        size=payload.size,
        price=payload.price,
        discount_price=payload.discount_price,
        image_url=payload.image_url,
        stock=payload.stock,
        description=payload.description,
        promotion_info=payload.promotion_info,
    )
    db.add(new_product)
    db.commit()
    db.refresh(new_product)

    logger.info("Бизнес/Shop #%d добавил товар '%s' (ID #%d)", target_shop_id, new_product.name, new_product.id)
    return new_product


@router.put("/products/{product_id}", response_model=ProductResponse)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    business: Business = Depends(get_current_business),
    db: Session = Depends(get_db),
):
    """
    Обновить параметры имеющегося товара (цену, остаток, название, описание, акцию и т.д.).
    """
    target_shop_id = business.shop_id or business.id
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.shop_id == target_shop_id)
        .first()
    )

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Товар с ID {product_id} не найден в каталоге бизнеса.",
        )

    if payload.article is not None:
        product.article = payload.article
    if payload.name is not None:
        product.name = payload.name
    if payload.category is not None:
        product.category = payload.category
    if payload.flower_composition is not None:
        product.flower_composition = payload.flower_composition
    if payload.size is not None:
        product.size = payload.size
    if payload.price is not None:
        product.price = payload.price
    if payload.discount_price is not None:
        product.discount_price = payload.discount_price
    if payload.image_url is not None:
        product.image_url = payload.image_url
    if payload.stock is not None:
        product.stock = payload.stock
    if payload.description is not None:
        product.description = payload.description
    if payload.promotion_info is not None:
        product.promotion_info = payload.promotion_info

    db.commit()
    db.refresh(product)

    logger.info("Бизнес/Shop #%d обновил товар ID #%d (%s, {price: %s, stock: %s})",
                target_shop_id, product.id, product.name, product.price, product.stock)
    return product


@router.delete("/products/{product_id}", status_code=status.HTTP_200_OK)
def delete_product(
    product_id: int,
    business: Business = Depends(get_current_business),
    db: Session = Depends(get_db),
):
    """
    Удалить товар из базы данных текущего бизнеса.
    """
    target_shop_id = business.shop_id or business.id
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.shop_id == target_shop_id)
        .first()
    )

    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Товар с ID {product_id} не найден в каталоге бизнеса.",
        )

    product_name = product.name
    db.delete(product)
    db.commit()

    logger.info("Бизнес/Shop #%d удалил товар '%s' (ID #%d)", target_shop_id, product_name, product_id)
    return {
        "status": "success",
        "message": f"Товар '{product_name}' (ID {product_id}) успешно удален.",
    }
