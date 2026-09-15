"""
tools/product_tools.py

Safe AI-callable product/catalog tools.

Architecture
------------

Cohere
    ↓
chooses a predefined tool
    ↓
this module validates arguments
    ↓
database/products.py
    ↓
Supabase
    ↓
verified structured result


IMPORTANT
---------

There is NO Python intent classifier here.

Cohere decides whether one of these tools should be used from the
function descriptions.

This module does not:

- execute raw SQL
- expose Supabase credentials
- allow arbitrary function execution
- calculate product facts using an LLM
- flatten SKU variants into fake separate products
"""

from __future__ import annotations

import copy
import logging
import math

from typing import (
    Any,
    Callable,
    Mapping,
)


# ============================================================
# DATABASE PRODUCT FUNCTIONS
# ============================================================

from database.products import (
    ProductDatabaseError,
    ProductValidationError,
    find_substitutes,
    get_catalog_stats,
    get_cheapest_variant,
    get_most_expensive_variant,
    get_product_by_key,
    get_product_by_sku_id,
    get_product_variants,
    get_sku_by_id,
    list_brands,
    list_categories,
    resolve_variant,
    search_products_with_suggestions,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# TOOL NAMES
# ============================================================

TOOL_SEARCH_PRODUCTS = (
    "search_products"
)

TOOL_GET_PRODUCT_DETAILS = (
    "get_product_details"
)

TOOL_GET_PRODUCT_VARIANTS = (
    "get_product_variants"
)

TOOL_RESOLVE_PRODUCT_VARIANT = (
    "resolve_product_variant"
)

TOOL_GET_CHEAPEST_VARIANT = (
    "get_cheapest_variant"
)

TOOL_GET_MOST_EXPENSIVE_VARIANT = (
    "get_most_expensive_variant"
)

TOOL_GET_SKU_DETAILS = (
    "get_sku_details"
)

TOOL_LIST_CATALOG_BRANDS = (
    "list_catalog_brands"
)

TOOL_LIST_CATALOG_CATEGORIES = (
    "list_catalog_categories"
)

TOOL_GET_CATALOG_STATS = (
    "get_catalog_stats"
)

TOOL_FIND_PRODUCT_SUBSTITUTES = (
    "find_product_substitutes"
)


# ============================================================
# LIMITS
# ============================================================

DEFAULT_PRODUCT_SEARCH_LIMIT = 8

MAX_PRODUCT_SEARCH_LIMIT = 20

DEFAULT_ALTERNATIVE_LIMIT = 4

MAX_ALTERNATIVE_LIMIT = 8

MAX_BRAND_LIST_LIMIT = 100

MAX_CATEGORY_LIST_LIMIT = 100

MAX_SUBSTITUTE_LIMIT = 10

MAX_QUERY_LENGTH = 300

MAX_TEXT_FILTER_LENGTH = 150

MAX_PRODUCT_KEY_LENGTH = 300

MAX_SKU_ID_LENGTH = 200


# ============================================================
# EXCEPTIONS
# ============================================================


class ProductToolError(
    RuntimeError
):
    pass


class ProductToolValidationError(
    ProductToolError
):
    pass


class ProductToolNotFoundError(
    ProductToolError
):
    pass


# ============================================================
# STANDARD RESULT
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
) -> dict[
    str,
    Any,
]:
    """
    Standard successful tool response.
    """

    result: dict[
        str,
        Any,
    ] = {
        "success":
            True,

        "source":
            "supabase",

        "domain":
            "catalog",

        "tool":
            tool,

        "data":
            data,
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
    message: str,
    error_code: str,
) -> dict[
    str,
    Any,
]:
    """
    Safe error result.

    Never expose raw database exceptions.
    """

    return {
        "success":
            False,

        "source":
            "supabase",

        "domain":
            "catalog",

        "tool":
            tool,

        "error": {
            "code":
                error_code,

            "message":
                message,
        },

        "data":
            None,
    }


# ============================================================
# ARGUMENT HELPERS
# ============================================================


def _arguments_mapping(
    arguments: Mapping[
        str,
        Any,
    ] | None,
) -> dict[
    str,
    Any,
]:

    if arguments is None:

        return {}

    if not isinstance(
        arguments,
        Mapping,
    ):

        raise ProductToolValidationError(
            "Tool arguments must be an object."
        )

    return dict(
        arguments
    )


def _optional_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str | None:

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

            raise ProductToolValidationError(
                f"{field_name} must be text."
            )

    value = (
        value.strip()
    )

    if not value:

        return None

    if (
        len(
            value
        )
        > max_length
    ):

        raise ProductToolValidationError(
            f"{field_name} is too long."
        )

    return value


def _required_string(
    value: Any,
    *,
    field_name: str,
    max_length: int,
) -> str:

    value = (
        _optional_string(
            value,
            field_name=(
                field_name
            ),
            max_length=(
                max_length
            ),
        )
    )

    if value is None:

        raise ProductToolValidationError(
            f"{field_name} is required."
        )

    return value


def _boolean(
    value: Any,
    *,
    field_name: str,
    default: bool,
) -> bool:

    if value is None:

        return default

    if isinstance(
        value,
        bool,
    ):

        return value

    if (
        isinstance(
            value,
            int,
        )
        and value
        in {
            0,
            1,
        }
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

    raise ProductToolValidationError(
        f"{field_name} must be true or false."
    )


def _integer(
    value: Any,
    *,
    field_name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:

    if value is None:

        return default

    if isinstance(
        value,
        bool,
    ):

        raise ProductToolValidationError(
            f"{field_name} must be an integer."
        )

    try:

        number = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise ProductToolValidationError(
            f"{field_name} must be an integer."
        ) from exc

    if number < minimum:

        raise ProductToolValidationError(
            f"{field_name} must be at least {minimum}."
        )

    if number > maximum:

        raise ProductToolValidationError(
            f"{field_name} cannot exceed {maximum}."
        )

    return number


def _price(
    value: Any,
    *,
    field_name: str,
) -> float | None:

    if value is None:

        return None

    if isinstance(
        value,
        bool,
    ):

        raise ProductToolValidationError(
            f"{field_name} must be numeric."
        )

    try:

        number = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise ProductToolValidationError(
            f"{field_name} must be numeric."
        ) from exc

    if not math.isfinite(
        number
    ):

        raise ProductToolValidationError(
            f"{field_name} must be finite."
        )

    if number < 0:

        raise ProductToolValidationError(
            f"{field_name} cannot be negative."
        )

    if number > 10_000_000:

        raise ProductToolValidationError(
            f"{field_name} is outside the supported range."
        )

    return number


def _enum_value(
    value: Any,
    *,
    field_name: str,
    allowed: set[str],
    default: str,
) -> str:

    if value is None:

        return default

    if not isinstance(
        value,
        str,
    ):

        raise ProductToolValidationError(
            f"{field_name} must be text."
        )

    value = (
        value
        .strip()
        .lower()
    )

    if value not in allowed:

        raise ProductToolValidationError(
            f"{field_name} must be one of: "
            f"{', '.join(sorted(allowed))}."
        )

    return value


# ============================================================
# SEARCH MESSAGE
# ============================================================


def _build_search_message(
    search_result: Mapping[
        str,
        Any,
    ],
) -> str:
    """
    Produce a factual machine-facing message from database status.

    Groq may rewrite it naturally later.
    """

    status = (
        str(
            search_result.get(
                "status"
            )
            or ""
        )
    )

    alternatives = list(
        search_result.get(
            "alternatives"
        )
        or []
    )

    if (
        status
        == "found"
    ):

        return (
            "Matching catalog products were found. "
            "Use the returned variants, prices, stock and images exactly."
        )

    if (
        status
        == "found_partially_available"
    ):

        return (
            "Matching products were found. "
            "Some matching products or variants are currently out of stock. "
            "Do not describe out-of-stock variants as unavailable from the "
            "catalog; they exist but cannot currently be purchased."
        )

    if (
        status
        in {
            "found_out_of_stock",
            "requested_in_stock_but_unavailable",
        }
    ):

        if alternatives:

            return (
                "The requested product exists in the catalog but is "
                "currently out of stock. "
                "In-stock alternative products are included in the result."
            )

        return (
            "The requested product exists in the catalog but is currently "
            "out of stock. No in-stock structured alternative was found."
        )

    if status == "not_found":

        if alternatives:

            return (
                "No sufficiently close direct catalog match was found. "
                "Possible in-stock alternatives are included separately. "
                "Do not claim an alternative is the exact requested product."
            )

        return (
            "No sufficiently close product match currently exists in the "
            "catalog."
        )

    return (
        "Catalog search completed."
    )


# ============================================================
# TOOL 1 — PRODUCT SEARCH
# ============================================================


def tool_search_products(
    *,
    query: Any = None,
    brand: Any = None,
    category: Any = None,
    min_price: Any = None,
    max_price: Any = None,

    # IMPORTANT:
    #
    # Generic product search must include out-of-stock catalog
    # products so that:
    #
    # "do you have milk?"
    #
    # can return:
    #
    # "Yes, Amul milk exists, but it is currently out of stock."
    #
    stock_state: Any = "any",

    on_sale_only: Any = False,
    featured_only: Any = False,
    sort_by: Any = "relevance",
    sort_order: Any = "asc",
    limit: Any = DEFAULT_PRODUCT_SEARCH_LIMIT,
    alternative_limit: Any = DEFAULT_ALTERNATIVE_LIMIT,
) -> dict[
    str,
    Any,
]:
    """
    Search real Supabase catalog.

    Supports:

        milk
        mil
        Amul milk
        maggie
        basmati rice
        milk under 100
        cheapest product
        most expensive product

    Search matching itself is performed by database/products.py.
    """

    tool_name = (
        TOOL_SEARCH_PRODUCTS
    )

    try:

        # ====================================================
        # NORMALIZE ARGUMENTS
        # ====================================================

        query = (
            _optional_string(
                query,
                field_name="query",
                max_length=(
                    MAX_QUERY_LENGTH
                ),
            )
        )

        brand = (
            _optional_string(
                brand,
                field_name="brand",
                max_length=(
                    MAX_TEXT_FILTER_LENGTH
                ),
            )
        )

        category = (
            _optional_string(
                category,
                field_name="category",
                max_length=(
                    MAX_TEXT_FILTER_LENGTH
                ),
            )
        )

        min_price = (
            _price(
                min_price,
                field_name=(
                    "min_price"
                ),
            )
        )

        max_price = (
            _price(
                max_price,
                field_name=(
                    "max_price"
                ),
            )
        )

        if (
            min_price is not None
            and max_price is not None
            and min_price > max_price
        ):

            raise ProductToolValidationError(
                "min_price cannot exceed max_price."
            )

        stock_state = (
            _enum_value(
                stock_state,
                field_name=(
                    "stock_state"
                ),
                allowed={
                    "any",
                    "in_stock",
                    "out_of_stock",
                },
                default="any",
            )
        )

        on_sale_only = (
            _boolean(
                on_sale_only,
                field_name=(
                    "on_sale_only"
                ),
                default=False,
            )
        )

        featured_only = (
            _boolean(
                featured_only,
                field_name=(
                    "featured_only"
                ),
                default=False,
            )
        )

        sort_by = (
            _enum_value(
                sort_by,
                field_name=(
                    "sort_by"
                ),
                allowed={
                    "relevance",
                    "name",
                    "price",
                    "rating",
                    "stock",
                },
                default="relevance",
            )
        )

        sort_order = (
            _enum_value(
                sort_order,
                field_name=(
                    "sort_order"
                ),
                allowed={
                    "asc",
                    "desc",
                },
                default="asc",
            )
        )

        limit = (
            _integer(
                limit,
                field_name=(
                    "limit"
                ),
                default=(
                    DEFAULT_PRODUCT_SEARCH_LIMIT
                ),
                minimum=1,
                maximum=(
                    MAX_PRODUCT_SEARCH_LIMIT
                ),
            )
        )

        alternative_limit = (
            _integer(
                alternative_limit,
                field_name=(
                    "alternative_limit"
                ),
                default=(
                    DEFAULT_ALTERNATIVE_LIMIT
                ),
                minimum=0,
                maximum=(
                    MAX_ALTERNATIVE_LIMIT
                ),
            )
        )

        # ====================================================
        # IMPORTANT CHEAPEST PRODUCT BEHAVIOR
        # ====================================================
        #
        # query is intentionally OPTIONAL.
        #
        # This allows:
        #
        #     "cheapest product"
        #
        # Cohere:
        #
        #     search_products(
        #         stock_state="in_stock",
        #         sort_by="price",
        #         sort_order="asc",
        #         limit=1
        #     )
        #
        # and:
        #
        #     "most expensive product"
        #
        #     sort_by="price"
        #     sort_order="desc"
        #
        # No fake query such as "*" is necessary.
        # ====================================================

        result = (
            search_products_with_suggestions(
                query=query,
                brand=brand,
                category=category,
                min_price=min_price,
                max_price=max_price,
                stock_state=stock_state,
                on_sale_only=(
                    on_sale_only
                ),
                featured_only=(
                    featured_only
                ),
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
                alternative_limit=(
                    alternative_limit
                ),
            )
        )

        products = list(
            result.get(
                "products"
            )
            or []
        )

        alternatives = list(
            result.get(
                "alternatives"
            )
            or []
        )

        total_variants = sum(
            int(
                product.get(
                    "variant_count",
                    0,
                )
                or 0
            )
            for product
            in products
        )

        matched_variants = sum(
            int(
                product.get(
                    "matched_variant_count",
                    0,
                )
                or 0
            )
            for product
            in products
        )

        return _success_result(
            tool=tool_name,

            # IMPORTANT:
            #
            # Return database response directly.
            #
            # brain.py / Groq can now see:
            #
            # status
            # products
            # alternatives
            # all_matches_out_of_stock
            # response_hint
            #
            data=result,

            message=(
                _build_search_message(
                    result
                )
            ),

            metadata={
                "status":
                    result.get(
                        "status"
                    ),

                "logical_product_count":
                    len(
                        products
                    ),

                "variant_count":
                    total_variants,

                "matched_variant_count":
                    matched_variants,

                "alternative_count":
                    len(
                        alternatives
                    ),

                "all_matches_out_of_stock":
                    bool(
                        result.get(
                            "all_matches_out_of_stock"
                        )
                    ),

                "filters": {
                    "query":
                        query,

                    "brand":
                        brand,

                    "category":
                        category,

                    "min_price":
                        min_price,

                    "max_price":
                        max_price,

                    "stock_state":
                        stock_state,

                    "on_sale_only":
                        on_sale_only,

                    "featured_only":
                        featured_only,

                    "sort_by":
                        sort_by,

                    "sort_order":
                        sort_order,

                    "limit":
                        limit,
                },
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_CATALOG_QUERY"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        logger.exception(
            "Product search database failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "The product catalog could not be searched right now."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected product search failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Product search could not be completed."
            ),
        )


# ============================================================
# TOOL 2 — PRODUCT DETAILS
# ============================================================


def tool_get_product_details(
    *,
    product_key: Any = None,
    sku_id: Any = None,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_PRODUCT_DETAILS
    )

    try:

        product_key = (
            _optional_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        sku_id = (
            _optional_string(
                sku_id,
                field_name=(
                    "sku_id"
                ),
                max_length=(
                    MAX_SKU_ID_LENGTH
                ),
            )
        )

        supplied = sum(
            value is not None
            for value in (
                product_key,
                sku_id,
            )
        )

        if supplied != 1:

            raise ProductToolValidationError(
                "Provide exactly one of product_key or sku_id."
            )

        if product_key:

            product = (
                get_product_by_key(
                    product_key
                )
            )

        else:

            product = (
                get_product_by_sku_id(
                    sku_id
                )
            )

        if product is None:

            return _success_result(
                tool=tool_name,

                data={
                    "product":
                        None,

                    "found":
                        False,
                },

                message=(
                    "The requested logical product was not found."
                ),

                metadata={
                    "found":
                        False,
                },
            )

        return _success_result(
            tool=tool_name,

            data={
                "product":
                    product,

                "found":
                    True,
            },

            message=(
                "Product details retrieved from the catalog."
            ),

            metadata={
                "found":
                    True,

                "variant_count":
                    product.get(
                        "variant_count",
                        0,
                    ),

                "is_available":
                    bool(
                        product.get(
                            "is_available"
                        )
                    ),

                "image_available":
                    bool(
                        product.get(
                            "image_url"
                        )
                    ),
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        logger.exception(
            "Product detail database failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Product details are temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected product detail failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Product details could not be retrieved."
            ),
        )


# ============================================================
# TOOL 3 — PRODUCT VARIANTS
# ============================================================


def tool_get_product_variants(
    *,
    product_key: Any,
    in_stock_only: Any = False,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_PRODUCT_VARIANTS
    )

    try:

        product_key = (
            _required_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        in_stock_only = (
            _boolean(
                in_stock_only,
                field_name=(
                    "in_stock_only"
                ),
                default=False,
            )
        )

        variants = (
            get_product_variants(
                product_key,
                in_stock_only=(
                    in_stock_only
                ),
            )
        )

        available_count = sum(
            1
            for variant
            in variants
            if variant.get(
                "in_stock"
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "product_key":
                    product_key,

                "variants":
                    variants,
            },

            message=(
                "Product variants retrieved."
                if variants
                else (
                    "No variants were found for that product."
                )
            ),

            metadata={
                "variant_count":
                    len(
                        variants
                    ),

                "available_variant_count":
                    available_count,

                "in_stock_only":
                    in_stock_only,
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Product variants are temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected variants failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Product variants could not be retrieved."
            ),
        )


# ============================================================
# TOOL 4 — RESOLVE VARIANT
# ============================================================


def tool_resolve_product_variant(
    *,
    product_key: Any,
    size: Any,
    require_in_stock: Any = True,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_RESOLVE_PRODUCT_VARIANT
    )

    try:

        product_key = (
            _required_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        size = (
            _required_string(
                size,
                field_name=(
                    "size"
                ),
                max_length=100,
            )
        )

        require_in_stock = (
            _boolean(
                require_in_stock,
                field_name=(
                    "require_in_stock"
                ),
                default=True,
            )
        )

        variant = (
            resolve_variant(
                product_key=(
                    product_key
                ),
                size=size,
                require_in_stock=(
                    require_in_stock
                ),
            )
        )

        if variant is None:

            all_variants = (
                get_product_variants(
                    product_key,
                    in_stock_only=False,
                )
            )

            eligible_variants = (
                [
                    item
                    for item
                    in all_variants
                    if item.get(
                        "in_stock"
                    )
                ]
                if require_in_stock
                else all_variants
            )

            return _success_result(
                tool=tool_name,

                data={
                    "resolved":
                        False,

                    "variant":
                        None,

                    "variants":
                        eligible_variants,

                    "all_variants":
                        all_variants,
                },

                message=(
                    "The requested size could not be resolved to one "
                    "eligible SKU. Ask the user to choose from the returned "
                    "variants instead of guessing."
                ),

                metadata={
                    "requires_clarification":
                        True,

                    "require_in_stock":
                        require_in_stock,
                },
            )

        return _success_result(
            tool=tool_name,

            data={
                "resolved":
                    True,

                "variant":
                    variant,
            },

            message=(
                "The product variant was resolved to an exact SKU."
            ),

            metadata={
                "requires_clarification":
                    False,
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Product variants could not be checked right now."
            ),
        )

    except Exception:

        logger.exception(
            "Variant resolution failed."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "The product variant could not be resolved."
            ),
        )


# ============================================================
# TOOL 5 — CHEAPEST VARIANT
# ============================================================


def tool_get_cheapest_variant(
    *,
    product_key: Any,
    in_stock_only: Any = True,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_CHEAPEST_VARIANT
    )

    try:

        product_key = (
            _required_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        in_stock_only = (
            _boolean(
                in_stock_only,
                field_name=(
                    "in_stock_only"
                ),
                default=True,
            )
        )

        variant = (
            get_cheapest_variant(
                product_key,
                in_stock_only=(
                    in_stock_only
                ),
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "product_key":
                    product_key,

                "variant":
                    variant,
            },

            message=(
                "Cheapest eligible variant retrieved."
                if variant
                else (
                    "No eligible variant was found."
                )
            ),

            metadata={
                "found":
                    variant is not None,

                "calculated_by":
                    "python",

                "in_stock_only":
                    in_stock_only,
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Variant pricing is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Cheapest variant failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "The cheapest variant could not be determined."
            ),
        )


# ============================================================
# TOOL 6 — MOST EXPENSIVE VARIANT
# ============================================================


def tool_get_most_expensive_variant(
    *,
    product_key: Any,
    in_stock_only: Any = True,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_MOST_EXPENSIVE_VARIANT
    )

    try:

        product_key = (
            _required_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        in_stock_only = (
            _boolean(
                in_stock_only,
                field_name=(
                    "in_stock_only"
                ),
                default=True,
            )
        )

        variant = (
            get_most_expensive_variant(
                product_key,
                in_stock_only=(
                    in_stock_only
                ),
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "product_key":
                    product_key,

                "variant":
                    variant,
            },

            message=(
                "Most expensive eligible variant retrieved."
                if variant
                else (
                    "No eligible variant was found."
                )
            ),

            metadata={
                "found":
                    variant is not None,

                "calculated_by":
                    "python",

                "in_stock_only":
                    in_stock_only,
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Variant pricing is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Most expensive variant failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "The most expensive variant could not be determined."
            ),
        )


# ============================================================
# TOOL 7 — EXACT SKU
# ============================================================


def tool_get_sku_details(
    *,
    sku_id: Any,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_SKU_DETAILS
    )

    try:

        sku_id = (
            _required_string(
                sku_id,
                field_name=(
                    "sku_id"
                ),
                max_length=(
                    MAX_SKU_ID_LENGTH
                ),
            )
        )

        sku = (
            get_sku_by_id(
                sku_id
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "sku":
                    sku,
            },

            message=(
                "SKU details retrieved."
                if sku
                else (
                    "The requested SKU was not found."
                )
            ),

            metadata={
                "found":
                    sku is not None,

                "in_stock":
                    (
                        bool(
                            sku.get(
                                "in_stock"
                            )
                        )
                        if sku
                        else False
                    ),

                "image_available":
                    (
                        bool(
                            sku.get(
                                "image_url"
                            )
                        )
                        if sku
                        else False
                    ),
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "SKU information is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "SKU details failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "SKU information could not be retrieved."
            ),
        )


# ============================================================
# TOOL 8 — BRANDS
# ============================================================


def tool_list_catalog_brands(
    *,
    limit: Any = 100,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_LIST_CATALOG_BRANDS
    )

    try:

        limit = (
            _integer(
                limit,
                field_name=(
                    "limit"
                ),
                default=100,
                minimum=1,
                maximum=(
                    MAX_BRAND_LIST_LIMIT
                ),
            )
        )

        brands = (
            list_brands(
                limit=limit
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "brands":
                    brands,
            },

            message=(
                "Catalog brands retrieved."
            ),

            metadata={
                "count":
                    len(
                        brands
                    ),
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Brand information is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Brand list failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Brand information could not be retrieved."
            ),
        )


# ============================================================
# TOOL 9 — CATEGORIES
# ============================================================


def tool_list_catalog_categories(
    *,
    limit: Any = 100,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_LIST_CATALOG_CATEGORIES
    )

    try:

        limit = (
            _integer(
                limit,
                field_name=(
                    "limit"
                ),
                default=100,
                minimum=1,
                maximum=(
                    MAX_CATEGORY_LIST_LIMIT
                ),
            )
        )

        categories = (
            list_categories(
                limit=limit
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "categories":
                    categories,
            },

            message=(
                "Catalog categories retrieved."
            ),

            metadata={
                "count":
                    len(
                        categories
                    ),
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Category information is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Category list failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Category information could not be retrieved."
            ),
        )


# ============================================================
# TOOL 10 — CATALOG STATS
# ============================================================


def tool_get_catalog_stats() -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_GET_CATALOG_STATS
    )

    try:

        stats = (
            get_catalog_stats()
        )

        return _success_result(
            tool=tool_name,

            data={
                "stats":
                    stats,
            },

            message=(
                "Catalog statistics retrieved."
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Catalog statistics are temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Catalog stats failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Catalog statistics could not be retrieved."
            ),
        )


# ============================================================
# TOOL 11 — STRUCTURED SUBSTITUTES
# ============================================================


def tool_find_product_substitutes(
    *,
    product_key: Any,
    limit: Any = 5,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        TOOL_FIND_PRODUCT_SUBSTITUTES
    )

    try:

        product_key = (
            _required_string(
                product_key,
                field_name=(
                    "product_key"
                ),
                max_length=(
                    MAX_PRODUCT_KEY_LENGTH
                ),
            )
        )

        limit = (
            _integer(
                limit,
                field_name=(
                    "limit"
                ),
                default=5,
                minimum=1,
                maximum=(
                    MAX_SUBSTITUTE_LIMIT
                ),
            )
        )

        products = (
            find_substitutes(
                product_key=(
                    product_key
                ),
                limit=limit,
            )
        )

        return _success_result(
            tool=tool_name,

            data={
                "products":
                    products,
            },

            message=(
                "Structured in-stock alternatives were found."
                if products
                else (
                    "No structured in-stock alternatives were found."
                )
            ),

            metadata={
                "count":
                    len(
                        products
                    ),

                "method":
                    "same_category",
            },
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except ProductDatabaseError:

        return _error_result(
            tool=tool_name,
            error_code=(
                "CATALOG_UNAVAILABLE"
            ),
            message=(
                "Product alternatives are temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Product substitute failure."
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Product alternatives could not be retrieved."
            ),
        )


# ============================================================
# COHERE PRODUCT TOOL SCHEMAS
# ============================================================
#
# Keep these descriptions compact.
#
# They are sent to Cohere on every relevant request.
# ============================================================

PRODUCT_TOOL_SCHEMAS: list[
    dict[
        str,
        Any,
    ]
] = [

    # ========================================================
    # SEARCH PRODUCTS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_SEARCH_PRODUCTS,

            "description": (
                "Search the real store catalog. Use for direct product "
                "queries such as milk, 'Amul milk', 'mil', 'maggie', price "
                "filters, product availability, cheapest product, most "
                "expensive product, brand/category filters and pack sizes. "
                "The search is typo tolerant. IMPORTANT: for a normal search "
                "or 'do you have X?', use stock_state='any' so existing "
                "out-of-stock products are still returned and identified as "
                "out of stock. Use stock_state='in_stock' only when the user "
                "explicitly wants only purchasable/in-stock results. "
                "The result already includes alternatives when the requested "
                "product is missing or fully out of stock, so normally STOP "
                "after this successful tool call. Do not call catalog stats, "
                "brand lists or category lists just to verify a search. "
                "For semantic requests such as 'healthy breakfast product', "
                "use semantic_product_search instead."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "query": {
                        "type":
                            "string",

                        "description": (
                            "Optional direct product text such as milk, "
                            "Amul milk, maggie, 500 ml milk or basmati rice. "
                            "May be omitted for catalog-wide ranking such as "
                            "'cheapest product'."
                        ),
                    },

                    "brand": {
                        "type":
                            "string",

                        "description":
                            "Optional brand filter.",
                    },

                    "category": {
                        "type":
                            "string",

                        "description":
                            "Optional category filter.",
                    },

                    "min_price": {
                        "type":
                            "number",

                        "minimum":
                            0,

                        "description":
                            "Optional minimum selling price.",
                    },

                    "max_price": {
                        "type":
                            "number",

                        "minimum":
                            0,

                        "description":
                            "Optional maximum selling price.",
                    },

                    "stock_state": {
                        "type":
                            "string",

                        "enum": [
                            "any",
                            "in_stock",
                            "out_of_stock",
                        ],

                        "description": (
                            "Default any. Use any for normal searches so "
                            "out-of-stock catalog products remain visible. "
                            "Use in_stock only for an explicit 'only available "
                            "now' request."
                        ),
                    },

                    "on_sale_only": {
                        "type":
                            "boolean",
                    },

                    "featured_only": {
                        "type":
                            "boolean",
                    },

                    "sort_by": {
                        "type":
                            "string",

                        "enum": [
                            "relevance",
                            "name",
                            "price",
                            "rating",
                            "stock",
                        ],

                        "description": (
                            "For 'cheapest product' use price. "
                            "For direct searches normally use relevance."
                        ),
                    },

                    "sort_order": {
                        "type":
                            "string",

                        "enum": [
                            "asc",
                            "desc",
                        ],

                        "description": (
                            "For cheapest use asc. "
                            "For most expensive use desc."
                        ),
                    },

                    "limit": {
                        "type":
                            "integer",

                        "minimum":
                            1,

                        "maximum":
                            MAX_PRODUCT_SEARCH_LIMIT,

                        "description": (
                            "Maximum logical products, not SKU rows. "
                            "For cheapest/most expensive single product "
                            "questions use 1."
                        ),
                    },

                    "alternative_limit": {
                        "type":
                            "integer",

                        "minimum":
                            0,

                        "maximum":
                            MAX_ALTERNATIVE_LIMIT,

                        "description": (
                            "Maximum alternatives when exact match is absent "
                            "or fully out of stock."
                        ),
                    },
                },

                # query is intentionally NOT required.
                #
                # Example:
                #
                # cheapest product
                # ->
                # {
                #   "stock_state": "in_stock",
                #   "sort_by": "price",
                #   "sort_order": "asc",
                #   "limit": 1
                # }

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # PRODUCT DETAILS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_PRODUCT_DETAILS,

            "description": (
                "Get one known logical product and all active SKU variants. "
                "Use only when product_key or sku_id is already known."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "sku_id": {
                        "type":
                            "string",
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # VARIANTS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_PRODUCT_VARIANTS,

            "description": (
                "Return sizes/pack variants of one known logical product. "
                "Do not use this when search_products already returned the "
                "variants unless the user specifically asks again."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "in_stock_only": {
                        "type":
                            "boolean",

                        "description": (
                            "False returns all active variants including "
                            "out-of-stock ones."
                        ),
                    },
                },

                "required": [
                    "product_key",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # RESOLVE EXACT VARIANT
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_RESOLVE_PRODUCT_VARIANT,

            "description": (
                "Resolve a size such as 1 litre or 500 ml to an exact SKU "
                "for a known logical product. Use before cart mutation when "
                "an exact SKU is not already known. Never invent an SKU."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "size": {
                        "type":
                            "string",
                    },

                    "require_in_stock": {
                        "type":
                            "boolean",
                    },
                },

                "required": [
                    "product_key",
                    "size",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # CHEAPEST VARIANT
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_CHEAPEST_VARIANT,

            "description": (
                "Find the cheapest SKU variant of one already-known logical "
                "product. For 'cheapest product in the entire store', use "
                "search_products with sort_by=price instead."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "in_stock_only": {
                        "type":
                            "boolean",
                    },
                },

                "required": [
                    "product_key",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # MOST EXPENSIVE VARIANT
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_MOST_EXPENSIVE_VARIANT,

            "description": (
                "Find the most expensive SKU variant of one already-known "
                "logical product. For most expensive product in the whole "
                "store, use search_products sorted by price descending."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "in_stock_only": {
                        "type":
                            "boolean",
                    },
                },

                "required": [
                    "product_key",
                ],

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # EXACT SKU DETAILS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_SKU_DETAILS,

            "description": (
                "Retrieve current price, stock, size, image and logical "
                "product information for one exact known SKU."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "sku_id": {
                        "type":
                            "string",
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
    # BRANDS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_LIST_CATALOG_BRANDS,

            "description": (
                "List brands represented in the catalog. Use only when the "
                "user actually asks which brands are carried."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "limit": {
                        "type":
                            "integer",

                        "minimum":
                            1,

                        "maximum":
                            MAX_BRAND_LIST_LIMIT,
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # CATEGORIES
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_LIST_CATALOG_CATEGORIES,

            "description": (
                "List grocery categories. Use only when the user actually "
                "asks to see categories."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "limit": {
                        "type":
                            "integer",

                        "minimum":
                            1,

                        "maximum":
                            MAX_CATEGORY_LIST_LIMIT,
                    },
                },

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # CATALOG STATS
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_GET_CATALOG_STATS,

            "description": (
                "Return catalog counts such as product, SKU, brand, category "
                "and stock counts. Use only when the user asks for catalog "
                "statistics or counts. Never call this to verify whether one "
                "specific product exists."
            ),

            "parameters": {

                "type":
                    "object",

                "properties":
                    {},

                "additionalProperties":
                    False,
            },
        },
    },


    # ========================================================
    # SUBSTITUTES
    # ========================================================

    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_FIND_PRODUCT_SUBSTITUTES,

            "description": (
                "Find same-category in-stock alternatives for one known "
                "logical product. Normal search_products already returns "
                "alternatives automatically when a matching product is fully "
                "out of stock, so do not call this redundantly. For semantic "
                "requests such as healthier alternatives use RAG."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "product_key": {
                        "type":
                            "string",
                    },

                    "limit": {
                        "type":
                            "integer",

                        "minimum":
                            1,

                        "maximum":
                            MAX_SUBSTITUTE_LIMIT,
                    },
                },

                "required": [
                    "product_key",
                ],

                "additionalProperties":
                    False,
            },
        },
    },
]


# ============================================================
# TOOL REGISTRY
# ============================================================

ProductToolFunction = Callable[
    ...,
    dict[
        str,
        Any,
    ],
]


PRODUCT_TOOL_FUNCTIONS: dict[
    str,
    ProductToolFunction,
] = {

    TOOL_SEARCH_PRODUCTS:
        tool_search_products,

    TOOL_GET_PRODUCT_DETAILS:
        tool_get_product_details,

    TOOL_GET_PRODUCT_VARIANTS:
        tool_get_product_variants,

    TOOL_RESOLVE_PRODUCT_VARIANT:
        tool_resolve_product_variant,

    TOOL_GET_CHEAPEST_VARIANT:
        tool_get_cheapest_variant,

    TOOL_GET_MOST_EXPENSIVE_VARIANT:
        tool_get_most_expensive_variant,

    TOOL_GET_SKU_DETAILS:
        tool_get_sku_details,

    TOOL_LIST_CATALOG_BRANDS:
        tool_list_catalog_brands,

    TOOL_LIST_CATALOG_CATEGORIES:
        tool_list_catalog_categories,

    TOOL_GET_CATALOG_STATS:
        tool_get_catalog_stats,

    TOOL_FIND_PRODUCT_SUBSTITUTES:
        tool_find_product_substitutes,
}


# ============================================================
# SCHEMAS
# ============================================================


def get_product_tool_schemas() -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Defensive copy of schemas for Cohere.
    """

    return copy.deepcopy(
        PRODUCT_TOOL_SCHEMAS
    )


# ============================================================
# TOOL NAMES
# ============================================================


def get_product_tool_names() -> set[
    str
]:
    """
    Approved product tool names.
    """

    return set(
        PRODUCT_TOOL_FUNCTIONS.keys()
    )


# ============================================================
# TOOL CHECK
# ============================================================


def is_product_tool(
    tool_name: str,
) -> bool:

    if not isinstance(
        tool_name,
        str,
    ):

        return False

    return (
        tool_name.strip()
        in PRODUCT_TOOL_FUNCTIONS
    )


# ============================================================
# SAFE EXECUTOR
# ============================================================


def execute_product_tool(
    *,
    tool_name: str,
    arguments: Mapping[
        str,
        Any,
    ] | None = None,
) -> dict[
    str,
    Any,
]:
    """
    Execute one allow-listed product tool.

    There is no eval(), exec() or dynamic function loading.
    """

    if not isinstance(
        tool_name,
        str,
    ):

        raise ProductToolNotFoundError(
            "Product tool name must be text."
        )

    tool_name = (
        tool_name.strip()
    )

    if not tool_name:

        raise ProductToolNotFoundError(
            "Product tool name is empty."
        )

    function = (
        PRODUCT_TOOL_FUNCTIONS.get(
            tool_name
        )
    )

    if function is None:

        logger.warning(
            "Rejected unknown product tool: %s",
            tool_name,
        )

        raise ProductToolNotFoundError(
            f"Unsupported product tool: {tool_name}"
        )

    try:

        arguments_dict = (
            _arguments_mapping(
                arguments
            )
        )

    except ProductToolValidationError as exc:

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    try:

        logger.debug(
            "Executing approved product tool. "
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

        # Usually an unsupported/missing model argument.
        #
        # Do not expose the raw Python error.

        logger.warning(
            "Product tool argument mismatch. tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INVALID_ARGUMENTS"
            ),
            message=(
                "The selected product tool received unsupported or "
                "incomplete arguments."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected product tool execution failure. tool=%s",
            tool_name,
        )

        return _error_result(
            tool=tool_name,
            error_code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "The product operation could not be completed."
            ),
        )