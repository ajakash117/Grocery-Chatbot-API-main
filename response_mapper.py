# """
# response_mapper.py

# Convert internal Grocery Chatbot backend responses into the stable,
# frontend-agnostic public API response defined in api_models.py.

# =======================================================================
# PURPOSE
# =======================================================================

# Internal flow:

#     core/brain.py
#         ↓
#     BrainResponse
#         ↓
#     ResponsePayload
#         ↓
#     response_mapper.py
#         ↓
#     ChatResponse
#         ↓
#     FastAPI
#         ↓
#     JSON
#         ↓
#     Any frontend

# The frontend may be:

#     React
#     Next.js
#     Vue
#     Angular
#     Flutter
#     React Native
#     Android
#     iOS
#     Postman
#     another API consumer


# =======================================================================
# IMPORTANT
# =======================================================================

# This file does NOT:

#     - call Cohere
#     - call Groq
#     - query Supabase
#     - calculate prices
#     - calculate stock
#     - generate product images
#     - invent SKU information
#     - authenticate users

# It only maps already-verified backend data into the public API shape.

# Product image URLs, prices, stock, cart totals and order values must come
# from structured backend data.

# Never parse those values out of LLM-generated conversational text.
# """

# from __future__ import annotations

# import copy
# import logging

# from dataclasses import (
#     asdict,
#     is_dataclass,
# )

# from typing import (
#     Any,
#     Mapping,
#     Sequence,
# )


# # ============================================================
# # API MODELS
# # ============================================================

# from api_models import (
#     API_VERSION,
#     CartAPI,
#     CartItemAPI,
#     ChatResponse,
#     OrderAPI,
#     OrderItemAPI,
#     ProductAPI,
#     ProductVariantAPI,
# )


# # ============================================================
# # STRUCTURED RESPONSE RECOVERY
# # ============================================================

# from core.database_responder import (
#     build_database_response_payload,
# )


# # ============================================================
# # LOGGER
# # ============================================================

# logger = logging.getLogger(
#     __name__
# )


# # ============================================================
# # CONSTANTS
# # ============================================================

# MAX_PUBLIC_METADATA_DEPTH = 5

# MAX_PUBLIC_METADATA_LIST_ITEMS = 50

# MAX_PUBLIC_METADATA_STRING = 5_000


# # ============================================================
# # SECRET / INTERNAL FIELD NAMES
# # ============================================================

# BLOCKED_METADATA_KEYS = {
#     "access_token",
#     "refresh_token",
#     "authorization",
#     "password",
#     "secret",
#     "api_key",
#     "apikey",
#     "groq_api_key",
#     "groq_api_key1",
#     "groq_api_key2",
#     "cohere_api_key",
#     "supabase_secret_key",
#     "supabase_publishable_key",
#     "jwt",
#     "bearer",
#     "raw_response",
#     "system_prompt",
#     "prompt",
# }


# # ============================================================
# # RESPONSE TYPES
# # ============================================================

# ALLOWED_RESPONSE_TYPES = {
#     "text",
#     "database",
#     "products",
#     "recommendations",
#     "cart",
#     "orders",
#     "error",
# }


# # ============================================================
# # GENERIC FIELD ACCESS
# # ============================================================


# def _value(
#     obj: Any,
#     name: str,
#     default: Any = None,
# ) -> Any:
#     """
#     Read a value from:

#         dict
#         Mapping
#         dataclass
#         Pydantic object
#         normal Python object
#     """

#     if obj is None:

#         return default

#     if isinstance(
#         obj,
#         Mapping,
#     ):

#         return obj.get(
#             name,
#             default,
#         )

#     return getattr(
#         obj,
#         name,
#         default,
#     )


# # ============================================================
# # FIRST AVAILABLE VALUE
# # ============================================================


# def _first_value(
#     obj: Any,
#     *names: str,
#     default: Any = None,
# ) -> Any:

#     for name in names:

#         value = _value(
#             obj,
#             name,
#             None,
#         )

#         if value is not None:

#             return value

#     return default


# # ============================================================
# # OPTIONAL TEXT
# # ============================================================


# def _text(
#     value: Any,
# ) -> str | None:

#     if value is None:

#         return None

#     value = str(
#         value
#     ).strip()

#     return (
#         value
#         if value
#         else None
#     )


# # ============================================================
# # FLOAT
# # ============================================================


# def _float_or_none(
#     value: Any,
# ) -> float | None:

#     if value is None:

#         return None

#     if isinstance(
#         value,
#         bool,
#     ):

#         return None

#     try:

#         return float(
#             value
#         )

#     except (
#         TypeError,
#         ValueError,
#     ):

#         return None


# # ============================================================
# # INTEGER
# # ============================================================


# def _int_or_none(
#     value: Any,
# ) -> int | None:

#     if value is None:

#         return None

#     if isinstance(
#         value,
#         bool,
#     ):

#         return None

#     try:

#         return int(
#             value
#         )

#     except (
#         TypeError,
#         ValueError,
#     ):

#         return None


# # ============================================================
# # BOOLEAN
# # ============================================================


# def _bool_value(
#     value: Any,
#     *,
#     default: bool = False,
# ) -> bool:

#     if value is None:

#         return default

#     if isinstance(
#         value,
#         bool,
#     ):

#         return value

#     if isinstance(
#         value,
#         (
#             int,
#             float,
#         ),
#     ):

#         return bool(
#             value
#         )

#     if isinstance(
#         value,
#         str,
#     ):

#         normalized = (
#             value
#             .strip()
#             .lower()
#         )

#         if normalized in {
#             "true",
#             "yes",
#             "1",
#             "available",
#             "in_stock",
#             "in stock",
#         }:

#             return True

#         if normalized in {
#             "false",
#             "no",
#             "0",
#             "unavailable",
#             "out_of_stock",
#             "out of stock",
#         }:

#             return False

#     return default


# # ============================================================
# # SEQUENCE
# # ============================================================


# def _sequence(
#     value: Any,
# ) -> list[Any]:

#     if value is None:

#         return []

#     if isinstance(
#         value,
#         Mapping,
#     ):

#         return []

#     if isinstance(
#         value,
#         Sequence,
#     ) and not isinstance(
#         value,
#         (
#             str,
#             bytes,
#             bytearray,
#         ),
#     ):

#         return list(
#             value
#         )

#     return []


# # ============================================================
# # PLAIN DICTIONARY
# # ============================================================


# def _to_plain_dict(
#     value: Any,
# ) -> dict[
#     str,
#     Any,
# ] | None:
#     """
#     Convert internal structured object to plain dictionary when possible.
#     """

#     if value is None:

#         return None

#     if isinstance(
#         value,
#         Mapping,
#     ):

#         return dict(
#             value
#         )

#     model_dump = getattr(
#         value,
#         "model_dump",
#         None,
#     )

#     if callable(
#         model_dump
#     ):

#         try:

#             dumped = model_dump()

#             if isinstance(
#                 dumped,
#                 Mapping,
#             ):

#                 return dict(
#                     dumped
#                 )

#         except Exception:

#             logger.debug(
#                 "Unable to model_dump structured object.",
#                 exc_info=True,
#             )

#     to_dict = getattr(
#         value,
#         "to_dict",
#         None,
#     )

#     if callable(
#         to_dict
#     ):

#         try:

#             dumped = to_dict()

#             if isinstance(
#                 dumped,
#                 Mapping,
#             ):

#                 return dict(
#                     dumped
#                 )

#         except Exception:

#             logger.debug(
#                 "Unable to serialize structured object via to_dict().",
#                 exc_info=True,
#             )

#     if is_dataclass(
#         value
#     ):

#         try:

#             dumped = asdict(
#                 value
#             )

#             if isinstance(
#                 dumped,
#                 Mapping,
#             ):

#                 return dict(
#                     dumped
#                 )

#         except Exception:

#             logger.debug(
#                 "Unable to serialize dataclass.",
#                 exc_info=True,
#             )

#     return None


# # ============================================================
# # HTTP IMAGE URL
# # ============================================================


# def _safe_image_url(
#     value: Any,
# ) -> str | None:
#     """
#     Only expose normal HTTP/HTTPS image URLs.

#     No URL is generated here.
#     """

#     value = _text(
#         value
#     )

#     if not value:

#         return None

#     lowered = (
#         value.lower()
#     )

#     if not (
#         lowered.startswith(
#             "https://"
#         )
#         or lowered.startswith(
#             "http://"
#         )
#     ):

#         return None

#     return value


# # ============================================================
# # SAFE PUBLIC METADATA
# # ============================================================


# def _safe_metadata_value(
#     value: Any,
#     *,
#     depth: int = 0,
# ) -> Any:

#     if depth > MAX_PUBLIC_METADATA_DEPTH:

#         return None

#     if value is None:

#         return None

#     if isinstance(
#         value,
#         (
#             bool,
#             int,
#             float,
#         ),
#     ):

#         return value

#     if isinstance(
#         value,
#         str,
#     ):

#         return value[
#             :MAX_PUBLIC_METADATA_STRING
#         ]

#     if isinstance(
#         value,
#         Mapping,
#     ):

#         output: dict[
#             str,
#             Any,
#         ] = {}

#         for (
#             key,
#             item,
#         ) in value.items():

#             key_text = str(
#                 key
#             ).strip()

#             if not key_text:

#                 continue

#             normalized_key = (
#                 key_text
#                 .lower()
#                 .replace(
#                     "-",
#                     "_",
#                 )
#             )

#             if (
#                 normalized_key
#                 in BLOCKED_METADATA_KEYS
#             ):

#                 continue

#             if (
#                 "password"
#                 in normalized_key
#                 or "secret"
#                 in normalized_key
#                 or "api_key"
#                 in normalized_key
#                 or "access_token"
#                 in normalized_key
#                 or "refresh_token"
#                 in normalized_key
#             ):

#                 continue

#             output[
#                 key_text
#             ] = (
#                 _safe_metadata_value(
#                     item,
#                     depth=(
#                         depth + 1
#                     ),
#                 )
#             )

#         return output

#     if isinstance(
#         value,
#         Sequence,
#     ) and not isinstance(
#         value,
#         (
#             str,
#             bytes,
#             bytearray,
#         ),
#     ):

#         return [
#             _safe_metadata_value(
#                 item,
#                 depth=(
#                     depth + 1
#                 ),
#             )
#             for item
#             in list(
#                 value
#             )[
#                 :MAX_PUBLIC_METADATA_LIST_ITEMS
#             ]
#         ]

#     plain = _to_plain_dict(
#         value
#     )

#     if plain is not None:

#         return _safe_metadata_value(
#             plain,
#             depth=depth + 1,
#         )

#     return str(
#         value
#     )[
#         :MAX_PUBLIC_METADATA_STRING
#     ]


# def _safe_metadata(
#     value: Any,
# ) -> dict[
#     str,
#     Any,
# ]:

#     if not isinstance(
#         value,
#         Mapping,
#     ):

#         plain = (
#             _to_plain_dict(
#                 value
#             )
#         )

#         if plain is None:

#             return {}

#         value = plain

#     result = (
#         _safe_metadata_value(
#             value
#         )
#     )

#     if isinstance(
#         result,
#         dict,
#     ):

#         return result

#     return {}


# # ============================================================
# # PRODUCT VARIANT IMAGE
# # ============================================================


# def _variant_image_url(
#     variant: Any,
# ) -> str | None:

#     return _safe_image_url(
#         _first_value(
#             variant,
#             "image_url",
#             "primary_image_url",
#             "thumbnail_url",
#         )
#     )


# # ============================================================
# # PRODUCT VARIANTS
# # ============================================================


# def _product_variants(
#     product: Any,
# ) -> list[Any]:
#     """
#     Support internal representations used by product/RAG tools.

#     Preferred:
#         variants

#     Compatibility:
#         eligible_variants
#         skus
#     """

#     for name in (
#         "variants",
#         "eligible_variants",
#         "skus",
#     ):

#         variants = (
#             _sequence(
#                 _value(
#                     product,
#                     name,
#                     None,
#                 )
#             )
#         )

#         if variants:

#             return variants

#     return []


# # ============================================================
# # PRODUCT IMAGE
# # ============================================================


# def _product_image_url(
#     product: Any,
# ) -> str | None:
#     """
#     Product-level image first.

#     If absent, use first verified SKU/variant image.
#     """

#     direct = (
#         _safe_image_url(
#             _first_value(
#                 product,
#                 "image_url",
#                 "primary_image_url",
#                 "thumbnail_url",
#             )
#         )
#     )

#     if direct:

#         return direct

#     for variant in (
#         _product_variants(
#             product
#         )
#     ):

#         variant_image = (
#             _variant_image_url(
#                 variant
#             )
#         )

#         if variant_image:

#             return variant_image

#     return None


# # ============================================================
# # VARIANT SIZE
# # ============================================================


# def _variant_size(
#     variant: Any,
# ) -> str | None:

#     direct = (
#         _text(
#             _first_value(
#                 variant,
#                 "size",
#                 "pack_size",
#                 "variant_name",
#             )
#         )
#     )

#     if direct:

#         return direct

#     specs = (
#         _value(
#             variant,
#             "specs",
#             None,
#         )
#     )

#     if isinstance(
#         specs,
#         Mapping,
#     ):

#         return _text(
#             specs.get(
#                 "pack_size"
#             )
#             or specs.get(
#                 "size"
#             )
#         )

#     return None


# # ============================================================
# # VARIANT PRICE
# # ============================================================


# def _variant_price(
#     variant: Any,
# ) -> float | None:
#     """
#     Prefer backend effective/selling price.

#     No price calculation occurs here.
#     """

#     return _float_or_none(
#         _first_value(
#             variant,
#             "price",
#             "effective_price",
#             "selling_price",
#             "discount_price",
#             "mrp",
#         )
#     )


# # ============================================================
# # VARIANT MRP
# # ============================================================


# def _variant_mrp(
#     variant: Any,
# ) -> float | None:

#     return _float_or_none(
#         _first_value(
#             variant,
#             "mrp",
#             "original_price",
#             "regular_price",
#             "price",
#         )
#     )


# # ============================================================
# # VARIANT STOCK
# # ============================================================


# def _variant_stock(
#     variant: Any,
# ) -> int | None:

#     return _int_or_none(
#         _first_value(
#             variant,
#             "stock",
#             "stock_quantity",
#             "available_stock",
#         )
#     )


# # ============================================================
# # VARIANT AVAILABILITY
# # ============================================================


# def _variant_in_stock(
#     variant: Any,
# ) -> bool:

#     explicit = (
#         _first_value(
#             variant,
#             "in_stock",
#             "is_available",
#             "available",
#             default=None,
#         )
#     )

#     if explicit is not None:

#         return _bool_value(
#             explicit
#         )

#     stock = (
#         _variant_stock(
#             variant
#         )
#     )

#     if stock is not None:

#         return stock > 0

#     return False


# # ============================================================
# # VARIANT DISCOUNT
# # ============================================================


# def _variant_is_discounted(
#     variant: Any,
# ) -> bool:

#     explicit = (
#         _first_value(
#             variant,
#             "is_discounted",
#             "discounted",
#             default=None,
#         )
#     )

#     if explicit is not None:

#         return _bool_value(
#             explicit
#         )

#     price = (
#         _variant_price(
#             variant
#         )
#     )

#     mrp = (
#         _variant_mrp(
#             variant
#         )
#     )

#     if (
#         price is not None
#         and mrp is not None
#     ):

#         return price < mrp

#     return False


# # ============================================================
# # MAP PRODUCT VARIANT
# # ============================================================


# def map_product_variant(
#     variant: Any,
# ) -> ProductVariantAPI:

#     metadata = (
#         _safe_metadata(
#             _value(
#                 variant,
#                 "metadata",
#                 {},
#             )
#         )
#     )

#     return ProductVariantAPI(
#         sku_id=(
#             _text(
#                 _first_value(
#                     variant,
#                     "sku_id",
#                     "id",
#                     "sku_key",
#                 )
#             )
#         ),

#         size=(
#             _variant_size(
#                 variant
#             )
#         ),

#         pack_size=(
#             _text(
#                 _first_value(
#                     variant,
#                     "pack_size",
#                     default=None,
#                 )
#             )
#         ),

#         quantity=(
#             _first_value(
#                 variant,
#                 "quantity",
#                 default=None,
#             )
#         ),

#         unit=(
#             _text(
#                 _first_value(
#                     variant,
#                     "unit",
#                     default=None,
#                 )
#             )
#         ),

#         price=(
#             _variant_price(
#                 variant
#             )
#         ),

#         mrp=(
#             _variant_mrp(
#                 variant
#             )
#         ),

#         currency=(
#             _text(
#                 _first_value(
#                     variant,
#                     "currency",
#                     default=None,
#                 )
#             )
#         ),

#         stock=(
#             _variant_stock(
#                 variant
#             )
#         ),

#         in_stock=(
#             _variant_in_stock(
#                 variant
#             )
#         ),

#         image_url=(
#             _variant_image_url(
#                 variant
#             )
#         ),

#         is_discounted=(
#             _variant_is_discounted(
#                 variant
#             )
#         ),

#         metadata=metadata,
#     )


# # ============================================================
# # STRING LIST
# # ============================================================


# def _string_list(
#     value: Any,
# ) -> list[str]:

#     result: list[
#         str
#     ] = []

#     for item in (
#         _sequence(
#             value
#         )
#     ):

#         text = _text(
#             item
#         )

#         if text:

#             result.append(
#                 text
#             )

#     return result


# # ============================================================
# # MAP PRODUCT
# # ============================================================


# def map_product(
#     product: Any,
# ) -> ProductAPI:

#     variants_raw = (
#         _product_variants(
#             product
#         )
#     )

#     variants = [
#         map_product_variant(
#             variant
#         )
#         for variant
#         in variants_raw
#     ]

#     available_variants = [
#         variant
#         for variant in variants
#         if variant.in_stock
#     ]

#     explicit_available = (
#         _first_value(
#             product,
#             "is_available",
#             "in_stock",
#             "available",
#             default=None,
#         )
#     )

#     if explicit_available is None:

#         is_available = bool(
#             available_variants
#         )

#     else:

#         is_available = (
#             _bool_value(
#                 explicit_available
#             )
#         )

#     variant_count = (
#         _int_or_none(
#             _first_value(
#                 product,
#                 "variant_count",
#                 "sku_count",
#                 default=None,
#             )
#         )
#     )

#     if variant_count is None:

#         variant_count = len(
#             variants
#         )

#     available_variant_count = (
#         _int_or_none(
#             _first_value(
#                 product,
#                 "available_variant_count",
#                 "available_sku_count",
#                 default=None,
#             )
#         )
#     )

#     if available_variant_count is None:

#         available_variant_count = (
#             len(
#                 available_variants
#             )
#         )

#     all_out = (
#         _first_value(
#             product,
#             "all_variants_out_of_stock",
#             default=None,
#         )
#     )

#     if all_out is None:

#         all_out = (
#             bool(
#                 variants
#             )
#             and not bool(
#                 available_variants
#             )
#         )

#     return ProductAPI(
#         product_key=(
#             _text(
#                 _first_value(
#                     product,
#                     "product_key",
#                     "logical_product_key",
#                     "product_group_key",
#                     "slug",
#                 )
#             )
#         ),

#         name=(
#             _text(
#                 _first_value(
#                     product,
#                     "name",
#                     "product_name",
#                     "title",
#                 )
#             )
#         ),

#         brand=(
#             _text(
#                 _value(
#                     product,
#                     "brand",
#                     None,
#                 )
#             )
#         ),

#         category=(
#             _text(
#                 _first_value(
#                     product,
#                     "category",
#                     "category_name",
#                     "category_slug",
#                 )
#             )
#         ),

#         category_id=(
#             _text(
#                 _value(
#                     product,
#                     "category_id",
#                     None,
#                 )
#             )
#         ),

#         image_url=(
#             _product_image_url(
#                 product
#             )
#         ),

#         short_description=(
#             _text(
#                 _first_value(
#                     product,
#                     "short_description",
#                     "description",
#                     default=None,
#                 )
#             )
#         ),

#         rating=(
#             _float_or_none(
#                 _value(
#                     product,
#                     "rating",
#                     None,
#                 )
#             )
#         ),

#         is_available=(
#             is_available
#         ),

#         all_variants_out_of_stock=(
#             _bool_value(
#                 all_out
#             )
#         ),

#         available_variant_count=(
#             available_variant_count
#         ),

#         variant_count=(
#             variant_count
#         ),

#         price_from=(
#             _float_or_none(
#                 _first_value(
#                     product,
#                     "price_from",
#                     "min_price",
#                     default=None,
#                 )
#             )
#         ),

#         price_to=(
#             _float_or_none(
#                 _first_value(
#                     product,
#                     "price_to",
#                     "max_price",
#                     default=None,
#                 )
#             )
#         ),

#         variants=variants,

#         recommendation_reason=(
#             _text(
#                 _first_value(
#                     product,
#                     "recommendation_reason",
#                     "reason",
#                     default=None,
#                 )
#             )
#         ),

#         semantic_tags=(
#             _string_list(
#                 _value(
#                     product,
#                     "semantic_tags",
#                     [],
#                 )
#             )
#         ),

#         meal_contexts=(
#             _string_list(
#                 _value(
#                     product,
#                     "meal_contexts",
#                     [],
#                 )
#             )
#         ),

#         use_cases=(
#             _string_list(
#                 _value(
#                     product,
#                     "use_cases",
#                     [],
#                 )
#             )
#         ),

#         metadata=(
#             _safe_metadata(
#                 _value(
#                     product,
#                     "metadata",
#                     {},
#                 )
#             )
#         ),
#     )


# # ============================================================
# # MAP PRODUCT COLLECTION
# # ============================================================


# def map_products(
#     products: Any,
# ) -> list[
#     ProductAPI
# ]:

#     return [
#         map_product(
#             product
#         )
#         for product
#         in _sequence(
#             products
#         )
#     ]


# # ============================================================
# # MAP CART ITEM
# # ============================================================


# def map_cart_item(
#     item: Any,
# ) -> CartItemAPI:

#     return CartItemAPI(
#         cart_item_id=(
#             _text(
#                 _first_value(
#                     item,
#                     "cart_item_id",
#                     "id",
#                 )
#             )
#         ),

#         sku_id=(
#             _text(
#                 _first_value(
#                     item,
#                     "sku_id",
#                     "product_id",
#                 )
#             )
#         ),

#         product_key=(
#             _text(
#                 _first_value(
#                     item,
#                     "product_key",
#                     "logical_product_key",
#                     "product_group_key",
#                 )
#             )
#         ),

#         product_name=(
#             _text(
#                 _first_value(
#                     item,
#                     "product_name",
#                     "name",
#                     "title",
#                 )
#             )
#         ),

#         brand=(
#             _text(
#                 _value(
#                     item,
#                     "brand",
#                     None,
#                 )
#             )
#         ),

#         size=(
#             _text(
#                 _first_value(
#                     item,
#                     "size",
#                     "pack_size",
#                 )
#             )
#         ),

#         quantity=(
#             _int_or_none(
#                 _value(
#                     item,
#                     "quantity",
#                     1,
#                 )
#             )
#             or 1
#         ),

#         unit_price=(
#             _float_or_none(
#                 _first_value(
#                     item,
#                     "unit_price",
#                     "price",
#                     "selling_price",
#                     default=None,
#                 )
#             )
#         ),

#         line_total=(
#             _float_or_none(
#                 _first_value(
#                     item,
#                     "line_total",
#                     "total_price",
#                     default=None,
#                 )
#             )
#         ),

#         currency=(
#             _text(
#                 _value(
#                     item,
#                     "currency",
#                     None,
#                 )
#             )
#         ),

#         stock=(
#             _int_or_none(
#                 _value(
#                     item,
#                     "stock",
#                     None,
#                 )
#             )
#         ),

#         in_stock=(
#             _bool_value(
#                 _first_value(
#                     item,
#                     "in_stock",
#                     "is_available",
#                     default=False,
#                 )
#             )
#         ),

#         image_url=(
#             _safe_image_url(
#                 _first_value(
#                     item,
#                     "image_url",
#                     "primary_image_url",
#                     default=None,
#                 )
#             )
#         ),
#     )


# # ============================================================
# # MAP CART
# # ============================================================


# def map_cart(
#     cart: Any,
# ) -> CartAPI | None:

#     if cart is None:

#         return None

#     items = [
#         map_cart_item(
#             item
#         )
#         for item in (
#             _sequence(
#                 _value(
#                     cart,
#                     "items",
#                     [],
#                 )
#             )
#         )
#     ]

#     item_count = (
#         _int_or_none(
#             _value(
#                 cart,
#                 "item_count",
#                 None,
#             )
#         )
#     )

#     if item_count is None:

#         item_count = len(
#             items
#         )

#     total_quantity = (
#         _int_or_none(
#             _value(
#                 cart,
#                 "total_quantity",
#                 None,
#             )
#         )
#     )

#     if total_quantity is None:

#         total_quantity = sum(
#             item.quantity
#             for item in items
#         )

#     explicit_empty = (
#         _value(
#             cart,
#             "is_empty",
#             None,
#         )
#     )

#     if explicit_empty is None:

#         is_empty = (
#             len(
#                 items
#             )
#             == 0
#         )

#     else:

#         is_empty = (
#             _bool_value(
#                 explicit_empty
#             )
#         )

#     return CartAPI(
#         items=items,

#         item_count=(
#             item_count
#         ),

#         total_quantity=(
#             total_quantity
#         ),

#         subtotal=(
#             _float_or_none(
#                 _value(
#                     cart,
#                     "subtotal",
#                     None,
#                 )
#             )
#         ),

#         total=(
#             _float_or_none(
#                 _first_value(
#                     cart,
#                     "total",
#                     "total_amount",
#                     default=None,
#                 )
#             )
#         ),

#         currency=(
#             _text(
#                 _value(
#                     cart,
#                     "currency",
#                     None,
#                 )
#             )
#         ),

#         is_empty=(
#             is_empty
#         ),
#     )


# # ============================================================
# # MAP ORDER ITEM
# # ============================================================


# def map_order_item(
#     item: Any,
#     *,
#     default_currency: str | None = None,
# ) -> OrderItemAPI:

#     return OrderItemAPI(
#         order_item_id=(
#             _text(
#                 _first_value(
#                     item,
#                     "order_item_id",
#                     "id",
#                 )
#             )
#         ),

#         sku_id=(
#             _text(
#                 _first_value(
#                     item,
#                     "sku_id",
#                     "product_id",
#                 )
#             )
#         ),

#         product_name=(
#             _text(
#                 _first_value(
#                     item,
#                     "product_name",
#                     "name",
#                     "title",
#                 )
#             )
#         ),

#         brand=(
#             _text(
#                 _value(
#                     item,
#                     "brand",
#                     None,
#                 )
#             )
#         ),

#         size=(
#             _text(
#                 _first_value(
#                     item,
#                     "size",
#                     "pack_size",
#                 )
#             )
#         ),

#         quantity=(
#             _int_or_none(
#                 _value(
#                     item,
#                     "quantity",
#                     1,
#                 )
#             )
#             or 1
#         ),

#         unit_price=(
#             _float_or_none(
#                 _first_value(
#                     item,
#                     "unit_price",
#                     "price",
#                     default=None,
#                 )
#             )
#         ),

#         line_total=(
#             _float_or_none(
#                 _first_value(
#                     item,
#                     "line_total",
#                     "total_price",
#                     default=None,
#                 )
#             )
#         ),

#         currency=(
#             _text(
#                 _value(
#                     item,
#                     "currency",
#                     default_currency,
#                 )
#             )
#             or default_currency
#         ),

#         image_url=(
#             _safe_image_url(
#                 _first_value(
#                     item,
#                     "image_url",
#                     "primary_image_url",
#                     default=None,
#                 )
#             )
#         ),
#     )


# # ============================================================
# # MAP ORDER
# # ============================================================


# def map_order(
#     order: Any,
# ) -> OrderAPI:

#     currency = (
#         _text(
#             _value(
#                 order,
#                 "currency",
#                 None,
#             )
#         )
#     )

#     items = [
#         map_order_item(
#             item,
#             default_currency=currency,
#         )
#         for item
#         in _sequence(
#             _value(
#                 order,
#                 "items",
#                 [],
#             )
#         )
#     ]

#     item_count = (
#         _int_or_none(
#             _value(
#                 order,
#                 "item_count",
#                 None,
#             )
#         )
#     )

#     if item_count is None:

#         item_count = len(
#             items
#         )

#     return OrderAPI(
#         order_id=(
#             _text(
#                 _first_value(
#                     order,
#                     "order_id",
#                     "id",
#                 )
#             )
#         ),

#         created_at=(
#             _text(
#                 _value(
#                     order,
#                     "created_at",
#                     None,
#                 )
#             )
#         ),

#         order_status=(
#             _text(
#                 _first_value(
#                     order,
#                     "order_status",
#                     "status",
#                     default=None,
#                 )
#             )
#         ),

#         payment_status=(
#             _text(
#                 _value(
#                     order,
#                     "payment_status",
#                     None,
#                 )
#             )
#         ),

#         total_amount=(
#             _float_or_none(
#                 _first_value(
#                     order,
#                     "total_amount",
#                     "total",
#                     default=None,
#                 )
#             )
#         ),

#         currency=currency,

#         item_count=(
#             item_count
#         ),

#         items=items,

#         metadata=(
#             _safe_metadata(
#                 _value(
#                     order,
#                     "metadata",
#                     {},
#                 )
#             )
#         ),
#     )


# # ============================================================
# # MAP ORDERS
# # ============================================================


# def map_orders(
#     orders: Any,
# ) -> list[
#     OrderAPI
# ]:

#     return [
#         map_order(
#             order
#         )
#         for order
#         in _sequence(
#             orders
#         )
#     ]


# # ============================================================
# # RESOLVE STRUCTURED PAYLOAD
# # ============================================================


# def resolve_response_payload(
#     result: Any,
# ) -> Any:
#     """
#     Preferred:

#         BrainResponse.response_payload

#     Compatibility fallback:

#         BrainResponse.tool_executions
#             ↓
#         build_database_response_payload()

#     The fallback uses already-executed verified tool results.

#     It does NOT rerun the user's tool/database operation.
#     """

#     payload = (
#         _value(
#             result,
#             "response_payload",
#             None,
#         )
#     )

#     if payload is not None:

#         return payload

#     executions = (
#         _value(
#             result,
#             "tool_executions",
#             None,
#         )
#     )

#     if not executions:

#         return None

#     try:

#         return (
#             build_database_response_payload(
#                 executions=executions
#             )
#         )

#     except Exception:

#         logger.exception(
#             "Unable to recover structured API response "
#             "from existing tool executions."
#         )

#         return None


# # ============================================================
# # RESPONSE TYPE NORMALIZATION
# # ============================================================


# def _normalize_response_type(
#     value: Any,
# ) -> str | None:

#     value = (
#         _text(
#             value
#         )
#     )

#     if not value:

#         return None

#     normalized = (
#         value
#         .strip()
#         .lower()
#         .replace(
#             "-",
#             "_",
#         )
#     )

#     aliases = {
#         "product":
#             "products",

#         "product_search":
#             "products",

#         "catalog":
#             "products",

#         "rag":
#             "recommendations",

#         "recommendation":
#             "recommendations",

#         "order":
#             "orders",

#         "latest_order":
#             "orders",

#         "database_response":
#             "database",
#     }

#     normalized = (
#         aliases.get(
#             normalized,
#             normalized,
#         )
#     )

#     if normalized in (
#         ALLOWED_RESPONSE_TYPES
#     ):

#         return normalized

#     return None


# # ============================================================
# # INFER RESPONSE TYPE
# # ============================================================


# def _infer_response_type(
#     *,
#     success: bool,
#     payload: Any,
#     products: list[ProductAPI],
#     alternatives: list[ProductAPI],
#     recommendations: list[ProductAPI],
#     cart: CartAPI | None,
#     orders: list[OrderAPI],
#     latest_order: OrderAPI | None,
# ) -> str:

#     explicit = (
#         _normalize_response_type(
#             _value(
#                 payload,
#                 "response_type",
#                 None,
#             )
#         )
#     )

#     if explicit:

#         return explicit

#     if not success:

#         return "error"

#     if cart is not None:

#         return "cart"

#     if (
#         latest_order is not None
#         or orders
#     ):

#         return "orders"

#     if recommendations:

#         return "recommendations"

#     if (
#         products
#         or alternatives
#     ):

#         return "products"

#     if payload is not None:

#         return "database"

#     return "text"


# # ============================================================
# # TOOL NAMES
# # ============================================================


# def _tool_names(
#     result: Any,
#     payload: Any,
# ) -> list[str]:

#     names: list[
#         str
#     ] = []

#     payload_names = (
#         _sequence(
#             _value(
#                 payload,
#                 "tool_names",
#                 [],
#             )
#         )
#     )

#     for item in payload_names:

#         value = (
#             _text(
#                 item
#             )
#         )

#         if (
#             value
#             and value not in names
#         ):

#             names.append(
#                 value
#             )

#     executions = (
#         _sequence(
#             _value(
#                 result,
#                 "tool_executions",
#                 [],
#             )
#         )
#     )

#     for execution in executions:

#         value = (
#             _text(
#                 _value(
#                     execution,
#                     "tool_name",
#                     None,
#                 )
#             )
#         )

#         if (
#             value
#             and value not in names
#         ):

#             names.append(
#                 value
#             )

#     return names


# # ============================================================
# # MAP BRAIN RESPONSE
# # ============================================================


# def map_brain_response(
#     result: Any,
#     *,
#     conversation_id: str | None = None,
# ) -> ChatResponse:
#     """
#     Convert BrainResponse into the public ChatResponse API model.

#     This is the main function api.py should call.
#     """

#     if result is None:

#         return ChatResponse(
#             api_version=API_VERSION,
#             success=False,
#             text=(
#                 "I couldn't process that request right now."
#             ),
#             response_type="error",
#             conversation_id=conversation_id,
#             error_code="EMPTY_BRAIN_RESPONSE",
#         )

#     # ========================================================
#     # STRUCTURED PAYLOAD
#     # ========================================================

#     payload = (
#         resolve_response_payload(
#             result
#         )
#     )

#     # ========================================================
#     # TEXT
#     # ========================================================

#     result_text = (
#         _text(
#             _value(
#                 result,
#                 "text",
#                 None,
#             )
#         )
#     )

#     payload_text = (
#         _text(
#             _value(
#                 payload,
#                 "text",
#                 None,
#             )
#         )
#     )

#     final_text = (
#         result_text
#         or payload_text
#         or ""
#     )

#     # ========================================================
#     # SUCCESS
#     # ========================================================

#     result_success = (
#         _bool_value(
#             _value(
#                 result,
#                 "success",
#                 True,
#             ),
#             default=True,
#         )
#     )

#     payload_success_value = (
#         _value(
#             payload,
#             "success",
#             None,
#         )
#     )

#     if payload_success_value is None:

#         final_success = (
#             result_success
#         )

#     else:

#         final_success = bool(
#             result_success
#             and _bool_value(
#                 payload_success_value,
#                 default=True,
#             )
#         )

#     # ========================================================
#     # PRODUCTS
#     # ========================================================

#     products = (
#         map_products(
#             _value(
#                 payload,
#                 "products",
#                 [],
#             )
#         )
#     )

#     alternatives = (
#         map_products(
#             _value(
#                 payload,
#                 "alternatives",
#                 [],
#             )
#         )
#     )

#     recommendations = (
#         map_products(
#             _value(
#                 payload,
#                 "recommendations",
#                 [],
#             )
#         )
#     )

#     # ========================================================
#     # CART
#     # ========================================================

#     cart = (
#         map_cart(
#             _value(
#                 payload,
#                 "cart",
#                 None,
#             )
#         )
#     )

#     # ========================================================
#     # ORDERS
#     # ========================================================

#     orders = (
#         map_orders(
#             _value(
#                 payload,
#                 "orders",
#                 [],
#             )
#         )
#     )

#     latest_order_raw = (
#         _value(
#             payload,
#             "latest_order",
#             None,
#         )
#     )

#     latest_order = (
#         map_order(
#             latest_order_raw
#         )
#         if latest_order_raw
#         is not None
#         else None
#     )

#     # ========================================================
#     # ERROR CODE
#     # ========================================================

#     error_code = (
#         _text(
#             _first_value(
#                 result,
#                 "error_code",
#                 default=None,
#             )
#         )
#     )

#     if not error_code:

#         error_code = (
#             _text(
#                 _value(
#                     payload,
#                     "error_code",
#                     None,
#                 )
#             )
#         )

#     # ========================================================
#     # RESPONSE TYPE
#     # ========================================================

#     response_type = (
#         _infer_response_type(
#             success=final_success,
#             payload=payload,
#             products=products,
#             alternatives=alternatives,
#             recommendations=recommendations,
#             cart=cart,
#             orders=orders,
#             latest_order=latest_order,
#         )
#     )

#     # ========================================================
#     # SAFE PUBLIC METADATA
#     # ========================================================

#     metadata = (
#         _safe_metadata(
#             _value(
#                 payload,
#                 "metadata",
#                 {},
#             )
#         )
#     )

#     tool_names = (
#         _tool_names(
#             result,
#             payload,
#         )
#     )

#     if tool_names:

#         metadata[
#             "tool_names"
#         ] = tool_names

#     metadata[
#         "has_structured_content"
#     ] = bool(
#         products
#         or alternatives
#         or recommendations
#         or cart is not None
#         or orders
#         or latest_order is not None
#     )

#     metadata[
#         "product_count"
#     ] = len(
#         products
#     )

#     metadata[
#         "alternative_count"
#     ] = len(
#         alternatives
#     )

#     metadata[
#         "recommendation_count"
#     ] = len(
#         recommendations
#     )

#     metadata[
#         "order_count"
#     ] = len(
#         orders
#     )

#     # ========================================================
#     # FINAL PUBLIC RESPONSE
#     # ========================================================

#     return ChatResponse(
#         api_version=API_VERSION,

#         success=final_success,

#         text=final_text,

#         response_type=response_type,

#         conversation_id=conversation_id,

#         products=products,

#         alternatives=alternatives,

#         recommendations=recommendations,

#         cart=cart,

#         orders=orders,

#         latest_order=latest_order,

#         error_code=error_code,

#         metadata=metadata,
#     )


# # ============================================================
# # DICTIONARY RESPONSE
# # ============================================================


# def map_brain_response_to_dict(
#     result: Any,
#     *,
#     conversation_id: str | None = None,
# ) -> dict[
#     str,
#     Any,
# ]:
#     """
#     Convenience helper when api.py needs a plain JSON-compatible dict.
#     """

#     response = (
#         map_brain_response(
#             result,
#             conversation_id=conversation_id,
#         )
#     )

#     return response.model_dump(
#         mode="json",
#         exclude_none=False,
#     )


# # ============================================================
# # SAFE FAILURE RESPONSE
# # ============================================================


# def build_error_chat_response(
#     *,
#     message: str,
#     error_code: str,
#     conversation_id: str | None = None,
# ) -> ChatResponse:
#     """
#     Build a safe frontend-facing error response.

#     Do not pass:
#         stack traces
#         raw exceptions
#         secrets
#     """

#     message = (
#         _text(
#             message
#         )
#         or "The request could not be completed."
#     )

#     error_code = (
#         _text(
#             error_code
#         )
#         or "API_ERROR"
#     )

#     return ChatResponse(
#         api_version=API_VERSION,

#         success=False,

#         text=message,

#         response_type="error",

#         conversation_id=conversation_id,

#         products=[],

#         alternatives=[],

#         recommendations=[],

#         cart=None,

#         orders=[],

#         latest_order=None,

#         error_code=error_code,

#         metadata={},
#     )


# # ============================================================
# # SAFE FAILURE DICTIONARY
# # ============================================================


# def build_error_chat_response_dict(
#     *,
#     message: str,
#     error_code: str,
#     conversation_id: str | None = None,
# ) -> dict[
#     str,
#     Any,
# ]:

#     response = (
#         build_error_chat_response(
#             message=message,
#             error_code=error_code,
#             conversation_id=conversation_id,
#         )
#     )

#     return response.model_dump(
#         mode="json",
#         exclude_none=False,
#     )


# # ============================================================
# # PUBLIC PAYLOAD COPY
# # ============================================================


# def copy_public_response(
#     response: ChatResponse,
# ) -> dict[
#     str,
#     Any,
# ]:
#     """
#     Return a detached JSON-compatible copy suitable for FastAPI.
#     """

#     data = response.model_dump(
#         mode="json",
#         exclude_none=False,
#     )

#     return copy.deepcopy(
#         data
#     )


"""
response_mapper.py

Convert internal Grocery Chatbot backend responses into the stable,
frontend-agnostic public API response defined in api_models.py.

=======================================================================
PURPOSE
=======================================================================

Internal flow:

    core/brain.py
        ↓
    BrainResponse
        ↓
    ResponsePayload
        ↓
    response_mapper.py
        ↓
    ChatResponse
        ↓
    FastAPI
        ↓
    JSON
        ↓
    Any frontend

The frontend may be:

    React
    Next.js
    Vue
    Angular
    Flutter
    React Native
    Android
    iOS
    Postman
    another API consumer


=======================================================================
IMPORTANT
=======================================================================

This file does NOT:

    - call Cohere
    - call Groq
    - query Supabase
    - calculate prices
    - calculate stock
    - generate product images
    - invent SKU information
    - authenticate users

It only maps already-verified backend data into the public API shape.

For the API-only deployment it also transports SAFE conversational
continuity state from BrainResponse.client_state to:

    response.metadata["client_state"]

so a stateless frontend can send that value back on its next request as:

    ChatRequest.client_context

Product image URLs, prices, stock, cart totals and order values must come
from structured backend data.

Never parse those values out of LLM-generated conversational text.
"""

from __future__ import annotations

import copy
import logging

from dataclasses import (
    asdict,
    is_dataclass,
)

from typing import (
    Any,
    Mapping,
    Sequence,
)


# ============================================================
# API MODELS
# ============================================================

from api_models import (
    API_VERSION,
    CartAPI,
    CartItemAPI,
    ChatResponse,
    OrderAPI,
    OrderItemAPI,
    ProductAPI,
    ProductVariantAPI,
)


# ============================================================
# STRUCTURED RESPONSE RECOVERY
# ============================================================

from core.database_responder import (
    build_database_response_payload,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# CONSTANTS
# ============================================================

MAX_PUBLIC_METADATA_DEPTH = 5

MAX_PUBLIC_METADATA_LIST_ITEMS = 50

MAX_PUBLIC_METADATA_STRING = 5_000


# ============================================================
# SECRET / INTERNAL FIELD NAMES
# ============================================================

BLOCKED_METADATA_KEYS = {
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "api_key",
    "apikey",
    "groq_api_key",
    "groq_api_key1",
    "groq_api_key2",
    "cohere_api_key",
    "supabase_secret_key",
    "supabase_publishable_key",
    "jwt",
    "bearer",
    "raw_response",
    "system_prompt",
    "prompt",
}


# ============================================================
# RESPONSE TYPES
# ============================================================

ALLOWED_RESPONSE_TYPES = {
    "text",
    "database",
    "products",
    "recommendations",
    "cart",
    "orders",
    "error",
}


# ============================================================
# GENERIC FIELD ACCESS
# ============================================================


def _value(
    obj: Any,
    name: str,
    default: Any = None,
) -> Any:
    """
    Read a value from:

        dict
        Mapping
        dataclass
        Pydantic object
        normal Python object
    """

    if obj is None:

        return default

    if isinstance(
        obj,
        Mapping,
    ):

        return obj.get(
            name,
            default,
        )

    return getattr(
        obj,
        name,
        default,
    )


# ============================================================
# FIRST AVAILABLE VALUE
# ============================================================


def _first_value(
    obj: Any,
    *names: str,
    default: Any = None,
) -> Any:

    for name in names:

        value = _value(
            obj,
            name,
            None,
        )

        if value is not None:

            return value

    return default


# ============================================================
# OPTIONAL TEXT
# ============================================================


def _text(
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


# ============================================================
# FLOAT
# ============================================================


def _float_or_none(
    value: Any,
) -> float | None:

    if value is None:

        return None

    if isinstance(
        value,
        bool,
    ):

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


# ============================================================
# INTEGER
# ============================================================


def _int_or_none(
    value: Any,
) -> int | None:

    if value is None:

        return None

    if isinstance(
        value,
        bool,
    ):

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
# BOOLEAN
# ============================================================


def _bool_value(
    value: Any,
    *,
    default: bool = False,
) -> bool:

    if value is None:

        return default

    if isinstance(
        value,
        bool,
    ):

        return value

    if isinstance(
        value,
        (
            int,
            float,
        ),
    ):

        return bool(
            value
        )

    if isinstance(
        value,
        str,
    ):

        normalized = (
            value
            .strip()
            .lower()
        )

        if normalized in {
            "true",
            "yes",
            "1",
            "available",
            "in_stock",
            "in stock",
        }:

            return True

        if normalized in {
            "false",
            "no",
            "0",
            "unavailable",
            "out_of_stock",
            "out of stock",
        }:

            return False

    return default


# ============================================================
# SEQUENCE
# ============================================================


def _sequence(
    value: Any,
) -> list[Any]:

    if value is None:

        return []

    if isinstance(
        value,
        Mapping,
    ):

        return []

    if isinstance(
        value,
        Sequence,
    ) and not isinstance(
        value,
        (
            str,
            bytes,
            bytearray,
        ),
    ):

        return list(
            value
        )

    return []


# ============================================================
# PLAIN DICTIONARY
# ============================================================


def _to_plain_dict(
    value: Any,
) -> dict[
    str,
    Any,
] | None:
    """
    Convert internal structured object to plain dictionary when possible.
    """

    if value is None:

        return None

    if isinstance(
        value,
        Mapping,
    ):

        return dict(
            value
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

            dumped = model_dump()

            if isinstance(
                dumped,
                Mapping,
            ):

                return dict(
                    dumped
                )

        except Exception:

            logger.debug(
                "Unable to model_dump structured object.",
                exc_info=True,
            )

    to_dict = getattr(
        value,
        "to_dict",
        None,
    )

    if callable(
        to_dict
    ):

        try:

            dumped = to_dict()

            if isinstance(
                dumped,
                Mapping,
            ):

                return dict(
                    dumped
                )

        except Exception:

            logger.debug(
                "Unable to serialize structured object via to_dict().",
                exc_info=True,
            )

    if is_dataclass(
        value
    ):

        try:

            dumped = asdict(
                value
            )

            if isinstance(
                dumped,
                Mapping,
            ):

                return dict(
                    dumped
                )

        except Exception:

            logger.debug(
                "Unable to serialize dataclass.",
                exc_info=True,
            )

    return None


# ============================================================
# HTTP IMAGE URL
# ============================================================


def _safe_image_url(
    value: Any,
) -> str | None:
    """
    Only expose normal HTTP/HTTPS image URLs.

    No URL is generated here.
    """

    value = _text(
        value
    )

    if not value:

        return None

    lowered = (
        value.lower()
    )

    if not (
        lowered.startswith(
            "https://"
        )
        or lowered.startswith(
            "http://"
        )
    ):

        return None

    return value


# ============================================================
# SAFE PUBLIC METADATA
# ============================================================


def _safe_metadata_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:

    if depth > MAX_PUBLIC_METADATA_DEPTH:

        return None

    if value is None:

        return None

    if isinstance(
        value,
        (
            bool,
            int,
            float,
        ),
    ):

        return value

    if isinstance(
        value,
        str,
    ):

        return value[
            :MAX_PUBLIC_METADATA_STRING
        ]

    if isinstance(
        value,
        Mapping,
    ):

        output: dict[
            str,
            Any,
        ] = {}

        for (
            key,
            item,
        ) in value.items():

            key_text = str(
                key
            ).strip()

            if not key_text:

                continue

            normalized_key = (
                key_text
                .lower()
                .replace(
                    "-",
                    "_",
                )
            )

            if (
                normalized_key
                in BLOCKED_METADATA_KEYS
            ):

                continue

            if (
                "password"
                in normalized_key
                or "secret"
                in normalized_key
                or "api_key"
                in normalized_key
                or "access_token"
                in normalized_key
                or "refresh_token"
                in normalized_key
            ):

                continue

            output[
                key_text
            ] = (
                _safe_metadata_value(
                    item,
                    depth=(
                        depth + 1
                    ),
                )
            )

        return output

    if isinstance(
        value,
        Sequence,
    ) and not isinstance(
        value,
        (
            str,
            bytes,
            bytearray,
        ),
    ):

        return [
            _safe_metadata_value(
                item,
                depth=(
                    depth + 1
                ),
            )
            for item
            in list(
                value
            )[
                :MAX_PUBLIC_METADATA_LIST_ITEMS
            ]
        ]

    plain = _to_plain_dict(
        value
    )

    if plain is not None:

        return _safe_metadata_value(
            plain,
            depth=depth + 1,
        )

    return str(
        value
    )[
        :MAX_PUBLIC_METADATA_STRING
    ]


def _safe_metadata(
    value: Any,
) -> dict[
    str,
    Any,
]:

    if not isinstance(
        value,
        Mapping,
    ):

        plain = (
            _to_plain_dict(
                value
            )
        )

        if plain is None:

            return {}

        value = plain

    result = (
        _safe_metadata_value(
            value
        )
    )

    if isinstance(
        result,
        dict,
    ):

        return result

    return {}


# ============================================================
# CLIENT CONVERSATION STATE
# ============================================================


def _safe_client_state(
    result: Any,
) -> dict[
    str,
    Any,
]:
    """
    Return the API-safe conversational state exported by core/brain.py.

    The updated brain exposes:

        BrainResponse.client_state

    which may contain:

        conversation_id
        chat_history
        conversation_context
        last_product_context
        pending_action

    This state is NON-AUTHORITATIVE.

    It is only for conversational continuity between stateless HTTP
    requests. The frontend may send it back on the next request as:

        ChatRequest.client_context

    It must never be treated as a source of truth for:

        user identity
        access tokens
        prices
        stock
        cart contents
        order data

    _safe_metadata() is deliberately reused here so secret-like fields
    are removed before the state is returned to a frontend.
    """

    raw_state = (
        _value(
            result,
            "client_state",
            None,
        )
    )

    if raw_state is None:

        return {}

    safe_state = (
        _safe_metadata(
            raw_state
        )
    )

    # Keep only the conversation-state fields the public API needs.
    # This prevents future internal BrainResponse fields from leaking
    # into client-visible state accidentally.
    allowed_keys = {
        "conversation_id",
        "chat_history",
        "conversation_context",
        "last_product_context",
        "pending_action",
    }

    return {
        key:
            copy.deepcopy(
                value
            )
        for (
            key,
            value,
        ) in safe_state.items()
        if key in allowed_keys
    }


# ============================================================
# PRODUCT VARIANT IMAGE
# ============================================================


def _variant_image_url(
    variant: Any,
) -> str | None:

    return _safe_image_url(
        _first_value(
            variant,
            "image_url",
            "primary_image_url",
            "thumbnail_url",
        )
    )


# ============================================================
# PRODUCT VARIANTS
# ============================================================


def _product_variants(
    product: Any,
) -> list[Any]:
    """
    Support internal representations used by product/RAG tools.

    Preferred:
        variants

    Compatibility:
        eligible_variants
        skus
    """

    for name in (
        "variants",
        "eligible_variants",
        "skus",
    ):

        variants = (
            _sequence(
                _value(
                    product,
                    name,
                    None,
                )
            )
        )

        if variants:

            return variants

    return []


# ============================================================
# PRODUCT IMAGE
# ============================================================


def _product_image_url(
    product: Any,
) -> str | None:
    """
    Product-level image first.

    If absent, use first verified SKU/variant image.
    """

    direct = (
        _safe_image_url(
            _first_value(
                product,
                "image_url",
                "primary_image_url",
                "thumbnail_url",
            )
        )
    )

    if direct:

        return direct

    for variant in (
        _product_variants(
            product
        )
    ):

        variant_image = (
            _variant_image_url(
                variant
            )
        )

        if variant_image:

            return variant_image

    return None


# ============================================================
# VARIANT SIZE
# ============================================================


def _variant_size(
    variant: Any,
) -> str | None:

    direct = (
        _text(
            _first_value(
                variant,
                "size",
                "pack_size",
                "variant_name",
            )
        )
    )

    if direct:

        return direct

    specs = (
        _value(
            variant,
            "specs",
            None,
        )
    )

    if isinstance(
        specs,
        Mapping,
    ):

        return _text(
            specs.get(
                "pack_size"
            )
            or specs.get(
                "size"
            )
        )

    return None


# ============================================================
# VARIANT PRICE
# ============================================================


def _variant_price(
    variant: Any,
) -> float | None:
    """
    Prefer backend effective/selling price.

    No price calculation occurs here.
    """

    return _float_or_none(
        _first_value(
            variant,
            "price",
            "effective_price",
            "selling_price",
            "discount_price",
            "mrp",
        )
    )


# ============================================================
# VARIANT MRP
# ============================================================


def _variant_mrp(
    variant: Any,
) -> float | None:

    return _float_or_none(
        _first_value(
            variant,
            "mrp",
            "original_price",
            "regular_price",
            "price",
        )
    )


# ============================================================
# VARIANT STOCK
# ============================================================


def _variant_stock(
    variant: Any,
) -> int | None:

    return _int_or_none(
        _first_value(
            variant,
            "stock",
            "stock_quantity",
            "available_stock",
        )
    )


# ============================================================
# VARIANT AVAILABILITY
# ============================================================


def _variant_in_stock(
    variant: Any,
) -> bool:

    explicit = (
        _first_value(
            variant,
            "in_stock",
            "is_available",
            "available",
            default=None,
        )
    )

    if explicit is not None:

        return _bool_value(
            explicit
        )

    stock = (
        _variant_stock(
            variant
        )
    )

    if stock is not None:

        return stock > 0

    return False


# ============================================================
# VARIANT DISCOUNT
# ============================================================


def _variant_is_discounted(
    variant: Any,
) -> bool:

    explicit = (
        _first_value(
            variant,
            "is_discounted",
            "discounted",
            default=None,
        )
    )

    if explicit is not None:

        return _bool_value(
            explicit
        )

    price = (
        _variant_price(
            variant
        )
    )

    mrp = (
        _variant_mrp(
            variant
        )
    )

    if (
        price is not None
        and mrp is not None
    ):

        return price < mrp

    return False


# ============================================================
# MAP PRODUCT VARIANT
# ============================================================


def map_product_variant(
    variant: Any,
) -> ProductVariantAPI:

    metadata = (
        _safe_metadata(
            _value(
                variant,
                "metadata",
                {},
            )
        )
    )

    return ProductVariantAPI(
        sku_id=(
            _text(
                _first_value(
                    variant,
                    "sku_id",
                    "id",
                    "sku_key",
                )
            )
        ),

        size=(
            _variant_size(
                variant
            )
        ),

        pack_size=(
            _text(
                _first_value(
                    variant,
                    "pack_size",
                    default=None,
                )
            )
        ),

        quantity=(
            _first_value(
                variant,
                "quantity",
                default=None,
            )
        ),

        unit=(
            _text(
                _first_value(
                    variant,
                    "unit",
                    default=None,
                )
            )
        ),

        price=(
            _variant_price(
                variant
            )
        ),

        mrp=(
            _variant_mrp(
                variant
            )
        ),

        currency=(
            _text(
                _first_value(
                    variant,
                    "currency",
                    default=None,
                )
            )
        ),

        stock=(
            _variant_stock(
                variant
            )
        ),

        in_stock=(
            _variant_in_stock(
                variant
            )
        ),

        image_url=(
            _variant_image_url(
                variant
            )
        ),

        is_discounted=(
            _variant_is_discounted(
                variant
            )
        ),

        metadata=metadata,
    )


# ============================================================
# STRING LIST
# ============================================================


def _string_list(
    value: Any,
) -> list[str]:

    result: list[
        str
    ] = []

    for item in (
        _sequence(
            value
        )
    ):

        text = _text(
            item
        )

        if text:

            result.append(
                text
            )

    return result


# ============================================================
# MAP PRODUCT
# ============================================================


def map_product(
    product: Any,
) -> ProductAPI:

    variants_raw = (
        _product_variants(
            product
        )
    )

    variants = [
        map_product_variant(
            variant
        )
        for variant
        in variants_raw
    ]

    available_variants = [
        variant
        for variant in variants
        if variant.in_stock
    ]

    explicit_available = (
        _first_value(
            product,
            "is_available",
            "in_stock",
            "available",
            default=None,
        )
    )

    if explicit_available is None:

        is_available = bool(
            available_variants
        )

    else:

        is_available = (
            _bool_value(
                explicit_available
            )
        )

    variant_count = (
        _int_or_none(
            _first_value(
                product,
                "variant_count",
                "sku_count",
                default=None,
            )
        )
    )

    if variant_count is None:

        variant_count = len(
            variants
        )

    available_variant_count = (
        _int_or_none(
            _first_value(
                product,
                "available_variant_count",
                "available_sku_count",
                default=None,
            )
        )
    )

    if available_variant_count is None:

        available_variant_count = (
            len(
                available_variants
            )
        )

    all_out = (
        _first_value(
            product,
            "all_variants_out_of_stock",
            default=None,
        )
    )

    if all_out is None:

        all_out = (
            bool(
                variants
            )
            and not bool(
                available_variants
            )
        )

    return ProductAPI(
        product_key=(
            _text(
                _first_value(
                    product,
                    "product_key",
                    "logical_product_key",
                    "product_group_key",
                    "slug",
                )
            )
        ),

        name=(
            _text(
                _first_value(
                    product,
                    "name",
                    "product_name",
                    "title",
                )
            )
        ),

        brand=(
            _text(
                _value(
                    product,
                    "brand",
                    None,
                )
            )
        ),

        category=(
            _text(
                _first_value(
                    product,
                    "category",
                    "category_name",
                    "category_slug",
                )
            )
        ),

        category_id=(
            _text(
                _value(
                    product,
                    "category_id",
                    None,
                )
            )
        ),

        image_url=(
            _product_image_url(
                product
            )
        ),

        short_description=(
            _text(
                _first_value(
                    product,
                    "short_description",
                    "description",
                    default=None,
                )
            )
        ),

        rating=(
            _float_or_none(
                _value(
                    product,
                    "rating",
                    None,
                )
            )
        ),

        is_available=(
            is_available
        ),

        all_variants_out_of_stock=(
            _bool_value(
                all_out
            )
        ),

        available_variant_count=(
            available_variant_count
        ),

        variant_count=(
            variant_count
        ),

        price_from=(
            _float_or_none(
                _first_value(
                    product,
                    "price_from",
                    "min_price",
                    default=None,
                )
            )
        ),

        price_to=(
            _float_or_none(
                _first_value(
                    product,
                    "price_to",
                    "max_price",
                    default=None,
                )
            )
        ),

        variants=variants,

        recommendation_reason=(
            _text(
                _first_value(
                    product,
                    "recommendation_reason",
                    "reason",
                    default=None,
                )
            )
        ),

        semantic_tags=(
            _string_list(
                _value(
                    product,
                    "semantic_tags",
                    [],
                )
            )
        ),

        meal_contexts=(
            _string_list(
                _value(
                    product,
                    "meal_contexts",
                    [],
                )
            )
        ),

        use_cases=(
            _string_list(
                _value(
                    product,
                    "use_cases",
                    [],
                )
            )
        ),

        metadata=(
            _safe_metadata(
                _value(
                    product,
                    "metadata",
                    {},
                )
            )
        ),
    )


# ============================================================
# MAP PRODUCT COLLECTION
# ============================================================


def map_products(
    products: Any,
) -> list[
    ProductAPI
]:

    return [
        map_product(
            product
        )
        for product
        in _sequence(
            products
        )
    ]


# ============================================================
# MAP CART ITEM
# ============================================================


def map_cart_item(
    item: Any,
) -> CartItemAPI:

    return CartItemAPI(
        cart_item_id=(
            _text(
                _first_value(
                    item,
                    "cart_item_id",
                    "id",
                )
            )
        ),

        sku_id=(
            _text(
                _first_value(
                    item,
                    "sku_id",
                    "product_id",
                )
            )
        ),

        product_key=(
            _text(
                _first_value(
                    item,
                    "product_key",
                    "logical_product_key",
                    "product_group_key",
                )
            )
        ),

        product_name=(
            _text(
                _first_value(
                    item,
                    "product_name",
                    "name",
                    "title",
                )
            )
        ),

        brand=(
            _text(
                _value(
                    item,
                    "brand",
                    None,
                )
            )
        ),

        size=(
            _text(
                _first_value(
                    item,
                    "size",
                    "pack_size",
                )
            )
        ),

        quantity=(
            _int_or_none(
                _value(
                    item,
                    "quantity",
                    1,
                )
            )
            or 1
        ),

        unit_price=(
            _float_or_none(
                _first_value(
                    item,
                    "unit_price",
                    "price",
                    "selling_price",
                    default=None,
                )
            )
        ),

        line_total=(
            _float_or_none(
                _first_value(
                    item,
                    "line_total",
                    "total_price",
                    default=None,
                )
            )
        ),

        currency=(
            _text(
                _value(
                    item,
                    "currency",
                    None,
                )
            )
        ),

        stock=(
            _int_or_none(
                _value(
                    item,
                    "stock",
                    None,
                )
            )
        ),

        in_stock=(
            _bool_value(
                _first_value(
                    item,
                    "in_stock",
                    "is_available",
                    default=False,
                )
            )
        ),

        image_url=(
            _safe_image_url(
                _first_value(
                    item,
                    "image_url",
                    "primary_image_url",
                    default=None,
                )
            )
        ),
    )


# ============================================================
# MAP CART
# ============================================================


def map_cart(
    cart: Any,
) -> CartAPI | None:

    if cart is None:

        return None

    items = [
        map_cart_item(
            item
        )
        for item in (
            _sequence(
                _value(
                    cart,
                    "items",
                    [],
                )
            )
        )
    ]

    item_count = (
        _int_or_none(
            _value(
                cart,
                "item_count",
                None,
            )
        )
    )

    if item_count is None:

        item_count = len(
            items
        )

    total_quantity = (
        _int_or_none(
            _value(
                cart,
                "total_quantity",
                None,
            )
        )
    )

    if total_quantity is None:

        total_quantity = sum(
            item.quantity
            for item in items
        )

    explicit_empty = (
        _value(
            cart,
            "is_empty",
            None,
        )
    )

    if explicit_empty is None:

        is_empty = (
            len(
                items
            )
            == 0
        )

    else:

        is_empty = (
            _bool_value(
                explicit_empty
            )
        )

    return CartAPI(
        items=items,

        item_count=(
            item_count
        ),

        total_quantity=(
            total_quantity
        ),

        subtotal=(
            _float_or_none(
                _value(
                    cart,
                    "subtotal",
                    None,
                )
            )
        ),

        total=(
            _float_or_none(
                _first_value(
                    cart,
                    "total",
                    "total_amount",
                    default=None,
                )
            )
        ),

        currency=(
            _text(
                _value(
                    cart,
                    "currency",
                    None,
                )
            )
        ),

        is_empty=(
            is_empty
        ),
    )


# ============================================================
# MAP ORDER ITEM
# ============================================================


def map_order_item(
    item: Any,
    *,
    default_currency: str | None = None,
) -> OrderItemAPI:

    return OrderItemAPI(
        order_item_id=(
            _text(
                _first_value(
                    item,
                    "order_item_id",
                    "id",
                )
            )
        ),

        sku_id=(
            _text(
                _first_value(
                    item,
                    "sku_id",
                    "product_id",
                )
            )
        ),

        product_name=(
            _text(
                _first_value(
                    item,
                    "product_name",
                    "name",
                    "title",
                )
            )
        ),

        brand=(
            _text(
                _value(
                    item,
                    "brand",
                    None,
                )
            )
        ),

        size=(
            _text(
                _first_value(
                    item,
                    "size",
                    "pack_size",
                )
            )
        ),

        quantity=(
            _int_or_none(
                _value(
                    item,
                    "quantity",
                    1,
                )
            )
            or 1
        ),

        unit_price=(
            _float_or_none(
                _first_value(
                    item,
                    "unit_price",
                    "price",
                    default=None,
                )
            )
        ),

        line_total=(
            _float_or_none(
                _first_value(
                    item,
                    "line_total",
                    "total_price",
                    default=None,
                )
            )
        ),

        currency=(
            _text(
                _value(
                    item,
                    "currency",
                    default_currency,
                )
            )
            or default_currency
        ),

        image_url=(
            _safe_image_url(
                _first_value(
                    item,
                    "image_url",
                    "primary_image_url",
                    default=None,
                )
            )
        ),
    )


# ============================================================
# MAP ORDER
# ============================================================


def map_order(
    order: Any,
) -> OrderAPI:

    currency = (
        _text(
            _value(
                order,
                "currency",
                None,
            )
        )
    )

    items = [
        map_order_item(
            item,
            default_currency=currency,
        )
        for item
        in _sequence(
            _value(
                order,
                "items",
                [],
            )
        )
    ]

    item_count = (
        _int_or_none(
            _value(
                order,
                "item_count",
                None,
            )
        )
    )

    if item_count is None:

        item_count = len(
            items
        )

    return OrderAPI(
        order_id=(
            _text(
                _first_value(
                    order,
                    "order_id",
                    "id",
                )
            )
        ),

        created_at=(
            _text(
                _value(
                    order,
                    "created_at",
                    None,
                )
            )
        ),

        order_status=(
            _text(
                _first_value(
                    order,
                    "order_status",
                    "status",
                    default=None,
                )
            )
        ),

        payment_status=(
            _text(
                _value(
                    order,
                    "payment_status",
                    None,
                )
            )
        ),

        total_amount=(
            _float_or_none(
                _first_value(
                    order,
                    "total_amount",
                    "total",
                    default=None,
                )
            )
        ),

        currency=currency,

        item_count=(
            item_count
        ),

        items=items,

        metadata=(
            _safe_metadata(
                _value(
                    order,
                    "metadata",
                    {},
                )
            )
        ),
    )


# ============================================================
# MAP ORDERS
# ============================================================


def map_orders(
    orders: Any,
) -> list[
    OrderAPI
]:

    return [
        map_order(
            order
        )
        for order
        in _sequence(
            orders
        )
    ]


# ============================================================
# RESOLVE STRUCTURED PAYLOAD
# ============================================================


def resolve_response_payload(
    result: Any,
) -> Any:
    """
    Preferred:

        BrainResponse.response_payload

    Compatibility fallback:

        BrainResponse.tool_executions
            ↓
        build_database_response_payload()

    The fallback uses already-executed verified tool results.

    It does NOT rerun the user's tool/database operation.
    """

    payload = (
        _value(
            result,
            "response_payload",
            None,
        )
    )

    if payload is not None:

        return payload

    executions = (
        _value(
            result,
            "tool_executions",
            None,
        )
    )

    if not executions:

        return None

    try:

        return (
            build_database_response_payload(
                executions=executions
            )
        )

    except Exception:

        logger.exception(
            "Unable to recover structured API response "
            "from existing tool executions."
        )

        return None


# ============================================================
# RESPONSE TYPE NORMALIZATION
# ============================================================


def _normalize_response_type(
    value: Any,
) -> str | None:

    value = (
        _text(
            value
        )
    )

    if not value:

        return None

    normalized = (
        value
        .strip()
        .lower()
        .replace(
            "-",
            "_",
        )
    )

    aliases = {
        "product":
            "products",

        "product_search":
            "products",

        "catalog":
            "products",

        "rag":
            "recommendations",

        "recommendation":
            "recommendations",

        "order":
            "orders",

        "latest_order":
            "orders",

        "database_response":
            "database",
    }

    normalized = (
        aliases.get(
            normalized,
            normalized,
        )
    )

    if normalized in (
        ALLOWED_RESPONSE_TYPES
    ):

        return normalized

    return None


# ============================================================
# INFER RESPONSE TYPE
# ============================================================


def _infer_response_type(
    *,
    success: bool,
    payload: Any,
    products: list[ProductAPI],
    alternatives: list[ProductAPI],
    recommendations: list[ProductAPI],
    cart: CartAPI | None,
    orders: list[OrderAPI],
    latest_order: OrderAPI | None,
) -> str:

    explicit = (
        _normalize_response_type(
            _value(
                payload,
                "response_type",
                None,
            )
        )
    )

    if explicit:

        return explicit

    if not success:

        return "error"

    if cart is not None:

        return "cart"

    if (
        latest_order is not None
        or orders
    ):

        return "orders"

    if recommendations:

        return "recommendations"

    if (
        products
        or alternatives
    ):

        return "products"

    if payload is not None:

        return "database"

    return "text"


# ============================================================
# TOOL NAMES
# ============================================================


def _tool_names(
    result: Any,
    payload: Any,
) -> list[str]:

    names: list[
        str
    ] = []

    payload_names = (
        _sequence(
            _value(
                payload,
                "tool_names",
                [],
            )
        )
    )

    for item in payload_names:

        value = (
            _text(
                item
            )
        )

        if (
            value
            and value not in names
        ):

            names.append(
                value
            )

    executions = (
        _sequence(
            _value(
                result,
                "tool_executions",
                [],
            )
        )
    )

    for execution in executions:

        value = (
            _text(
                _value(
                    execution,
                    "tool_name",
                    None,
                )
            )
        )

        if (
            value
            and value not in names
        ):

            names.append(
                value
            )

    return names


# ============================================================
# MAP BRAIN RESPONSE
# ============================================================


def map_brain_response(
    result: Any,
    *,
    conversation_id: str | None = None,
) -> ChatResponse:
    """
    Convert BrainResponse into the public ChatResponse API model.

    This is the main function api.py should call.
    """

    if result is None:

        return ChatResponse(
            api_version=API_VERSION,
            success=False,
            text=(
                "I couldn't process that request right now."
            ),
            response_type="error",
            conversation_id=conversation_id,
            error_code="EMPTY_BRAIN_RESPONSE",
        )

    # ========================================================
    # STRUCTURED PAYLOAD
    # ========================================================

    payload = (
        resolve_response_payload(
            result
        )
    )

    # ========================================================
    # CLIENT CONVERSATION STATE
    # ========================================================

    client_state = (
        _safe_client_state(
            result
        )
    )

    resolved_conversation_id = (
        _text(
            conversation_id
        )
        or _text(
            client_state.get(
                "conversation_id"
            )
        )
    )

    # Keep the ID inside client_state aligned with the top-level API
    # conversation_id returned to the frontend.
    if resolved_conversation_id:

        client_state[
            "conversation_id"
        ] = resolved_conversation_id

    # ========================================================
    # TEXT
    # ========================================================

    result_text = (
        _text(
            _value(
                result,
                "text",
                None,
            )
        )
    )

    payload_text = (
        _text(
            _value(
                payload,
                "text",
                None,
            )
        )
    )

    final_text = (
        result_text
        or payload_text
        or ""
    )

    # ========================================================
    # SUCCESS
    # ========================================================

    result_success = (
        _bool_value(
            _value(
                result,
                "success",
                True,
            ),
            default=True,
        )
    )

    payload_success_value = (
        _value(
            payload,
            "success",
            None,
        )
    )

    if payload_success_value is None:

        final_success = (
            result_success
        )

    else:

        final_success = bool(
            result_success
            and _bool_value(
                payload_success_value,
                default=True,
            )
        )

    # ========================================================
    # PRODUCTS
    # ========================================================

    products = (
        map_products(
            _value(
                payload,
                "products",
                [],
            )
        )
    )

    alternatives = (
        map_products(
            _value(
                payload,
                "alternatives",
                [],
            )
        )
    )

    recommendations = (
        map_products(
            _value(
                payload,
                "recommendations",
                [],
            )
        )
    )

    # ========================================================
    # CART
    # ========================================================

    cart = (
        map_cart(
            _value(
                payload,
                "cart",
                None,
            )
        )
    )

    # ========================================================
    # ORDERS
    # ========================================================

    orders = (
        map_orders(
            _value(
                payload,
                "orders",
                [],
            )
        )
    )

    latest_order_raw = (
        _value(
            payload,
            "latest_order",
            None,
        )
    )

    latest_order = (
        map_order(
            latest_order_raw
        )
        if latest_order_raw
        is not None
        else None
    )

    # ========================================================
    # ERROR CODE
    # ========================================================

    error_code = (
        _text(
            _first_value(
                result,
                "error_code",
                default=None,
            )
        )
    )

    if not error_code:

        error_code = (
            _text(
                _value(
                    payload,
                    "error_code",
                    None,
                )
            )
        )

    # ========================================================
    # RESPONSE TYPE
    # ========================================================

    response_type = (
        _infer_response_type(
            success=final_success,
            payload=payload,
            products=products,
            alternatives=alternatives,
            recommendations=recommendations,
            cart=cart,
            orders=orders,
            latest_order=latest_order,
        )
    )

    # ========================================================
    # SAFE PUBLIC METADATA
    # ========================================================

    metadata = (
        _safe_metadata(
            _value(
                payload,
                "metadata",
                {},
            )
        )
    )

    tool_names = (
        _tool_names(
            result,
            payload,
        )
    )

    if tool_names:

        metadata[
            "tool_names"
        ] = tool_names

    # --------------------------------------------------------
    # Stateless frontend conversation continuity
    # --------------------------------------------------------
    #
    # api_models.py already exposes a safe metadata object, so the
    # client_state is placed there. This keeps the mapper compatible
    # with the current ChatResponse schema without requiring a breaking
    # API-model change.
    #
    # Frontend next request:
    #
    #     client_context = response.metadata.client_state
    #
    # Authentication tokens are NOT present in this state.
    # --------------------------------------------------------

    if client_state:

        metadata[
            "client_state"
        ] = (
            client_state
        )

    metadata[
        "has_structured_content"
    ] = bool(
        products
        or alternatives
        or recommendations
        or cart is not None
        or orders
        or latest_order is not None
    )

    metadata[
        "product_count"
    ] = len(
        products
    )

    metadata[
        "alternative_count"
    ] = len(
        alternatives
    )

    metadata[
        "recommendation_count"
    ] = len(
        recommendations
    )

    metadata[
        "order_count"
    ] = len(
        orders
    )

    # ========================================================
    # FINAL PUBLIC RESPONSE
    # ========================================================

    return ChatResponse(
        api_version=API_VERSION,

        success=final_success,

        text=final_text,

        response_type=response_type,

        conversation_id=(
            resolved_conversation_id
        ),

        products=products,

        alternatives=alternatives,

        recommendations=recommendations,

        cart=cart,

        orders=orders,

        latest_order=latest_order,

        error_code=error_code,

        metadata=metadata,
    )


# ============================================================
# CLIENT STATE EXTRACTION
# ============================================================


def get_response_client_state(
    response: Any,
) -> dict[
    str,
    Any,
]:
    """
    Return client_state from either:

        BrainResponse
        ChatResponse
        plain API response dictionary

    This helper is optional but useful for tests and API adapters.
    """

    # BrainResponse path.
    direct = (
        _safe_client_state(
            response
        )
    )

    if direct:

        return direct

    # ChatResponse / dictionary path.
    metadata = (
        _value(
            response,
            "metadata",
            {},
        )
    )

    if isinstance(
        metadata,
        Mapping,
    ):

        value = metadata.get(
            "client_state"
        )

        if value is not None:

            return _safe_metadata(
                value
            )

    return {}


# ============================================================
# DICTIONARY RESPONSE
# ============================================================


def map_brain_response_to_dict(
    result: Any,
    *,
    conversation_id: str | None = None,
) -> dict[
    str,
    Any,
]:
    """
    Convenience helper when api.py needs a plain JSON-compatible dict.
    """

    response = (
        map_brain_response(
            result,
            conversation_id=conversation_id,
        )
    )

    return response.model_dump(
        mode="json",
        exclude_none=False,
    )


# ============================================================
# SAFE FAILURE RESPONSE
# ============================================================


def build_error_chat_response(
    *,
    message: str,
    error_code: str,
    conversation_id: str | None = None,
) -> ChatResponse:
    """
    Build a safe frontend-facing error response.

    Do not pass:
        stack traces
        raw exceptions
        secrets
    """

    message = (
        _text(
            message
        )
        or "The request could not be completed."
    )

    error_code = (
        _text(
            error_code
        )
        or "API_ERROR"
    )

    return ChatResponse(
        api_version=API_VERSION,

        success=False,

        text=message,

        response_type="error",

        conversation_id=conversation_id,

        products=[],

        alternatives=[],

        recommendations=[],

        cart=None,

        orders=[],

        latest_order=None,

        error_code=error_code,

        metadata={},
    )


# ============================================================
# SAFE FAILURE DICTIONARY
# ============================================================


def build_error_chat_response_dict(
    *,
    message: str,
    error_code: str,
    conversation_id: str | None = None,
) -> dict[
    str,
    Any,
]:

    response = (
        build_error_chat_response(
            message=message,
            error_code=error_code,
            conversation_id=conversation_id,
        )
    )

    return response.model_dump(
        mode="json",
        exclude_none=False,
    )


# ============================================================
# PUBLIC PAYLOAD COPY
# ============================================================


def copy_public_response(
    response: ChatResponse,
) -> dict[
    str,
    Any,
]:
    """
    Return a detached JSON-compatible copy suitable for FastAPI.
    """

    data = response.model_dump(
        mode="json",
        exclude_none=False,
    )

    return copy.deepcopy(
        data
    )