"""
utils.py

Shared utility helpers for the Grocery Chatbot.

========================================================================
PURPOSE
========================================================================

This module contains small reusable helpers that are safe to use across:

    core/
    database/
    tools/
    rag/
    auth/
    app.py

It deliberately contains NO:

    Supabase queries
    Cohere calls
    Groq calls
    cart logic
    order logic
    product-search logic
    intent classification


========================================================================
MAIN UTILITIES
========================================================================

- safe Decimal conversion
- money calculations
- JSON-safe serialization
- text normalization
- ID validation
- UTC timestamps
- secret masking
- safe dictionary logging
- bounded text
"""

from __future__ import annotations

import json
import math
import re

from dataclasses import (
    asdict,
    is_dataclass,
)
from datetime import (
    date,
    datetime,
    timezone,
)
from decimal import (
    Decimal,
    InvalidOperation,
)
from typing import Any
from uuid import UUID


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_CURRENCY = "INR"

MAX_IDENTIFIER_LENGTH = 200

MAX_TEXT_LENGTH = 20_000


# ============================================================
# TIME HELPERS
# ============================================================


def utc_now() -> datetime:
    """
    Return timezone-aware UTC datetime.
    """

    return datetime.now(
        timezone.utc
    )


def utc_now_iso() -> str:
    """
    Return current UTC time in ISO format.
    """

    return utc_now().isoformat()


# ============================================================
# TEXT NORMALIZATION
# ============================================================


def clean_text(
    value: Any,
    *,
    default: str = "",
) -> str:
    """
    Convert value to normalized text.

    Repeated spaces/newlines collapse into one space.

    Example:

        "  Amul   Milk  "

    becomes:

        "Amul Milk"
    """

    if value is None:
        return default

    text = str(
        value
    ).strip()

    if not text:
        return default

    return " ".join(
        text.split()
    )


def optional_text(
    value: Any,
) -> str | None:
    """
    Return cleaned text or None.
    """

    result = clean_text(
        value
    )

    return (
        result
        if result
        else None
    )


def bounded_text(
    value: Any,
    *,
    max_length: int = MAX_TEXT_LENGTH,
) -> str:
    """
    Return text bounded to safe maximum length.

    Useful before sending large backend context to an LLM.
    """

    text = str(
        value
        if value is not None
        else ""
    )

    if len(text) <= max_length:
        return text

    return (
        text[:max_length]
        + "..."
    )


# ============================================================
# SEARCH NORMALIZATION
# ============================================================


def normalize_search_text(
    value: Any,
) -> str:
    """
    Simple normalized searchable representation.

    Examples:

        "Coca-Cola"  -> "coca cola"
        "AMUL Milk"  -> "amul milk"

    Semantic retrieval belongs to RAG.
    """

    text = clean_text(
        value
    ).lower()

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    )

    return " ".join(
        text.split()
    )


# ============================================================
# IDENTIFIER VALIDATION
# ============================================================


def validate_identifier(
    value: Any,
    *,
    field_name: str = "id",
) -> str:
    """
    Validate generic database identifier.

    We intentionally do not force UUID because some tables may use
    another valid identifier type.
    """

    if value is None:

        raise ValueError(
            f"{field_name} is required."
        )

    value = str(
        value
    ).strip()

    if not value:

        raise ValueError(
            f"{field_name} cannot be empty."
        )

    if len(value) > MAX_IDENTIFIER_LENGTH:

        raise ValueError(
            f"{field_name} is invalid."
        )

    return value


# ============================================================
# DECIMAL HELPERS
# ============================================================


def to_decimal(
    value: Any,
    *,
    default: Decimal | None = None,
) -> Decimal | None:
    """
    Safely convert numeric value to Decimal.

    Examples:

        29
        29.5
        "29.50"

    become Decimal values.

    Invalid/non-finite values return default.
    """

    if value is None:
        return default

    try:

        result = Decimal(
            str(value)
        )

        if not result.is_finite():
            return default

        return result

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return default


# ============================================================
# MONEY ROUNDING
# ============================================================


def round_money(
    value: Any,
) -> Decimal:
    """
    Round monetary value to 2 decimal places.

    Uses Decimal rather than float.
    """

    decimal_value = (
        to_decimal(
            value,
            default=Decimal("0"),
        )
        or Decimal("0")
    )

    return decimal_value.quantize(
        Decimal("0.01")
    )


# ============================================================
# SUBTOTAL
# ============================================================


def calculate_subtotal(
    unit_price: Any,
    quantity: Any,
) -> Decimal:
    """
    Calculate:

        unit_price × quantity

    in Python.

    Example:

        58 × 3 = 174.00

    The LLM should not perform trusted commerce calculations.
    """

    price = to_decimal(
        unit_price
    )

    if price is None:

        raise ValueError(
            "unit_price must be numeric."
        )

    if price < 0:

        raise ValueError(
            "unit_price cannot be negative."
        )

    if isinstance(
        quantity,
        bool,
    ):

        raise ValueError(
            "quantity must be an integer."
        )

    try:

        quantity = int(
            quantity
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise ValueError(
            "quantity must be an integer."
        ) from exc

    if quantity < 0:

        raise ValueError(
            "quantity cannot be negative."
        )

    return round_money(
        price
        * Decimal(
            quantity
        )
    )


# ============================================================
# MONEY OUTPUT
# ============================================================


def money_to_float(
    value: Any,
) -> float | None:
    """
    Convert money to JSON-safe float.

    Keeps calculations in Decimal until final serialization.
    """

    decimal_value = (
        to_decimal(
            value
        )
    )

    if decimal_value is None:
        return None

    return float(
        round_money(
            decimal_value
        )
    )


# ============================================================
# SAFE INTEGER
# ============================================================


def safe_int(
    value: Any,
    *,
    default: int = 0,
) -> int:
    """
    Convert value to integer safely.
    """

    if isinstance(
        value,
        bool,
    ):

        return default

    try:

        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return default


# ============================================================
# SAFE FLOAT
# ============================================================


def safe_float(
    value: Any,
    *,
    default: float = 0.0,
) -> float:
    """
    Safely convert numeric value to finite float.
    """

    try:

        result = float(
            value
        )

        if not math.isfinite(
            result
        ):

            return default

        return result

    except (
        TypeError,
        ValueError,
    ):

        return default


# ============================================================
# JSON SERIALIZATION
# ============================================================


def json_default(
    value: Any,
) -> Any:
    """
    JSON serializer for common application objects.

    Supports:

        Decimal
        datetime
        date
        UUID
        dataclasses
        Pydantic models
        sets
    """

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
            datetime,
            date,
        ),
    ):

        return value.isoformat()

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

        return asdict(
            value
        )

    if hasattr(
        value,
        "model_dump",
    ):

        try:

            return value.model_dump()

        except Exception:
            pass

    if isinstance(
        value,
        set,
    ):

        return list(
            value
        )

    if hasattr(
        value,
        "__dict__",
    ):

        try:

            return dict(
                value.__dict__
            )

        except Exception:
            pass

    return str(
        value
    )


def to_json(
    value: Any,
    *,
    pretty: bool = False,
) -> str:
    """
    Serialize Python value to JSON safely.
    """

    if pretty:

        return json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(
            ",",
            ":",
        ),
        default=json_default,
    )


# ============================================================
# JSON-SAFE OBJECT
# ============================================================


def make_json_safe(
    value: Any,
) -> Any:
    """
    Convert arbitrary supported value into JSON-safe Python object.

    Useful before giving verified context to Groq/Cohere.
    """

    return json.loads(
        to_json(
            value
        )
    )


# ============================================================
# SECRET MASKING
# ============================================================


def mask_secret(
    value: Any,
    *,
    visible_start: int = 4,
    visible_end: int = 4,
) -> str:
    """
    Mask secrets for development diagnostics.

    Example:

        abcdefghijklmnop

    becomes:

        abcd********mnop

    Never use this as a substitute for proper secret handling.
    """

    if value is None:

        return ""

    text = str(
        value
    )

    if not text:

        return ""

    if len(text) <= (
        visible_start
        + visible_end
    ):

        return "*" * len(
            text
        )

    hidden_length = (
        len(text)
        - visible_start
        - visible_end
    )

    return (
        text[:visible_start]
        + (
            "*"
            * hidden_length
        )
        + text[
            -visible_end:
        ]
    )


# ============================================================
# SENSITIVE KEY DETECTION
# ============================================================


SENSITIVE_KEYWORDS = (
    "password",
    "access_token",
    "refresh_token",
    "api_key",
    "secret_key",
    "authorization",
    "bearer",
    "client_secret",
)


def is_sensitive_key(
    key: Any,
) -> bool:
    """
    Determine whether dictionary key may contain sensitive data.
    """

    key = str(
        key
    ).lower()

    return any(
        keyword in key
        for keyword
        in SENSITIVE_KEYWORDS
    )


# ============================================================
# SAFE LOG DICTIONARY
# ============================================================


def safe_log_value(
    value: Any,
) -> Any:
    """
    Recursively remove/mask sensitive values.

    Useful only for logging/debugging.

    Example:

        {
            "email": "...",
            "access_token": "..."
        }

    becomes:

        {
            "email": "...",
            "access_token": "***"
        }
    """

    if isinstance(
        value,
        Mapping,
    ):

        result: dict[
            str,
            Any
        ] = {}

        for key, item in (
            value.items()
        ):

            key_string = str(
                key
            )

            if is_sensitive_key(
                key_string
            ):

                result[
                    key_string
                ] = "***"

            else:

                result[
                    key_string
                ] = safe_log_value(
                    item
                )

        return result

    if isinstance(
        value,
        list,
    ):

        return [
            safe_log_value(
                item
            )
            for item in value
        ]

    if isinstance(
        value,
        tuple,
    ):

        return tuple(
            safe_log_value(
                item
            )
            for item in value
        )

    if isinstance(
        value,
        set,
    ):

        return [
            safe_log_value(
                item
            )
            for item in value
        ]

    return value


# ============================================================
# BOOLEAN NORMALIZATION
# ============================================================


def parse_bool(
    value: Any,
    *,
    default: bool | None = None,
) -> bool:
    """
    Parse a flexible boolean.

    Supports:

        True
        False
        1
        0
        yes
        no
        true
        false

    Raises ValueError if ambiguous and no default was provided.
    """

    if value is None:

        if default is not None:
            return default

        raise ValueError(
            "Boolean value is required."
        )

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
            "1",
            "yes",
            "y",
            "on",
        }:

            return True

        if normalized in {
            "false",
            "0",
            "no",
            "n",
            "off",
        }:

            return False

    if default is not None:

        return default

    raise ValueError(
        "Unable to interpret boolean value."
    )


# ============================================================
# DICTIONARY HELPERS
# ============================================================


def remove_none_values(
    value: Mapping[
        str,
        Any
    ],
) -> dict[str, Any]:
    """
    Return dictionary without None-valued fields.

    Useful for optional Supabase update payloads.
    """

    return {
        key: item
        for key, item
        in value.items()
        if item is not None
    }


# ============================================================
# SAFE LIST
# ============================================================


def ensure_list(
    value: Any,
) -> list[Any]:
    """
    Convert common values into list.

    None:
        []

    list:
        unchanged copy

    tuple/set:
        list

    scalar:
        [scalar]
    """

    if value is None:

        return []

    if isinstance(
        value,
        list,
    ):

        return list(
            value
        )

    if isinstance(
        value,
        (
            tuple,
            set,
        ),
    ):

        return list(
            value
        )

    return [
        value
    ]


# ============================================================
# DICT MERGE
# ============================================================


def merge_dicts(
    *values: Mapping[
        str,
        Any
    ] | None,
) -> dict[str, Any]:
    """
    Merge dictionaries left-to-right.

    None values are ignored.

    Example:

        merge_dicts(
            {"a": 1},
            None,
            {"b": 2}
        )

    becomes:

        {
            "a": 1,
            "b": 2
        }
    """

    result: dict[
        str,
        Any
    ] = {}

    for value in values:

        if not value:
            continue

        result.update(
            value
        )

    return result