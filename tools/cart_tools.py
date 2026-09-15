"""
tools/cart_tools.py

Safe AI-callable cart tools for the Grocery Chatbot.

========================================================================
SUPPORTED OPERATIONS
========================================================================

1. View current cart
2. Get cart summary
3. Add exact SKU to cart
4. Update exact SKU quantity
5. Remove exact SKU
6. Clear cart

========================================================================
IMPORTANT PRODUCT / SKU RULE
========================================================================

The cart stores:

    cart_items.product_id = products.id

In the current database, products.id identifies an SKU/variant.

Example:

Logical product:
    Amul Taaza Milk

Variants:
    SKU 101 -> 500 ml
    SKU 102 -> 1 L
    SKU 103 -> 2 L

Cart must store:

    SKU 102

not:

    "Amul Taaza Milk"


Therefore add_to_cart() requires an exact sku_id.

If user says:

    "Add Amul milk"

and several variants exist:

    Cohere
        ↓
    search_products
        ↓
    multiple variants
        ↓
    ask clarification

Then:

    "1 litre"

        ↓
    resolve_product_variant
        ↓
    exact sku_id
        ↓
    add_to_cart


========================================================================
USER SECURITY
========================================================================

NO CART TOOL ACCEPTS:

    user_id

The current user comes from:

    authenticated Supabase session
        ↓
    database/users.py
        ↓
    RLS


========================================================================
BUSINESS LOGIC
========================================================================

database/users.py performs low-level private database reads/writes.

This file performs business rules:

    - verify SKU exists
    - verify stock
    - verify requested quantity
    - calculate final quantity
    - calculate subtotal
    - calculate cart total
    - prevent quantity > current stock


========================================================================
PRICE SOURCE
========================================================================

Cart rows do NOT determine current product price.

For every cart read:

    cart_items
        +
    live product SKU
        ↓
    current verified price

Python calculates:

    subtotal = price * quantity

    total = sum(subtotals)

Groq never calculates trusted commerce totals.
"""

from __future__ import annotations

import copy
import logging
import math

from decimal import Decimal, InvalidOperation
from typing import (
    Any,
    Callable,
    Mapping,
)


# ============================================================
# PRODUCT DATABASE
# ============================================================

from database.products import (
    ProductDatabaseError,
    get_sku_by_id,
)


# ============================================================
# PRIVATE USER DATABASE
# ============================================================

from database.users import (
    UserDatabaseAuthenticationError,
    UserDatabaseConflictError,
    UserDatabaseError,
    UserDatabaseNotFoundError,
    UserDatabaseValidationError,
    clear_cart,
    create_cart_item,
    delete_cart_item,
    get_cart_item_by_sku,
    get_cart_items,
    update_cart_item_quantity,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# TOOL NAMES
# ============================================================

TOOL_GET_CART = "get_cart"

TOOL_GET_CART_SUMMARY = (
    "get_cart_summary"
)

TOOL_ADD_TO_CART = (
    "add_to_cart"
)

TOOL_UPDATE_CART_QUANTITY = (
    "update_cart_quantity"
)

TOOL_REMOVE_FROM_CART = (
    "remove_from_cart"
)

TOOL_CLEAR_CART = (
    "clear_cart"
)


# ============================================================
# LIMITS
# ============================================================

MIN_CART_QUANTITY = 1

MAX_CART_QUANTITY = 999

MAX_SKU_ID_LENGTH = 200


# ============================================================
# EXCEPTIONS
# ============================================================


class CartToolError(
    RuntimeError
):
    """
    Base cart tool error.
    """

    pass


class CartToolValidationError(
    CartToolError
):
    """
    Invalid model/tool argument.
    """

    pass


class CartToolNotFoundError(
    CartToolError
):
    """
    Unknown cart tool.
    """

    pass


# ============================================================
# STANDARD TOOL RESULT
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
    Build standard successful cart-tool response.
    """

    result: dict[
        str,
        Any,
    ] = {
        "success": True,
        "source": "supabase",
        "domain": "cart",
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
    data: Any = None,
) -> dict[str, Any]:
    """
    Build safe cart-tool failure.

    Raw Supabase/Python exception messages are intentionally excluded.
    """

    return {
        "success": False,
        "source": "supabase",
        "domain": "cart",
        "tool": tool,

        "error": {
            "code": code,
            "message": message,
        },

        "data": data,
    }


# ============================================================
# ARGUMENT HELPERS
# ============================================================


def _required_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:
    """
    Validate required textual argument.
    """

    if value is None:

        raise CartToolValidationError(
            f"{field_name} is required."
        )

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

            raise CartToolValidationError(
                f"{field_name} must be text."
            )

    value = value.strip()

    if not value:

        raise CartToolValidationError(
            f"{field_name} cannot be empty."
        )

    if len(
        value
    ) > max_length:

        raise CartToolValidationError(
            f"{field_name} is too long."
        )

    return value


def _quantity(
    value: Any,
    *,
    default: int | None = None,
) -> int:
    """
    Validate cart quantity.

    We do not allow zero here.

    Removing a product should use:

        remove_from_cart

    rather than:

        update quantity to zero
    """

    if value is None:

        if default is None:

            raise CartToolValidationError(
                "quantity is required."
            )

        return default

    if isinstance(
        value,
        bool,
    ):

        raise CartToolValidationError(
            "quantity must be an integer."
        )

    try:

        result = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise CartToolValidationError(
            "quantity must be an integer."
        ) from exc

    if result < (
        MIN_CART_QUANTITY
    ):

        raise CartToolValidationError(
            "quantity must be at least 1."
        )

    if result > (
        MAX_CART_QUANTITY
    ):

        raise CartToolValidationError(
            f"quantity cannot exceed "
            f"{MAX_CART_QUANTITY}."
        )

    return result


def _arguments_mapping(
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

        raise CartToolValidationError(
            "Tool arguments must be an object."
        )

    return dict(
        arguments
    )


# ============================================================
# MONEY HELPERS
# ============================================================


def _money(
    value: Any,
) -> Decimal | None:
    """
    Convert verified numeric database value to Decimal.

    Returns None if value cannot be safely interpreted.
    """

    if value is None:

        return None

    try:

        result = Decimal(
            str(value)
        )

        if not result.is_finite():

            return None

        if result < 0:

            return None

        return result

    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):

        return None


def _money_output(
    value: Decimal | None,
) -> float | None:
    """
    Convert Decimal into JSON-safe number.
    """

    if value is None:

        return None

    return round(
        float(value),
        2,
    )


# ============================================================
# SAFE INT
# ============================================================


def _safe_int(
    value: Any,
    *,
    default: int = 0,
) -> int:
    """
    Convert value into integer safely.
    """

    try:

        value = int(
            value
        )

        return value

    except (
        TypeError,
        ValueError,
    ):

        return default


# ============================================================
# VERIFIED SKU
# ============================================================


def _get_verified_sku(
    sku_id: str,
) -> dict[str, Any]:
    """
    Retrieve current SKU information from Supabase.

    Raises business-safe errors when SKU cannot be purchased.
    """

    try:

        sku = (
            get_sku_by_id(
                sku_id
            )
        )

    except ProductDatabaseError as exc:

        raise CartToolError(
            "Product catalog is unavailable."
        ) from exc

    if sku is None:

        raise CartToolValidationError(
            "The selected product variant no longer exists."
        )

    return sku


# ============================================================
# VALIDATE SKU FOR PURCHASE
# ============================================================


def _validate_sku_quantity(
    *,
    sku: Mapping[
        str,
        Any
    ],
    requested_quantity: int,
) -> None:
    """
    Ensure requested quantity can currently be purchased.

    Database/product layer remains authoritative for stock.
    """

    in_stock = bool(
        sku.get(
            "in_stock"
        )
    )

    stock = _safe_int(
        sku.get(
            "stock"
        ),
        default=0,
    )

    if not in_stock:

        raise CartToolValidationError(
            "The selected product variant is currently out of stock."
        )

    if stock <= 0:

        raise CartToolValidationError(
            "The selected product variant is currently out of stock."
        )

    if (
        requested_quantity
        > stock
    ):

        size = (
            sku.get(
                "size"
            )
            or "selected variant"
        )

        raise CartToolValidationError(
            f"Only {stock} unit"
            f"{'' if stock == 1 else 's'} "
            f"of the {size} variant are currently available."
        )


# ============================================================
# BUILD VERIFIED CART ITEM
# ============================================================


def _build_cart_item(
    cart_row: Mapping[
        str,
        Any,
    ],
) -> dict[str, Any]:
    """
    Combine private cart row with live SKU data.

    If a product has been removed/deactivated after it was added,
    preserve the cart row as an unavailable item rather than silently
    inventing current product information.
    """

    sku_id_raw = (
        cart_row.get(
            "product_id"
        )
    )

    sku_id = (
        str(
            sku_id_raw
        )
        if sku_id_raw
        is not None
        else None
    )

    quantity = max(
        _safe_int(
            cart_row.get(
                "quantity"
            ),
            default=0,
        ),
        0,
    )

    if not sku_id:

        return {
            "cart_item_id": (
                cart_row.get(
                    "id"
                )
            ),

            "sku_id": None,

            "quantity": quantity,

            "catalog_status": (
                "invalid_reference"
            ),

            "available": False,

            "price": None,

            "subtotal": None,
        }

    # --------------------------------------------------------
    # Current SKU
    # --------------------------------------------------------

    try:

        sku = (
            get_sku_by_id(
                sku_id
            )
        )

    except ProductDatabaseError:

        # Catalog failure means we cannot safely calculate price.
        return {
            "cart_item_id": (
                cart_row.get(
                    "id"
                )
            ),

            "sku_id": sku_id,

            "quantity": quantity,

            "catalog_status": (
                "catalog_unavailable"
            ),

            "available": False,

            "price": None,

            "subtotal": None,
        }

    # --------------------------------------------------------
    # Deleted/deactivated SKU
    # --------------------------------------------------------

    if sku is None:

        return {
            "cart_item_id": (
                cart_row.get(
                    "id"
                )
            ),

            "sku_id": sku_id,

            "quantity": quantity,

            "catalog_status": (
                "sku_missing"
            ),

            "available": False,

            "price": None,

            "subtotal": None,
        }

    price = _money(
        sku.get(
            "price"
        )
    )

    subtotal: Decimal | None = None

    if (
        price is not None
        and quantity > 0
    ):

        subtotal = (
            price
            * Decimal(
                quantity
            )
        )

    stock = max(
        _safe_int(
            sku.get(
                "stock"
            ),
            default=0,
        ),
        0,
    )

    in_stock = bool(
        sku.get(
            "in_stock"
        )
    )

    # Item may still exist in cart but requested cart quantity may now
    # exceed available stock.

    quantity_available = (
        in_stock
        and stock >= quantity
    )

    return {
        "cart_item_id": (
            str(
                cart_row.get(
                    "id"
                )
            )
            if cart_row.get(
                "id"
            )
            is not None
            else None
        ),

        "sku_id": sku_id,

        "product_key": (
            sku.get(
                "product_key"
            )
        ),

        "name": (
            sku.get(
                "product_name"
            )
        ),

        "brand": (
            sku.get(
                "brand"
            )
        ),

        "size": (
            sku.get(
                "size"
            )
        ),

        "quantity": quantity,

        "price": (
            _money_output(
                price
            )
        ),

        "mrp": (
            sku.get(
                "mrp"
            )
        ),

        "subtotal": (
            _money_output(
                subtotal
            )
        ),

        "currency": (
            sku.get(
                "currency"
            )
            or "INR"
        ),

        "stock": stock,

        "in_stock": (
            in_stock
        ),

        "quantity_available": (
            quantity_available
        ),

        "available": (
            in_stock
        ),

        "image_url": (
            sku.get(
                "image_url"
            )
        ),

        "catalog_status": "active",
    }


# ============================================================
# BUILD COMPLETE VERIFIED CART
# ============================================================


def _build_verified_cart() -> dict[
    str,
    Any
]:
    """
    Build current user's cart using:

        private cart rows
        +
        current live SKU information

    All totals are calculated here in Python.
    """

    rows = (
        get_cart_items()
    )

    items = [
        _build_cart_item(
            row
        )
        for row in rows
    ]

    total_units = sum(
        max(
            _safe_int(
                item.get(
                    "quantity"
                )
            ),
            0,
        )
        for item in items
    )

    # --------------------------------------------------------
    # Only verified monetary rows participate in cart total.
    # --------------------------------------------------------

    verified_subtotals: list[
        Decimal
    ] = []

    for item in items:

        subtotal = _money(
            item.get(
                "subtotal"
            )
        )

        if subtotal is not None:

            verified_subtotals.append(
                subtotal
            )

    total_amount = sum(
        verified_subtotals,
        Decimal("0"),
    )

    unavailable_items = [
        item
        for item in items
        if not item.get(
            "available"
        )
    ]

    insufficient_stock_items = [
        item
        for item in items
        if (
            item.get(
                "catalog_status"
            )
            == "active"
            and not item.get(
                "quantity_available",
                False,
            )
        )
    ]

    purchasable = (
        len(items) > 0
        and not unavailable_items
        and not insufficient_stock_items
    )

    return {
        "items": items,

        # Number of different SKU lines.
        "line_count": len(
            items
        ),

        # Sum of quantities.
        "total_units": (
            total_units
        ),

        "total_amount": (
            _money_output(
                total_amount
            )
        ),

        "currency": "INR",

        "is_empty": (
            len(items) == 0
        ),

        "is_fully_purchasable": (
            purchasable
        ),

        "unavailable_item_count": (
            len(
                unavailable_items
            )
        ),

        "insufficient_stock_item_count": (
            len(
                insufficient_stock_items
            )
        ),

        "calculated_by": "python",
    }


# ============================================================
# TOOL — VIEW CART
# ============================================================


def tool_get_cart() -> dict[
    str,
    Any
]:
    """
    Return current authenticated user's verified cart.
    """

    tool_name = (
        TOOL_GET_CART
    )

    try:

        cart = (
            _build_verified_cart()
        )

        return _success_result(
            tool=tool_name,

            data={
                "cart": cart,
            },

            message=(
                "Your cart is empty."
                if cart[
                    "is_empty"
                ]
                else "Cart retrieved."
            ),

            metadata={
                "calculated_by": (
                    "python"
                ),
            },
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code=(
                "AUTHENTICATION_REQUIRED"
            ),
            message=(
                "Please sign in to view your cart."
            ),
        )

    except (
        UserDatabaseError,
        ProductDatabaseError,
    ):

        logger.exception(
            "Cart retrieval failed."
        )

        return _error_result(
            tool=tool_name,
            code="CART_UNAVAILABLE",
            message=(
                "Your cart could not be "
                "retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected cart retrieval failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your cart could not be retrieved."
            ),
        )


# ============================================================
# TOOL — CART SUMMARY
# ============================================================


def tool_get_cart_summary() -> dict[
    str,
    Any
]:
    """
    Return concise Python-calculated cart statistics.

    Useful for:

        "How many things are in my cart?"
        "What's my cart total?"
        "How many different items are there?"
    """

    tool_name = (
        TOOL_GET_CART_SUMMARY
    )

    try:

        cart = (
            _build_verified_cart()
        )

        summary = {
            "line_count": (
                cart[
                    "line_count"
                ]
            ),

            "total_units": (
                cart[
                    "total_units"
                ]
            ),

            "total_amount": (
                cart[
                    "total_amount"
                ]
            ),

            "currency": (
                cart[
                    "currency"
                ]
            ),

            "is_empty": (
                cart[
                    "is_empty"
                ]
            ),

            "is_fully_purchasable": (
                cart[
                    "is_fully_purchasable"
                ]
            ),

            "unavailable_item_count": (
                cart[
                    "unavailable_item_count"
                ]
            ),

            "insufficient_stock_item_count": (
                cart[
                    "insufficient_stock_item_count"
                ]
            ),
        }

        return _success_result(
            tool=tool_name,

            data={
                "summary": summary,
            },

            message=(
                "Cart summary calculated."
            ),

            metadata={
                "calculated_by": "python",
            },
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in to view "
                "your cart summary."
            ),
        )

    except UserDatabaseError:

        return _error_result(
            tool=tool_name,
            code="CART_UNAVAILABLE",
            message=(
                "Your cart summary could "
                "not be retrieved right now."
            ),
        )

    except Exception:

        logger.exception(
            "Cart-summary failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your cart summary could "
                "not be calculated."
            ),
        )


# ============================================================
# TOOL — ADD TO CART
# ============================================================


def tool_add_to_cart(
    *,
    sku_id: Any,
    quantity: Any = 1,
) -> dict[str, Any]:
    """
    Add exact SKU to current user's cart.

    If already present:

        existing quantity + requested quantity

    is used.

    Example:

        existing = 2
        add quantity = 3

        final quantity = 5

    Stock is validated against final quantity.
    """

    tool_name = (
        TOOL_ADD_TO_CART
    )

    try:

        sku_id = (
            _required_string(
                sku_id,
                field_name="sku_id",
                max_length=(
                    MAX_SKU_ID_LENGTH
                ),
            )
        )

        quantity = _quantity(
            quantity,
            default=1,
        )

        # ----------------------------------------------------
        # Verify SKU from live catalog
        # ----------------------------------------------------

        sku = (
            _get_verified_sku(
                sku_id
            )
        )

        existing = (
            get_cart_item_by_sku(
                sku_id
            )
        )

        existing_quantity = (
            max(
                _safe_int(
                    existing.get(
                        "quantity"
                    )
                    if existing
                    else 0
                ),
                0,
            )
        )

        final_quantity = (
            existing_quantity
            + quantity
        )

        if (
            final_quantity
            > MAX_CART_QUANTITY
        ):

            raise CartToolValidationError(
                f"Cart quantity cannot exceed "
                f"{MAX_CART_QUANTITY}."
            )

        # ----------------------------------------------------
        # Validate stock against FINAL quantity
        # ----------------------------------------------------

        _validate_sku_quantity(
            sku=sku,
            requested_quantity=(
                final_quantity
            ),
        )

        # ----------------------------------------------------
        # Write
        # ----------------------------------------------------

        if existing:

            cart_row = (
                update_cart_item_quantity(
                    sku_id=sku_id,
                    quantity=(
                        final_quantity
                    ),
                )
            )

            action = "quantity_increased"

        else:

            cart_row = (
                create_cart_item(
                    sku_id=sku_id,
                    quantity=quantity,
                )
            )

            action = "added"

        cart = (
            _build_verified_cart()
        )

        return _success_result(
            tool=tool_name,

            data={
                "action": action,

                "sku": sku,

                "requested_add_quantity": (
                    quantity
                ),

                "previous_quantity": (
                    existing_quantity
                ),

                "final_quantity": (
                    final_quantity
                ),

                "cart_row": (
                    cart_row
                ),

                "cart": cart,
            },

            message=(
                "Item added to cart."
                if action == "added"
                else "Cart quantity increased."
            ),

            metadata={
                "database_write": True,

                "stock_verified": True,

                "calculated_by": (
                    "python"
                ),
            },
        )

    except CartToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_CART_REQUEST",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in before "
                "adding items to your cart."
            ),
        )

    except UserDatabaseConflictError:

        return _error_result(
            tool=tool_name,
            code="CART_CONFLICT",
            message=(
                "The cart changed while the "
                "item was being added. Please retry."
            ),
        )

    except (
        UserDatabaseError,
        ProductDatabaseError,
    ):

        logger.exception(
            "Add-to-cart failure."
        )

        return _error_result(
            tool=tool_name,
            code="CART_DATABASE_ERROR",
            message=(
                "The item could not be added "
                "to your cart right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected add-to-cart failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "The item could not be added "
                "to your cart."
            ),
        )


# ============================================================
# TOOL — SET QUANTITY
# ============================================================


def tool_update_cart_quantity(
    *,
    sku_id: Any,
    quantity: Any,
) -> dict[str, Any]:
    """
    Set exact quantity of an existing cart SKU.

    Example:

        existing = 2

        user:
            "make it 5"

        final = 5

    NOT:

        2 + 5 = 7
    """

    tool_name = (
        TOOL_UPDATE_CART_QUANTITY
    )

    try:

        sku_id = (
            _required_string(
                sku_id,
                field_name="sku_id",
                max_length=(
                    MAX_SKU_ID_LENGTH
                ),
            )
        )

        quantity = (
            _quantity(
                quantity
            )
        )

        existing = (
            get_cart_item_by_sku(
                sku_id
            )
        )

        if existing is None:

            return _error_result(
                tool=tool_name,
                code="ITEM_NOT_IN_CART",
                message=(
                    "That product variant is "
                    "not currently in your cart."
                ),
            )

        # ----------------------------------------------------
        # Re-fetch live SKU
        # ----------------------------------------------------

        sku = (
            _get_verified_sku(
                sku_id
            )
        )

        _validate_sku_quantity(
            sku=sku,
            requested_quantity=quantity,
        )

        previous_quantity = max(
            _safe_int(
                existing.get(
                    "quantity"
                ),
                default=0,
            ),
            0,
        )

        updated = (
            update_cart_item_quantity(
                sku_id=sku_id,
                quantity=quantity,
            )
        )

        cart = (
            _build_verified_cart()
        )

        return _success_result(
            tool=tool_name,

            data={
                "action": (
                    "quantity_updated"
                ),

                "sku": sku,

                "previous_quantity": (
                    previous_quantity
                ),

                "final_quantity": (
                    quantity
                ),

                "cart_row": updated,

                "cart": cart,
            },

            message=(
                "Cart quantity updated."
            ),

            metadata={
                "database_write": True,

                "stock_verified": True,

                "calculated_by": (
                    "python"
                ),
            },
        )

    except CartToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_CART_REQUEST",
            message=str(exc),
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in before "
                "changing your cart."
            ),
        )

    except UserDatabaseNotFoundError:

        return _error_result(
            tool=tool_name,
            code="ITEM_NOT_IN_CART",
            message=(
                "That product variant is "
                "not currently in your cart."
            ),
        )

    except (
        UserDatabaseError,
        ProductDatabaseError,
    ):

        logger.exception(
            "Cart quantity update failed."
        )

        return _error_result(
            tool=tool_name,
            code="CART_DATABASE_ERROR",
            message=(
                "The cart quantity could "
                "not be updated right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected cart quantity failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "The cart quantity could not be updated."
            ),
        )


# ============================================================
# TOOL — REMOVE FROM CART
# ============================================================


def tool_remove_from_cart(
    *,
    sku_id: Any,
) -> dict[str, Any]:
    """
    Completely remove exact SKU from current user's cart.
    """

    tool_name = (
        TOOL_REMOVE_FROM_CART
    )

    try:

        sku_id = (
            _required_string(
                sku_id,
                field_name="sku_id",
                max_length=(
                    MAX_SKU_ID_LENGTH
                ),
            )
        )

        existing = (
            get_cart_item_by_sku(
                sku_id
            )
        )

        if existing is None:

            return _success_result(
                tool=tool_name,

                data={
                    "removed": False,

                    "sku_id": sku_id,

                    "cart": (
                        _build_verified_cart()
                    ),
                },

                message=(
                    "That item was not in your cart."
                ),

                metadata={
                    "database_write": False,
                },
            )

        removed = (
            delete_cart_item(
                sku_id=sku_id
            )
        )

        cart = (
            _build_verified_cart()
        )

        return _success_result(
            tool=tool_name,

            data={
                "removed": removed,

                "sku_id": sku_id,

                "previous_quantity": (
                    _safe_int(
                        existing.get(
                            "quantity"
                        )
                    )
                ),

                "cart": cart,
            },

            message=(
                "Item removed from cart."
                if removed
                else "The item could not be removed."
            ),

            metadata={
                "database_write": (
                    removed
                ),
            },
        )

    except CartToolValidationError as exc:

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
                "Please sign in before "
                "changing your cart."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Remove-from-cart failure."
        )

        return _error_result(
            tool=tool_name,
            code="CART_DATABASE_ERROR",
            message=(
                "The item could not be removed "
                "from your cart right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected cart remove failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "The item could not be removed "
                "from your cart."
            ),
        )


# ============================================================
# TOOL — CLEAR CART
# ============================================================


def tool_clear_cart() -> dict[
    str,
    Any
]:
    """
    Remove every SKU belonging to current authenticated user's cart.

    No other user's cart can be affected.
    """

    tool_name = (
        TOOL_CLEAR_CART
    )

    try:

        existing = (
            get_cart_items()
        )

        count = clear_cart()

        empty_cart = (
            _build_verified_cart()
        )

        return _success_result(
            tool=tool_name,

            data={
                "cleared": True,

                "removed_line_count": (
                    count
                ),

                "previous_items": [
                    {
                        "sku_id": (
                            str(
                                row.get(
                                    "product_id"
                                )
                            )
                            if row.get(
                                "product_id"
                            )
                            is not None
                            else None
                        ),

                        "quantity": (
                            _safe_int(
                                row.get(
                                    "quantity"
                                )
                            )
                        ),
                    }
                    for row in existing
                ],

                "cart": empty_cart,
            },

            message=(
                "Your cart was already empty."
                if count == 0
                else "Your cart has been cleared."
            ),

            metadata={
                "database_write": (
                    count > 0
                ),
            },
        )

    except UserDatabaseAuthenticationError:

        return _error_result(
            tool=tool_name,
            code="AUTHENTICATION_REQUIRED",
            message=(
                "Please sign in before "
                "changing your cart."
            ),
        )

    except UserDatabaseError:

        logger.exception(
            "Clear-cart failure."
        )

        return _error_result(
            tool=tool_name,
            code="CART_DATABASE_ERROR",
            message=(
                "Your cart could not be "
                "cleared right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected clear-cart failure."
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "Your cart could not be cleared."
            ),
        )


# ============================================================
# COHERE CART TOOL SCHEMAS
# ============================================================

CART_TOOL_SCHEMAS: list[
    dict[str, Any]
] = [

    # ========================================================
    # VIEW CART
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_CART,

            "description": (
                "Retrieve the currently authenticated user's cart with "
                "live SKU names, variants, prices, stock and Python-"
                "calculated subtotals and total. Use for requests such "
                "as 'show my cart', 'what is in my cart?', or "
                "'what items do I currently have?'."
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
    # SUMMARY
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_GET_CART_SUMMARY,

            "description": (
                "Return Python-calculated current cart totals and counts. "
                "Use for questions such as 'what is my cart total?', "
                "'how many items are in my cart?', or "
                "'how many different products are in my cart?'."
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
    # ADD
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_ADD_TO_CART,

            "description": (
                "Add an exact grocery SKU/variant to the authenticated "
                "user's cart. Requires a verified sku_id previously "
                "returned by a product or variant-resolution tool. If "
                "the same SKU is already in the cart, the requested "
                "quantity is added to its existing quantity. Never "
                "invent an sku_id and never use this tool when the "
                "product variant is still ambiguous."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "sku_id": {
                        "type": "string",

                        "description": (
                            "Exact verified SKU identifier returned "
                            "by a product tool."
                        ),
                    },

                    "quantity": {
                        "type": "integer",

                        "minimum": 1,

                        "maximum": (
                            MAX_CART_QUANTITY
                        ),

                        "description": (
                            "Number of additional units to add. "
                            "Defaults to 1."
                        ),
                    },
                },

                "required": [
                    "sku_id",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # UPDATE QUANTITY
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_UPDATE_CART_QUANTITY,

            "description": (
                "Set the exact cart quantity for an SKU that is already "
                "in the authenticated user's cart. Use for requests such "
                "as 'make it 3', 'change the 1 litre milk quantity to 2', "
                "or 'I want 5 of those'. The quantity is an absolute "
                "final quantity, not an amount to add."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "sku_id": {
                        "type": "string",

                        "description": (
                            "Exact SKU identifier."
                        ),
                    },

                    "quantity": {
                        "type": "integer",

                        "minimum": 1,

                        "maximum": (
                            MAX_CART_QUANTITY
                        ),
                    },
                },

                "required": [
                    "sku_id",
                    "quantity",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # REMOVE
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_REMOVE_FROM_CART,

            "description": (
                "Completely remove one exact SKU/variant from the "
                "authenticated user's cart. Use only when the exact SKU "
                "is known from the cart or previous product context."
            ),

            "parameters": {

                "type": "object",

                "properties": {

                    "sku_id": {
                        "type": "string",
                    },
                },

                "required": [
                    "sku_id",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # CLEAR
    # ========================================================

    {
        "type": "function",

        "function": {

            "name":
                TOOL_CLEAR_CART,

            "description": (
                "Remove every item from the authenticated user's cart. "
                "Use only when the user clearly asks to clear or empty "
                "their entire cart."
            ),

            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties":
                    False,
            },
        },
    },
]


# ============================================================
# SAFE TOOL REGISTRY
# ============================================================

CartToolFunction = Callable[
    ...,
    dict[str, Any]
]


CART_TOOL_FUNCTIONS: dict[
    str,
    CartToolFunction,
] = {

    TOOL_GET_CART:
        tool_get_cart,

    TOOL_GET_CART_SUMMARY:
        tool_get_cart_summary,

    TOOL_ADD_TO_CART:
        tool_add_to_cart,

    TOOL_UPDATE_CART_QUANTITY:
        tool_update_cart_quantity,

    TOOL_REMOVE_FROM_CART:
        tool_remove_from_cart,

    TOOL_CLEAR_CART:
        tool_clear_cart,
}


# ============================================================
# GET SCHEMAS
# ============================================================


def get_cart_tool_schemas() -> list[
    dict[str, Any]
]:
    """
    Return defensive copy of Cohere cart tool definitions.
    """

    return copy.deepcopy(
        CART_TOOL_SCHEMAS
    )


# ============================================================
# GET NAMES
# ============================================================


def get_cart_tool_names() -> set[
    str
]:
    """
    Return approved cart tool names.
    """

    return set(
        CART_TOOL_FUNCTIONS.keys()
    )


# ============================================================
# IS CART TOOL
# ============================================================


def is_cart_tool(
    tool_name: str,
) -> bool:
    """
    Check whether a tool belongs to cart registry.
    """

    if not isinstance(
        tool_name,
        str,
    ):

        return False

    return (
        tool_name.strip()
        in CART_TOOL_FUNCTIONS
    )


# ============================================================
# EXECUTE CART TOOL
# ============================================================


def execute_cart_tool(
    *,
    tool_name: str,
    arguments: Mapping[
        str,
        Any,
    ] | None = None,
) -> dict[str, Any]:
    """
    Execute an APPROVED cart tool.

    No eval/exec/dynamic imports are used.
    """

    if not isinstance(
        tool_name,
        str,
    ):

        raise CartToolNotFoundError(
            "Cart tool name must be text."
        )

    tool_name = (
        tool_name.strip()
    )

    if not tool_name:

        raise CartToolNotFoundError(
            "Cart tool name cannot be empty."
        )

    function = (
        CART_TOOL_FUNCTIONS.get(
            tool_name
        )
    )

    if function is None:

        logger.warning(
            "Rejected unknown cart tool: %s",
            tool_name,
        )

        raise CartToolNotFoundError(
            f"Unsupported cart tool: "
            f"{tool_name}"
        )

    try:

        argument_dict = (
            _arguments_mapping(
                arguments
            )
        )

    except CartToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=str(exc),
        )

    try:

        logger.debug(
            "Executing approved cart tool. "
            "tool=%s argument_keys=%s",
            tool_name,
            sorted(
                argument_dict.keys()
            ),
        )

        return function(
            **argument_dict
        )

    except TypeError:

        logger.warning(
            "Cart tool argument mismatch. "
            "tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            code="INVALID_ARGUMENTS",
            message=(
                "The selected cart operation "
                "received unsupported or incomplete arguments."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected cart tool execution failure. "
            "tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            code="INTERNAL_ERROR",
            message=(
                "The cart operation could not be completed."
            ),
        )