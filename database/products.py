"""
database/products.py

Structured Supabase grocery catalog access.

Responsibilities
----------------

- Read active products/SKUs from Supabase
- Group SKU rows into logical products
- Search product name / brand / SKU identity
- Support typo/close matching
- Return out-of-stock products instead of hiding them
- Return product images
- Return deterministic alternatives
- Resolve product variants
- Calculate cheapest / most expensive variants
- List brands/categories
- Return catalog statistics


IMPORTANT
---------

This module does NOT:

- use Cohere
- use Groq
- perform semantic RAG
- modify cart
- modify orders
- classify user intent

Semantic requests such as:

    "something healthy for breakfast"

belong to rag/rag.py.

Direct catalog requests such as:

    "milk"
    "amil milk"
    "maggie"
    "500 ml Amul milk"
    "milk under ₹100"

belong here.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata

from collections import defaultdict
from decimal import (
    Decimal,
    InvalidOperation,
)
from difflib import SequenceMatcher

from typing import (
    Any,
    Iterable,
    Literal,
    Mapping,
    Sequence,
)


# ============================================================
# SUPABASE
# ============================================================

from database.supabase import (
    create_public_client,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# TYPES
# ============================================================

StockState = Literal[
    "any",
    "in_stock",
    "out_of_stock",
]

ProductSortField = Literal[
    "relevance",
    "name",
    "price",
    "rating",
    "stock",
]

SortOrder = Literal[
    "asc",
    "desc",
]


# ============================================================
# TABLES
# ============================================================

PRODUCTS_TABLE = "products"

CATEGORIES_TABLE = "categories"

PRODUCT_IMAGES_TABLE = "product_images"


# ============================================================
# LIMITS
# ============================================================

CATALOG_PAGE_SIZE = 1000

MAX_SEARCH_RESULTS = 50

MAX_CATALOG_LIST_RESULTS = 100

MAX_ALTERNATIVE_RESULTS = 10


# ============================================================
# SEARCH CONFIG
# ============================================================

# Token similarity used for typo tolerance.
#
# Examples:
#
# maggie -> maggi
# biscut -> biscuit

TOKEN_FUZZY_THRESHOLD = 0.78


# Whole phrase similarity threshold.
PHRASE_FUZZY_THRESHOLD = 0.64


# Lower threshold used ONLY for fallback suggestions.
#
# We do not classify these as actual search matches.
ALTERNATIVE_FUZZY_THRESHOLD = 0.46


# ============================================================
# PRODUCT SELECT
# ============================================================

PRODUCT_SELECT = (
    "id,"
    "category_id,"
    "name,"
    "slug,"
    "brand,"
    "description,"
    "short_description,"
    "price,"
    "discount_price,"
    "stock,"
    "quantity,"
    "unit,"
    "pack_size,"
    "rating,"
    "currency,"
    "is_featured,"
    "is_active,"
    "features,"
    "specs,"
    "variants,"
    "product_group_key,"
    "created_at,"
    "updated_at"
)


# ============================================================
# ERRORS
# ============================================================


class ProductDatabaseError(
    RuntimeError
):
    """
    Base catalog error.
    """

    pass


class ProductValidationError(
    ProductDatabaseError
):
    """
    Invalid search/filter input.
    """

    pass


class ProductNotFoundError(
    ProductDatabaseError
):
    """
    Explicit product/SKU not found.
    """

    pass


# ============================================================
# BASIC CONVERSION
# ============================================================


def _safe_decimal(
    value: Any,
    *,
    default: Decimal = Decimal("0"),
) -> Decimal:
    """
    Safely convert commerce values to Decimal.
    """

    if value is None:

        return default

    try:

        result = Decimal(
            str(
                value
            )
        )

        if not result.is_finite():

            return default

        return result

    except (
        InvalidOperation,
        TypeError,
        ValueError,
    ):

        return default


def _safe_float(
    value: Any,
    *,
    default: float = 0.0,
) -> float:

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


def _safe_int(
    value: Any,
    *,
    default: int = 0,
) -> int:

    try:

        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):

        return default


def _clean_optional_text(
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
# TEXT NORMALIZATION
# ============================================================

_UNIT_ALIASES = {

    # Weight
    "g": "g",
    "gm": "g",
    "gms": "g",
    "gram": "g",
    "grams": "g",

    "kg": "kg",
    "kgs": "kg",
    "kilogram": "kg",
    "kilograms": "kg",

    # Volume
    "ml": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "milliliter": "ml",
    "milliliters": "ml",

    "l": "l",
    "lt": "l",
    "ltr": "l",
    "ltrs": "l",
    "litre": "l",
    "litres": "l",
    "liter": "l",
    "liters": "l",

    # Count
    "pc": "pc",
    "pcs": "pc",
    "piece": "pc",
    "pieces": "pc",

    "pack": "pack",
    "packs": "pack",
}


def _search_key(
    value: Any,
) -> str:
    """
    Normalize text for catalog matching.

    Examples:

        Maggi's     -> maggis
        500ml       -> 500 ml
        1 litre     -> 1 l
        Coca-Cola   -> coca cola
    """

    if value is None:

        return ""

    text = unicodedata.normalize(
        "NFKD",
        str(
            value
        ),
    )

    text = "".join(
        char
        for char in text
        if not unicodedata.combining(
            char
        )
    )

    text = (
        text
        .lower()
        .replace(
            "’",
            "'",
        )
        .replace(
            "`",
            "'",
        )
        .replace(
            "'",
            "",
        )
    )

    # 500ml -> 500 ml
    text = re.sub(
        r"(?<=\d)(?=[a-z])",
        " ",
        text,
    )

    # ml500 -> ml 500
    text = re.sub(
        r"(?<=[a-z])(?=\d)",
        " ",
        text,
    )

    text = re.sub(
        r"[^a-z0-9.]+",
        " ",
        text,
    )

    tokens = [
        token
        for token in text.split()
        if token
    ]

    normalized_tokens = [
        _UNIT_ALIASES.get(
            token,
            token,
        )
        for token in tokens
    ]

    return " ".join(
        normalized_tokens
    )


def _search_tokens(
    value: Any,
) -> list[str]:

    return [
        token
        for token in _search_key(
            value
        ).split()
        if token
    ]


# ============================================================
# SINGULAR / PLURAL
# ============================================================


def _token_variants(
    token: str,
) -> set[str]:

    token = _search_key(
        token
    )

    if not token:

        return set()

    result = {
        token
    }

    if (
        len(
            token
        ) > 4
        and token.endswith(
            "ies"
        )
    ):

        result.add(
            token[:-3]
            + "y"
        )

    if (
        len(
            token
        ) > 4
        and token.endswith(
            "es"
        )
    ):

        result.add(
            token[:-2]
        )

    if (
        len(
            token
        ) > 3
        and token.endswith(
            "s"
        )
    ):

        result.add(
            token[:-1]
        )

    return result


# ============================================================
# FUZZY TOKEN SCORE
# ============================================================


def _token_similarity(
    query_token: str,
    candidate_token: str,
) -> float:
    """
    Compare one query token with one candidate token.

    Examples:

        milk   / milk     -> 1.0
        mil    / milk     -> high
        maggie / maggi    -> high
        biscut / biscuit  -> high

    Short 1-2 letter queries are intentionally NOT fuzzy matched.
    """

    query_token = _search_key(
        query_token
    )

    candidate_token = _search_key(
        candidate_token
    )

    if (
        not query_token
        or not candidate_token
    ):

        return 0.0

    query_variants = (
        _token_variants(
            query_token
        )
    )

    candidate_variants = (
        _token_variants(
            candidate_token
        )
    )

    # Exact / singular plural.
    if (
        query_variants
        & candidate_variants
    ):

        return 1.0

    # --------------------------------------------------------
    # Prefix matching
    #
    # mil -> milk
    # bisc -> biscuit
    # --------------------------------------------------------

    if (
        len(
            query_token
        ) >= 3
        and candidate_token.startswith(
            query_token
        )
    ):

        coverage = (
            len(
                query_token
            )
            / max(
                len(
                    candidate_token
                ),
                1,
            )
        )

        return max(
            0.84,
            min(
                0.96,
                0.84
                + (
                    coverage
                    * 0.12
                ),
            ),
        )

    if (
        len(
            candidate_token
        ) >= 3
        and query_token.startswith(
            candidate_token
        )
    ):

        coverage = (
            len(
                candidate_token
            )
            / max(
                len(
                    query_token
                ),
                1,
            )
        )

        return max(
            0.80,
            min(
                0.94,
                0.80
                + (
                    coverage
                    * 0.12
                ),
            ),
        )

    # --------------------------------------------------------
    # Typo similarity
    # --------------------------------------------------------

    if (
        len(
            query_token
        ) < 3
        or len(
            candidate_token
        ) < 3
    ):

        return 0.0

    return SequenceMatcher(
        None,
        query_token,
        candidate_token,
    ).ratio()


# ============================================================
# TERM MATCH
# ============================================================


def _best_token_score(
    query_token: str,
    candidate_tokens: Sequence[
        str
    ],
) -> float:

    if not candidate_tokens:

        return 0.0

    return max(
        (
            _token_similarity(
                query_token,
                candidate,
            )
            for candidate
            in candidate_tokens
        ),
        default=0.0,
    )


# ============================================================
# PRODUCT IDENTITY
# ============================================================


def _sku_identity_text(
    row: Mapping[
        str,
        Any,
    ],
) -> str:
    """
    Direct-search fields only.

    Do NOT search description/features here.

    Example:

        searching "milk"

    must not return Cornflakes merely because its description says
    "serve with milk".
    """

    values = (
        row.get(
            "name"
        ),
        row.get(
            "brand"
        ),
        row.get(
            "slug"
        ),
        row.get(
            "product_group_key"
        ),
        row.get(
            "pack_size"
        ),
        row.get(
            "quantity"
        ),
        row.get(
            "unit"
        ),
    )

    return " ".join(
        str(
            value
        )
        for value in values
        if value is not None
    )


# ============================================================
# SKU QUERY SCORE
# ============================================================


def _query_match_score(
    row: Mapping[
        str,
        Any,
    ],
    query: str | None,
) -> float:
    """
    Calculate direct catalog relevance between 0 and 1.

    This is lexical/typo matching, NOT semantic RAG.
    """

    if query is None:

        return 1.0

    query_key = _search_key(
        query
    )

    if not query_key:

        return 1.0

    name_key = _search_key(
        row.get(
            "name"
        )
    )

    brand_key = _search_key(
        row.get(
            "brand"
        )
    )

    slug_key = _search_key(
        row.get(
            "slug"
        )
    )

    group_key = _search_key(
        row.get(
            "product_group_key"
        )
    )

    identity_key = _search_key(
        _sku_identity_text(
            row
        )
    )

    # ========================================================
    # EXACT BEST MATCHES
    # ========================================================

    if query_key == name_key:

        return 1.0

    if query_key == brand_key:

        return 0.99

    if query_key in {
        slug_key,
        group_key,
    }:

        return 0.98

    # ========================================================
    # PHRASE INSIDE PRODUCT IDENTITY
    # ========================================================

    if (
        query_key
        and query_key in name_key
    ):

        return 0.97

    if (
        query_key
        and query_key in brand_key
    ):

        return 0.96

    if (
        query_key
        and query_key in identity_key
    ):

        return 0.94

    # ========================================================
    # COMPACT PHRASE
    #
    # coca cola ↔ cocacola
    # ========================================================

    compact_query = (
        query_key.replace(
            " ",
            "",
        )
    )

    compact_name = (
        name_key.replace(
            " ",
            "",
        )
    )

    compact_brand = (
        brand_key.replace(
            " ",
            "",
        )
    )

    if (
        compact_query
        and compact_query
        in compact_name
    ):

        return 0.93

    if (
        compact_query
        and compact_query
        in compact_brand
    ):

        return 0.92

    # ========================================================
    # TOKEN FUZZY MATCH
    # ========================================================

    query_tokens = (
        _search_tokens(
            query
        )
    )

    candidate_tokens = (
        _search_tokens(
            _sku_identity_text(
                row
            )
        )
    )

    if (
        not query_tokens
        or not candidate_tokens
    ):

        return 0.0

    token_scores = [
        _best_token_score(
            query_token,
            candidate_tokens,
        )
        for query_token
        in query_tokens
    ]

    # Every meaningful query token should have a reasonable match.
    if any(
        score
        < TOKEN_FUZZY_THRESHOLD
        for score
        in token_scores
    ):

        # Whole phrase typo fallback.
        phrase_ratio = (
            SequenceMatcher(
                None,
                query_key,
                name_key,
            ).ratio()
        )

        if (
            phrase_ratio
            >= PHRASE_FUZZY_THRESHOLD
        ):

            return (
                phrase_ratio
                * 0.88
            )

        return 0.0

    average = (
        sum(
            token_scores
        )
        / len(
            token_scores
        )
    )

    return min(
        0.91,
        average
        * 0.91,
    )


def _matches_query(
    row: Mapping[
        str,
        Any,
    ],
    query: str | None,
) -> bool:

    return (
        _query_match_score(
            row,
            query,
        )
        > 0.0
    )


# ============================================================
# LOGICAL PRODUCT KEY
# ============================================================


def _logical_product_key(
    row: Mapping[
        str,
        Any,
    ],
) -> str:

    group_key = (
        _clean_optional_text(
            row.get(
                "product_group_key"
            )
        )
    )

    if group_key:

        return _search_key(
            group_key
        )

    brand = _search_key(
        row.get(
            "brand"
        )
    )

    name = _search_key(
        row.get(
            "name"
        )
    )

    if (
        not brand
        and not name
    ):

        return (
            "sku:"
            + str(
                row.get(
                    "id"
                )
                or ""
            )
        )

    return (
        f"{brand}|{name}"
    )


# ============================================================
# PRICE
# ============================================================


def _regular_price(
    row: Mapping[
        str,
        Any,
    ],
) -> Decimal:

    return max(
        _safe_decimal(
            row.get(
                "price"
            )
        ),
        Decimal(
            "0"
        ),
    )


def _discount_price(
    row: Mapping[
        str,
        Any,
    ],
) -> Decimal | None:

    raw = (
        row.get(
            "discount_price"
        )
    )

    if raw is None:

        return None

    regular = (
        _regular_price(
            row
        )
    )

    discount = (
        _safe_decimal(
            raw,
            default=regular,
        )
    )

    if (
        discount
        >= Decimal(
            "0"
        )
        and discount
        < regular
    ):

        return discount

    return None


def _effective_price(
    row: Mapping[
        str,
        Any,
    ],
) -> Decimal:

    discount = (
        _discount_price(
            row
        )
    )

    if discount is not None:

        return discount

    return _regular_price(
        row
    )


def _price_output(
    row: Mapping[
        str,
        Any,
    ],
) -> tuple[
    float,
    float | None,
]:

    selling = (
        _effective_price(
            row
        )
    )

    discount = (
        _discount_price(
            row
        )
    )

    if discount is not None:

        return (
            float(
                selling
            ),
            float(
                _regular_price(
                    row
                )
            ),
        )

    return (
        float(
            selling
        ),
        None,
    )


# ============================================================
# STOCK
# ============================================================


def _stock(
    row: Mapping[
        str,
        Any,
    ],
) -> int:

    return max(
        _safe_int(
            row.get(
                "stock"
            )
        ),
        0,
    )


def _is_in_stock(
    row: Mapping[
        str,
        Any,
    ],
) -> bool:

    return (
        bool(
            row.get(
                "is_active",
                True,
            )
        )
        and _stock(
            row
        )
        > 0
    )


# ============================================================
# VARIANT LABEL
# ============================================================


def _variant_label(
    row: Mapping[
        str,
        Any,
    ],
) -> str:

    pack_size = (
        _clean_optional_text(
            row.get(
                "pack_size"
            )
        )
    )

    if pack_size:

        return pack_size

    quantity = (
        row.get(
            "quantity"
        )
    )

    unit = (
        _clean_optional_text(
            row.get(
                "unit"
            )
        )
    )

    if (
        quantity is not None
        and unit
    ):

        return (
            f"{quantity} {unit}"
        )

    if unit:

        return unit

    return "Standard"


# ============================================================
# NATURAL VARIANT SORT
# ============================================================


def _size_sort_key(
    row: Mapping[
        str,
        Any,
    ],
) -> tuple[
    int,
    float,
    str,
]:

    label = _search_key(
        _variant_label(
            row
        )
    )

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*(kg|g|l|ml|pc|pack)?",
        label,
    )

    if not match:

        return (
            99,
            float(
                "inf"
            ),
            label,
        )

    number = (
        _safe_float(
            match.group(
                1
            )
        )
    )

    unit = (
        match.group(
            2
        )
        or ""
    )

    if unit == "kg":

        return (
            1,
            number
            * 1000,
            label,
        )

    if unit == "g":

        return (
            1,
            number,
            label,
        )

    if unit == "l":

        return (
            2,
            number
            * 1000,
            label,
        )

    if unit == "ml":

        return (
            2,
            number,
            label,
        )

    if unit == "pc":

        return (
            3,
            number,
            label,
        )

    if unit == "pack":

        return (
            4,
            number,
            label,
        )

    return (
        90,
        number,
        label,
    )


# ============================================================
# DATABASE CLIENT
# ============================================================


def _get_catalog_client():

    try:

        return (
            create_public_client()
        )

    except Exception as exc:

        raise ProductDatabaseError(
            "Unable to connect to the product catalog."
        ) from exc


# ============================================================
# FETCH CATALOG
# ============================================================


def _fetch_active_sku_rows() -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Fetch all active SKU rows.

    Stock=0 rows are intentionally INCLUDED.
    """

    client = (
        _get_catalog_client()
    )

    rows: list[
        dict[
            str,
            Any,
        ]
    ] = []

    offset = 0

    while True:

        try:

            response = (
                client
                .table(
                    PRODUCTS_TABLE
                )
                .select(
                    PRODUCT_SELECT
                )
                .eq(
                    "is_active",
                    True,
                )
                .range(
                    offset,
                    offset
                    + CATALOG_PAGE_SIZE
                    - 1,
                )
                .execute()
            )

        except Exception as exc:

            logger.exception(
                "Failed to retrieve product catalog."
            )

            raise ProductDatabaseError(
                "Failed to retrieve product catalog."
            ) from exc

        batch = list(
            response.data
            or []
        )

        rows.extend(
            dict(
                row
            )
            for row in batch
        )

        if (
            len(
                batch
            )
            < CATALOG_PAGE_SIZE
        ):

            break

        offset += (
            CATALOG_PAGE_SIZE
        )

    return rows


# ============================================================
# FETCH ONE SKU
# ============================================================


def _fetch_sku_row(
    sku_id: str,
) -> dict[
    str,
    Any,
] | None:

    sku_id = str(
        sku_id
    ).strip()

    if not sku_id:

        raise ProductValidationError(
            "sku_id cannot be empty."
        )

    client = (
        _get_catalog_client()
    )

    try:

        response = (
            client
            .table(
                PRODUCTS_TABLE
            )
            .select(
                PRODUCT_SELECT
            )
            .eq(
                "id",
                sku_id,
            )
            .eq(
                "is_active",
                True,
            )
            .limit(
                1
            )
            .execute()
        )

    except Exception as exc:

        raise ProductDatabaseError(
            "Failed to retrieve SKU."
        ) from exc

    rows = list(
        response.data
        or []
    )

    if not rows:

        return None

    return dict(
        rows[
            0
        ]
    )


# ============================================================
# CATEGORIES
# ============================================================


def _get_category_map(
    category_ids: Iterable[
        Any
    ],
) -> dict[
    str,
    str,
]:

    ids = list(
        dict.fromkeys(
            str(
                value
            ).strip()
            for value in category_ids
            if value is not None
            and str(
                value
            ).strip()
        )
    )

    if not ids:

        return {}

    client = (
        _get_catalog_client()
    )

    try:

        response = (
            client
            .table(
                CATEGORIES_TABLE
            )
            .select(
                "id,name"
            )
            .in_(
                "id",
                ids,
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Unable to retrieve categories: %s",
            type(
                exc
            ).__name__,
        )

        return {}

    return {
        str(
            row[
                "id"
            ]
        ):
        str(
            row[
                "name"
            ]
        )
        for row
        in (
            response.data
            or []
        )
        if row.get(
            "id"
        ) is not None
        and row.get(
            "name"
        ) is not None
    }


def _category_matches(
    category_name: str | None,
    category_id: Any,
    category_map: Mapping[
        str,
        str,
    ],
) -> bool:

    if not category_name:

        return True

    if category_id is None:

        return False

    actual_name = (
        category_map.get(
            str(
                category_id
            )
        )
    )

    if not actual_name:

        return False

    requested = (
        _search_key(
            category_name
        )
    )

    actual = (
        _search_key(
            actual_name
        )
    )

    if requested == actual:

        return True

    if (
        requested
        and requested
        in actual
    ):

        return True

    ratio = SequenceMatcher(
        None,
        requested,
        actual,
    ).ratio()

    return (
        ratio
        >= 0.78
    )


# ============================================================
# IMAGE LOOKUP
# ============================================================


def _get_images_by_sku(
    sku_ids: Iterable[
        Any
    ],
) -> dict[
    str,
    str,
]:
    """
    Return preferred image URL for every SKU.

    Priority:

        primary first
        then lowest sort_order
    """

    ids = list(
        dict.fromkeys(
            str(
                value
            ).strip()
            for value in sku_ids
            if value is not None
            and str(
                value
            ).strip()
        )
    )

    if not ids:

        return {}

    client = (
        _get_catalog_client()
    )

    try:

        response = (
            client
            .table(
                PRODUCT_IMAGES_TABLE
            )
            .select(
                "product_id,"
                "image_url,"
                "is_primary,"
                "sort_order"
            )
            .in_(
                "product_id",
                ids,
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Unable to retrieve product images: %s",
            type(
                exc
            ).__name__,
        )

        return {}

    grouped: dict[
        str,
        list[
            dict[
                str,
                Any,
            ]
        ],
    ] = defaultdict(
        list
    )

    for image in (
        response.data
        or []
    ):

        product_id = (
            image.get(
                "product_id"
            )
        )

        image_url = (
            _clean_optional_text(
                image.get(
                    "image_url"
                )
            )
        )

        if (
            product_id is None
            or not image_url
        ):

            continue

        grouped[
            str(
                product_id
            )
        ].append(
            dict(
                image
            )
        )

    image_map: dict[
        str,
        str,
    ] = {}

    for (
        product_id,
        images,
    ) in grouped.items():

        images.sort(
            key=lambda item: (
                not bool(
                    item.get(
                        "is_primary"
                    )
                ),
                _safe_int(
                    item.get(
                        "sort_order"
                    ),
                    default=999999,
                ),
            )
        )

        for image in images:

            url = (
                _clean_optional_text(
                    image.get(
                        "image_url"
                    )
                )
            )

            if url:

                image_map[
                    product_id
                ] = url

                break

    return image_map


# ============================================================
# GROUP SKUS
# ============================================================


def _group_skus(
    rows: Iterable[
        Mapping[
            str,
            Any,
        ]
    ],
) -> dict[
    str,
    list[
        dict[
            str,
            Any,
        ]
    ],
]:

    grouped: dict[
        str,
        list[
            dict[
                str,
                Any,
            ]
        ],
    ] = defaultdict(
        list
    )

    for row in rows:

        row = dict(
            row
        )

        grouped[
            _logical_product_key(
                row
            )
        ].append(
            row
        )

    return dict(
        grouped
    )


# ============================================================
# REPRESENTATIVE SKU
# ============================================================


def _representative_sku(
    rows: Sequence[
        Mapping[
            str,
            Any,
        ]
    ],
) -> Mapping[
    str,
    Any,
]:

    if not rows:

        raise ValueError(
            "Cannot choose representative SKU from empty rows."
        )

    return sorted(
        rows,
        key=lambda row: (
            not _is_in_stock(
                row
            ),
            _effective_price(
                row
            ),
            str(
                row.get(
                    "id"
                )
                or ""
            ),
        ),
    )[
        0
    ]


# ============================================================
# BUILD VARIANT
# ============================================================


def _build_variant(
    row: Mapping[
        str,
        Any,
    ],
    *,
    image_url: str | None,
    matched: bool,
    match_score: float = 0.0,
) -> dict[
    str,
    Any,
]:

    price, mrp = (
        _price_output(
            row
        )
    )

    stock = (
        _stock(
            row
        )
    )

    return {
        "sku_id":
            str(
                row.get(
                    "id"
                )
            ),

        "size":
            _variant_label(
                row
            ),

        "pack_size":
            _clean_optional_text(
                row.get(
                    "pack_size"
                )
            ),

        "quantity":
            row.get(
                "quantity"
            ),

        "unit":
            _clean_optional_text(
                row.get(
                    "unit"
                )
            ),

        "price":
            price,

        "mrp":
            mrp,

        "is_discounted":
            mrp is not None,

        "stock":
            stock,

        "in_stock":
            _is_in_stock(
                row
            ),

        # Do not invent currency.
        "currency":
            _clean_optional_text(
                row.get(
                    "currency"
                )
            ),

        "image_url":
            image_url,

        "matched":
            bool(
                matched
            ),

        "match_score":
            round(
                float(
                    match_score
                ),
                4,
            ),
    }


# ============================================================
# BUILD LOGICAL PRODUCT
# ============================================================


def _build_product(
    *,
    product_key: str,
    rows: Sequence[
        Mapping[
            str,
            Any,
        ]
    ],
    matched_sku_ids: set[
        str
    ] | None,
    category_map: Mapping[
        str,
        str,
    ],
    image_map: Mapping[
        str,
        str,
    ],
    sku_match_scores: Mapping[
        str,
        float,
    ] | None = None,
) -> dict[
    str,
    Any,
]:

    if not rows:

        raise ValueError(
            "Product rows cannot be empty."
        )

    representative = (
        _representative_sku(
            rows
        )
    )

    matched_sku_ids = (
        matched_sku_ids
        or set()
    )

    sku_match_scores = (
        sku_match_scores
        or {}
    )

    sorted_rows = sorted(
        rows,
        key=(
            _size_sort_key
        ),
    )

    variants: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for row in sorted_rows:

        sku_id = str(
            row.get(
                "id"
            )
        )

        variants.append(
            _build_variant(
                row,
                image_url=(
                    image_map.get(
                        sku_id
                    )
                ),
                matched=(
                    sku_id
                    in matched_sku_ids
                ),
                match_score=(
                    sku_match_scores.get(
                        sku_id,
                        0.0,
                    )
                ),
            )
        )

    # ========================================================
    # PRODUCT IMAGE
    # ========================================================

    product_image: (
        str
        | None
    ) = None

    representative_id = str(
        representative.get(
            "id"
        )
    )

    product_image = (
        image_map.get(
            representative_id
        )
    )

    if not product_image:

        # Prefer matched SKU image.
        for variant in variants:

            if (
                variant.get(
                    "matched"
                )
                and variant.get(
                    "image_url"
                )
            ):

                product_image = (
                    variant.get(
                        "image_url"
                    )
                )

                break

    if not product_image:

        for variant in variants:

            if variant.get(
                "image_url"
            ):

                product_image = (
                    variant.get(
                        "image_url"
                    )
                )

                break

    # ========================================================
    # CATEGORY
    # ========================================================

    category_id = (
        representative.get(
            "category_id"
        )
    )

    category = (
        category_map.get(
            str(
                category_id
            )
        )
        if category_id is not None
        else None
    )

    # ========================================================
    # MATCHED ROWS
    # ========================================================

    matched_rows = [
        row
        for row in rows
        if str(
            row.get(
                "id"
            )
        )
        in matched_sku_ids
    ]

    all_prices = [
        _effective_price(
            row
        )
        for row in rows
    ]

    matched_prices = [
        _effective_price(
            row
        )
        for row in matched_rows
    ]

    available_variants = [
        variant
        for variant in variants
        if variant.get(
            "in_stock"
        )
    ]

    matched_available = [
        variant
        for variant in variants
        if variant.get(
            "matched"
        )
        and variant.get(
            "in_stock"
        )
    ]

    product_match_score = max(
        (
            sku_match_scores.get(
                str(
                    row.get(
                        "id"
                    )
                ),
                0.0,
            )
            for row in rows
        ),
        default=0.0,
    )

    return {
        "product_key":
            product_key,

        "name":
            (
                _clean_optional_text(
                    representative.get(
                        "name"
                    )
                )
                or "Unnamed Product"
            ),

        "brand":
            _clean_optional_text(
                representative.get(
                    "brand"
                )
            ),

        "slug":
            _clean_optional_text(
                representative.get(
                    "slug"
                )
            ),

        "category_id":
            (
                str(
                    category_id
                )
                if category_id is not None
                else None
            ),

        "category":
            category,

        "description":
            _clean_optional_text(
                representative.get(
                    "description"
                )
            ),

        "short_description":
            _clean_optional_text(
                representative.get(
                    "short_description"
                )
            ),

        "image_url":
            product_image,

        "rating":
            _safe_float(
                representative.get(
                    "rating"
                ),
                default=0.0,
            ),

        "is_featured":
            any(
                bool(
                    row.get(
                        "is_featured"
                    )
                )
                for row in rows
            ),

        # ----------------------------------------------------
        # Variants
        # ----------------------------------------------------

        "variant_count":
            len(
                variants
            ),

        "variants":
            variants,

        "available_variant_count":
            len(
                available_variants
            ),

        "is_available":
            bool(
                available_variants
            ),

        "all_variants_out_of_stock":
            (
                len(
                    variants
                ) > 0
                and not bool(
                    available_variants
                )
            ),

        # ----------------------------------------------------
        # Price
        # ----------------------------------------------------

        "price_from":
            (
                float(
                    min(
                        all_prices
                    )
                )
                if all_prices
                else None
            ),

        "price_to":
            (
                float(
                    max(
                        all_prices
                    )
                )
                if all_prices
                else None
            ),

        # ----------------------------------------------------
        # Match
        # ----------------------------------------------------

        "matched_variant_count":
            len(
                matched_rows
            ),

        "matched_available_variant_count":
            len(
                matched_available
            ),

        "matched_sku_ids":
            [
                str(
                    row.get(
                        "id"
                    )
                )
                for row in matched_rows
            ],

        "matched_price_from":
            (
                float(
                    min(
                        matched_prices
                    )
                )
                if matched_prices
                else None
            ),

        "matched_price_to":
            (
                float(
                    max(
                        matched_prices
                    )
                )
                if matched_prices
                else None
            ),

        "search_score":
            round(
                product_match_score,
                4,
            ),
    }


# ============================================================
# FILTER VALIDATION
# ============================================================


def _validate_search_filters(
    *,
    min_price: float | None,
    max_price: float | None,
    limit: int,
) -> None:

    if (
        limit < 1
        or limit
        > MAX_SEARCH_RESULTS
    ):

        raise ProductValidationError(
            f"limit must be between 1 and {MAX_SEARCH_RESULTS}."
        )

    if (
        min_price is not None
        and min_price < 0
    ):

        raise ProductValidationError(
            "min_price cannot be negative."
        )

    if (
        max_price is not None
        and max_price < 0
    ):

        raise ProductValidationError(
            "max_price cannot be negative."
        )

    if (
        min_price is not None
        and max_price is not None
        and min_price
        > max_price
    ):

        raise ProductValidationError(
            "min_price cannot exceed max_price."
        )


# ============================================================
# SKU STRUCTURED FILTER
# ============================================================


def _sku_matches_non_text_filters(
    row: Mapping[
        str,
        Any,
    ],
    *,
    brand: str | None,
    category: str | None,
    category_map: Mapping[
        str,
        str,
    ],
    min_price: float | None,
    max_price: float | None,
    stock_state: StockState,
    on_sale_only: bool,
    featured_only: bool,
) -> bool:

    # ========================================================
    # BRAND
    # ========================================================

    if brand:

        brand_score = (
            SequenceMatcher(
                None,
                _search_key(
                    brand
                ),
                _search_key(
                    row.get(
                        "brand"
                    )
                ),
            ).ratio()
        )

        brand_query = (
            _search_key(
                brand
            )
        )

        row_brand = (
            _search_key(
                row.get(
                    "brand"
                )
            )
        )

        if not (
            brand_query
            == row_brand
            or (
                brand_query
                and brand_query
                in row_brand
            )
            or brand_score
            >= 0.78
        ):

            return False

    # ========================================================
    # CATEGORY
    # ========================================================

    if not _category_matches(
        category,
        row.get(
            "category_id"
        ),
        category_map,
    ):

        return False

    # ========================================================
    # PRICE
    # ========================================================

    price = (
        _effective_price(
            row
        )
    )

    if (
        min_price is not None
        and price
        < Decimal(
            str(
                min_price
            )
        )
    ):

        return False

    if (
        max_price is not None
        and price
        > Decimal(
            str(
                max_price
            )
        )
    ):

        return False

    # ========================================================
    # STOCK
    # ========================================================

    is_in_stock = (
        _is_in_stock(
            row
        )
    )

    if (
        stock_state
        == "in_stock"
        and not is_in_stock
    ):

        return False

    if (
        stock_state
        == "out_of_stock"
        and is_in_stock
    ):

        return False

    # ========================================================
    # SALE
    # ========================================================

    if (
        on_sale_only
        and _discount_price(
            row
        )
        is None
    ):

        return False

    # ========================================================
    # FEATURED
    # ========================================================

    if (
        featured_only
        and not bool(
            row.get(
                "is_featured"
            )
        )
    ):

        return False

    return True


# ============================================================
# PRODUCT SORTING
# ============================================================


def _relevance_rank(
    product: Mapping[
        str,
        Any,
    ],
) -> tuple[
    float,
    int,
    float,
    str,
]:

    score = (
        _safe_float(
            product.get(
                "search_score"
            )
        )
    )

    # Relevance first.
    #
    # Availability is a tie-breaker, NOT a filter.
    #
    # Therefore exact out-of-stock milk remains above an unrelated
    # in-stock product.

    availability_rank = (
        0
        if product.get(
            "is_available"
        )
        else 1
    )

    price = (
        product.get(
            "matched_price_from"
        )
    )

    if price is None:

        price = (
            product.get(
                "price_from"
            )
        )

    return (
        -score,
        availability_rank,
        _safe_float(
            price,
            default=float(
                "inf"
            ),
        ),
        _search_key(
            product.get(
                "name"
            )
        ),
    )


def _sort_products(
    products: list[
        dict[
            str,
            Any,
        ]
    ],
    *,
    sort_by: ProductSortField,
    sort_order: SortOrder,
) -> list[
    dict[
        str,
        Any,
    ]
]:

    if (
        sort_by
        == "relevance"
    ):

        return sorted(
            products,
            key=(
                _relevance_rank
            ),
        )

    reverse = (
        sort_order
        == "desc"
    )

    if (
        sort_by
        == "price"
    ):

        key = lambda product: (
            product.get(
                "matched_price_from"
            )
            if product.get(
                "matched_price_from"
            )
            is not None
            else (
                product.get(
                    "price_from"
                )
                if product.get(
                    "price_from"
                )
                is not None
                else float(
                    "inf"
                )
            )
        )

    elif (
        sort_by
        == "rating"
    ):

        key = lambda product: (
            _safe_float(
                product.get(
                    "rating"
                )
            )
        )

    elif (
        sort_by
        == "stock"
    ):

        key = lambda product: (
            sum(
                _safe_int(
                    variant.get(
                        "stock"
                    )
                )
                for variant
                in product.get(
                    "variants",
                    [],
                )
            )
        )

    else:

        key = lambda product: (
            _search_key(
                product.get(
                    "name"
                )
            )
        )

    return sorted(
        products,
        key=key,
        reverse=reverse,
    )


# ============================================================
# MAIN SEARCH
# ============================================================


def search_products(
    *,
    query: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,

    # IMPORTANT CHANGE:
    #
    # Normal searches include out-of-stock products.
    #
    # Only use "in_stock" when the user explicitly requests
    # available/in-stock products.
    stock_state: StockState = "any",

    on_sale_only: bool = False,
    featured_only: bool = False,
    sort_by: ProductSortField = "relevance",
    sort_order: SortOrder = "asc",
    limit: int = 20,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Search logical products.

    Examples:

        search_products(query="milk")
            -> returns milk even when stock=0

        search_products(query="mil")
            -> typo/prefix match to milk

        search_products(query="maggie")
            -> close match to Maggi

        search_products(
            query="milk",
            stock_state="in_stock",
        )
            -> only available milk SKUs qualify
    """

    _validate_search_filters(
        min_price=min_price,
        max_price=max_price,
        limit=limit,
    )

    if stock_state not in {
        "any",
        "in_stock",
        "out_of_stock",
    }:

        raise ProductValidationError(
            "stock_state must be 'any', 'in_stock' or 'out_of_stock'."
        )

    if sort_by not in {
        "relevance",
        "name",
        "price",
        "rating",
        "stock",
    }:

        raise ProductValidationError(
            "Invalid sort_by."
        )

    if sort_order not in {
        "asc",
        "desc",
    }:

        raise ProductValidationError(
            "sort_order must be 'asc' or 'desc'."
        )

    all_rows = (
        _fetch_active_sku_rows()
    )

    if not all_rows:

        return []

    category_map = (
        _get_category_map(
            row.get(
                "category_id"
            )
            for row in all_rows
        )
    )

    # ========================================================
    # SCORE / FILTER EACH SKU
    # ========================================================

    matched_rows: list[
        dict[
            str,
            Any,
        ]
    ] = []

    score_by_sku: dict[
        str,
        float,
    ] = {}

    for row in all_rows:

        score = (
            _query_match_score(
                row,
                query,
            )
        )

        if (
            query
            and score <= 0.0
        ):

            continue

        if not _sku_matches_non_text_filters(
            row,
            brand=brand,
            category=category,
            category_map=category_map,
            min_price=min_price,
            max_price=max_price,
            stock_state=stock_state,
            on_sale_only=on_sale_only,
            featured_only=featured_only,
        ):

            continue

        matched_rows.append(
            row
        )

        score_by_sku[
            str(
                row.get(
                    "id"
                )
            )
        ] = score

    if not matched_rows:

        return []

    # ========================================================
    # FIND MATCHED LOGICAL PRODUCTS
    # ========================================================

    matched_by_group = (
        _group_skus(
            matched_rows
        )
    )

    all_groups = (
        _group_skus(
            all_rows
        )
    )

    selected_groups = {
        product_key:
            all_groups[
                product_key
            ]
        for product_key
        in matched_by_group
        if product_key
        in all_groups
    }

    selected_sku_ids = [
        row.get(
            "id"
        )
        for rows
        in selected_groups.values()
        for row
        in rows
        if row.get(
            "id"
        )
        is not None
    ]

    image_map = (
        _get_images_by_sku(
            selected_sku_ids
        )
    )

    products: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for (
        product_key,
        rows,
    ) in selected_groups.items():

        matched_group_rows = (
            matched_by_group.get(
                product_key,
                [],
            )
        )

        matched_ids = {
            str(
                row.get(
                    "id"
                )
            )
            for row
            in matched_group_rows
        }

        product_scores = {
            sku_id:
                score_by_sku.get(
                    sku_id,
                    0.0,
                )
            for sku_id
            in matched_ids
        }

        product = (
            _build_product(
                product_key=product_key,
                rows=rows,
                matched_sku_ids=matched_ids,
                category_map=category_map,
                image_map=image_map,
                sku_match_scores=product_scores,
            )
        )

        products.append(
            product
        )

    products = (
        _sort_products(
            products,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    )

    return products[
        :limit
    ]


# ============================================================
# FALLBACK QUERY SIMILARITY
# ============================================================


def _product_query_similarity(
    *,
    query: str,
    product: Mapping[
        str,
        Any,
    ],
) -> float:
    """
    Broader lexical similarity used ONLY for suggestions.

    It does not make a product a true direct search match.
    """

    query_key = (
        _search_key(
            query
        )
    )

    if not query_key:

        return 0.0

    name = (
        _search_key(
            product.get(
                "name"
            )
        )
    )

    brand = (
        _search_key(
            product.get(
                "brand"
            )
        )
    )

    combined = " ".join(
        item
        for item in (
            brand,
            name,
        )
        if item
    )

    if not combined:

        return 0.0

    if query_key in combined:

        return 0.90

    phrase_score = (
        SequenceMatcher(
            None,
            query_key,
            combined,
        ).ratio()
    )

    query_tokens = (
        _search_tokens(
            query_key
        )
    )

    candidate_tokens = (
        _search_tokens(
            combined
        )
    )

    if not query_tokens:

        return phrase_score

    token_scores = [
        _best_token_score(
            token,
            candidate_tokens,
        )
        for token
        in query_tokens
    ]

    token_average = (
        sum(
            token_scores
        )
        / len(
            token_scores
        )
    )

    return max(
        phrase_score,
        token_average
        * 0.9,
    )


# ============================================================
# BUILD ALL LOGICAL PRODUCTS
# ============================================================


def _build_all_products(
    rows: Sequence[
        Mapping[
            str,
            Any,
        ]
    ],
) -> list[
    dict[
        str,
        Any,
    ]
]:

    if not rows:

        return []

    groups = (
        _group_skus(
            rows
        )
    )

    category_map = (
        _get_category_map(
            row.get(
                "category_id"
            )
            for row in rows
        )
    )

    image_map = (
        _get_images_by_sku(
            row.get(
                "id"
            )
            for row in rows
            if row.get(
                "id"
            )
            is not None
        )
    )

    products = []

    for (
        product_key,
        group_rows,
    ) in groups.items():

        ids = {
            str(
                row.get(
                    "id"
                )
            )
            for row
            in group_rows
        }

        products.append(
            _build_product(
                product_key=product_key,
                rows=group_rows,
                matched_sku_ids=ids,
                category_map=category_map,
                image_map=image_map,
                sku_match_scores={},
            )
        )

    return products


# ============================================================
# ALTERNATIVES BY CATEGORY
# ============================================================


def _alternatives_for_products(
    products: Sequence[
        Mapping[
            str,
            Any,
        ]
    ],
    *,
    limit: int,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Find IN-STOCK alternatives from the same category.

    Useful when exact product exists but is currently out of stock.
    """

    if not products:

        return []

    excluded_keys = {
        str(
            product.get(
                "product_key"
            )
        )
        for product
        in products
        if product.get(
            "product_key"
        )
    }

    category_ids = {
        str(
            product.get(
                "category_id"
            )
        )
        for product
        in products
        if product.get(
            "category_id"
        )
        is not None
    }

    if not category_ids:

        return []

    rows = (
        _fetch_active_sku_rows()
    )

    candidate_rows = [
        row
        for row in rows
        if (
            row.get(
                "category_id"
            )
            is not None
            and str(
                row.get(
                    "category_id"
                )
            )
            in category_ids
            and _is_in_stock(
                row
            )
            and _logical_product_key(
                row
            )
            not in excluded_keys
        )
    ]

    if not candidate_rows:

        return []

    candidates = (
        _build_all_products(
            candidate_rows
        )
    )

    candidates = [
        product
        for product
        in candidates
        if product.get(
            "is_available"
        )
    ]

    candidates.sort(
        key=lambda product: (
            -_safe_float(
                product.get(
                    "rating"
                )
            ),
            _safe_float(
                product.get(
                    "price_from"
                ),
                default=float(
                    "inf"
                ),
            ),
            _search_key(
                product.get(
                    "name"
                )
            ),
        )
    )

    return candidates[
        :limit
    ]


# ============================================================
# CLOSE ALTERNATIVES FOR NOT-FOUND QUERY
# ============================================================


def _close_query_alternatives(
    query: str,
    *,
    limit: int,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Suggest only reasonably close IN-STOCK catalog products.

    This is intentionally conservative.

    If no product is meaningfully close, return [] instead of suggesting
    random groceries.
    """

    query = str(
        query
        or ""
    ).strip()

    if not query:

        return []

    rows = (
        _fetch_active_sku_rows()
    )

    products = (
        _build_all_products(
            rows
        )
    )

    scored: list[
        tuple[
            float,
            dict[
                str,
                Any,
            ],
        ]
    ] = []

    for product in products:

        if not product.get(
            "is_available"
        ):

            continue

        score = (
            _product_query_similarity(
                query=query,
                product=product,
            )
        )

        if (
            score
            < ALTERNATIVE_FUZZY_THRESHOLD
        ):

            continue

        scored.append(
            (
                score,
                product,
            )
        )

    scored.sort(
        key=lambda item: (
            -item[
                0
            ],
            _search_key(
                item[
                    1
                ].get(
                    "name"
                )
            ),
        )
    )

    result: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for (
        score,
        product,
    ) in scored[
        :limit
    ]:

        item = dict(
            product
        )

        item[
            "alternative_score"
        ] = round(
            score,
            4,
        )

        result.append(
            item
        )

    return result


# ============================================================
# SEARCH WITH AVAILABILITY + SUGGESTIONS
# ============================================================


def search_products_with_suggestions(
    *,
    query: str | None = None,
    brand: str | None = None,
    category: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    stock_state: StockState = "any",
    on_sale_only: bool = False,
    featured_only: bool = False,
    sort_by: ProductSortField = "relevance",
    sort_order: SortOrder = "asc",
    limit: int = 20,
    alternative_limit: int = 5,
) -> dict[
    str,
    Any,
]:
    """
    Higher-level structured search result intended for product_tools.py.

    Possible statuses:

        found
        found_partially_available
        found_out_of_stock
        requested_in_stock_but_unavailable
        not_found

    This function DOES NOT generate conversational prose.

    The tool/Groq layer can render:

        "Amul Taaza Milk exists, but it is currently out of stock.
         Here are some alternatives..."
    """

    if (
        alternative_limit < 0
        or alternative_limit
        > MAX_ALTERNATIVE_RESULTS
    ):

        raise ProductValidationError(
            f"alternative_limit must be between 0 and "
            f"{MAX_ALTERNATIVE_RESULTS}."
        )

    # ========================================================
    # NORMAL REQUEST
    # ========================================================

    requested_products = (
        search_products(
            query=query,
            brand=brand,
            category=category,
            min_price=min_price,
            max_price=max_price,
            stock_state=stock_state,
            on_sale_only=on_sale_only,
            featured_only=featured_only,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
        )
    )

    # ========================================================
    # IF EXPLICIT IN-STOCK SEARCH RETURNED NOTHING,
    # CHECK WHETHER THE PRODUCT ACTUALLY EXISTS OUT OF STOCK.
    # ========================================================

    detected_products = list(
        requested_products
    )

    if (
        not requested_products
        and stock_state
        == "in_stock"
    ):

        detected_products = (
            search_products(
                query=query,
                brand=brand,
                category=category,
                min_price=min_price,
                max_price=max_price,
                stock_state="any",
                on_sale_only=on_sale_only,
                featured_only=featured_only,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
            )
        )

    available_products = [
        product
        for product
        in detected_products
        if product.get(
            "is_available"
        )
    ]

    unavailable_products = [
        product
        for product
        in detected_products
        if not product.get(
            "is_available"
        )
    ]

    # ========================================================
    # STATUS
    # ========================================================

    if detected_products:

        if (
            available_products
            and unavailable_products
        ):

            status = (
                "found_partially_available"
            )

        elif available_products:

            status = (
                "found"
            )

        else:

            if (
                stock_state
                == "in_stock"
            ):

                status = (
                    "requested_in_stock_but_unavailable"
                )

            else:

                status = (
                    "found_out_of_stock"
                )

    else:

        status = (
            "not_found"
        )

    # ========================================================
    # ALTERNATIVES
    # ========================================================

    alternatives: list[
        dict[
            str,
            Any,
        ]
    ] = []

    if (
        alternative_limit > 0
    ):

        if (
            detected_products
            and not available_products
        ):

            # Product exists but all variants unavailable.
            alternatives = (
                _alternatives_for_products(
                    detected_products,
                    limit=alternative_limit,
                )
            )

        elif not detected_products:

            # Nothing directly matched.
            alternatives = (
                _close_query_alternatives(
                    str(
                        query
                        or brand
                        or category
                        or ""
                    ),
                    limit=alternative_limit,
                )
            )

    return {
        "query":
            query,

        "status":
            status,

        "found":
            bool(
                detected_products
            ),

        "products":
            detected_products,

        "product_count":
            len(
                detected_products
            ),

        "available_product_count":
            len(
                available_products
            ),

        "unavailable_product_count":
            len(
                unavailable_products
            ),

        "all_matches_out_of_stock":
            (
                bool(
                    detected_products
                )
                and not bool(
                    available_products
                )
            ),

        "requested_stock_state":
            stock_state,

        "alternatives":
            alternatives,

        "alternative_count":
            len(
                alternatives
            ),

        # Machine-readable response hints.
        #
        # Tool/Groq can use these without inventing facts.
        "response_hint": (
            "show_products"
            if status
            in {
                "found",
                "found_partially_available",
            }
            else (
                "product_exists_but_currently_unavailable"
                if status
                in {
                    "found_out_of_stock",
                    "requested_in_stock_but_unavailable",
                }
                else (
                    "product_not_found"
                )
            )
        ),
    }


# ============================================================
# GET ONE SKU
# ============================================================


def get_sku_by_id(
    sku_id: str,
) -> dict[
    str,
    Any,
] | None:
    """
    Return one exact purchasable SKU.
    """

    row = (
        _fetch_sku_row(
            sku_id
        )
    )

    if row is None:

        return None

    image_map = (
        _get_images_by_sku(
            [
                row.get(
                    "id"
                )
            ]
        )
    )

    sku_string = str(
        row.get(
            "id"
        )
    )

    variant = (
        _build_variant(
            row,
            image_url=(
                image_map.get(
                    sku_string
                )
            ),
            matched=True,
            match_score=1.0,
        )
    )

    variant.update(
        {
            "product_key":
                _logical_product_key(
                    row
                ),

            "product_name":
                _clean_optional_text(
                    row.get(
                        "name"
                    )
                ),

            "brand":
                _clean_optional_text(
                    row.get(
                        "brand"
                    )
                ),

            "category_id":
                (
                    str(
                        row.get(
                            "category_id"
                        )
                    )
                    if row.get(
                        "category_id"
                    )
                    is not None
                    else None
                ),
        }
    )

    return variant


# ============================================================
# GET PRODUCT BY SKU
# ============================================================


def get_product_by_sku_id(
    sku_id: str,
) -> dict[
    str,
    Any,
] | None:

    target = (
        _fetch_sku_row(
            sku_id
        )
    )

    if target is None:

        return None

    product_key = (
        _logical_product_key(
            target
        )
    )

    rows = (
        _fetch_active_sku_rows()
    )

    group_rows = [
        row
        for row in rows
        if _logical_product_key(
            row
        )
        == product_key
    ]

    if not group_rows:

        return None

    category_map = (
        _get_category_map(
            row.get(
                "category_id"
            )
            for row in group_rows
        )
    )

    image_map = (
        _get_images_by_sku(
            row.get(
                "id"
            )
            for row in group_rows
        )
    )

    return _build_product(
        product_key=product_key,
        rows=group_rows,
        matched_sku_ids={
            str(
                sku_id
            )
        },
        category_map=category_map,
        image_map=image_map,
        sku_match_scores={
            str(
                sku_id
            ):
                1.0
        },
    )


# ============================================================
# GET PRODUCT BY KEY
# ============================================================


def get_product_by_key(
    product_key: str,
) -> dict[
    str,
    Any,
] | None:

    if not isinstance(
        product_key,
        str,
    ):

        raise ProductValidationError(
            "product_key must be text."
        )

    product_key = (
        product_key.strip()
    )

    if not product_key:

        raise ProductValidationError(
            "product_key cannot be empty."
        )

    requested_key = (
        _search_key(
            product_key
        )
        if "|"
        not in product_key
        else product_key
    )

    rows = (
        _fetch_active_sku_rows()
    )

    matching_rows = [
        row
        for row in rows
        if (
            _logical_product_key(
                row
            )
            == requested_key
            or _logical_product_key(
                row
            )
            == product_key
        )
    ]

    if not matching_rows:

        return None

    category_map = (
        _get_category_map(
            row.get(
                "category_id"
            )
            for row
            in matching_rows
        )
    )

    image_map = (
        _get_images_by_sku(
            row.get(
                "id"
            )
            for row
            in matching_rows
        )
    )

    ids = {
        str(
            row.get(
                "id"
            )
        )
        for row
        in matching_rows
    }

    return _build_product(
        product_key=(
            _logical_product_key(
                matching_rows[
                    0
                ]
            )
        ),
        rows=matching_rows,
        matched_sku_ids=ids,
        category_map=category_map,
        image_map=image_map,
        sku_match_scores={
            sku_id:
                1.0
            for sku_id
            in ids
        },
    )


# ============================================================
# VARIANTS
# ============================================================


def get_product_variants(
    product_key: str,
    *,
    in_stock_only: bool = False,
) -> list[
    dict[
        str,
        Any,
    ]
]:

    product = (
        get_product_by_key(
            product_key
        )
    )

    if product is None:

        return []

    variants = list(
        product.get(
            "variants",
            [],
        )
    )

    if in_stock_only:

        variants = [
            variant
            for variant
            in variants
            if variant.get(
                "in_stock"
            )
        ]

    return variants


# ============================================================
# RESOLVE VARIANT
# ============================================================


def resolve_variant(
    *,
    product_key: str,
    size: str,
    require_in_stock: bool = True,
) -> dict[
    str,
    Any,
] | None:

    if (
        not isinstance(
            size,
            str,
        )
        or not size.strip()
    ):

        raise ProductValidationError(
            "size cannot be empty."
        )

    variants = (
        get_product_variants(
            product_key,
            in_stock_only=False,
        )
    )

    if not variants:

        return None

    requested = (
        _search_key(
            size
        )
    )

    scored: list[
        tuple[
            float,
            dict[
                str,
                Any,
            ],
        ]
    ] = []

    for variant in variants:

        if (
            require_in_stock
            and not variant.get(
                "in_stock"
            )
        ):

            continue

        candidate = (
            _search_key(
                variant.get(
                    "size"
                )
            )
        )

        if requested == candidate:

            score = 1.0

        elif (
            requested
            and requested
            in candidate
        ):

            score = 0.95

        else:

            score = (
                SequenceMatcher(
                    None,
                    requested,
                    candidate,
                ).ratio()
            )

        if (
            score
            >= 0.78
        ):

            scored.append(
                (
                    score,
                    variant,
                )
            )

    if not scored:

        return None

    scored.sort(
        key=lambda item:
            -item[
                0
            ]
    )

    # Only return when best match is clearly deterministic.
    if (
        len(
            scored
        ) > 1
        and abs(
            scored[
                0
            ][
                0
            ]
            -
            scored[
                1
            ][
                0
            ]
        )
        < 0.04
    ):

        return None

    return scored[
        0
    ][
        1
    ]


# ============================================================
# CHEAPEST VARIANT
# ============================================================


def get_cheapest_variant(
    product_key: str,
    *,
    in_stock_only: bool = True,
) -> dict[
    str,
    Any,
] | None:

    variants = (
        get_product_variants(
            product_key,
            in_stock_only=(
                in_stock_only
            ),
        )
    )

    if not variants:

        return None

    return min(
        variants,
        key=lambda variant:
            _safe_decimal(
                variant.get(
                    "price"
                )
            ),
    )


# ============================================================
# MOST EXPENSIVE VARIANT
# ============================================================


def get_most_expensive_variant(
    product_key: str,
    *,
    in_stock_only: bool = True,
) -> dict[
    str,
    Any,
] | None:

    variants = (
        get_product_variants(
            product_key,
            in_stock_only=(
                in_stock_only
            ),
        )
    )

    if not variants:

        return None

    return max(
        variants,
        key=lambda variant:
            _safe_decimal(
                variant.get(
                    "price"
                )
            ),
    )


# ============================================================
# LIST BRANDS
# ============================================================


def list_brands(
    *,
    limit: int = 100,
) -> list[str]:

    if (
        limit < 1
        or limit
        > MAX_CATALOG_LIST_RESULTS
    ):

        raise ProductValidationError(
            f"limit must be between 1 and "
            f"{MAX_CATALOG_LIST_RESULTS}."
        )

    rows = (
        _fetch_active_sku_rows()
    )

    brands = {
        str(
            row.get(
                "brand"
            )
        ).strip()
        for row in rows
        if row.get(
            "brand"
        )
        and str(
            row.get(
                "brand"
            )
        ).strip()
    }

    return sorted(
        brands,
        key=str.lower,
    )[
        :limit
    ]


# ============================================================
# LIST CATEGORIES
# ============================================================


def list_categories(
    *,
    limit: int = 100,
) -> list[
    dict[
        str,
        str,
    ]
]:

    if (
        limit < 1
        or limit
        > MAX_CATALOG_LIST_RESULTS
    ):

        raise ProductValidationError(
            f"limit must be between 1 and "
            f"{MAX_CATALOG_LIST_RESULTS}."
        )

    rows = (
        _fetch_active_sku_rows()
    )

    category_ids = {
        str(
            row.get(
                "category_id"
            )
        )
        for row in rows
        if row.get(
            "category_id"
        )
        is not None
    }

    category_map = (
        _get_category_map(
            category_ids
        )
    )

    categories = [
        {
            "id":
                category_id,

            "name":
                name,
        }
        for (
            category_id,
            name,
        )
        in category_map.items()
    ]

    categories.sort(
        key=lambda item:
            item[
                "name"
            ].lower()
    )

    return categories[
        :limit
    ]


# ============================================================
# CATALOG STATS
# ============================================================


def get_catalog_stats() -> dict[
    str,
    Any,
]:

    rows = (
        _fetch_active_sku_rows()
    )

    groups = (
        _group_skus(
            rows
        )
    )

    in_stock_skus = sum(
        1
        for row
        in rows
        if _is_in_stock(
            row
        )
    )

    in_stock_products = sum(
        1
        for group_rows
        in groups.values()
        if any(
            _is_in_stock(
                row
            )
            for row
            in group_rows
        )
    )

    brands = {
        _search_key(
            row.get(
                "brand"
            )
        )
        for row in rows
        if _search_key(
            row.get(
                "brand"
            )
        )
    }

    category_ids = {
        str(
            row.get(
                "category_id"
            )
        )
        for row in rows
        if row.get(
            "category_id"
        )
        is not None
    }

    return {
        "source":
            "supabase",

        "product_count":
            len(
                groups
            ),

        "sku_count":
            len(
                rows
            ),

        "brand_count":
            len(
                brands
            ),

        "category_count":
            len(
                category_ids
            ),

        "in_stock_product_count":
            in_stock_products,

        "out_of_stock_product_count":
            (
                len(
                    groups
                )
                - in_stock_products
            ),

        "in_stock_sku_count":
            in_stock_skus,

        "out_of_stock_sku_count":
            (
                len(
                    rows
                )
                - in_stock_skus
            ),
    }


# ============================================================
# DETERMINISTIC SUBSTITUTES
# ============================================================


def find_substitutes(
    *,
    product_key: str,
    limit: int = 5,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Same-category in-stock substitutes.

    Semantic replacements still belong to RAG.
    """

    if (
        limit < 1
        or limit > 20
    ):

        raise ProductValidationError(
            "limit must be between 1 and 20."
        )

    product = (
        get_product_by_key(
            product_key
        )
    )

    if product is None:

        return []

    return _alternatives_for_products(
        [
            product
        ],
        limit=limit,
    )


# ============================================================
# DATABASE HEALTH
# ============================================================


def check_product_database() -> bool:

    try:

        client = (
            _get_catalog_client()
        )

        (
            client
            .table(
                PRODUCTS_TABLE
            )
            .select(
                "id"
            )
            .limit(
                1
            )
            .execute()
        )

        return True

    except Exception:

        logger.exception(
            "Product database health check failed."
        )

        return False