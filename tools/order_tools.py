"""
tools/order_tools.py

Safe AI-callable ORDER INFORMATION tools for the Grocery Chatbot.

========================================================================
CURRENT PURPOSE
========================================================================

For the current phase, this file is READ-ONLY.

It allows the logged-in user to ask things such as:

    "Show my orders"

    "What was my latest order?"

    "Show order 8f12..."

    "How many orders have I placed?"

    "How much have I spent?"

    "How many delivered orders do I have?"

    "Show my pending orders"

    "What was the total of my last order?"

    "What products were in my previous order?"

    "Show my paid orders"

    "What's the status of order XYZ?"


It DOES NOT currently:

    - create an order
    - cancel an order
    - modify order status
    - modify payment status
    - issue refunds

Those actions can be added separately once checkout/order-creation
behavior is finalized.


========================================================================
SECURITY MODEL
========================================================================

CRITICAL:

NO tool in this module accepts:

    user_id

The authenticated user is determined only by:

    auth/session.py
        ↓
    authenticated Supabase JWT
        ↓
    database/users.py
        ↓
    RLS
        ↓
    current user's orders


So Cohere cannot generate:

    {
        "user_id": "someone-else"
    }

because the tool schema contains no user_id parameter.


========================================================================
ARCHITECTURE
========================================================================

User:

    "Show my last order"

        ↓

Cohere

        ↓

get_latest_order

        ↓

tools/order_tools.py

        ↓

database/users.py

        ↓

authenticated Supabase client

        ↓

orders + order_items

        ↓

Python calculates/normalizes

        ↓

verified structured result

        ↓

Groq

        ↓

natural response


========================================================================
DATABASE SOURCE OF TRUTH
========================================================================

The database is authoritative for:

    order ID
    order date
    order status
    payment status
    total
    order items
    quantities
    unit prices
    line totals

Groq must never invent these values.


========================================================================
IMPORTANT
========================================================================

Order history is PRIVATE USER DATA.

It must NEVER be retrieved through:

    get_admin_client()

for normal chatbot requests.

It must always go through the authenticated user's JWT/RLS path.
"""

from __future__ import annotations

import copy
import logging
import math

from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import (
    Any,
    Callable,
    Mapping,
    Sequence,
)


# ============================================================
# DATABASE IMPORTS
# ============================================================

from database.users import (
    UserDatabaseAuthenticationError,
    UserDatabaseError,
    UserDatabaseValidationError,
    get_order,
    get_order_count,
    list_orders,
)

from database.products import (
    ProductDatabaseError,
    get_sku_by_id,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# TOOL NAMES
# ============================================================

TOOL_GET_MY_ORDERS = (
    "get_my_orders"
)

TOOL_GET_ORDER_DETAILS = (
    "get_order_details"
)

TOOL_GET_LATEST_ORDER = (
    "get_latest_order"
)

TOOL_GET_ORDER_COUNT = (
    "get_order_count"
)

TOOL_GET_ORDER_HISTORY_SUMMARY = (
    "get_order_history_summary"
)

TOOL_GET_ORDER_STATUS = (
    "get_order_status"
)


# ============================================================
# LIMITS
# ============================================================

DEFAULT_ORDER_LIMIT = 10

MAX_ORDER_LIMIT = 50

MAX_ORDER_SCAN_ROWS = 1000

ORDER_SCAN_BATCH_SIZE = 100

MAX_IDENTIFIER_LENGTH = 200

MAX_STATUS_LENGTH = 100


# ============================================================
# CUSTOM TOOL ERRORS
# ============================================================


class OrderToolError(
    RuntimeError
):
    """
    Base exception for order tool failures.
    """

    pass


class OrderToolValidationError(
    OrderToolError
):
    """
    Raised when model-supplied arguments are invalid.
    """

    pass


class OrderToolNotFoundError(
    OrderToolError
):
    """
    Raised when brain.py requests a tool that is not registered here.
    """

    pass


# ============================================================
# TOOL RESULT BUILDERS
# ============================================================


def _success_result(
    *,
    tool: str,
    data: Any,
    message: str | None = None,
    metadata: Mapping[
        str,
        Any,
    ] | None = None,
) -> dict[str, Any]:
    """
    Standard success envelope.

    Every order tool returns the same high-level shape.

    Example:

        {
            "success": True,
            "source": "supabase",
            "domain": "orders",
            "tool": "get_my_orders",
            "data": {...},
            "metadata": {...}
        }
    """

    result: dict[
        str,
        Any,
    ] = {
        "success": True,
        "source": "supabase",
        "domain": "orders",
        "tool": tool,
        "data": data,
    }

    if message:

        result[
            "message"
        ] = message

    if metadata:

        result[
            "metadata"
        ] = dict(
            metadata
        )

    return result


def _error_result(
    *,
    tool: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """
    Standard safe error result.

    Raw Python/Supabase exceptions are never returned to Groq.
    """

    return {
        "success": False,
        "source": "supabase",
        "domain": "orders",
        "tool": tool,

        "error": {
            "code": code,
            "message": message,
        },

        "data": None,
    }


# ============================================================
# STRING VALIDATION
# ============================================================


def _optional_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str | None:
    """
    Normalize optional textual argument.
    """

    if value is None:

        return None

    if not isinstance(
        value,
        str,
    ):

        if isinstance(
            value,
            (
                int,
                float,
            ),
        ):

            value = str(
                value
            )

        else:

            raise OrderToolValidationError(
                f"{field_name} must be text."
            )

    value = value.strip()

    if not value:

        return None

    if len(
        value
    ) > max_length:

        raise OrderToolValidationError(
            f"{field_name} is too long."
        )

    return value


def _required_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Normalize required textual argument.
    """

    result = _optional_string(
        value,
        field_name=field_name,
        max_length=max_length,
    )

    if result is None:

        raise OrderToolValidationError(
            f"{field_name} is required."
        )

    return result


# ============================================================
# INTEGER VALIDATION
# ============================================================


def _integer(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """
    Validate integer tool arguments.
    """

    if value is None:

        return default

    if isinstance(
        value,
        bool,
    ):

        raise OrderToolValidationError(
            f"{field_name} must be an integer."
        )

    try:

        result = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise OrderToolValidationError(
            f"{field_name} must be an integer."
        ) from exc

    if result < minimum:

        raise OrderToolValidationError(
            f"{field_name} must be at least {minimum}."
        )

    if result > maximum:

        raise OrderToolValidationError(
            f"{field_name} cannot exceed {maximum}."
        )

    return result


# ============================================================
# BOOLEAN VALIDATION
# ============================================================


def _boolean(
    value: Any,
    *,
    field_name: str,
    default: bool,
) -> bool:
    """
    Normalize boolean model arguments safely.
    """

    if value is None:

        return default

    if isinstance(
        value,
        bool,
    ):

        return value

    if isinstance(
        value,
        int,
    ) and value in {
        0,
        1,
    }:

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
            "on",
        }:

            return True

        if normalized in {
            "false",
            "no",
            "0",
            "off",
        }:

            return False

    raise OrderToolValidationError(
        f"{field_name} must be true or false."
    )


# ============================================================
# TOOL ARGUMENT VALIDATION
# ============================================================


def _argument_mapping(
    arguments: Mapping[
        str,
        Any,
    ] | None,
) -> dict[str, Any]:
    """
    Validate parsed Cohere function arguments.
    """

    if arguments is None:

        return {}

    if not isinstance(
        arguments,
        Mapping,
    ):

        raise OrderToolValidationError(
            "Tool arguments must be an object."
        )

    return dict(
        arguments
    )


# ============================================================
# NUMERIC HELPERS
# ============================================================


def _safe_decimal(
    value: Any,
) -> Decimal | None:
    """
    Convert database numeric value into Decimal.

    Returns None when value is missing/unusable.

    We do not silently transform a missing order total into 0 because
    that could misrepresent historical commerce information.
    """

    if value is None:

        return None

    try:

        result = Decimal(
            str(value)
        )

        if not result.is_finite():

            return None

        return result

    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):

        return None


def _decimal_to_number(
    value: Decimal | None,
) -> float | None:
    """
    Convert Decimal to JSON-friendly numeric output.
    """

    if value is None:

        return None

    return float(
        value
    )


# ============================================================
# FLEXIBLE DATABASE FIELD ACCESS
# ============================================================


def _first_value(
    row: Mapping[
        str,
        Any,
    ],
    *field_names: str,
) -> Any:
    """
    Return first present/non-None field.

    This keeps the tools resilient if your order table uses:

        total_amount

    while another future schema calls it:

        grand_total

    We do NOT invent a value.

    We simply check known candidate field names.
    """

    for field_name in (
        field_names
    ):

        if (
            field_name in row
            and row[
                field_name
            ] is not None
        ):

            return row[
                field_name
            ]

    return None


# ============================================================
# ORDER TOTAL
# ============================================================


def _order_total(
    order: Mapping[
        str,
        Any,
    ],
) -> Decimal | None:
    """
    Extract authoritative stored order total.

    Priority:

        total_amount
        grand_total
        final_total
        total

    We deliberately do NOT rebuild a historical order total from current
    product prices.
    """

    value = _first_value(
        order,
        "total_amount",
        "grand_total",
        "final_total",
        "total",
    )

    return _safe_decimal(
        value
    )


# ============================================================
# ORDER STATUS
# ============================================================


def _order_status(
    order: Mapping[
        str,
        Any,
    ],
) -> str | None:
    """
    Extract stored order status.
    """

    value = _first_value(
        order,
        "order_status",
        "status",
    )

    if value is None:

        return None

    value = str(
        value
    ).strip()

    return value or None


# ============================================================
# PAYMENT STATUS
# ============================================================


def _payment_status(
    order: Mapping[
        str,
        Any,
    ],
) -> str | None:
    """
    Extract stored payment status.
    """

    value = _first_value(
        order,
        "payment_status",
    )

    if value is None:

        return None

    value = str(
        value
    ).strip()

    return value or None


# ============================================================
# ORDER DATE
# ============================================================


def _order_created_at(
    order: Mapping[
        str,
        Any,
    ],
) -> str | None:
    """
    Return stored order creation timestamp.
    """

    value = _first_value(
        order,
        "created_at",
        "ordered_at",
        "order_date",
    )

    if value is None:

        return None

    return str(
        value
    )


# ============================================================
# ORDER IDENTIFIER
# ============================================================


def _order_identifier(
    order: Mapping[
        str,
        Any,
    ],
) -> str | None:
    """
    Return database order identifier.
    """

    value = _first_value(
        order,
        "id",
        "order_id",
    )

    if value is None:

        return None

    return str(
        value
    )


# ============================================================
# ORDER SUMMARY
# ============================================================


def _normalize_order_header(
    order: Mapping[
        str,
        Any,
    ],
) -> dict[str, Any]:
    """
    Produce a safe compact order representation.

    The order table remains authoritative for historical totals/status.

    IMPORTANT:
    We do NOT invent a currency when it is missing.  A missing currency
    remains None so the response layer cannot accidentally state an
    unsupported currency.
    """

    total = _order_total(
        order
    )

    currency_value = _first_value(
        order,
        "currency",
    )

    return {
        "order_id": (
            _order_identifier(
                order
            )
        ),

        "created_at": (
            _order_created_at(
                order
            )
        ),

        "order_status": (
            _order_status(
                order
            )
        ),

        "payment_status": (
            _payment_status(
                order
            )
        ),

        "total_amount": (
            _decimal_to_number(
                total
            )
        ),

        "currency": (
            str(
                currency_value
            ).strip()
            if currency_value is not None
            and str(
                currency_value
            ).strip()
            else None
        ),
    }


# ============================================================
# ORDER ITEM NORMALIZATION
# ============================================================


def _catalog_display_metadata(
    sku_id: str | None,
    *,
    cache: dict[
        str,
        dict[str, Any] | None,
    ],
) -> dict[str, Any] | None:
    """
    Fetch CURRENT catalog metadata for display only.

    This helper may supply:
        product_name
        brand
        size
        pack_size
        image_url

    It MUST NOT supply historical order pricing.

    Historical:
        quantity
        unit_price
        line_total

    always remain sourced from order_items.
    """

    if not sku_id:

        return None

    if sku_id in cache:

        return cache[
            sku_id
        ]

    try:

        sku = get_sku_by_id(
            sku_id
        )

    except ProductDatabaseError:

        logger.warning(
            "Could not enrich order item from catalog. sku_id=%s",
            sku_id,
        )

        sku = None

    except Exception:

        logger.debug(
            "Unexpected catalog enrichment failure. sku_id=%s",
            sku_id,
            exc_info=True,
        )

        sku = None

    if isinstance(
        sku,
        Mapping,
    ):

        result = dict(
            sku
        )

    else:

        result = None

    cache[
        sku_id
    ] = result

    return result


def _normalize_order_item(
    item: Mapping[
        str,
        Any,
    ],
    *,
    order_currency: str | None = None,
    catalog_cache: dict[
        str,
        dict[str, Any] | None,
    ] | None = None,
) -> dict[str, Any]:
    """
    Normalize one HISTORICAL order line.

    Historical commercial facts come ONLY from order_items:

        quantity
        unit_price
        line_total

    Current catalog data may only enrich display information that is absent
    from the order snapshot:

        product name
        brand
        size / pack size
        image URL

    We never replace historical order prices with current product prices.
    """

    if catalog_cache is None:

        catalog_cache = {}

    # --------------------------------------------------------
    # SKU identity
    # --------------------------------------------------------

    sku_raw = _first_value(
        item,
        "sku_id",
        "product_id",
    )

    sku_id = (
        str(
            sku_raw
        )
        if sku_raw is not None
        else None
    )

    catalog_sku = (
        _catalog_display_metadata(
            sku_id,
            cache=catalog_cache,
        )
        if sku_id
        else None
    )

    # --------------------------------------------------------
    # Historical quantity
    # --------------------------------------------------------

    quantity_raw = item.get(
        "quantity"
    )

    try:

        quantity = int(
            quantity_raw
        )

    except (
        TypeError,
        ValueError,
    ):

        quantity = None

    # --------------------------------------------------------
    # Historical prices
    # --------------------------------------------------------

    unit_price = (
        _safe_decimal(
            _first_value(
                item,
                "unit_price",
                "price",
            )
        )
    )

    line_total = (
        _safe_decimal(
            _first_value(
                item,
                "line_total",
                "total_price",
                "subtotal",
            )
        )
    )

    calculated_line_total = False

    if (
        line_total is None
        and unit_price is not None
        and quantity is not None
        and quantity >= 0
    ):

        line_total = (
            unit_price
            * Decimal(
                quantity
            )
        )

        calculated_line_total = True

    # --------------------------------------------------------
    # Display metadata
    # --------------------------------------------------------

    product_name = (
        _first_value(
            item,
            "product_name",
            "name",
        )
    )

    if (
        product_name is None
        and catalog_sku
    ):

        product_name = (
            catalog_sku.get(
                "product_name"
            )
            or catalog_sku.get(
                "name"
            )
        )

    brand = (
        _first_value(
            item,
            "brand",
        )
    )

    if (
        brand is None
        and catalog_sku
    ):

        brand = (
            catalog_sku.get(
                "brand"
            )
        )

    size = (
        _first_value(
            item,
            "size",
            "variant",
            "pack_size",
        )
    )

    if (
        size is None
        and catalog_sku
    ):

        size = (
            catalog_sku.get(
                "size"
            )
            or catalog_sku.get(
                "pack_size"
            )
        )

    pack_size = (
        _first_value(
            item,
            "pack_size",
        )
    )

    if (
        pack_size is None
        and catalog_sku
    ):

        pack_size = (
            catalog_sku.get(
                "pack_size"
            )
        )

    image_url = (
        _first_value(
            item,
            "image_url",
            "product_image_url",
        )
    )

    catalog_display_enriched = False

    if (
        image_url is None
        and catalog_sku
        and catalog_sku.get(
            "image_url"
        )
    ):

        image_url = (
            catalog_sku.get(
                "image_url"
            )
        )

        catalog_display_enriched = True

    # If any missing identity/display field came from the catalog, mark it.
    if catalog_sku:

        if (
            _first_value(
                item,
                "product_name",
                "name",
            )
            is None
            and product_name is not None
        ):

            catalog_display_enriched = True

        if (
            _first_value(
                item,
                "brand",
            )
            is None
            and brand is not None
        ):

            catalog_display_enriched = True

        if (
            _first_value(
                item,
                "size",
                "variant",
                "pack_size",
            )
            is None
            and size is not None
        ):

            catalog_display_enriched = True

    item_currency = (
        _first_value(
            item,
            "currency",
        )
    )

    currency = (
        str(
            item_currency
        ).strip()
        if item_currency is not None
        and str(
            item_currency
        ).strip()
        else order_currency
    )

    normalized_line_total = (
        _decimal_to_number(
            line_total
        )
    )

    return {
        "order_item_id": (
            str(
                item.get(
                    "id"
                )
            )
            if item.get(
                "id"
            )
            is not None
            else None
        ),

        "sku_id": sku_id,

        "product_name": (
            str(
                product_name
            )
            if product_name is not None
            else None
        ),

        "brand": (
            str(
                brand
            )
            if brand is not None
            else None
        ),

        # Keep both canonical and compatibility names.
        "size": (
            str(
                size
            )
            if size is not None
            else None
        ),

        "variant": (
            str(
                size
            )
            if size is not None
            else None
        ),

        "pack_size": (
            str(
                pack_size
            )
            if pack_size is not None
            else None
        ),

        "quantity": quantity,

        "unit_price": (
            _decimal_to_number(
                unit_price
            )
        ),

        # Canonical field used by ResponseModels.
        "line_total": (
            normalized_line_total
        ),

        # Compatibility alias for existing callers.
        "total_price": (
            normalized_line_total
        ),

        "currency": currency,

        "image_url": (
            str(
                image_url
            )
            if image_url is not None
            else None
        ),

        "line_total_calculated_by_python": (
            calculated_line_total
        ),

        "catalog_display_enriched": (
            catalog_display_enriched
        ),
    }


# ============================================================
# NORMALIZE COMPLETE ORDER
# ============================================================


def _normalize_complete_order(
    order: Mapping[
        str,
        Any,
    ],
) -> dict[str, Any]:
    """
    Normalize an order plus historical line items.

    Output is intentionally shaped for core/response_models.py:

        order_id
        created_at
        order_status
        payment_status
        total_amount
        currency
        item_count
        items[]

    The line-item array preserves historical quantity/pricing while allowing
    non-financial display enrichment from the current catalog.
    """

    result = (
        _normalize_order_header(
            order
        )
    )

    items_raw = (
        order.get(
            "items"
        )
    )

    if items_raw is None:

        items_raw = (
            order.get(
                "order_items"
            )
        )

    normalized_items: list[
        dict[str, Any]
    ] = []

    catalog_cache: dict[
        str,
        dict[str, Any] | None,
    ] = {}

    order_currency = (
        result.get(
            "currency"
        )
    )

    if (
        isinstance(
            items_raw,
            Sequence,
        )
        and not isinstance(
            items_raw,
            (
                str,
                bytes,
            ),
        )
    ):

        for item in items_raw:

            if not isinstance(
                item,
                Mapping,
            ):

                continue

            normalized_items.append(
                _normalize_order_item(
                    item,
                    order_currency=(
                        order_currency
                    ),
                    catalog_cache=(
                        catalog_cache
                    ),
                )
            )

    result[
        "items"
    ] = normalized_items

    # Canonical field for ResponseModels.
    result[
        "item_count"
    ] = len(
        normalized_items
    )

    # Compatibility field retained for any older caller.
    result[
        "item_line_count"
    ] = len(
        normalized_items
    )

    result[
        "total_units"
    ] = sum(
        int(
            item[
                "quantity"
            ]
        )
        for item in normalized_items
        if isinstance(
            item.get(
                "quantity"
            ),
            int,
        )
        and item[
            "quantity"
        ] > 0
    )

    # --------------------------------------------------------
    # Preserve selected useful order-header details only when present.
    # --------------------------------------------------------

    optional_fields = (
        "payment_method",
        "delivery_address",
        "shipping_address",
        "delivery_fee",
        "discount_amount",
        "subtotal",
        "notes",
        "tracking_id",
    )

    for field_name in (
        optional_fields
    ):

        if (
            field_name in order
            and order[
                field_name
            ] is not None
        ):

            result[
                field_name
            ] = order[
                field_name
            ]

    return result


# ============================================================
# FETCH ORDER HISTORY FOR STATISTICS
# ============================================================


def _fetch_order_history(
    *,
    max_rows: int = (
        MAX_ORDER_SCAN_ROWS
    ),
) -> list[
    dict[str, Any]
]:
    """
    Fetch current user's order history using bounded pagination.

    Used for calculations such as:

        total orders
        total spent
        delivered-order count
        status distribution

    We intentionally set a hard maximum to prevent accidental unlimited
    reads into model/tool calls.
    """

    if max_rows < 1:

        return []

    rows: list[
        dict[str, Any]
    ] = []

    offset = 0

    while (
        len(rows)
        < max_rows
    ):

        remaining = (
            max_rows
            - len(rows)
        )

        batch_limit = min(
            ORDER_SCAN_BATCH_SIZE,
            remaining,
        )

        batch = list_orders(
            limit=batch_limit,
            offset=offset,
        )

        rows.extend(
            batch
        )

        if len(
            batch
        ) < batch_limit:

            break

        offset += (
            batch_limit
        )

    return rows


# ============================================================
# TOOL 1 — GET MY ORDERS
# ============================================================


def tool_get_my_orders(
    *,
    limit: Any = DEFAULT_ORDER_LIMIT,
    offset: Any = 0,
    order_status: Any = None,
    payment_status: Any = None,
) -> dict[str, Any]:
    """
    Retrieve current authenticated user's order history.

    No user_id argument exists.
    """

    tool_name = (
        TOOL_GET_MY_ORDERS
    )

    try:

        limit = _integer(
            limit,
            field_name="limit",
            default=(
                DEFAULT_ORDER_LIMIT
            ),
            minimum=1,
            maximum=(
                MAX_ORDER_LIMIT
            ),
        )

        offset = _integer(
            offset,
            field_name="offset",
            default=0,
            minimum=0,
            maximum=100_000,
        )

        order_status = (
            _optional_string(
                order_status,
                field_name=(
                    "order_status"
                ),
                max_length=(
                    MAX_STATUS_LENGTH
                ),
            )
        )

        payment_status = (
            _optional_string(
                payment_status,
                field_name=(
                    "payment_status"
                ),
                max_length=(
                    MAX_STATUS_LENGTH
                ),
            )
        )

        orders = list_orders(
            limit=limit,
            offset=offset,
            order_status=(
                order_status
            ),
            payment_status=(
                payment_status
            ),
        )

        normalized_orders = [
            _normalize_order_header(
                order
            )
            for order in orders
        ]

        return _success_result(
            tool=tool_name,

            data={
                "orders": (
                    normalized_orders
                )
            },

            message=(
                "Order history retrieved."
                if normalized_orders
                else "No matching orders were found."
            ),

            metadata={
                "returned_count": len(
                    normalized_orders
                ),

                "offset": offset,

                "limit": limit,

                "filters": {
                    "order_status": (
                        order_status
                    ),

                    "payment_status": (
                        payment_status
                    ),
                },
            },
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view your orders."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Order-history database query failed."
        )

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Your order history could not "
                "be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected order-history failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your order history could not be retrieved."
            ),
        )


# ============================================================
# TOOL 2 — ORDER DETAILS
# ============================================================


def tool_get_order_details(
    *,
    order_id: Any,
    include_items: Any = True,
) -> dict[str, Any]:
    """
    Retrieve ONE order belonging to current authenticated user.

    Even if another user's order ID is guessed, database/users.py checks:

        order.id
        AND
        order.user_id = authenticated user
    """

    tool_name = (
        TOOL_GET_ORDER_DETAILS
    )

    try:

        order_id = (
            _required_string(
                order_id,
                field_name="order_id",
                max_length=(
                    MAX_IDENTIFIER_LENGTH
                ),
            )
        )

        include_items = (
            _boolean(
                include_items,
                field_name=(
                    "include_items"
                ),
                default=True,
            )
        )

        order = get_order(
            order_id,
            include_items=(
                include_items
            ),
        )

        if order is None:

            # Do NOT say:
            #
            # "that order belongs to another user."
            #
            # We deliberately avoid leaking its existence.

            return _success_result(
                tool=tool_name,

                data={
                    "order": None,
                },

                message=(
                    "The requested order could not "
                    "be found in your account."
                ),

                metadata={
                    "found": False,
                },
            )

        if include_items:

            normalized = (
                _normalize_complete_order(
                    order
                )
            )

        else:

            normalized = (
                _normalize_order_header(
                    order
                )
            )

        return _success_result(
            tool=tool_name,

            data={
                "order": normalized,
            },

            message=(
                "Order details retrieved."
            ),

            metadata={
                "found": True,

                "include_items": (
                    include_items
                ),
            },
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view order details."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Order-details query failed."
        )

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Order information could not "
                "be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected order-details failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Order information could not be retrieved."
            ),
        )


# ============================================================
# TOOL 3 — LATEST ORDER
# ============================================================


def tool_get_latest_order(
    *,
    include_items: Any = True,
) -> dict[str, Any]:
    """
    Retrieve the current user's most recent order.

    Broad requests such as:

        "what was my last order?"
        "show my latest order"
        "what did I order last time?"

    should include historical line items by default.

    list_orders() is expected to return newest orders first.
    """

    tool_name = (
        TOOL_GET_LATEST_ORDER
    )

    try:

        include_items = (
            _boolean(
                include_items,
                field_name=(
                    "include_items"
                ),
                default=True,
            )
        )

        orders = list_orders(
            limit=1,
            offset=0,
        )

        if not orders:

            return _success_result(
                tool=tool_name,

                data={
                    "order": None,
                    "found": False,
                },

                message=(
                    "You do not have any orders yet."
                ),

                metadata={
                    "found": False,
                    "include_items": include_items,
                    "item_count": 0,
                },
            )

        latest_header = (
            orders[
                0
            ]
        )

        order_id = (
            _order_identifier(
                latest_header
            )
        )

        normalized: dict[
            str,
            Any,
        ]

        # ----------------------------------------------------
        # Broad latest-order requests normally include items.
        # ----------------------------------------------------

        if (
            include_items
            and order_id
        ):

            complete_order = (
                get_order(
                    order_id,
                    include_items=True,
                )
            )

            if complete_order:

                normalized = (
                    _normalize_complete_order(
                        complete_order
                    )
                )

            else:

                # If detailed re-fetch fails without raising, preserve the
                # verified order header rather than fabricating items.
                normalized = (
                    _normalize_order_header(
                        latest_header
                    )
                )

                normalized[
                    "items"
                ] = []

                normalized[
                    "item_count"
                ] = 0

                normalized[
                    "item_line_count"
                ] = 0

                normalized[
                    "total_units"
                ] = 0

        else:

            normalized = (
                _normalize_order_header(
                    latest_header
                )
            )

        item_count = (
            int(
                normalized.get(
                    "item_count"
                )
                or 0
            )
            if include_items
            else None
        )

        if (
            include_items
            and item_count
            and item_count > 0
        ):

            message = (
                "Latest order and historical line items retrieved."
            )

        elif include_items:

            message = (
                "Latest order retrieved. No line items were available "
                "in the returned order record."
            )

        else:

            message = (
                "Latest order summary retrieved."
            )

        return _success_result(
            tool=tool_name,

            data={
                "order": normalized,

                # Explicit status helps response layers distinguish
                # "found but no items" from "no order".
                "found": True,

                "include_items": include_items,
            },

            message=message,

            metadata={
                "found": True,

                "include_items":
                    include_items,

                "item_count":
                    item_count,
            },
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view your latest order."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Latest-order database query failed."
        )

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Your latest order could not "
                "be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected latest-order failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your latest order could not be retrieved."
            ),
        )


# ============================================================
# TOOL 4 — ORDER COUNT
# ============================================================


def tool_get_order_count() -> dict[
    str,
    Any
]:
    """
    Return current authenticated user's order count.

    Python/database calculates it.

    Groq does not count order rows itself.
    """

    tool_name = (
        TOOL_GET_ORDER_COUNT
    )

    try:

        count = (
            get_order_count()
        )

        return _success_result(
            tool=tool_name,

            data={
                "order_count": count,
            },

            message=(
                "Order count retrieved."
            ),

            metadata={
                "calculated_by": (
                    "python_database"
                ),
            },
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view your order count."
            ),
        )

    except UserDatabaseError:

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Your order count could not "
                "be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Order-count failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your order count could not be retrieved."
            ),
        )


# ============================================================
# TOOL 5 — ORDER STATUS
# ============================================================


def tool_get_order_status(
    *,
    order_id: Any,
) -> dict[str, Any]:
    """
    Return status/payment state for one user's own order.

    More efficient and cleaner than returning all line-item data when
    user only asks:

        "Where is my order?"
        "Is order ABC paid?"
        "What's its status?"
    """

    tool_name = (
        TOOL_GET_ORDER_STATUS
    )

    try:

        order_id = (
            _required_string(
                order_id,
                field_name="order_id",
                max_length=(
                    MAX_IDENTIFIER_LENGTH
                ),
            )
        )

        order = get_order(
            order_id,
            include_items=False,
        )

        if order is None:

            return _success_result(
                tool=tool_name,

                data={
                    "order": None,
                },

                message=(
                    "The requested order could not "
                    "be found in your account."
                ),

                metadata={
                    "found": False,
                },
            )

        result = {
            "order_id": (
                _order_identifier(
                    order
                )
            ),

            "created_at": (
                _order_created_at(
                    order
                )
            ),

            "order_status": (
                _order_status(
                    order
                )
            ),

            "payment_status": (
                _payment_status(
                    order
                )
            ),

            "total_amount": (
                _decimal_to_number(
                    _order_total(
                        order
                    )
                )
            ),
        }

        return _success_result(
            tool=tool_name,

            data={
                "order": result,
            },

            message=(
                "Order status retrieved."
            ),

            metadata={
                "found": True,
            },
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view order status."
            ),
        )

    except UserDatabaseError:

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Order status could not be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected order-status failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Order status could not be retrieved."
            ),
        )


# ============================================================
# TOOL 6 — ORDER HISTORY SUMMARY
# ============================================================


def tool_get_order_history_summary(
    *,
    max_orders: Any = 500,
) -> dict[str, Any]:
    """
    Calculate useful facts about user's historical orders in Python.

    Supports questions such as:

        How many orders have I placed?
        How much have I spent?
        How many delivered orders do I have?
        What was my most expensive order?
        What was my cheapest order?
        How many pending orders do I have?

    IMPORTANT:

    Database order totals are used.

    We never reconstruct historical totals from CURRENT product prices.
    """

    tool_name = (
        TOOL_GET_ORDER_HISTORY_SUMMARY
    )

    try:

        max_orders = _integer(
            max_orders,
            field_name="max_orders",
            default=500,
            minimum=1,
            maximum=(
                MAX_ORDER_SCAN_ROWS
            ),
        )

        orders = (
            _fetch_order_history(
                max_rows=max_orders
            )
        )

        if not orders:

            return _success_result(
                tool=tool_name,

                data={
                    "summary": {
                        "order_count": 0,
                        "total_spent": 0.0,
                        "average_order_value": None,
                        "minimum_order_value": None,
                        "maximum_order_value": None,
                        "order_status_counts": {},
                        "payment_status_counts": {},
                        "latest_order": None,
                        "most_expensive_order": None,
                        "least_expensive_order": None,
                    }
                },

                message=(
                    "You do not have any orders yet."
                ),
            )

        # ----------------------------------------------------
        # Historical totals
        # ----------------------------------------------------

        orders_with_totals: list[
            tuple[
                Mapping[
                    str,
                    Any
                ],
                Decimal,
            ]
        ] = []

        for order in orders:

            total = _order_total(
                order
            )

            if total is None:

                continue

            orders_with_totals.append(
                (
                    order,
                    total,
                )
            )

        total_spent: (
            Decimal
            | None
        )

        if orders_with_totals:

            total_spent = sum(
                (
                    total
                    for _, total
                    in orders_with_totals
                ),
                Decimal("0"),
            )

            average_order_value = (
                total_spent
                / Decimal(
                    len(
                        orders_with_totals
                    )
                )
            )

            minimum_pair = min(
                orders_with_totals,
                key=lambda pair:
                    pair[1],
            )

            maximum_pair = max(
                orders_with_totals,
                key=lambda pair:
                    pair[1],
            )

        else:

            total_spent = None

            average_order_value = None

            minimum_pair = None

            maximum_pair = None

        # ----------------------------------------------------
        # Status counts
        # ----------------------------------------------------

        order_status_counter: Counter[
            str
        ] = Counter()

        payment_status_counter: Counter[
            str
        ] = Counter()

        for order in orders:

            order_status = (
                _order_status(
                    order
                )
            )

            if order_status:

                order_status_counter[
                    order_status
                ] += 1

            payment_status = (
                _payment_status(
                    order
                )
            )

            if payment_status:

                payment_status_counter[
                    payment_status
                ] += 1

        # list_orders() returns newest first.

        latest_order = (
            _normalize_order_header(
                orders[0]
            )
            if orders
            else None
        )

        minimum_order = (
            _normalize_order_header(
                minimum_pair[0]
            )
            if minimum_pair
            else None
        )

        maximum_order = (
            _normalize_order_header(
                maximum_pair[0]
            )
            if maximum_pair
            else None
        )

        summary = {
            "order_count": len(
                orders
            ),

            # Number of orders whose stored total was actually available.
            "orders_with_known_total": len(
                orders_with_totals
            ),

            "total_spent": (
                _decimal_to_number(
                    total_spent
                )
            ),

            "average_order_value": (
                round(
                    float(
                        average_order_value
                    ),
                    2,
                )
                if average_order_value
                is not None
                else None
            ),

            "minimum_order_value": (
                _decimal_to_number(
                    minimum_pair[
                        1
                    ]
                )
                if minimum_pair
                else None
            ),

            "maximum_order_value": (
                _decimal_to_number(
                    maximum_pair[
                        1
                    ]
                )
                if maximum_pair
                else None
            ),

            "order_status_counts": (
                dict(
                    order_status_counter
                )
            ),

            "payment_status_counts": (
                dict(
                    payment_status_counter
                )
            ),

            "latest_order": (
                latest_order
            ),

            "most_expensive_order": (
                maximum_order
            ),

            "least_expensive_order": (
                minimum_order
            ),
        }

        return _success_result(
            tool=tool_name,

            data={
                "summary": summary,
            },

            message=(
                "Order history summary calculated."
            ),

            metadata={
                "calculated_by": "python",

                "source_rows_scanned": len(
                    orders
                ),

                "scan_limit": max_orders,

                "history_may_be_truncated": (
                    len(orders)
                    >= max_orders
                ),
            },
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view your order history."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Order-history summary database failure."
        )

        return _error_result(
            tool=tool_name,
            code="ORDER_DATABASE_UNAVAILABLE",
            message=(
                "Your order history summary could "
                "not be calculated right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected order-history summary failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your order history summary could "
                "not be calculated."
            ),
        )


# ============================================================
# COHERE ORDER TOOL SCHEMAS
# ============================================================

"""
These are the only ORDER capabilities Cohere sees right now.

No write operations exist here.

There is no:

    create_order
    cancel_order
    delete_order
    modify_status

at this stage.
"""


ORDER_TOOL_SCHEMAS: list[
    dict[str, Any]
] = [

    # ========================================================
    # GET MY ORDERS
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_MY_ORDERS,

            "description": (
                "Retrieve the currently authenticated user's existing "
                "orders from Supabase. Use this for requests such as "
                "'show my orders', 'show delivered orders', "
                "'show pending orders', or 'show paid orders'. "
                "Never ask for or supply a user ID because authentication "
                "determines the user automatically."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "limit": {
                        "type": "integer",

                        "minimum": 1,

                        "maximum": (
                            MAX_ORDER_LIMIT
                        ),

                        "description": (
                            "Maximum number of orders to return."
                        ),
                    },

                    "offset": {
                        "type": "integer",

                        "minimum": 0,

                        "description": (
                            "Pagination offset."
                        ),
                    },

                    "order_status": {
                        "type": "string",

                        "description": (
                            "Optional exact stored order status filter "
                            "when requested by the user, for example "
                            "delivered, pending, processing or cancelled. "
                            "Do not invent a status value if it is unknown."
                        ),
                    },

                    "payment_status": {
                        "type": "string",

                        "description": (
                            "Optional payment status filter such as paid "
                            "or pending when explicitly requested."
                        ),
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # ORDER DETAILS
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_ORDER_DETAILS,

            "description": (
                "Retrieve full details and historical line items for one "
                "specific order belonging to the logged-in user. Use when "
                "the user provides or refers to a known order ID, or asks "
                "what products were inside a specific order."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "order_id": {
                        "type": "string",

                        "description": (
                            "Order ID belonging to the current user."
                        ),
                    },

                    "include_items": {
                        "type": "boolean",

                        "description": (
                            "Defaults to true. Include historical line items "
                            "unless the user explicitly needs only the order "
                            "header/status."
                        ),
                    },
                },

                "required": [
                    "order_id",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # LATEST ORDER
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_LATEST_ORDER,

            "description": (
                "Retrieve the logged-in user's most recent order. "
                "Use for requests such as 'my last order', 'latest order', "
                "'what did I order last time?', 'what was my previous "
                "order total?', or 'show my previous purchase'. "
                "For broad requests like 'what was my last order?', omit "
                "include_items or set it to true so the response includes "
                "historical line items. Set include_items=false only when "
                "the user explicitly asks only for status/date/total."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "include_items": {
                        "type": "boolean",

                        "description": (
                            "Defaults to true. Keep true or omit for broad "
                            "latest-order requests so product line items are "
                            "returned. Use false only when the user explicitly "
                            "asks only for status, date or total."
                        ),
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # ORDER COUNT
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_ORDER_COUNT,

            "description": (
                "Return the total number of orders belonging to the "
                "currently authenticated user."
            ),

            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # ORDER STATUS
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_ORDER_STATUS,

            "description": (
                "Retrieve the current stored order status and payment "
                "status for one specific order belonging to the logged-in "
                "user. Use when the user asks where an order is, whether "
                "it is delivered, pending, cancelled or paid."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "order_id": {
                        "type": "string",
                    },
                },

                "required": [
                    "order_id",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # HISTORY SUMMARY
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_ORDER_HISTORY_SUMMARY,

            "description": (
                "Calculate Python-backed statistics from the current "
                "user's order history. Use for questions such as total "
                "spending, average order value, cheapest order, most "
                "expensive order, number of delivered orders, or payment/"
                "order status counts. Never calculate these values using "
                "the language model."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "max_orders": {
                        "type": "integer",

                        "minimum": 1,

                        "maximum": (
                            MAX_ORDER_SCAN_ROWS
                        ),

                        "description": (
                            "Maximum number of historical orders Python "
                            "may scan. Usually omit this and use the "
                            "default."
                        ),
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },
]


# ============================================================
# SAFE ORDER TOOL REGISTRY
# ============================================================


OrderToolFunction = Callable[
    ...,
    dict[str, Any]
]


ORDER_TOOL_FUNCTIONS: dict[
    str,
    OrderToolFunction,
] = {

    TOOL_GET_MY_ORDERS:
        tool_get_my_orders,

    TOOL_GET_ORDER_DETAILS:
        tool_get_order_details,

    TOOL_GET_LATEST_ORDER:
        tool_get_latest_order,

    TOOL_GET_ORDER_COUNT:
        tool_get_order_count,

    TOOL_GET_ORDER_STATUS:
        tool_get_order_status,

    TOOL_GET_ORDER_HISTORY_SUMMARY:
        tool_get_order_history_summary,
}


# ============================================================
# TOOL SCHEMAS
# ============================================================


def get_order_tool_schemas() -> list[
    dict[str, Any]
]:
    """
    Return defensive copy of Cohere order schemas.

    brain.py can safely combine this with:

        product tools
        cart tools
        RAG tools
    """

    return copy.deepcopy(
        ORDER_TOOL_SCHEMAS
    )


# ============================================================
# TOOL NAMES
# ============================================================


def get_order_tool_names() -> set[
    str
]:
    """
    Return allow-listed order tool names.
    """

    return set(
        ORDER_TOOL_FUNCTIONS.keys()
    )


# ============================================================
# IS ORDER TOOL
# ============================================================


def is_order_tool(
    tool_name: str,
) -> bool:
    """
    Return whether tool name is a registered order tool.
    """

    if not isinstance(
        tool_name,
        str,
    ):

        return False

    return (
        tool_name.strip()
        in ORDER_TOOL_FUNCTIONS
    )


# ============================================================
# EXECUTE ORDER TOOL
# ============================================================


def execute_order_tool(
    *,
    tool_name: str,
    arguments: Mapping[
        str,
        Any,
    ] | None = None,
) -> dict[str, Any]:
    """
    Execute ONE approved order tool.

    SECURITY
    --------

    We explicitly use an allow-list.

    We NEVER use:

        eval(tool_name)

        exec(...)

        globals()[tool_name]

        arbitrary module imports

    Cohere can only invoke functions present in ORDER_TOOL_FUNCTIONS.
    """

    if not isinstance(
        tool_name,
        str,
    ):

        raise OrderToolNotFoundError(
            "Order tool name must be text."
        )

    tool_name = (
        tool_name.strip()
    )

    if not tool_name:

        raise OrderToolNotFoundError(
            "Order tool name cannot be empty."
        )

    function = (
        ORDER_TOOL_FUNCTIONS.get(
            tool_name
        )
    )

    if function is None:

        logger.warning(
            "Rejected unknown order tool: %s",
            tool_name,
        )

        raise OrderToolNotFoundError(
            f"Unsupported order tool: "
            f"{tool_name}"
        )

    try:

        arguments_dict = (
            _argument_mapping(
                arguments
            )
        )

    except OrderToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    try:

        logger.debug(
            "Executing order tool. "
            "tool=%s argument_keys=%s",
            tool_name,
            sorted(
                arguments_dict.keys()
            ),
        )

        return function(
            **arguments_dict
        )

    except TypeError:

        # Usually model passed unexpected/missing arguments.

        logger.warning(
            "Order tool argument mismatch. "
            "tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=(
                "The selected order operation received "
                "unsupported or incomplete arguments."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected order tool failure. "
            "tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "The order operation could not be completed."
            ),
        )