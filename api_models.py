"""
api_models.py

Public API request/response models for the Grocery Shopping Assistant.

This module is intentionally frontend-agnostic.

It can be consumed by:

    React
    Next.js
    Vue
    Angular
    Flutter
    React Native
    Android
    iOS
    Postman
    another Python application
    any HTTP client

The API exposes stable JSON.

IMPORTANT
---------

This file contains API DATA MODELS only.

It does NOT:

    - call Supabase
    - call Cohere
    - call Groq
    - execute tools
    - perform authentication
    - access Streamlit
    - calculate prices
    - calculate totals
    - generate image URLs

All commerce data comes from verified backend responses.
"""

from __future__ import annotations

from typing import (
    Any,
    Literal,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


# ============================================================
# COMMON BASE MODEL
# ============================================================


class APIModel(
    BaseModel
):
    """
    Shared configuration for all public API models.

    from_attributes=True allows us to convert dataclasses such as:

        ResponsePayload
        ProductResponse
        OrderResponse

    directly into API models later.

    extra="ignore" keeps the public API stable even if internal backend
    models gain additional fields.
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        extra="ignore",
        str_strip_whitespace=True,
    )


# ============================================================
# API VERSION
# ============================================================


API_VERSION = "1.0"


# ============================================================
# CHAT REQUEST
# ============================================================


class ChatRequest(
    APIModel
):
    """
    Request sent by any frontend.

    Minimum request:

        {
            "message": "do you have milk"
        }

    A frontend may optionally maintain a conversation_id.

    Example:

        {
            "message": "add the 1 litre one",
            "conversation_id": "abc-123"
        }

    Authentication is NOT sent in this JSON.

    Authentication will use:

        Authorization: Bearer <SUPABASE_ACCESS_TOKEN>
    """

    message: str = Field(
        ...,
        min_length=1,
        max_length=20_000,
        description=(
            "Natural-language message sent by the user."
        ),
        examples=[
            "do you have milk",
            "show coffee under 500",
            "I need something healthy for breakfast",
            "what is in my cart",
            "what was my last order",
        ],
    )

    conversation_id: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Optional client conversation identifier. "
            "Useful for maintaining context across HTTP requests."
        ),
    )

    locale: str | None = Field(
        default=None,
        max_length=30,
        description=(
            "Optional frontend locale such as en-IN or hi-IN."
        ),
        examples=[
            "en-IN"
        ],
    )

    client_context: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict,
        description=(
            "Optional non-authoritative frontend context. "
            "For conversational continuity, send the previous "
            "ChatResponse.client_state object here. "
            "Must never be treated as trusted database data."
        ),
    )


# ============================================================
# PRODUCT VARIANT / SKU
# ============================================================


class ProductVariantAPI(
    APIModel
):
    """
    One exact purchasable SKU.

    Example:

        Amul Taaza Milk
        500 ml

    and:

        Amul Taaza Milk
        2 L

    are separate SKU variants of the same logical product.
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
    ] = Field(
        default_factory=dict
    )


# ============================================================
# PRODUCT
# ============================================================


class ProductAPI(
    APIModel
):
    """
    One logical catalog product.

    A ProductAPI may contain multiple ProductVariantAPI objects.

    Frontend example:

        Product card
            image
            name
            brand

            Variant 1
            Variant 2
            Variant 3
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
        ProductVariantAPI
    ] = Field(
        default_factory=list
    )

    recommendation_reason: str | None = None

    semantic_tags: list[
        str
    ] = Field(
        default_factory=list
    )

    meal_contexts: list[
        str
    ] = Field(
        default_factory=list
    )

    use_cases: list[
        str
    ] = Field(
        default_factory=list
    )

    metadata: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict
    )


# ============================================================
# CART ITEM
# ============================================================


class CartItemAPI(
    APIModel
):

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


# ============================================================
# CART
# ============================================================


class CartAPI(
    APIModel
):

    items: list[
        CartItemAPI
    ] = Field(
        default_factory=list
    )

    item_count: int = 0

    total_quantity: int = 0

    subtotal: float | None = None

    total: float | None = None

    currency: str | None = None

    is_empty: bool = True


# ============================================================
# ORDER ITEM
# ============================================================


class OrderItemAPI(
    APIModel
):
    """
    Historical purchased item.

    IMPORTANT:

    unit_price and line_total must come from order history.

    They must NOT be replaced with today's catalog prices.
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


# ============================================================
# ORDER
# ============================================================


class OrderAPI(
    APIModel
):

    order_id: str | None = None

    created_at: str | None = None

    order_status: str | None = None

    payment_status: str | None = None

    total_amount: float | None = None

    currency: str | None = None

    item_count: int = 0

    items: list[
        OrderItemAPI
    ] = Field(
        default_factory=list
    )

    metadata: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict
    )


# ============================================================
# RESPONSE TYPE
# ============================================================


ResponseType = Literal[
    "text",
    "database",
    "products",
    "recommendations",
    "cart",
    "orders",
    "error",
]


# ============================================================
# API CONVERSATION / CLIENT STATE
# ============================================================


class ClientStateAPI(
    APIModel
):
    """
    Safe conversational state returned by the API.

    PURPOSE
    -------

    The Hugging Face backend is API-only and therefore does not use
    Streamlit st.session_state.

    A stateless frontend can:

        1. receive client_state from POST /chat
        2. keep it locally for the active conversation
        3. send it back on the next request as ChatRequest.client_context

    Example:

        Response:

            {
                "conversation_id": "conversation-123",
                "client_state": {
                    "chat_history": [...],
                    "conversation_context": {...},
                    "last_product_context": {...},
                    "pending_action": null
                }
            }

        Next request:

            {
                "message": "add the 1 litre one",
                "conversation_id": "conversation-123",
                "client_context": previousResponse.client_state
            }

    SECURITY
    --------

    This model must NEVER contain:

        access_token
        refresh_token
        Authorization header
        Supabase secret key
        Groq API keys
        Cohere API key
        passwords

    It is conversational context only.

    It is NOT authoritative for:

        authenticated user identity
        price
        stock
        SKU availability
        cart contents
        cart totals
        orders
        order status

    Those values continue to come from verified backend tools and
    Supabase/RLS.
    """

    conversation_id: str | None = Field(
        default=None,
        max_length=200,
    )

    chat_history: list[
        dict[
            str,
            str,
        ]
    ] = Field(
        default_factory=list,
        description=(
            "Safe user/assistant role-content history for conversational "
            "continuity."
        ),
    )

    conversation_context: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict,
        description=(
            "Small non-authoritative conversational context maintained "
            "by the backend."
        ),
    )

    last_product_context: Any = Field(
        default=None,
        description=(
            "Previously displayed verified product/SKU reference context "
            "used for safe follow-up phrases such as 'the 1 litre one'."
        ),
    )

    pending_action: dict[
        str,
        Any,
    ] | None = Field(
        default=None,
        description=(
            "Optional unresolved conversational action waiting for user "
            "clarification."
        ),
    )


# ============================================================
# CHAT RESPONSE
# ============================================================


class ChatResponse(
    APIModel
):
    """
    Main response returned by:

        POST /chat

    This model contains BOTH:

        conversational response text

    and:

        structured verified commerce data

    Therefore a frontend never needs to parse the chatbot text to find
    product information.

    For follow-up conversational context, the frontend should preserve
    client_state and send it back as client_context on the next request.
    """

    api_version: str = Field(
        default=API_VERSION
    )

    success: bool = True

    text: str = ""

    response_type: ResponseType = (
        "text"
    )

    conversation_id: str | None = None

    # ========================================================
    # SAFE CONVERSATIONAL STATE
    # ========================================================

    client_state: ClientStateAPI = Field(
        default_factory=ClientStateAPI,
        description=(
            "Safe request-to-request conversational state. "
            "Frontends may send this value back as client_context on "
            "the next POST /chat request."
        ),
    )

    # ========================================================
    # PRODUCTS
    # ========================================================

    products: list[
        ProductAPI
    ] = Field(
        default_factory=list
    )

    # ========================================================
    # PRODUCT ALTERNATIVES
    # ========================================================

    alternatives: list[
        ProductAPI
    ] = Field(
        default_factory=list
    )

    # ========================================================
    # SEMANTIC / RAG RECOMMENDATIONS
    # ========================================================

    recommendations: list[
        ProductAPI
    ] = Field(
        default_factory=list
    )

    # ========================================================
    # CART
    # ========================================================

    cart: CartAPI | None = None

    # ========================================================
    # ORDER HISTORY
    # ========================================================

    orders: list[
        OrderAPI
    ] = Field(
        default_factory=list
    )

    # ========================================================
    # LATEST ORDER
    # ========================================================

    latest_order: OrderAPI | None = None

    # ========================================================
    # ERROR
    # ========================================================

    error_code: str | None = None

    # ========================================================
    # OPTIONAL SAFE PUBLIC METADATA
    # ========================================================

    metadata: dict[
        str,
        Any,
    ] = Field(
        default_factory=dict
    )


# ============================================================
# ERROR RESPONSE
# ============================================================


class APIErrorResponse(
    APIModel
):
    """
    Standard API error body.

    Example:

        {
            "success": false,
            "error": {
                "code": "AUTH_REQUIRED",
                "message": "Authentication is required."
            }
        }
    """

    class ErrorDetail(
        APIModel
    ):

        code: str

        message: str

        details: dict[
            str,
            Any,
        ] = Field(
            default_factory=dict
        )

    api_version: str = (
        API_VERSION
    )

    success: bool = False

    error: ErrorDetail


# ============================================================
# AUTHENTICATED USER
# ============================================================


class APIUser(
    APIModel
):
    """
    Safe user information available to API request context.

    Never expose:

        access_token
        refresh_token
        service key
        passwords
    """

    id: str

    email: str | None = None

    display_name: str | None = None


# ============================================================
# HEALTH CHECK
# ============================================================


class HealthResponse(
    APIModel
):

    status: Literal[
        "ok",
        "degraded",
    ] = "ok"

    service: str = (
        "grocery-chatbot-api"
    )

    api_version: str = (
        API_VERSION
    )


# ============================================================
# SIMPLE ROOT RESPONSE
# ============================================================


class RootResponse(
    APIModel
):

    service: str = (
        "Voice Command Shopping Assistant API"
    )

    api_version: str = (
        API_VERSION
    )

    status: str = (
        "running"
    )

    endpoints: dict[
        str,
        str,
    ] = Field(
        default_factory=lambda: {
            "health":
                "/health",

            "chat":
                "/chat",

            "docs":
                "/docs",
        }
    )


# ============================================================
# PUBLIC API EXAMPLE
# ============================================================


CHAT_RESPONSE_EXAMPLE = {
    "api_version":
        "1.0",

    "success":
        True,

    "text":
        (
            "Yes — Amul Taaza Toned Milk is in our catalog, "
            "but both available variants are currently out of stock."
        ),

    "response_type":
        "products",

    "conversation_id":
        "conversation-123",

    "client_state": {
        "conversation_id":
            "conversation-123",

        "chat_history": [
            {
                "role":
                    "user",

                "content":
                    "do you have Amul milk",
            },

            {
                "role":
                    "assistant",

                "content":
                    (
                        "Yes — Amul Taaza Toned Milk is in our catalog, "
                        "but both available variants are currently out "
                        "of stock."
                    ),
            },
        ],

        "conversation_context": {
            "last_tool":
                "search_products",

            "last_tool_success":
                True,
        },

        "last_product_context": {
            "source_tool":
                "search_products",
        },

        "pending_action":
            None,
    },

    "products": [
        {
            "product_key":
                "amul-taaza-toned-milk",

            "name":
                "Amul Taaza Toned Milk",

            "brand":
                "Amul",

            "category":
                "Milk",

            "image_url":
                "https://example.com/amul-milk.jpg",

            "is_available":
                False,

            "all_variants_out_of_stock":
                True,

            "available_variant_count":
                0,

            "variant_count":
                2,

            "variants": [
                {
                    "sku_id":
                        "sku-example-1",

                    "size":
                        "500 ml",

                    "price":
                        29,

                    "mrp":
                        29,

                    "currency":
                        "INR",

                    "stock":
                        0,

                    "in_stock":
                        False,

                    "image_url":
                        "https://example.com/amul-milk.jpg",
                },

                {
                    "sku_id":
                        "sku-example-2",

                    "size":
                        "2 L",

                    "price":
                        116,

                    "mrp":
                        116,

                    "currency":
                        "INR",

                    "stock":
                        0,

                    "in_stock":
                        False,
                },
            ],
        }
    ],

    "alternatives":
        [],

    "recommendations":
        [],

    "cart":
        None,

    "orders":
        [],

    "latest_order":
        None,

    "error_code":
        None,

    "metadata":
        {},
}