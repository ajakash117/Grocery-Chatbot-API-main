"""
core/database_responder.py

Grounded database-response layer.

Purpose
-------

Cohere
    ↓
selects backend tool
    ↓
Python / Supabase / RAG
    ↓
verified result
    ↓
database_responder
    ├── builds ResponsePayload
    ├── preserves product images / SKUs / order data
    ├── sends a SMALL verified summary to Groq API 3
    └── uses prompts/rules.txt
    ↓
natural chatbot text + structured UI payload


IMPORTANT
---------

Database response generation uses ONLY:

    GROQ_API_KEY2

This is Groq API slot 3.

It does NOT rotate through:

    GROQ_API_KEY
    GROQ_API_KEY1

If API 3 is rate limited, Python creates a safe deterministic fallback.

Groq does NOT decide database facts.

Supabase/Python remains authoritative for:

    product existence
    price
    MRP
    stock
    SKU
    variants
    cart
    orders
    historical order prices
"""

from __future__ import annotations

import json
import logging
import os

from functools import lru_cache
from pathlib import Path

from typing import (
    Any,
    Mapping,
    Sequence,
)


# ============================================================
# GROQ
# ============================================================

from groq import Groq


# ============================================================
# CONFIG
# ============================================================

from config import settings


# ============================================================
# RESPONSE MODELS
# ============================================================

from core.response_models import (
    CartResponse,
    OrderResponse,
    ProductResponse,
    ResponsePayload,
    make_response_payload,
    normalize_orders,
    normalize_products,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# GROQ API SLOT
# ============================================================

DATABASE_GROQ_ENV_NAME = (
    "GROQ_API_KEY2"
)

DATABASE_GROQ_KEY_SLOT = 3


# ============================================================
# PROMPT LIMITS
# ============================================================
#
# Keep these SMALL.
#
# The previous architecture was sending thousands of tokens to Groq.
# Database response generation does not need that.
# ============================================================

DATABASE_MAX_COMPLETION_TOKENS = 260

MAX_RULES_CHARACTERS = 6_000

MAX_FACTS_CHARACTERS = 6_000

MAX_USER_MESSAGE_CHARACTERS = 1_000

MAX_TOOL_MESSAGE_CHARACTERS = 500

MAX_PRODUCTS_FOR_LLM = 4

MAX_VARIANTS_FOR_LLM = 5

MAX_RECOMMENDATIONS_FOR_LLM = 4

MAX_ORDERS_FOR_LLM = 3

MAX_ORDER_ITEMS_FOR_LLM = 8

MAX_CART_ITEMS_FOR_LLM = 8

MAX_GENERIC_LIST_ITEMS = 8

MAX_STRING_VALUE = 500


# ============================================================
# TOOL GROUPS
# ============================================================

PRODUCT_TOOLS = {
    "search_products",
    "get_product_details",
    "get_product_variants",
    "resolve_product_variant",
    "get_cheapest_variant",
    "get_most_expensive_variant",
    "get_sku_details",
    "find_product_substitutes",
}


RAG_TOOLS = {
    "semantic_product_search",
}


CART_TOOLS = {
    "get_cart",
    "get_cart_summary",
    "add_to_cart",
    "update_cart_quantity",
    "remove_from_cart",
    "clear_cart",
}


ORDER_TOOLS = {
    "get_my_orders",
    "get_order_details",
    "get_latest_order",
    "get_order_count",
    "get_order_history_summary",
    "get_order_status",
}


CATALOG_TOOLS = {
    "list_catalog_brands",
    "list_catalog_categories",
    "get_catalog_stats",
}


# ============================================================
# INTERNAL HELPERS
# ============================================================


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


def _execution_value(
    execution: Any,
    field_name: str,
    default: Any = None,
) -> Any:
    """
    Support either:

        ToolExecutionRecord dataclass

    or:

        dictionary-shaped execution
    """

    if isinstance(
        execution,
        Mapping,
    ):

        return execution.get(
            field_name,
            default,
        )

    return getattr(
        execution,
        field_name,
        default,
    )


def _execution_tool_name(
    execution: Any,
) -> str:

    return str(
        _execution_value(
            execution,
            "tool_name",
            "",
        )
        or ""
    ).strip()


def _execution_result(
    execution: Any,
) -> dict[
    str,
    Any,
]:

    value = (
        _execution_value(
            execution,
            "result",
            {},
        )
    )

    if isinstance(
        value,
        Mapping,
    ):

        return dict(
            value
        )

    return {}


def _execution_success(
    execution: Any,
) -> bool:

    result = (
        _execution_result(
            execution
        )
    )

    explicit = (
        _execution_value(
            execution,
            "success",
            None,
        )
    )

    if explicit is not None:

        return bool(
            explicit
        )

    return bool(
        result.get(
            "success"
        )
    )


# ============================================================
# RULES.TXT
# ============================================================


def _project_root() -> Path:

    return (
        Path(
            __file__
        )
        .resolve()
        .parent
        .parent
    )


@lru_cache(
    maxsize=1
)
def load_rules_text() -> str:
    """
    Load existing project rules.

    Preferred:

        prompts/rules.txt

    Compatibility fallback:

        rules.txt
    """

    root = (
        _project_root()
    )

    candidates = [
        root
        / "prompts"
        / "rules.txt",

        root
        / "rules.txt",
    ]

    for path in candidates:

        if not path.exists():

            continue

        try:

            text = (
                path
                .read_text(
                    encoding="utf-8"
                )
                .strip()
            )

        except Exception:

            logger.exception(
                "Unable to read rules file. path=%s",
                path,
            )

            continue

        if text:

            return text

    raise FileNotFoundError(
        "Could not find prompts/rules.txt or rules.txt."
    )


# ============================================================
# RULE SECTION SELECTION
# ============================================================


BASE_RULE_KEYWORDS = {
    "source of truth",
    "verified",
    "database",
    "supabase",
    "invent",
    "groq",
    "response",
    "backend",
}


PRODUCT_RULE_KEYWORDS = {
    "product",
    "products",
    "sku",
    "variant",
    "stock",
    "price",
    "mrp",
    "brand",
    "category",
}


CART_RULE_KEYWORDS = {
    "cart",
    "quantity",
    "add",
    "remove",
}


ORDER_RULE_KEYWORDS = {
    "order",
    "orders",
    "order item",
    "historical",
    "payment",
    "total",
}


RAG_RULE_KEYWORDS = {
    "rag",
    "recommendation",
    "semantic",
    "health",
    "meal",
    "breakfast",
}


def _rule_keywords_for_tools(
    tool_names: Sequence[
        str
    ],
) -> set[str]:

    keywords = set(
        BASE_RULE_KEYWORDS
    )

    tool_set = set(
        tool_names
    )

    if (
        tool_set
        & PRODUCT_TOOLS
    ):

        keywords.update(
            PRODUCT_RULE_KEYWORDS
        )

    if (
        tool_set
        & CART_TOOLS
    ):

        keywords.update(
            CART_RULE_KEYWORDS
        )

    if (
        tool_set
        & ORDER_TOOLS
    ):

        keywords.update(
            ORDER_RULE_KEYWORDS
        )

    if (
        tool_set
        & RAG_TOOLS
    ):

        keywords.update(
            RAG_RULE_KEYWORDS
        )

    return keywords


def _split_rule_blocks(
    rules: str,
) -> list[str]:
    """
    Split rules.txt into meaningful blocks.

    The original rules.txt may be large.

    Sending the entire file on every database response defeats the
    purpose of reducing Groq token usage.
    """

    blocks = []

    current = []

    for line in (
        rules.splitlines()
    ):

        stripped = (
            line.strip()
        )

        if (
            not stripped
            and current
        ):

            blocks.append(
                "\n".join(
                    current
                ).strip()
            )

            current = []

            continue

        current.append(
            line
        )

    if current:

        blocks.append(
            "\n".join(
                current
            ).strip()
        )

    return [
        block
        for block in blocks
        if block
    ]


def _block_score(
    block: str,
    keywords: set[
        str
    ],
) -> int:

    normalized = (
        block.lower()
    )

    score = 0

    for keyword in keywords:

        if keyword in normalized:

            score += 1

    # Core truth/safety statements receive additional priority.

    if (
        "source of truth"
        in normalized
    ):

        score += 4

    if (
        "never invent"
        in normalized
    ):

        score += 4

    if (
        "verified"
        in normalized
    ):

        score += 2

    if (
        "sku"
        in normalized
    ):

        score += 1

    return score


def select_database_response_rules(
    *,
    tool_names: Sequence[
        str
    ],
) -> str:
    """
    IMPORTANT:

    We DO use rules.txt.

    But we do NOT send a massive rules file to Groq for every response.

    Instead, select the most relevant rules.txt blocks according to the
    backend tools already selected by Cohere.

    This is NOT natural-language intent classification.

    Cohere has already selected the tools.
    """

    try:

        rules = (
            load_rules_text()
        )

    except FileNotFoundError:

        logger.warning(
            "rules.txt is unavailable; using built-in safety rules."
        )

        return ""

    if (
        len(
            rules
        )
        <= MAX_RULES_CHARACTERS
    ):

        return rules

    keywords = (
        _rule_keywords_for_tools(
            tool_names
        )
    )

    blocks = (
        _split_rule_blocks(
            rules
        )
    )

    scored = []

    for index, block in enumerate(
        blocks
    ):

        score = (
            _block_score(
                block,
                keywords,
            )
        )

        if score <= 0:

            continue

        scored.append(
            (
                score,
                index,
                block,
            )
        )

    # Highest-relevance rules first.

    scored.sort(
        key=lambda item: (
            -item[
                0
            ],
            item[
                1
            ],
        )
    )

    selected = []

    current_length = 0

    for (
        score,
        original_index,
        block,
    ) in scored:

        del score

        block_length = (
            len(
                block
            )
        )

        if (
            current_length
            + block_length
            > MAX_RULES_CHARACTERS
        ):

            continue

        selected.append(
            (
                original_index,
                block,
            )
        )

        current_length += (
            block_length
        )

        if (
            current_length
            >= MAX_RULES_CHARACTERS
        ):

            break

    # Restore original rules.txt ordering.

    selected.sort(
        key=lambda item:
            item[
                0
            ]
    )

    text = "\n\n".join(
        block
        for (
            _,
            block,
        )
        in selected
    )

    # Defensive fallback if scoring somehow found nothing.

    if not text:

        text = (
            rules[
                :MAX_RULES_CHARACTERS
            ]
        )

    return text


# ============================================================
# PRODUCT DEDUPLICATION
# ============================================================


def _product_identity(
    product: ProductResponse,
) -> str:

    if product.product_key:

        return (
            "key:"
            + product.product_key
        )

    return (
        "name:"
        + str(
            product.brand
            or ""
        ).lower()
        + "|"
        + str(
            product.name
            or ""
        ).lower()
    )


def _deduplicate_products(
    products: Sequence[
        ProductResponse
    ],
) -> list[
    ProductResponse
]:

    results = []

    seen = set()

    for product in products:

        identity = (
            _product_identity(
                product
            )
        )

        if identity in seen:

            continue

        seen.add(
            identity
        )

        results.append(
            product
        )

    return results


# ============================================================
# RAG PRODUCT NORMALIZATION
# ============================================================


def _rag_product_mapping(
    value: Mapping[
        str,
        Any,
    ],
) -> Mapping[
    str,
    Any,
]:
    """
    Supports both current compact RAG format:

        {
            product_name,
            brand,
            variants,
            ...
        }

    and older hydrated format:

        {
            semantic_match: {...},
            live_product: {...},
            eligible_variants: [...]
        }
    """

    live_product = (
        value.get(
            "live_product"
        )
    )

    if not isinstance(
        live_product,
        Mapping,
    ):

        return value

    product = dict(
        live_product
    )

    eligible = (
        value.get(
            "eligible_variants"
        )
    )

    if (
        isinstance(
            eligible,
            Sequence,
        )
        and not isinstance(
            eligible,
            (
                str,
                bytes,
            ),
        )
    ):

        product[
            "variants"
        ] = list(
            eligible
        )

    semantic = (
        value.get(
            "semantic_match"
        )
    )

    if isinstance(
        semantic,
        Mapping,
    ):

        product[
            "semantic_tags"
        ] = (
            semantic.get(
                "semantic_tags"
            )
            or []
        )

        product[
            "meal_contexts"
        ] = (
            semantic.get(
                "meal_contexts"
            )
            or []
        )

        product[
            "use_cases"
        ] = (
            semantic.get(
                "use_cases"
            )
            or []
        )

        product[
            "recommendation_reason"
        ] = (
            semantic.get(
                "health_context"
            )
            or semantic.get(
                "description"
            )
        )

    return product


# ============================================================
# ORDER NORMALIZATION
# ============================================================


def _looks_like_order(
    value: Mapping[
        str,
        Any,
    ],
) -> bool:

    keys = set(
        value.keys()
    )

    return bool(
        keys
        & {
            "order_id",
            "id",
            "order_status",
            "status",
            "total_amount",
            "created_at",
            "order_date",
        }
    )


def _merge_order_items(
    order: Mapping[
        str,
        Any,
    ],
    data: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

    merged = dict(
        order
    )

    if (
        not merged.get(
            "items"
        )
        and not merged.get(
            "order_items"
        )
    ):

        items = (
            data.get(
                "items"
            )
            or data.get(
                "order_items"
            )
        )

        if items:

            merged[
                "items"
            ] = items

    return merged


def _extract_orders_from_data(
    *,
    data: Mapping[
        str,
        Any,
    ],
    tool_name: str,
) -> tuple[
    list[
        OrderResponse
    ],
    OrderResponse | None,
]:

    orders: list[
        OrderResponse
    ] = []

    latest_order: (
        OrderResponse
        | None
    ) = None

    # ========================================================
    # EXPLICIT LATEST ORDER
    # ========================================================

    raw_latest = (
        data.get(
            "latest_order"
        )
    )

    if isinstance(
        raw_latest,
        Mapping,
    ):

        raw_latest = (
            _merge_order_items(
                raw_latest,
                data,
            )
        )

        latest_order = (
            OrderResponse.from_mapping(
                raw_latest
            )
        )

    # ========================================================
    # SINGLE ORDER
    # ========================================================

    raw_order = (
        data.get(
            "order"
        )
    )

    if isinstance(
        raw_order,
        Mapping,
    ):

        raw_order = (
            _merge_order_items(
                raw_order,
                data,
            )
        )

        normalized = (
            OrderResponse.from_mapping(
                raw_order
            )
        )

        if (
            tool_name
            == "get_latest_order"
        ):

            latest_order = (
                normalized
            )

        else:

            orders.append(
                normalized
            )

    # ========================================================
    # ORDER DETAILS WRAPPER
    # ========================================================

    details = (
        data.get(
            "details"
        )
    )

    if isinstance(
        details,
        Mapping,
    ):

        candidate = (
            details.get(
                "order"
            )
            if isinstance(
                details.get(
                    "order"
                ),
                Mapping,
            )
            else details
        )

        if isinstance(
            candidate,
            Mapping,
        ) and _looks_like_order(
            candidate
        ):

            candidate = (
                _merge_order_items(
                    candidate,
                    details,
                )
            )

            normalized = (
                OrderResponse.from_mapping(
                    candidate
                )
            )

            if (
                tool_name
                == "get_latest_order"
            ):

                latest_order = (
                    normalized
                )

            else:

                orders.append(
                    normalized
                )

    # ========================================================
    # ORDER LIST
    # ========================================================

    raw_orders = (
        data.get(
            "orders"
        )
    )

    if (
        isinstance(
            raw_orders,
            Sequence,
        )
        and not isinstance(
            raw_orders,
            (
                str,
                bytes,
            ),
        )
    ):

        normalized_orders = (
            normalize_orders(
                raw_orders
            )
        )

        if (
            tool_name
            == "get_latest_order"
            and normalized_orders
            and latest_order
            is None
        ):

            latest_order = (
                normalized_orders[
                    0
                ]
            )

        else:

            orders.extend(
                normalized_orders
            )

    # ========================================================
    # DATA ITSELF IS AN ORDER
    # ========================================================

    if (
        latest_order is None
        and not orders
        and _looks_like_order(
            data
        )
    ):

        normalized = (
            OrderResponse.from_mapping(
                data
            )
        )

        if (
            tool_name
            == "get_latest_order"
        ):

            latest_order = (
                normalized
            )

        else:

            orders.append(
                normalized
            )

    return (
        orders,
        latest_order,
    )


# ============================================================
# CART NORMALIZATION
# ============================================================


def _extract_cart_from_data(
    data: Mapping[
        str,
        Any,
    ],
) -> CartResponse | None:

    raw_cart = (
        data.get(
            "cart"
        )
    )

    if isinstance(
        raw_cart,
        Mapping,
    ):

        return (
            CartResponse.from_mapping(
                raw_cart
            )
        )

    if (
        "items"
        in data
        or "cart_items"
        in data
        or "item_count"
        in data
        or "total_quantity"
        in data
        or "subtotal"
        in data
        or "total"
        in data
    ):

        return (
            CartResponse.from_mapping(
                data
            )
        )

    summary = (
        data.get(
            "summary"
        )
    )

    if isinstance(
        summary,
        Mapping,
    ):

        return (
            CartResponse.from_mapping(
                summary
            )
        )

    return None


# ============================================================
# SINGLE SKU → PRODUCT
# ============================================================


def _sku_to_product(
    data: Mapping[
        str,
        Any,
    ],
) -> ProductResponse | None:

    sku = (
        data.get(
            "sku"
        )
        or data.get(
            "variant"
        )
    )

    if not isinstance(
        sku,
        Mapping,
    ):

        return None

    product_mapping = {
        "product_key":
            (
                sku.get(
                    "product_key"
                )
                or data.get(
                    "product_key"
                )
            ),

        "name":
            (
                sku.get(
                    "product_name"
                )
                or sku.get(
                    "name"
                )
            ),

        "brand":
            sku.get(
                "brand"
            ),

        "image_url":
            sku.get(
                "image_url"
            ),

        "is_available":
            bool(
                sku.get(
                    "in_stock"
                )
            ),

        "variant_count":
            1,

        "available_variant_count":
            (
                1
                if sku.get(
                    "in_stock"
                )
                else 0
            ),

        "variants": [
            sku
        ],
    }

    return (
        ProductResponse.from_mapping(
            product_mapping
        )
    )


# ============================================================
# BUILD RESPONSE PAYLOAD
# ============================================================


def build_database_response_payload(
    *,
    executions: Sequence[
        Any
    ],
) -> ResponsePayload:
    """
    Convert successful tool executions into one stable UI payload.

    This does NOT call Groq.
    """

    products: list[
        ProductResponse
    ] = []

    alternatives: list[
        ProductResponse
    ] = []

    recommendations: list[
        ProductResponse
    ] = []

    orders: list[
        OrderResponse
    ] = []

    latest_order: (
        OrderResponse
        | None
    ) = None

    cart: (
        CartResponse
        | None
    ) = None

    tool_names = []

    backend_messages = []

    generic_facts: list[
        dict[
            str,
            Any,
        ]
    ] = []

    response_type = (
        "database"
    )

    any_success = False

    # ========================================================
    # EXECUTIONS
    # ========================================================

    for execution in executions:

        tool_name = (
            _execution_tool_name(
                execution
            )
        )

        if not tool_name:

            continue

        if (
            tool_name
            not in tool_names
        ):

            tool_names.append(
                tool_name
            )

        result = (
            _execution_result(
                execution
            )
        )

        if not _execution_success(
            execution
        ):

            error = (
                result.get(
                    "error"
                )
            )

            if error:

                generic_facts.append(
                    {
                        "tool":
                            tool_name,

                        "success":
                            False,

                        "error":
                            error,
                    }
                )

            continue

        any_success = True

        message = (
            _optional_text(
                result.get(
                    "message"
                )
            )
        )

        if message:

            backend_messages.append(
                message[
                    :MAX_TOOL_MESSAGE_CHARACTERS
                ]
            )

        data = (
            result.get(
                "data"
            )
        )

        if not isinstance(
            data,
            Mapping,
        ):

            continue

        # ====================================================
        # PRODUCT SEARCH
        # ====================================================

        if (
            tool_name
            == "search_products"
        ):

            response_type = (
                "products"
            )

            products.extend(
                normalize_products(
                    data.get(
                        "products"
                    )
                )
            )

            alternatives.extend(
                normalize_products(
                    data.get(
                        "alternatives"
                    )
                )
            )

            generic_facts.append(
                {
                    "tool":
                        tool_name,

                    "status":
                        data.get(
                            "status"
                        ),

                    "found":
                        data.get(
                            "found"
                        ),

                    "all_matches_out_of_stock":
                        data.get(
                            "all_matches_out_of_stock"
                        ),

                    "product_count":
                        data.get(
                            "product_count"
                        ),
                }
            )

            continue

        # ====================================================
        # RAG
        # ====================================================

        if (
            tool_name
            == "semantic_product_search"
        ):

            response_type = (
                "recommendations"
            )

            raw_matches = (
                data.get(
                    "matches"
                )
                or []
            )

            normalized_rag = []

            if (
                isinstance(
                    raw_matches,
                    Sequence,
                )
                and not isinstance(
                    raw_matches,
                    (
                        str,
                        bytes,
                    ),
                )
            ):

                for item in raw_matches:

                    if isinstance(
                        item,
                        Mapping,
                    ):

                        normalized_rag.append(
                            _rag_product_mapping(
                                item
                            )
                        )

            recommendations.extend(
                normalize_products(
                    normalized_rag
                )
            )

            raw_unavailable = (
                data.get(
                    "unavailable_matches"
                )
                or []
            )

            normalized_unavailable = []

            if (
                isinstance(
                    raw_unavailable,
                    Sequence,
                )
                and not isinstance(
                    raw_unavailable,
                    (
                        str,
                        bytes,
                    ),
                )
            ):

                for item in (
                    raw_unavailable
                ):

                    if isinstance(
                        item,
                        Mapping,
                    ):

                        normalized_unavailable.append(
                            _rag_product_mapping(
                                item
                            )
                        )

            alternatives.extend(
                normalize_products(
                    normalized_unavailable
                )
            )

            generic_facts.append(
                {
                    "tool":
                        tool_name,

                    "status":
                        data.get(
                            "status"
                        ),

                    "match_count":
                        data.get(
                            "match_count"
                        ),

                    "unavailable_match_count":
                        data.get(
                            "unavailable_match_count"
                        ),
                }
            )

            continue

        # ====================================================
        # SINGLE PRODUCT DETAILS
        # ====================================================

        if (
            tool_name
            == "get_product_details"
        ):

            response_type = (
                "products"
            )

            raw_product = (
                data.get(
                    "product"
                )
            )

            if isinstance(
                raw_product,
                Mapping,
            ):

                products.append(
                    ProductResponse.from_mapping(
                        raw_product
                    )
                )

            continue

        # ====================================================
        # SKU / VARIANT
        # ====================================================

        if tool_name in {
            "get_sku_details",
            "resolve_product_variant",
            "get_cheapest_variant",
            "get_most_expensive_variant",
        }:

            response_type = (
                "products"
            )

            product = (
                _sku_to_product(
                    data
                )
            )

            if product:

                products.append(
                    product
                )

            continue

        # ====================================================
        # SUBSTITUTES
        # ====================================================

        if (
            tool_name
            == "find_product_substitutes"
        ):

            response_type = (
                "products"
            )

            alternatives.extend(
                normalize_products(
                    data.get(
                        "products"
                    )
                )
            )

            continue

        # ====================================================
        # CART
        # ====================================================

        if (
            tool_name
            in CART_TOOLS
        ):

            response_type = (
                "cart"
            )

            extracted_cart = (
                _extract_cart_from_data(
                    data
                )
            )

            if extracted_cart:

                cart = (
                    extracted_cart
                )

            generic_facts.append(
                {
                    "tool":
                        tool_name,

                    "data":
                        _compact_generic(
                            data
                        ),
                }
            )

            continue

        # ====================================================
        # ORDERS
        # ====================================================

        if (
            tool_name
            in ORDER_TOOLS
        ):

            response_type = (
                "orders"
            )

            (
                extracted_orders,
                extracted_latest,
            ) = (
                _extract_orders_from_data(
                    data=data,
                    tool_name=tool_name,
                )
            )

            orders.extend(
                extracted_orders
            )

            if (
                extracted_latest
                is not None
            ):

                latest_order = (
                    extracted_latest
                )

            generic_facts.append(
                {
                    "tool":
                        tool_name,

                    "data":
                        _compact_generic(
                            data
                        ),
                }
            )

            continue

        # ====================================================
        # OTHER DATABASE FACTS
        # ====================================================

        generic_facts.append(
            {
                "tool":
                    tool_name,

                "data":
                    _compact_generic(
                        data
                    ),
            }
        )

    # ========================================================
    # DEDUPE
    # ========================================================

    products = (
        _deduplicate_products(
            products
        )
    )

    alternatives = (
        _deduplicate_products(
            alternatives
        )
    )

    recommendations = (
        _deduplicate_products(
            recommendations
        )
    )

    # ========================================================
    # PAYLOAD
    # ========================================================

    return make_response_payload(
        text="",

        response_type=(
            response_type
        ),

        success=(
            any_success
        ),

        products=(
            products
        ),

        alternatives=(
            alternatives
        ),

        recommendations=(
            recommendations
        ),

        cart=(
            cart
        ),

        orders=(
            orders
        ),

        latest_order=(
            latest_order
        ),

        tool_names=(
            tool_names
        ),

        requires_groq=True,

        groq_used=False,

        groq_key_slot=(
            DATABASE_GROQ_KEY_SLOT
        ),

        metadata={
            "backend_messages":
                backend_messages[
                    :5
                ],

            "backend_facts":
                generic_facts[
                    :8
                ],
        },
    )


# ============================================================
# GENERIC COMPACTION
# ============================================================


def _compact_generic(
    value: Any,
    *,
    depth: int = 0,
) -> Any:

    if depth > 5:

        return str(
            value
        )[
            :MAX_STRING_VALUE
        ]

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

        return (
            value[
                :MAX_STRING_VALUE
            ]
        )

    if isinstance(
        value,
        Mapping,
    ):

        excluded = {
            "embedding",
            "vector",
            "rag_text",
            "raw_response",
            "debug",
            "internal_debug",
            "image_url",
            "created_at",
            "updated_at",
            "description",
            "short_description",
        }

        result = {}

        for (
            key,
            item,
        ) in value.items():

            key = str(
                key
            )

            if key in excluded:

                continue

            result[
                key
            ] = (
                _compact_generic(
                    item,
                    depth=(
                        depth + 1
                    ),
                )
            )

        return result

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
            _compact_generic(
                item,
                depth=(
                    depth + 1
                ),
            )
            for item
            in list(
                value
            )[
                :MAX_GENERIC_LIST_ITEMS
            ]
        ]

    return str(
        value
    )[
        :MAX_STRING_VALUE
    ]


# ============================================================
# PRODUCT FACT FOR GROQ
# ============================================================


def _product_fact(
    product: ProductResponse,
) -> dict[
    str,
    Any,
]:

    variants = []

    for variant in (
        product.variants[
            :MAX_VARIANTS_FOR_LLM
        ]
    ):

        variants.append(
            {
                "sku_id":
                    variant.sku_id,

                "size":
                    variant.size,

                "pack_size":
                    variant.pack_size,

                "price":
                    variant.price,

                "mrp":
                    variant.mrp,

                "currency":
                    variant.currency,

                "stock":
                    variant.stock,

                "in_stock":
                    variant.in_stock,
            }
        )

    return {
        "product_key":
            product.product_key,

        "name":
            product.name,

        "brand":
            product.brand,

        "category":
            product.category,

        "is_available":
            product.is_available,

        "all_variants_out_of_stock":
            product.all_variants_out_of_stock,

        "available_variant_count":
            product.available_variant_count,

        "price_from":
            product.price_from,

        "price_to":
            product.price_to,

        "recommendation_reason":
            product.recommendation_reason,

        "semantic_tags":
            product.semantic_tags[
                :5
            ],

        "meal_contexts":
            product.meal_contexts[
                :5
            ],

        "use_cases":
            product.use_cases[
                :5
            ],

        "variants":
            variants,
    }


# ============================================================
# ORDER FACT FOR GROQ
# ============================================================


def _order_fact(
    order: OrderResponse,
) -> dict[
    str,
    Any,
]:

    return {
        "order_id":
            order.order_id,

        "created_at":
            order.created_at,

        "order_status":
            order.order_status,

        "payment_status":
            order.payment_status,

        "total_amount":
            order.total_amount,

        "currency":
            order.currency,

        "item_count":
            order.item_count,

        "items": [
            {
                "sku_id":
                    item.sku_id,

                "product_name":
                    item.product_name,

                "brand":
                    item.brand,

                "size":
                    item.size,

                "quantity":
                    item.quantity,

                "unit_price":
                    item.unit_price,

                "line_total":
                    item.line_total,

                "currency":
                    item.currency,
            }
            for item
            in order.items[
                :MAX_ORDER_ITEMS_FOR_LLM
            ]
        ],
    }


# ============================================================
# CART FACT FOR GROQ
# ============================================================


def _cart_fact(
    cart: CartResponse,
) -> dict[
    str,
    Any,
]:

    return {
        "item_count":
            cart.item_count,

        "total_quantity":
            cart.total_quantity,

        "subtotal":
            cart.subtotal,

        "total":
            cart.total,

        "currency":
            cart.currency,

        "is_empty":
            cart.is_empty,

        "items": [
            {
                "sku_id":
                    item.sku_id,

                "product_name":
                    item.product_name,

                "brand":
                    item.brand,

                "size":
                    item.size,

                "quantity":
                    item.quantity,

                "unit_price":
                    item.unit_price,

                "line_total":
                    item.line_total,

                "currency":
                    item.currency,

                "in_stock":
                    item.in_stock,
            }
            for item
            in cart.items[
                :MAX_CART_ITEMS_FOR_LLM
            ]
        ],
    }


# ============================================================
# BUILD LLM FACTS
# ============================================================


def build_compact_database_facts(
    payload: ResponsePayload,
) -> dict[
    str,
    Any,
]:
    """
    Build a deliberately SMALL Groq payload.

    image_url is intentionally excluded.

    Images are rendered directly by Streamlit from the structured
    ResponsePayload and do not need to consume Groq tokens.
    """

    facts: dict[
        str,
        Any,
    ] = {
        "response_type":
            payload.response_type,

        "success":
            payload.success,
    }

    if payload.products:

        facts[
            "products"
        ] = [
            _product_fact(
                product
            )
            for product
            in payload.products[
                :MAX_PRODUCTS_FOR_LLM
            ]
        ]

    if payload.alternatives:

        facts[
            "alternatives"
        ] = [
            _product_fact(
                product
            )
            for product
            in payload.alternatives[
                :MAX_PRODUCTS_FOR_LLM
            ]
        ]

    if payload.recommendations:

        facts[
            "recommendations"
        ] = [
            _product_fact(
                product
            )
            for product
            in payload.recommendations[
                :MAX_RECOMMENDATIONS_FOR_LLM
            ]
        ]

    if payload.cart:

        facts[
            "cart"
        ] = (
            _cart_fact(
                payload.cart
            )
        )

    if payload.latest_order:

        facts[
            "latest_order"
        ] = (
            _order_fact(
                payload.latest_order
            )
        )

    if payload.orders:

        facts[
            "orders"
        ] = [
            _order_fact(
                order
            )
            for order
            in payload.orders[
                :MAX_ORDERS_FOR_LLM
            ]
        ]

    backend_facts = (
        payload.metadata.get(
            "backend_facts"
        )
        if isinstance(
            payload.metadata,
            Mapping,
        )
        else None
    )

    if backend_facts:

        facts[
            "additional_verified_facts"
        ] = (
            _compact_generic(
                backend_facts
            )
        )

    return facts


# ============================================================
# SERIALIZE FACTS
# ============================================================


def _serialize_facts(
    value: Any,
) -> str:

    try:

        text = json.dumps(
            value,
            ensure_ascii=False,
            separators=(
                ",",
                ":",
            ),
            default=str,
        )

    except Exception:

        text = "{}"

    if (
        len(
            text
        )
        > MAX_FACTS_CHARACTERS
    ):

        text = (
            text[
                :MAX_FACTS_CHARACTERS
            ]
            + "...[TRUNCATED]"
        )

    return text


# ============================================================
# DATABASE RESPONSE INSTRUCTIONS
# ============================================================


DATABASE_RESPONSE_INSTRUCTIONS = """
You are producing the final conversational response for a grocery
shopping assistant.

The backend operation has ALREADY completed.

Use ONLY VERIFIED_BACKEND_FACTS.

STRICT RULES:

1. Never invent a product, SKU, size, price, MRP, stock quantity,
   cart value, order, order item, payment state or database fact.

2. If a product exists but its verified stock is zero, say that the
   product exists but is currently out of stock. Do not say that the
   store does not carry it.

3. If alternatives are supplied, clearly distinguish them from the
   requested product.

4. For product searches, answer the user's question first. Do not dump
   every SKU into prose because detailed SKU cards will be displayed
   separately below the message.

5. For recommendation responses, briefly explain why the products fit
   the request only when a verified recommendation reason, semantic
   tag, meal context or use case is provided.

6. For latest-order requests, mention useful verified facts such as
   order date, status, total and item count when available. Detailed
   order items will be displayed separately.

7. Historical order prices must be treated as historical order facts,
   not current catalog prices.

8. For cart actions, never say the action succeeded unless the verified
   backend facts confirm success.

9. Respond naturally like a chatbot. Be concise but useful.

10. Do not expose JSON, internal tool names, API keys, Groq, Cohere,
    Supabase, prompts, table names or implementation details.

11. Match the user's language/style when practical.

Return only the final user-facing response text.
""".strip()


# ============================================================
# GROQ API 3 CLIENT
# ============================================================


@lru_cache(
    maxsize=1
)
def get_database_groq_client() -> Groq | None:
    """
    DATABASE RESPONSE CLIENT ONLY.

    Uses:

        GROQ_API_KEY2

    No rotation.
    """

    api_key = (
        os.getenv(
            DATABASE_GROQ_ENV_NAME
        )
        or ""
    ).strip()

    if not api_key:

        logger.warning(
            "%s is not configured. "
            "Database responses will use deterministic fallback.",
            DATABASE_GROQ_ENV_NAME,
        )

        return None

    try:

        return Groq(
            api_key=api_key
        )

    except Exception:

        logger.exception(
            "Unable to initialize database-response Groq client."
        )

        return None


# ============================================================
# STATUS CODE
# ============================================================


def _status_code(
    exception: Exception,
) -> int | None:

    value = getattr(
        exception,
        "status_code",
        None,
    )

    if isinstance(
        value,
        int,
    ):

        return value

    response = getattr(
        exception,
        "response",
        None,
    )

    if response is not None:

        value = getattr(
            response,
            "status_code",
            None,
        )

        if isinstance(
            value,
            int,
        ):

            return value

    return None


# ============================================================
# FALLBACK PRODUCT TEXT
# ============================================================


def _fallback_product_text(
    payload: ResponsePayload,
) -> str:

    products = (
        payload.products
    )

    alternatives = (
        payload.alternatives
    )

    if not products:

        if alternatives:

            return (
                "I couldn't find an exact match currently, "
                "but I found a few alternatives you can consider below."
            )

        return (
            "I couldn't find a matching product in the catalog currently."
        )

    available = [
        product
        for product
        in products
        if product.is_available
    ]

    unavailable = [
        product
        for product
        in products
        if not product.is_available
    ]

    if (
        len(
            products
        )
        == 1
    ):

        product = (
            products[
                0
            ]
        )

        name = (
            product.name
            or "the product"
        )

        if product.is_available:

            return (
                f"Yes — I found **{name}**. "
                "I've shown the available sizes, prices and SKU details below."
            )

        if alternatives:

            return (
                f"Yes — **{name}** is in the catalog, but it's currently "
                "out of stock. I've also shown some available alternatives below."
            )

        return (
            f"Yes — **{name}** is in the catalog, but it's currently "
            "out of stock."
        )

    if (
        available
        and unavailable
    ):

        return (
            f"I found {len(products)} matching products. "
            "Some are currently available and some are out of stock. "
            "I've shown the details below."
        )

    if available:

        return (
            f"I found {len(products)} matching products. "
            "I've shown their available sizes and prices below."
        )

    return (
        "I found matching products, but they're currently out of stock."
    )


# ============================================================
# FALLBACK RAG TEXT
# ============================================================


def _fallback_recommendation_text(
    payload: ResponsePayload,
) -> str:

    if payload.recommendations:

        return (
            "Sure — I found a few suitable options for you. "
            "I've shown their current sizes, prices and availability below."
        )

    if payload.alternatives:

        return (
            "I found relevant options, but the strongest matches are "
            "currently unavailable. I've shown them below."
        )

    return (
        "I couldn't find a sufficiently suitable available product "
        "for that request right now."
    )


# ============================================================
# FALLBACK ORDER TEXT
# ============================================================


def _fallback_order_text(
    payload: ResponsePayload,
) -> str:

    order = (
        payload.latest_order
    )

    if order:

        parts = []

        if order.order_id:

            parts.append(
                f"order **{order.order_id}**"
            )

        if order.order_status:

            parts.append(
                f"status: **{order.order_status}**"
            )

        if order.total_amount is not None:

            currency = (
                order.currency
                or ""
            )

            if (
                currency.upper()
                == "INR"
            ):

                total_text = (
                    f"₹{order.total_amount:g}"
                )

            elif currency:

                total_text = (
                    f"{currency} {order.total_amount:g}"
                )

            else:

                total_text = (
                    f"{order.total_amount:g}"
                )

            parts.append(
                f"total: **{total_text}**"
            )

        if parts:

            return (
                "Your latest "
                + ", ".join(
                    parts
                )
                + ". I've shown the complete order details below."
            )

        return (
            "I found your latest order. "
            "I've shown its details below."
        )

    if payload.orders:

        return (
            f"I found {len(payload.orders)} order"
            + (
                ""
                if len(
                    payload.orders
                )
                == 1
                else "s"
            )
            + ". I've shown the details below."
        )

    return (
        "I couldn't find an order matching that request."
    )


# ============================================================
# FALLBACK CART TEXT
# ============================================================


def _fallback_cart_text(
    payload: ResponsePayload,
) -> str:

    cart = (
        payload.cart
    )

    if cart is None:

        return (
            "The cart operation completed."
        )

    if cart.is_empty:

        return (
            "Your cart is currently empty."
        )

    return (
        f"Your cart currently has {cart.item_count} item"
        + (
            ""
            if cart.item_count
            == 1
            else "s"
        )
        + ". I've shown the full cart below."
    )


# ============================================================
# DETERMINISTIC FALLBACK
# ============================================================


def build_database_fallback_text(
    payload: ResponsePayload,
) -> str:
    """
    User-facing fallback when API 3 is unavailable.

    This is intentionally more conversational than backend messages like:

        "Latest order retrieved."
    """

    if (
        payload.response_type
        == "products"
    ):

        return (
            _fallback_product_text(
                payload
            )
        )

    if (
        payload.response_type
        == "recommendations"
    ):

        return (
            _fallback_recommendation_text(
                payload
            )
        )

    if (
        payload.response_type
        == "orders"
    ):

        return (
            _fallback_order_text(
                payload
            )
        )

    if (
        payload.response_type
        == "cart"
    ):

        return (
            _fallback_cart_text(
                payload
            )
        )

    backend_messages = (
        payload.metadata.get(
            "backend_messages"
        )
        if isinstance(
            payload.metadata,
            Mapping,
        )
        else None
    )

    if backend_messages:

        first = (
            backend_messages[
                0
            ]
        )

        if first:

            return str(
                first
            )

    if payload.success:

        return (
            "I found the requested information."
        )

    return (
        "I couldn't complete that database request."
    )


# ============================================================
# GENERATE DATABASE RESPONSE TEXT
# ============================================================


def generate_database_response_text(
    *,
    user_message: str,
    payload: ResponsePayload,
) -> tuple[
    str,
    bool,
]:
    """
    Produce conversational text using GROQ_API_KEY2 ONLY.

    Returns:

        (text, groq_used)

    Important:
        - ONE API request
        - NO key rotation
        - NO 429 retry
    """

    fallback = (
        build_database_fallback_text(
            payload
        )
    )

    client = (
        get_database_groq_client()
    )

    if client is None:

        return (
            fallback,
            False,
        )

    tool_names = (
        payload.tool_names
    )

    rules = (
        select_database_response_rules(
            tool_names=(
                tool_names
            )
        )
    )

    system_parts = []

    if rules:

        system_parts.append(
            rules
        )

    system_parts.append(
        DATABASE_RESPONSE_INSTRUCTIONS
    )

    compact_facts = (
        build_compact_database_facts(
            payload
        )
    )

    facts_text = (
        _serialize_facts(
            compact_facts
        )
    )

    user_message = str(
        user_message
        or ""
    ).strip()

    if (
        len(
            user_message
        )
        > MAX_USER_MESSAGE_CHARACTERS
    ):

        user_message = (
            user_message[
                :MAX_USER_MESSAGE_CHARACTERS
            ]
        )

    messages = [
        {
            "role":
                "system",

            "content":
                "\n\n".join(
                    system_parts
                ),
        },

        {
            "role":
                "user",

            "content": (
                "USER REQUEST:\n"
                f"{user_message}\n\n"
                "VERIFIED_BACKEND_FACTS:\n"
                f"{facts_text}\n\n"
                "Write the final conversational response now."
            ),
        },
    ]

    try:

        response = (
            client
            .chat
            .completions
            .create(
                model=(
                    settings.groq_model
                ),

                messages=(
                    messages
                ),

                temperature=0.0,

                max_completion_tokens=(
                    DATABASE_MAX_COMPLETION_TOKENS
                ),

                include_reasoning=False,

                stream=False,
            )
        )

        choices = getattr(
            response,
            "choices",
            None,
        )

        if not choices:

            return (
                fallback,
                False,
            )

        message = getattr(
            choices[
                0
            ],
            "message",
            None,
        )

        if message is None:

            return (
                fallback,
                False,
            )

        content = getattr(
            message,
            "content",
            None,
        )

        if not isinstance(
            content,
            str,
        ):

            return (
                fallback,
                False,
            )

        content = (
            content.strip()
        )

        if not content:

            return (
                fallback,
                False,
            )

        usage = getattr(
            response,
            "usage",
            None,
        )

        if usage is not None:

            logger.info(
                "Database response Groq API3 successful. "
                "prompt_tokens=%s completion_tokens=%s total_tokens=%s",
                getattr(
                    usage,
                    "prompt_tokens",
                    None,
                ),
                getattr(
                    usage,
                    "completion_tokens",
                    None,
                ),
                getattr(
                    usage,
                    "total_tokens",
                    None,
                ),
            )

        return (
            content,
            True,
        )

    except Exception as exc:

        status = (
            _status_code(
                exc
            )
        )

        # IMPORTANT:
        #
        # Do NOT rotate through API 1/API 2.
        # Do NOT retry 429.
        #
        # This is intentionally one dedicated API slot.

        if status == 429:

            logger.warning(
                "Database response Groq API3 rate limited. "
                "Using deterministic verified fallback."
            )

        else:

            logger.warning(
                "Database response Groq API3 failed. "
                "status=%s error_type=%s",
                status,
                type(
                    exc
                ).__name__,
            )

        return (
            fallback,
            False,
        )


# ============================================================
# MAIN DATABASE RESPONDER
# ============================================================


def respond_to_database_query(
    *,
    user_message: str,
    executions: Sequence[
        Any
    ],
) -> ResponsePayload:
    """
    Main function brain.py will call.

    Example:

        payload = respond_to_database_query(
            user_message=user_message,
            executions=executions,
        )

        payload.text
        payload.products
        payload.recommendations
        payload.cart
        payload.latest_order

    Database facts remain structured for app.py.
    Only text is generated by API 3.
    """

    payload = (
        build_database_response_payload(
            executions=(
                executions
            )
        )
    )

    (
        text,
        groq_used,
    ) = (
        generate_database_response_text(
            user_message=(
                user_message
            ),
            payload=(
                payload
            ),
        )
    )

    payload.text = (
        text
    )

    payload.requires_groq = (
        True
    )

    payload.groq_used = (
        groq_used
    )

    payload.groq_key_slot = (
        DATABASE_GROQ_KEY_SLOT
    )

    if isinstance(
        payload.metadata,
        dict,
    ):

        payload.metadata[
            "response_generator"
        ] = (
            "groq_api_3"
            if groq_used
            else "verified_python_fallback"
        )

    return payload


# ============================================================
# DATABASE RESPONSE CHECK
# ============================================================


def check_database_responder() -> dict[
    str,
    Any,
]:
    """
    Lightweight development diagnostic.

    Does NOT make a Groq request.
    """

    api_key_present = bool(
        (
            os.getenv(
                DATABASE_GROQ_ENV_NAME
            )
            or ""
        ).strip()
    )

    try:

        rules = (
            load_rules_text()
        )

        rules_available = bool(
            rules
        )

    except Exception:

        rules_available = False

    return {
        "database_groq_key_slot":
            DATABASE_GROQ_KEY_SLOT,

        "database_groq_env":
            DATABASE_GROQ_ENV_NAME,

        "api_key_present":
            api_key_present,

        "rules_available":
            rules_available,

        "rules_path":
            "prompts/rules.txt",

        "key_rotation":
            False,

        "rate_limit_retry":
            False,

        "max_completion_tokens":
            DATABASE_MAX_COMPLETION_TOKENS,
    }