"""
core/response_models.py

Structured response models for the Grocery Shopping Assistant.

Purpose
-------

The chatbot should NOT depend on generated text for structured commerce
information.

Example:

User:
    "do you have coffee?"

Backend:
    Supabase returns the real product and SKU variants.

Groq:
    Generates conversational text such as:
        "Yes, we have Nescafe Classic Instant Coffee..."

Frontend:
    Receives structured product information separately and renders:
        - product image
        - SKU
        - size
        - price
        - stock
        - availability


Architecture
------------

Cohere
    ↓
Tool
    ↓
Supabase / RAG
    ↓
Verified tool result
    ↓
ResponsePayload
    ├── text
    ├── products
    ├── cart
    ├── orders
    ├── recommendations
    └── metadata
    ↓
Groq API 3 only creates/rewrites `text`
    ↓
Streamlit renders both text + structured cards


IMPORTANT
---------

These models contain no database logic.

They do not:
    - query Supabase
    - call Cohere
    - call Groq
    - calculate prices
    - invent SKUs
    - alter cart/orders

They only normalize verified data into a consistent response structure.
"""

from __future__ import annotations

from dataclasses import (
    asdict,
    dataclass,
    field,
    is_dataclass,
)

from datetime import (
    date,
    datetime,
)

from decimal import Decimal

from typing import (
    Any,
    Mapping,
    Sequence,
)

from uuid import UUID


# ============================================================
# HELPERS
# ============================================================


def _to_plain_value(
    value: Any,
) -> Any:
    """
    Convert common Python/backend objects into JSON-safe values.
    """

    if value is None:

        return None

    if isinstance(
        value,
        (
            str,
            int,
            float,
            bool,
        ),
    ):

        return value

    if isinstance(
        value,
        Decimal,
    ):

        return float(
            value
        )

    if isinstance(
        value,
        (
            date,
            datetime,
        ),
    ):

        return (
            value.isoformat()
        )

    if isinstance(
        value,
        UUID,
    ):

        return str(
            value
        )

    if is_dataclass(
        value
    ):

        return _to_plain_value(
            asdict(
                value
            )
        )

    model_dump = getattr(
        value,
        "model_dump",
        None,
    )

    if callable(
        model_dump
    ):

        try:

            return _to_plain_value(
                model_dump()
            )

        except Exception:

            pass

    if isinstance(
        value,
        Mapping,
    ):

        return {
            str(
                key
            ):
                _to_plain_value(
                    item
                )
            for (
                key,
                item,
            )
            in value.items()
        }

    if (
        isinstance(
            value,
            Sequence,
        )
        and not isinstance(
            value,
            (
                str,
                bytes,
            ),
        )
    ):

        return [
            _to_plain_value(
                item
            )
            for item in value
        ]

    return str(
        value
    )


def _optional_text(
    value: Any,
) -> str | None:

    if value is None:

        return None

    value = str(
        value
    ).strip()

    return (
        value
        if value
        else None
    )


def _safe_float(
    value: Any,
) -> float | None:

    if value is None:

        return None

    try:

        return float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


def _safe_int(
    value: Any,
) -> int | None:

    if value is None:

        return None

    try:

        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return None


# ============================================================
# PRODUCT VARIANT
# ============================================================


@dataclass(
    slots=True
)
class ProductVariantResponse:
    """
    One purchasable SKU variant.

    Example:

        Amul Milk
            500 ml
            SKU: abc
            ₹32
            stock: 12
    """

    sku_id: str | None = None

    size: str | None = None

    pack_size: str | None = None

    quantity: Any = None

    unit: str | None = None

    price: float | None = None

    mrp: float | None = None

    currency: str | None = None

    stock: int | None = None

    in_stock: bool = False

    image_url: str | None = None

    is_discounted: bool = False

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "ProductVariantResponse":

        return cls(
            sku_id=(
                _optional_text(
                    value.get(
                        "sku_id"
                    )
                    or value.get(
                        "id"
                    )
                    or value.get(
                        "product_id"
                    )
                )
            ),

            size=(
                _optional_text(
                    value.get(
                        "size"
                    )
                    or value.get(
                        "variant_label"
                    )
                )
            ),

            pack_size=(
                _optional_text(
                    value.get(
                        "pack_size"
                    )
                )
            ),

            quantity=(
                _to_plain_value(
                    value.get(
                        "quantity"
                    )
                )
            ),

            unit=(
                _optional_text(
                    value.get(
                        "unit"
                    )
                )
            ),

            price=(
                _safe_float(
                    value.get(
                        "price"
                    )
                )
            ),

            mrp=(
                _safe_float(
                    value.get(
                        "mrp"
                    )
                )
            ),

            currency=(
                _optional_text(
                    value.get(
                        "currency"
                    )
                )
            ),

            stock=(
                _safe_int(
                    value.get(
                        "stock"
                    )
                )
            ),

            in_stock=(
                bool(
                    value.get(
                        "in_stock",
                        False,
                    )
                )
            ),

            image_url=(
                _optional_text(
                    value.get(
                        "image_url"
                    )
                )
            ),

            is_discounted=(
                bool(
                    value.get(
                        "is_discounted",
                        False,
                    )
                )
            ),

            metadata={},
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return _to_plain_value(
            asdict(
                self
            )
        )


# ============================================================
# PRODUCT
# ============================================================


@dataclass(
    slots=True
)
class ProductResponse:
    """
    One logical product containing multiple SKU variants.
    """

    product_key: str | None = None

    name: str | None = None

    brand: str | None = None

    category: str | None = None

    category_id: str | None = None

    image_url: str | None = None

    short_description: str | None = None

    rating: float | None = None

    is_available: bool = False

    all_variants_out_of_stock: bool = False

    available_variant_count: int = 0

    variant_count: int = 0

    price_from: float | None = None

    price_to: float | None = None

    variants: list[
        ProductVariantResponse
    ] = field(
        default_factory=list
    )

    recommendation_reason: str | None = None

    semantic_tags: list[
        str
    ] = field(
        default_factory=list
    )

    meal_contexts: list[
        str
    ] = field(
        default_factory=list
    )

    use_cases: list[
        str
    ] = field(
        default_factory=list
    )

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "ProductResponse":

        raw_variants = (
            value.get(
                "variants"
            )
            or value.get(
                "eligible_variants"
            )
            or []
        )

        variants: list[
            ProductVariantResponse
        ] = []

        if (
            isinstance(
                raw_variants,
                Sequence,
            )
            and not isinstance(
                raw_variants,
                (
                    str,
                    bytes,
                ),
            )
        ):

            for variant in raw_variants:

                if isinstance(
                    variant,
                    Mapping,
                ):

                    variants.append(
                        ProductVariantResponse.from_mapping(
                            variant
                        )
                    )

        available_variant_count = (
            value.get(
                "available_variant_count"
            )
        )

        if available_variant_count is None:

            available_variant_count = sum(
                1
                for variant
                in variants
                if variant.in_stock
            )

        variant_count = (
            value.get(
                "variant_count"
            )
        )

        if variant_count is None:

            variant_count = len(
                variants
            )

        semantic_tags = (
            value.get(
                "semantic_tags"
            )
            or []
        )

        meal_contexts = (
            value.get(
                "meal_contexts"
            )
            or []
        )

        use_cases = (
            value.get(
                "use_cases"
            )
            or []
        )

        return cls(
            product_key=(
                _optional_text(
                    value.get(
                        "product_key"
                    )
                    or value.get(
                        "logical_product_key"
                    )
                )
            ),

            name=(
                _optional_text(
                    value.get(
                        "name"
                    )
                    or value.get(
                        "product_name"
                    )
                )
            ),

            brand=(
                _optional_text(
                    value.get(
                        "brand"
                    )
                )
            ),

            category=(
                _optional_text(
                    value.get(
                        "category"
                    )
                    or value.get(
                        "category_slug"
                    )
                )
            ),

            category_id=(
                _optional_text(
                    value.get(
                        "category_id"
                    )
                )
            ),

            image_url=(
                _optional_text(
                    value.get(
                        "image_url"
                    )
                )
            ),

            short_description=(
                _optional_text(
                    value.get(
                        "short_description"
                    )
                    or value.get(
                        "semantic_description"
                    )
                )
            ),

            rating=(
                _safe_float(
                    value.get(
                        "rating"
                    )
                )
            ),

            is_available=(
                bool(
                    value.get(
                        "is_available",
                        any(
                            variant.in_stock
                            for variant
                            in variants
                        ),
                    )
                )
            ),

            all_variants_out_of_stock=(
                bool(
                    value.get(
                        "all_variants_out_of_stock",
                        (
                            bool(
                                variants
                            )
                            and not any(
                                variant.in_stock
                                for variant
                                in variants
                            )
                        ),
                    )
                )
            ),

            available_variant_count=(
                int(
                    available_variant_count
                    or 0
                )
            ),

            variant_count=(
                int(
                    variant_count
                    or 0
                )
            ),

            price_from=(
                _safe_float(
                    value.get(
                        "price_from"
                    )
                )
            ),

            price_to=(
                _safe_float(
                    value.get(
                        "price_to"
                    )
                )
            ),

            variants=(
                variants
            ),

            recommendation_reason=(
                _optional_text(
                    value.get(
                        "recommendation_reason"
                    )
                    or value.get(
                        "health_context"
                    )
                )
            ),

            semantic_tags=[
                str(
                    item
                )
                for item
                in semantic_tags
                if item is not None
            ],

            meal_contexts=[
                str(
                    item
                )
                for item
                in meal_contexts
                if item is not None
            ],

            use_cases=[
                str(
                    item
                )
                for item
                in use_cases
                if item is not None
            ],

            metadata={},
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return {
            "product_key":
                self.product_key,

            "name":
                self.name,

            "brand":
                self.brand,

            "category":
                self.category,

            "category_id":
                self.category_id,

            "image_url":
                self.image_url,

            "short_description":
                self.short_description,

            "rating":
                self.rating,

            "is_available":
                self.is_available,

            "all_variants_out_of_stock":
                self.all_variants_out_of_stock,

            "available_variant_count":
                self.available_variant_count,

            "variant_count":
                self.variant_count,

            "price_from":
                self.price_from,

            "price_to":
                self.price_to,

            "variants": [
                variant.to_dict()
                for variant
                in self.variants
            ],

            "recommendation_reason":
                self.recommendation_reason,

            "semantic_tags":
                list(
                    self.semantic_tags
                ),

            "meal_contexts":
                list(
                    self.meal_contexts
                ),

            "use_cases":
                list(
                    self.use_cases
                ),

            "metadata":
                _to_plain_value(
                    self.metadata
                ),
        }


# ============================================================
# CART ITEM
# ============================================================


@dataclass(
    slots=True
)
class CartItemResponse:

    cart_item_id: str | None = None

    sku_id: str | None = None

    product_key: str | None = None

    product_name: str | None = None

    brand: str | None = None

    size: str | None = None

    quantity: int = 1

    unit_price: float | None = None

    line_total: float | None = None

    currency: str | None = None

    stock: int | None = None

    in_stock: bool = False

    image_url: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "CartItemResponse":

        quantity = (
            _safe_int(
                value.get(
                    "quantity"
                )
            )
            or 1
        )

        return cls(
            cart_item_id=(
                _optional_text(
                    value.get(
                        "cart_item_id"
                    )
                    or value.get(
                        "id"
                    )
                )
            ),

            sku_id=(
                _optional_text(
                    value.get(
                        "sku_id"
                    )
                    or value.get(
                        "product_id"
                    )
                )
            ),

            product_key=(
                _optional_text(
                    value.get(
                        "product_key"
                    )
                )
            ),

            product_name=(
                _optional_text(
                    value.get(
                        "product_name"
                    )
                    or value.get(
                        "name"
                    )
                )
            ),

            brand=(
                _optional_text(
                    value.get(
                        "brand"
                    )
                )
            ),

            size=(
                _optional_text(
                    value.get(
                        "size"
                    )
                    or value.get(
                        "pack_size"
                    )
                )
            ),

            quantity=(
                quantity
            ),

            unit_price=(
                _safe_float(
                    value.get(
                        "unit_price"
                    )
                    or value.get(
                        "price"
                    )
                )
            ),

            line_total=(
                _safe_float(
                    value.get(
                        "line_total"
                    )
                    or value.get(
                        "subtotal"
                    )
                )
            ),

            currency=(
                _optional_text(
                    value.get(
                        "currency"
                    )
                )
            ),

            stock=(
                _safe_int(
                    value.get(
                        "stock"
                    )
                )
            ),

            in_stock=(
                bool(
                    value.get(
                        "in_stock",
                        False,
                    )
                )
            ),

            image_url=(
                _optional_text(
                    value.get(
                        "image_url"
                    )
                )
            ),
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return _to_plain_value(
            asdict(
                self
            )
        )


# ============================================================
# CART
# ============================================================


@dataclass(
    slots=True
)
class CartResponse:

    items: list[
        CartItemResponse
    ] = field(
        default_factory=list
    )

    item_count: int = 0

    total_quantity: int = 0

    subtotal: float | None = None

    total: float | None = None

    currency: str | None = None

    is_empty: bool = True

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "CartResponse":

        raw_items = (
            value.get(
                "items"
            )
            or value.get(
                "cart_items"
            )
            or []
        )

        items: list[
            CartItemResponse
        ] = []

        if (
            isinstance(
                raw_items,
                Sequence,
            )
            and not isinstance(
                raw_items,
                (
                    str,
                    bytes,
                ),
            )
        ):

            for item in raw_items:

                if isinstance(
                    item,
                    Mapping,
                ):

                    items.append(
                        CartItemResponse.from_mapping(
                            item
                        )
                    )

        total_quantity = (
            value.get(
                "total_quantity"
            )
        )

        if total_quantity is None:

            total_quantity = sum(
                item.quantity
                for item
                in items
            )

        return cls(
            items=items,

            item_count=(
                _safe_int(
                    value.get(
                        "item_count"
                    )
                )
                or len(
                    items
                )
            ),

            total_quantity=(
                int(
                    total_quantity
                    or 0
                )
            ),

            subtotal=(
                _safe_float(
                    value.get(
                        "subtotal"
                    )
                )
            ),

            total=(
                _safe_float(
                    value.get(
                        "total"
                    )
                    or value.get(
                        "total_amount"
                    )
                )
            ),

            currency=(
                _optional_text(
                    value.get(
                        "currency"
                    )
                )
            ),

            is_empty=(
                not bool(
                    items
                )
            ),
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return {
            "items": [
                item.to_dict()
                for item
                in self.items
            ],

            "item_count":
                self.item_count,

            "total_quantity":
                self.total_quantity,

            "subtotal":
                self.subtotal,

            "total":
                self.total,

            "currency":
                self.currency,

            "is_empty":
                self.is_empty,
        }


# ============================================================
# ORDER ITEM
# ============================================================


@dataclass(
    slots=True
)
class OrderItemResponse:
    """
    Historical purchased item.

    Important:
    unit_price and line_total refer to historical order data,
    not current catalog pricing.
    """

    order_item_id: str | None = None

    sku_id: str | None = None

    product_name: str | None = None

    brand: str | None = None

    size: str | None = None

    quantity: int = 1

    unit_price: float | None = None

    line_total: float | None = None

    currency: str | None = None

    image_url: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "OrderItemResponse":

        return cls(
            order_item_id=(
                _optional_text(
                    value.get(
                        "order_item_id"
                    )
                    or value.get(
                        "id"
                    )
                )
            ),

            sku_id=(
                _optional_text(
                    value.get(
                        "sku_id"
                    )
                    or value.get(
                        "product_id"
                    )
                )
            ),

            product_name=(
                _optional_text(
                    value.get(
                        "product_name"
                    )
                    or value.get(
                        "name"
                    )
                )
            ),

            brand=(
                _optional_text(
                    value.get(
                        "brand"
                    )
                )
            ),

            size=(
                _optional_text(
                    value.get(
                        "size"
                    )
                    or value.get(
                        "pack_size"
                    )
                )
            ),

            quantity=(
                _safe_int(
                    value.get(
                        "quantity"
                    )
                )
                or 1
            ),

            unit_price=(
                _safe_float(
                    value.get(
                        "unit_price"
                    )
                    or value.get(
                        "price"
                    )
                )
            ),

            line_total=(
                _safe_float(
                    value.get(
                        "line_total"
                    )
                    or value.get(
                        "subtotal"
                    )
                )
            ),

            currency=(
                _optional_text(
                    value.get(
                        "currency"
                    )
                )
            ),

            image_url=(
                _optional_text(
                    value.get(
                        "image_url"
                    )
                )
            ),
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return _to_plain_value(
            asdict(
                self
            )
        )


# ============================================================
# ORDER
# ============================================================


@dataclass(
    slots=True
)
class OrderResponse:

    order_id: str | None = None

    created_at: str | None = None

    order_status: str | None = None

    payment_status: str | None = None

    total_amount: float | None = None

    currency: str | None = None

    item_count: int = 0

    items: list[
        OrderItemResponse
    ] = field(
        default_factory=list
    )

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[
            str,
            Any,
        ],
    ) -> "OrderResponse":

        raw_items = (
            value.get(
                "items"
            )
            or value.get(
                "order_items"
            )
            or []
        )

        items: list[
            OrderItemResponse
        ] = []

        if (
            isinstance(
                raw_items,
                Sequence,
            )
            and not isinstance(
                raw_items,
                (
                    str,
                    bytes,
                ),
            )
        ):

            for item in raw_items:

                if isinstance(
                    item,
                    Mapping,
                ):

                    items.append(
                        OrderItemResponse.from_mapping(
                            item
                        )
                    )

        return cls(
            order_id=(
                _optional_text(
                    value.get(
                        "order_id"
                    )
                    or value.get(
                        "id"
                    )
                )
            ),

            created_at=(
                _optional_text(
                    value.get(
                        "created_at"
                    )
                    or value.get(
                        "order_date"
                    )
                )
            ),

            order_status=(
                _optional_text(
                    value.get(
                        "order_status"
                    )
                    or value.get(
                        "status"
                    )
                )
            ),

            payment_status=(
                _optional_text(
                    value.get(
                        "payment_status"
                    )
                )
            ),

            total_amount=(
                _safe_float(
                    value.get(
                        "total_amount"
                    )
                    or value.get(
                        "total"
                    )
                )
            ),

            currency=(
                _optional_text(
                    value.get(
                        "currency"
                    )
                )
            ),

            item_count=(
                _safe_int(
                    value.get(
                        "item_count"
                    )
                )
                or len(
                    items
                )
            ),

            items=items,

            metadata={},
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return {
            "order_id":
                self.order_id,

            "created_at":
                self.created_at,

            "order_status":
                self.order_status,

            "payment_status":
                self.payment_status,

            "total_amount":
                self.total_amount,

            "currency":
                self.currency,

            "item_count":
                self.item_count,

            "items": [
                item.to_dict()
                for item
                in self.items
            ],

            "metadata":
                _to_plain_value(
                    self.metadata
                ),
        }


# ============================================================
# RESPONSE PAYLOAD
# ============================================================


@dataclass(
    slots=True
)
class ResponsePayload:
    """
    Main response object returned by the response layer.

    This is what app.py should eventually render.

    Example:

        response.text
            -> conversational chatbot sentence

        response.products
            -> product cards

        response.orders
            -> order cards

        response.cart
            -> cart section
    """

    text: str = ""

    response_type: str = "text"

    success: bool = True

    products: list[
        ProductResponse
    ] = field(
        default_factory=list
    )

    alternatives: list[
        ProductResponse
    ] = field(
        default_factory=list
    )

    recommendations: list[
        ProductResponse
    ] = field(
        default_factory=list
    )

    cart: CartResponse | None = None

    orders: list[
        OrderResponse
    ] = field(
        default_factory=list
    )

    latest_order: OrderResponse | None = None

    tool_names: list[
        str
    ] = field(
        default_factory=list
    )

    requires_groq: bool = False

    groq_used: bool = False

    groq_key_slot: int | None = None

    error_code: str | None = None

    metadata: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    def has_structured_content(
        self,
    ) -> bool:

        return bool(
            self.products
            or self.alternatives
            or self.recommendations
            or self.cart is not None
            or self.orders
            or self.latest_order is not None
        )

    def to_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return {
            "text":
                self.text,

            "response_type":
                self.response_type,

            "success":
                self.success,

            "products": [
                product.to_dict()
                for product
                in self.products
            ],

            "alternatives": [
                product.to_dict()
                for product
                in self.alternatives
            ],

            "recommendations": [
                product.to_dict()
                for product
                in self.recommendations
            ],

            "cart": (
                self.cart.to_dict()
                if self.cart
                else None
            ),

            "orders": [
                order.to_dict()
                for order
                in self.orders
            ],

            "latest_order": (
                self.latest_order.to_dict()
                if self.latest_order
                else None
            ),

            "tool_names":
                list(
                    self.tool_names
                ),

            "requires_groq":
                self.requires_groq,

            "groq_used":
                self.groq_used,

            "groq_key_slot":
                self.groq_key_slot,

            "error_code":
                self.error_code,

            "metadata":
                _to_plain_value(
                    self.metadata
                ),
        }


# ============================================================
# PRODUCT LIST NORMALIZER
# ============================================================


def normalize_products(
    values: Any,
) -> list[
    ProductResponse
]:
    """
    Convert arbitrary verified product mappings into ProductResponse
    objects.
    """

    if values is None:

        return []

    if isinstance(
        values,
        Mapping,
    ):

        values = [
            values
        ]

    if (
        not isinstance(
            values,
            Sequence,
        )
        or isinstance(
            values,
            (
                str,
                bytes,
            ),
        )
    ):

        return []

    products: list[
        ProductResponse
    ] = []

    for value in values:

        if isinstance(
            value,
            ProductResponse,
        ):

            products.append(
                value
            )

            continue

        if isinstance(
            value,
            Mapping,
        ):

            products.append(
                ProductResponse.from_mapping(
                    value
                )
            )

    return products


# ============================================================
# ORDER LIST NORMALIZER
# ============================================================


def normalize_orders(
    values: Any,
) -> list[
    OrderResponse
]:

    if values is None:

        return []

    if isinstance(
        values,
        Mapping,
    ):

        values = [
            values
        ]

    if (
        not isinstance(
            values,
            Sequence,
        )
        or isinstance(
            values,
            (
                str,
                bytes,
            ),
        )
    ):

        return []

    orders: list[
        OrderResponse
    ] = []

    for value in values:

        if isinstance(
            value,
            OrderResponse,
        ):

            orders.append(
                value
            )

            continue

        if isinstance(
            value,
            Mapping,
        ):

            orders.append(
                OrderResponse.from_mapping(
                    value
                )
            )

    return orders


# ============================================================
# RESPONSE FACTORY
# ============================================================


def make_response_payload(
    *,
    text: str = "",
    response_type: str = "text",
    success: bool = True,
    products: Any = None,
    alternatives: Any = None,
    recommendations: Any = None,
    cart: Mapping[
        str,
        Any,
    ] | CartResponse | None = None,
    orders: Any = None,
    latest_order: (
        Mapping[
            str,
            Any,
        ]
        | OrderResponse
        | None
    ) = None,
    tool_names: Sequence[
        str
    ] | None = None,
    requires_groq: bool = False,
    groq_used: bool = False,
    groq_key_slot: int | None = None,
    error_code: str | None = None,
    metadata: Mapping[
        str,
        Any,
    ] | None = None,
) -> ResponsePayload:
    """
    Convenience factory used later by response_engine.py.
    """

    normalized_cart: (
        CartResponse
        | None
    )

    if isinstance(
        cart,
        CartResponse,
    ):

        normalized_cart = cart

    elif isinstance(
        cart,
        Mapping,
    ):

        normalized_cart = (
            CartResponse.from_mapping(
                cart
            )
        )

    else:

        normalized_cart = None

    normalized_latest_order: (
        OrderResponse
        | None
    )

    if isinstance(
        latest_order,
        OrderResponse,
    ):

        normalized_latest_order = (
            latest_order
        )

    elif isinstance(
        latest_order,
        Mapping,
    ):

        normalized_latest_order = (
            OrderResponse.from_mapping(
                latest_order
            )
        )

    else:

        normalized_latest_order = None

    return ResponsePayload(
        text=(
            str(
                text
                or ""
            ).strip()
        ),

        response_type=(
            str(
                response_type
                or "text"
            ).strip()
        ),

        success=(
            bool(
                success
            )
        ),

        products=(
            normalize_products(
                products
            )
        ),

        alternatives=(
            normalize_products(
                alternatives
            )
        ),

        recommendations=(
            normalize_products(
                recommendations
            )
        ),

        cart=(
            normalized_cart
        ),

        orders=(
            normalize_orders(
                orders
            )
        ),

        latest_order=(
            normalized_latest_order
        ),

        tool_names=[
            str(
                name
            )
            for name
            in (
                tool_names
                or []
            )
            if name
        ],

        requires_groq=(
            bool(
                requires_groq
            )
        ),

        groq_used=(
            bool(
                groq_used
            )
        ),

        groq_key_slot=(
            groq_key_slot
        ),

        error_code=(
            _optional_text(
                error_code
            )
        ),

        metadata=(
            _to_plain_value(
                dict(
                    metadata
                    or {}
                )
            )
        ),
    )