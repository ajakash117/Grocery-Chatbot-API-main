"""
rag/rag.py

Semantic product retrieval for Grocery Chatbot.

Architecture
------------

User semantic request
    ↓
Cohere selects semantic_product_search
    ↓
product_descriptions
    ↓
Cohere rerank + metadata ranking
    ↓
logical_product_key
    ↓
database/products.py
    ↓
live SKU / price / stock / image
    ↓
compact verified RAG result


Examples
--------

"I need something good for breakfast"

"healthy evening snack"

"something for tea time"

"something for a smoothie"

"something useful for baking"

"party snacks under 100"


IMPORTANT
---------

RAG is NEVER authoritative for:

- price
- stock
- SKU
- cart
- orders

Those facts always come from database/products.py.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import threading
import time
import unicodedata

from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)


# ============================================================
# LIGHTWEIGHT SEMANTIC RANKING
# ============================================================

# IMPORTANT:
# Do not import sentence-transformers / torch in this runtime.
# Render Free is limited to 512 MB and loading a local transformer
# model can terminate the web process. Semantic quality is preserved
# by using Cohere Rerank remotely, while lexical + metadata scoring
# remains available as a deterministic fallback.


# ============================================================
# CONFIG
# ============================================================

from config import settings


# ============================================================
# SUPABASE
# ============================================================

from database.supabase import (
    create_public_client,
)


# ============================================================
# LIVE PRODUCT DATABASE
# ============================================================

from database.products import (
    ProductDatabaseError,
    get_product_by_key,
    search_products,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# TABLE
# ============================================================

DESCRIPTION_TABLE = (
    "product_descriptions"
)


# ============================================================
# SEMANTIC RERANK
# ============================================================

# Remote reranking preserves semantic retrieval quality without
# loading sentence-transformers / PyTorch into the Render process.
#
# The model can be overridden at deploy time with:
#
#     COHERE_RERANK_MODEL=rerank-v3.5
#
# No config.py change is required because this module reads the
# optional environment variable directly.
DEFAULT_COHERE_RERANK_MODEL = "rerank-v4.0-fast"
FALLBACK_COHERE_RERANK_MODEL = "rerank-v3.5"

# Product descriptions are compact. Sending the active rows to
# Cohere Rerank keeps semantic recall high and avoids aggressive
# lexical pre-filtering that could hurt recommendation quality.
RERANK_TOP_N_MULTIPLIER = 4
MIN_RERANK_RESULTS = 40
MAX_RERANK_RESULTS = 200

# A normalized semantic score is used for the existing hybrid
# weighting. This raw-score floor prevents an unrelated top result
# from passing only because it was normalized to 1.0.
MIN_RAW_RERANK_SCORE = 0.01


# ============================================================
# CACHE
# ============================================================

DESCRIPTION_CACHE_TTL_SECONDS = (
    5 * 60
)


_description_cache_lock = (
    threading.RLock()
)


_description_cache_rows: (
    list[
        dict[
            str,
            Any,
        ]
    ]
    | None
) = None


_description_cache_time: float = 0.0


# Cohere rerank client is created lazily only when semantic search
# is actually used. Keeping this client lightweight avoids importing
# any local ML runtime.
_rerank_client_lock = (
    threading.RLock()
)

_rerank_client: Any = None

# Cache the first model that succeeds so a compatibility fallback is
# not retried on every request.
_active_rerank_model: str | None = None


# ============================================================
# LIMITS
# ============================================================

MAX_QUERY_LENGTH = 1000

DEFAULT_TOP_K = 5

MAX_TOP_K = 10


# We retrieve more semantic candidates than final results because
# hydration / stock / price filters can remove some candidates.
MIN_DESCRIPTION_CANDIDATES = 15

MAX_DESCRIPTION_CANDIDATES = 40


MAX_RAG_ROWS = 500

MAX_TOOL_PRICE = 10_000_000


# Keep Groq context compact.
MAX_CONTEXT_MATCHES = 5

MAX_CONTEXT_VARIANTS = 6

MAX_CONTEXT_TAGS = 8

MAX_DESCRIPTION_CONTEXT_LENGTH = 450

MAX_HEALTH_CONTEXT_LENGTH = 350


# ============================================================
# DOMAINS
# ============================================================

ALLOWED_DOMAINS = {
    "food",
    "food-beverage",
    "personal-care",
    "household-care",
    "baby-care",
}


# ============================================================
# TOOL NAME
# ============================================================

TOOL_SEMANTIC_PRODUCT_SEARCH = (
    "semantic_product_search"
)


# ============================================================
# EXCEPTIONS
# ============================================================


class RAGError(
    RuntimeError
):
    pass


class RAGConfigurationError(
    RAGError
):
    pass


class RAGDatabaseError(
    RAGError
):
    pass


class RAGEmbeddingError(
    RAGError
):
    pass


class RAGValidationError(
    RAGError
):
    pass


class RAGToolNotFoundError(
    RAGError
):
    pass


# ============================================================
# COHERE RERANK CLIENT
# ============================================================


def _secret_text(
    value: Any,
) -> str:
    """
    Convert a normal string or Pydantic SecretStr-like value to text
    without logging the secret.
    """

    if value is None:

        return ""

    getter = getattr(
        value,
        "get_secret_value",
        None,
    )

    if callable(
        getter
    ):

        try:

            value = getter()

        except Exception:

            return ""

    return str(
        value
    ).strip()


def _cohere_api_key() -> str:
    """
    Read the already-configured Cohere API key.

    Prefer settings.cohere_api_key so this stays aligned with the
    rest of the backend. The environment fallback keeps this module
    robust if config.py exposes a different representation.
    """

    value = getattr(
        settings,
        "cohere_api_key",
        None,
    )

    api_key = (
        _secret_text(
            value
        )
    )

    if api_key:

        return api_key

    return str(
        os.getenv(
            "COHERE_API_KEY",
            "",
        )
    ).strip()


def _configured_rerank_model() -> str:
    """
    Return an explicitly configured rerank model, if one exists.
    """

    configured = str(
        os.getenv(
            "COHERE_RERANK_MODEL",
            "",
        )
    ).strip()

    if configured:

        return configured

    settings_value = getattr(
        settings,
        "cohere_rerank_model",
        None,
    )

    return (
        _secret_text(
            settings_value
        )
    )


def _cohere_rerank_model() -> str:
    """
    Return the model that should be reported/used first.

    An explicit environment/config value always wins. Otherwise prefer
    Cohere's fast current rerank model, with a cached compatibility
    fallback if the current model is unavailable to the account.
    """

    configured = (
        _configured_rerank_model()
    )

    if configured:

        return configured

    if _active_rerank_model:

        return _active_rerank_model

    return DEFAULT_COHERE_RERANK_MODEL


def _rerank_model_candidates() -> list[str]:
    """
    Build the small model fallback chain.

    If the deploy explicitly sets COHERE_RERANK_MODEL we respect it
    exactly and do not silently substitute another model.
    """

    configured = (
        _configured_rerank_model()
    )

    if configured:

        return [
            configured
        ]

    if _active_rerank_model:

        return [
            _active_rerank_model
        ]

    return [
        DEFAULT_COHERE_RERANK_MODEL,
        FALLBACK_COHERE_RERANK_MODEL,
    ]


def _get_rerank_client() -> Any:
    """
    Create Cohere ClientV2 lazily.

    Importing Cohere is inexpensive compared with importing a local
    transformer model. The cached client is safe to reuse between
    semantic-search calls.
    """

    global _rerank_client

    if _rerank_client is not None:

        return _rerank_client

    api_key = (
        _cohere_api_key()
    )

    if not api_key:

        raise RAGConfigurationError(
            "Cohere API key is not configured for semantic ranking."
        )

    with _rerank_client_lock:

        if _rerank_client is not None:

            return _rerank_client

        try:

            import cohere

            client = (
                cohere.ClientV2(
                    api_key=api_key
                )
            )

        except Exception as exc:

            logger.exception(
                "Unable to initialize Cohere rerank client."
            )

            raise RAGEmbeddingError(
                "Semantic ranking client could not be initialized."
            ) from exc

        _rerank_client = client

        return _rerank_client


# ============================================================
# RERANK DOCUMENT
# ============================================================


def _rerank_document_text(
    row: Mapping[
        str,
        Any,
    ],
) -> str:
    """
    Build one compact semantic document for Cohere Rerank.

    rag_text is preferred when present because it was prepared
    specifically for retrieval. Otherwise the same structured
    product-description fields are composed locally.
    """

    rag_text = str(
        row.get(
            "rag_text"
        )
        or ""
    ).strip()

    if rag_text:

        return rag_text

    return (
        _build_rag_text(
            row
        )
    )


# ============================================================
# REMOTE SEMANTIC SCORES
# ============================================================


def _cohere_rerank_scores(
    *,
    query: str,
    rows: Sequence[
        Mapping[
            str,
            Any,
        ]
    ],
    candidate_limit: int,
) -> tuple[
    dict[
        int,
        float,
    ],
    str,
]:
    """
    Return semantic relevance scores indexed by the original row index.

    The call is remote, so the web process does not load PyTorch or a
    sentence-transformer model. This directly fixes the Render 512 MB
    out-of-memory failure while retaining model-based semantic ranking.

    If Cohere Rerank is temporarily unavailable or rate-limited, return
    an empty score map and let the existing lexical + metadata ranking
    act as a deterministic fallback instead of failing the whole search.
    """

    if not rows:

        return (
            {},
            "empty",
        )

    documents: list[
        str
    ] = []

    original_indices: list[
        int
    ] = []

    for index, row in enumerate(
        rows
    ):

        document = (
            _rerank_document_text(
                row
            )
        )

        if not document:

            continue

        documents.append(
            document
        )

        original_indices.append(
            index
        )

    if not documents:

        return (
            {},
            "no_documents",
        )

    top_n = min(
        len(
            documents
        ),
        MAX_RERANK_RESULTS,
        max(
            MIN_RERANK_RESULTS,
            candidate_limit
            * RERANK_TOP_N_MULTIPLIER,
        ),
    )

    try:

        client = (
            _get_rerank_client()
        )

        global _active_rerank_model

        response = None
        last_model_error: Exception | None = None

        for model_name in (
            _rerank_model_candidates()
        ):

            try:

                response = (
                    client.rerank(
                        model=model_name,
                        query=query,
                        documents=documents,
                        top_n=top_n,
                    )
                )

                _active_rerank_model = (
                    model_name
                )

                break

            except Exception as exc:

                last_model_error = exc

                status_code = getattr(
                    exc,
                    "status_code",
                    None,
                )

                # Only try the compatibility model when the preferred
                # model ID is unavailable. Rate limits, auth failures,
                # network errors, etc. should fall back locally instead
                # of making another unnecessary API call.
                if (
                    status_code != 404
                    or model_name
                    == _rerank_model_candidates()[
                        -1
                    ]
                ):

                    raise

                logger.warning(
                    "Cohere rerank model unavailable; trying compatibility "
                    "fallback. model=%s",
                    model_name,
                )

        if response is None:

            if last_model_error is not None:

                raise last_model_error

            raise RAGEmbeddingError(
                "Semantic rerank returned no response."
            )

        results = (
            getattr(
                response,
                "results",
                None,
            )
            or []
        )

        scores: dict[
            int,
            float,
        ] = {}

        for result in results:

            result_index = getattr(
                result,
                "index",
                None,
            )

            relevance_score = getattr(
                result,
                "relevance_score",
                None,
            )

            try:

                document_index = int(
                    result_index
                )

                score = float(
                    relevance_score
                )

            except (
                TypeError,
                ValueError,
            ):

                continue

            if (
                document_index < 0
                or document_index
                >= len(
                    original_indices
                )
                or not math.isfinite(
                    score
                )
            ):

                continue

            original_index = (
                original_indices[
                    document_index
                ]
            )

            scores[
                original_index
            ] = max(
                0.0,
                min(
                    score,
                    1.0,
                ),
            )

        return (
            scores,
            "cohere_rerank",
        )

    except Exception:

        logger.exception(
            "Cohere rerank unavailable; using lexical/metadata fallback."
        )

        return (
            {},
            "lexical_metadata_fallback",
        )


# ============================================================
# NORMALIZE RERANK SCORES
# ============================================================


def _normalize_rerank_scores(
    scores: Mapping[
        int,
        float,
    ],
) -> dict[
    int,
    float,
]:
    """
    Normalize returned Cohere scores to [0, 1] for the existing hybrid
    weighting.

    The raw score is also retained separately by the ranking function,
    so a tiny unrelated score cannot pass only because it normalized
    to 1.0.
    """

    if not scores:

        return {}

    maximum = max(
        (
            float(
                value
            )
            for value in scores.values()
            if math.isfinite(
                float(
                    value
                )
            )
        ),
        default=0.0,
    )

    if maximum <= 0:

        return {
            int(
                key
            ):
                0.0
            for key in scores
        }

    return {
        int(
            key
        ):
            max(
                0.0,
                min(
                    float(
                        value
                    )
                    / maximum,
                    1.0,
                ),
            )
        for key, value in scores.items()
    }


# ============================================================
# ARRAY TEXT
# ============================================================


def _array_values(
    value: Any,
) -> list[str]:

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
            str(
                item
            ).strip()
            for item in value
            if item is not None
            and str(
                item
            ).strip()
        ]

    if value is None:

        return []

    value = (
        str(
            value
        ).strip()
    )

    return (
        [
            value
        ]
        if value
        else []
    )


# ============================================================
# BUILD RAG DOCUMENT
# ============================================================


def _build_rag_text(
    row: Mapping[
        str,
        Any,
    ],
) -> str:

    sections = [
        (
            "Product: "
            + str(
                row.get(
                    "product_name"
                )
                or ""
            )
        ),
        (
            "Brand: "
            + str(
                row.get(
                    "brand"
                )
                or ""
            )
        ),
        (
            "Category: "
            + str(
                row.get(
                    "category_slug"
                )
                or ""
            )
        ),
        (
            "Domain: "
            + str(
                row.get(
                    "product_domain"
                )
                or ""
            )
        ),
        str(
            row.get(
                "description"
            )
            or ""
        ),
        (
            "Semantic tags: "
            + ", ".join(
                _array_values(
                    row.get(
                        "semantic_tags"
                    )
                )
            )
        ),
        (
            "Use cases: "
            + ", ".join(
                _array_values(
                    row.get(
                        "use_cases"
                    )
                )
            )
        ),
        (
            "Meal contexts: "
            + ", ".join(
                _array_values(
                    row.get(
                        "meal_contexts"
                    )
                )
            )
        ),
        (
            "Health tags: "
            + ", ".join(
                _array_values(
                    row.get(
                        "health_tags"
                    )
                )
            )
        ),
        (
            "Health context: "
            + str(
                row.get(
                    "health_context"
                )
                or ""
            )
        ),
    ]

    return "\n".join(
        section
        for section in sections
        if section.strip()
    ).strip()


# ============================================================
# DESCRIPTION ROW KEY
# ============================================================


def _description_cache_key(
    row: Mapping[
        str,
        Any,
    ],
) -> str:

    row_id = (
        row.get(
            "id"
        )
    )

    if row_id is not None:

        return (
            "id:"
            + str(
                row_id
            )
        )

    return (
        "key:"
        + str(
            row.get(
                "logical_product_key"
            )
            or row.get(
                "product_name"
            )
            or ""
        )
    )


# ============================================================
# FETCH DESCRIPTION ROWS
# ============================================================


def _fetch_description_rows(
    *,
    force_refresh: bool = False,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Read active product_descriptions using the public client.

    Normal RAG requests do NOT use SUPABASE_SECRET_KEY.
    """

    global _description_cache_rows
    global _description_cache_time

    now = (
        time.monotonic()
    )

    with _description_cache_lock:

        if (
            not force_refresh
            and _description_cache_rows
            is not None
            and (
                now
                - _description_cache_time
            )
            < DESCRIPTION_CACHE_TTL_SECONDS
        ):

            return [
                dict(
                    row
                )
                for row
                in _description_cache_rows
            ]

    client = (
        create_public_client()
    )

    try:

        response = (
            client
            .table(
                DESCRIPTION_TABLE
            )
            .select(
                "id,"
                "logical_product_key,"
                "product_name,"
                "brand,"
                "category_id,"
                "category_slug,"
                "product_domain,"
                "description,"
                "semantic_tags,"
                "use_cases,"
                "meal_contexts,"
                "health_tags,"
                "health_context,"
                "rag_text"
            )
            .eq(
                "is_active",
                True,
            )
            .limit(
                MAX_RAG_ROWS
            )
            .execute()
        )

    except Exception as exc:

        logger.exception(
            "Unable to read product_descriptions."
        )

        raise RAGDatabaseError(
            "Semantic product descriptions could not be retrieved."
        ) from exc

    rows = [
        dict(
            row
        )
        for row in (
            response.data
            or []
        )
        if isinstance(
            row,
            Mapping,
        )
    ]

    with _description_cache_lock:

        _description_cache_rows = [
            dict(
                row
            )
            for row in rows
        ]

        _description_cache_time = (
            time.monotonic()
        )

    return rows


# ============================================================
# CLEAR CACHE
# ============================================================


def clear_rag_cache() -> None:

    global _description_cache_rows
    global _description_cache_time

    with _description_cache_lock:

        _description_cache_rows = None

        _description_cache_time = 0.0



# ============================================================
# SEMANTIC RUNTIME NOTE
# ============================================================

# Local document embeddings are intentionally not generated at request
# time. Cohere Rerank scores the description rows remotely, while the
# lexical / metadata layers below remain local and deterministic.


# ============================================================
# TEXT NORMALIZATION
# ============================================================


_QUERY_STOPWORDS = {
    "i",
    "me",
    "my",
    "we",
    "our",
    "a",
    "an",
    "the",
    "for",
    "to",
    "of",
    "from",
    "with",
    "some",
    "something",
    "anything",
    "give",
    "show",
    "want",
    "need",
    "looking",
    "look",
    "find",
    "suggest",
    "recommend",
    "product",
    "products",
    "option",
    "options",
    "good",
    "nice",
}


def _normalize_text(
    value: Any,
) -> str:

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
        text.lower()
    )

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text,
    )

    return (
        " ".join(
            text.split()
        )
    )


def _text_tokens(
    value: Any,
    *,
    remove_stopwords: bool = True,
) -> set[str]:

    text = (
        _normalize_text(
            value
        )
    )

    if not text:

        return set()

    tokens = {
        token
        for token in text.split()
        if len(
            token
        ) > 1
    }

    if remove_stopwords:

        tokens = {
            token
            for token in tokens
            if token
            not in _QUERY_STOPWORDS
        }

    return tokens


# ============================================================
# ROW SEARCH TEXT
# ============================================================


def _row_search_text(
    row: Mapping[
        str,
        Any,
    ],
) -> str:

    values: list[
        str
    ] = []

    for field in (
        "product_name",
        "brand",
        "category_slug",
        "product_domain",
        "description",
        "health_context",
    ):

        value = (
            row.get(
                field
            )
        )

        if value:

            values.append(
                str(
                    value
                )
            )

    for field in (
        "semantic_tags",
        "use_cases",
        "meal_contexts",
        "health_tags",
    ):

        values.extend(
            _array_values(
                row.get(
                    field
                )
            )
        )

    return " ".join(
        values
    )


# ============================================================
# LEXICAL SCORE
# ============================================================


def _lexical_score(
    query: str,
    row: Mapping[
        str,
        Any,
    ],
) -> float:
    """
    Query-token coverage across all semantic row text.

    Example:

        "I need something good for breakfast"

    important query token:
        breakfast

    row meal_context:
        breakfast

    -> high lexical score
    """

    query_tokens = (
        _text_tokens(
            query
        )
    )

    if not query_tokens:

        return 0.0

    row_tokens = (
        _text_tokens(
            _row_search_text(
                row
            ),
            remove_stopwords=False,
        )
    )

    if not row_tokens:

        return 0.0

    overlap = (
        query_tokens
        & row_tokens
    )

    return min(
        (
            len(
                overlap
            )
            / max(
                len(
                    query_tokens
                ),
                1,
            )
        ),
        1.0,
    )


# ============================================================
# STRUCTURED METADATA SCORE
# ============================================================


def _metadata_score(
    query: str,
    row: Mapping[
        str,
        Any,
    ],
) -> float:
    """
    Give extra value to exact matches in purpose-built semantic fields:

        semantic_tags
        use_cases
        meal_contexts
        health_tags

    This makes queries such as:

        breakfast
        tea time
        smoothie
        baking
        party snack

    reliable even when embedding similarity between very short phrases
    is imperfect.
    """

    query_tokens = (
        _text_tokens(
            query
        )
    )

    if not query_tokens:

        return 0.0

    metadata_tokens: set[
        str
    ] = set()

    for field in (
        "semantic_tags",
        "use_cases",
        "meal_contexts",
        "health_tags",
    ):

        for value in (
            _array_values(
                row.get(
                    field
                )
            )
        ):

            metadata_tokens.update(
                _text_tokens(
                    value,
                    remove_stopwords=False,
                )
            )

    if not metadata_tokens:

        return 0.0

    overlap = (
        query_tokens
        & metadata_tokens
    )

    return min(
        len(
            overlap
        )
        / max(
            len(
                query_tokens
            ),
            1,
        ),
        1.0,
    )


# ============================================================
# NAME SCORE
# ============================================================


def _name_score(
    query: str,
    row: Mapping[
        str,
        Any,
    ],
) -> float:

    query_tokens = (
        _text_tokens(
            query
        )
    )

    if not query_tokens:

        return 0.0

    identity = " ".join(
        [
            str(
                row.get(
                    "brand"
                )
                or ""
            ),
            str(
                row.get(
                    "product_name"
                )
                or ""
            ),
        ]
    )

    identity_tokens = (
        _text_tokens(
            identity,
            remove_stopwords=False,
        )
    )

    if not identity_tokens:

        return 0.0

    overlap = (
        query_tokens
        & identity_tokens
    )

    return min(
        len(
            overlap
        )
        / max(
            len(
                query_tokens
            ),
            1,
        ),
        1.0,
    )


# ============================================================
# DOMAIN VALIDATION
# ============================================================


def _normalize_domains(
    domains: Sequence[
        str
    ] | None,
) -> set[str] | None:

    if domains is None:

        return None

    if isinstance(
        domains,
        (
            str,
            bytes,
        ),
    ):

        domains = [
            str(
                domains
            )
        ]

    result: set[
        str
    ] = set()

    for domain in domains:

        value = (
            str(
                domain
            )
            .strip()
            .lower()
        )

        if not value:

            continue

        if (
            value
            not in ALLOWED_DOMAINS
        ):

            raise RAGValidationError(
                f"Unsupported product domain: {value}"
            )

        result.add(
            value
        )

    return (
        result
        if result
        else None
    )


# ============================================================
# QUERY VALIDATION
# ============================================================


def _validate_query(
    query: Any,
) -> str:

    if not isinstance(
        query,
        str,
    ):

        raise RAGValidationError(
            "Semantic query must be text."
        )

    query = (
        query.strip()
    )

    if not query:

        raise RAGValidationError(
            "Semantic query cannot be empty."
        )

    if (
        len(
            query
        )
        > MAX_QUERY_LENGTH
    ):

        raise RAGValidationError(
            "Semantic query is too long."
        )

    return query


# ============================================================
# TOP K
# ============================================================


def _validate_top_k(
    value: Any,
) -> int:

    if value is None:

        value = (
            settings.rag_match_count
        )

    try:

        value = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise RAGValidationError(
            "top_k must be an integer."
        ) from exc

    if (
        value < 1
        or value > MAX_TOP_K
    ):

        raise RAGValidationError(
            f"top_k must be between 1 and {MAX_TOP_K}."
        )

    return value


# ============================================================
# THRESHOLD
# ============================================================


def _validate_similarity_threshold(
    value: Any,
) -> float:

    if value is None:

        value = (
            settings
            .rag_similarity_threshold
        )

    try:

        value = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise RAGValidationError(
            "similarity_threshold must be numeric."
        ) from exc

    if not (
        0.0
        <= value
        <= 1.0
    ):

        raise RAGValidationError(
            "similarity_threshold must be between 0 and 1."
        )

    return value


# ============================================================
# PRICE VALIDATION
# ============================================================


def _optional_price(
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

        raise RAGValidationError(
            f"{field_name} must be numeric."
        )

    try:

        result = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise RAGValidationError(
            f"{field_name} must be numeric."
        ) from exc

    if (
        not math.isfinite(
            result
        )
        or result < 0
        or result
        > MAX_TOOL_PRICE
    ):

        raise RAGValidationError(
            f"{field_name} is outside the supported range."
        )

    return result


# ============================================================
# RANK DESCRIPTION CANDIDATES
# ============================================================


def _rank_description_candidates(
    *,
    query: str,
    candidate_limit: int,
    similarity_threshold: float,
    domains: Sequence[
        str
    ] | None,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Hybrid semantic retrieval without a local ML model.

    Ranking keeps the same four-part design used by the previous
    implementation:

        semantic relevance   72%
        lexical coverage     16%
        structured metadata  10%
        product-name overlap  2%

    The semantic relevance now comes from Cohere Rerank rather than
    sentence-transformers. This keeps model-based retrieval quality
    while avoiding PyTorch/model memory inside the Render instance.

    If the rerank endpoint is temporarily unavailable, lexical and
    metadata signals still provide a deterministic fallback.
    """

    normalized_domains = (
        _normalize_domains(
            domains
        )
    )

    rows = (
        _fetch_description_rows()
    )

    if normalized_domains:

        rows = [
            row
            for row in rows
            if (
                str(
                    row.get(
                        "product_domain"
                    )
                    or ""
                )
                .strip()
                .lower()
                in normalized_domains
            )
        ]

    if not rows:

        return []

    raw_rerank_scores, ranking_source = (
        _cohere_rerank_scores(
            query=query,
            rows=rows,
            candidate_limit=(
                candidate_limit
            ),
        )
    )

    normalized_rerank_scores = (
        _normalize_rerank_scores(
            raw_rerank_scores
        )
    )

    scored: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for index, row in enumerate(
        rows
    ):

        lexical = (
            _lexical_score(
                query,
                row,
            )
        )

        metadata = (
            _metadata_score(
                query,
                row,
            )
        )

        name_score = (
            _name_score(
                query,
                row,
            )
        )

        raw_rerank = float(
            raw_rerank_scores.get(
                index,
                0.0,
            )
        )

        semantic = float(
            normalized_rerank_scores.get(
                index,
                0.0,
            )
        )

        # If Cohere is unavailable, retain useful retrieval instead
        # of failing the tool. Metadata remains especially strong
        # because product_descriptions was designed for these semantic
        # use-cases (meal context, health tags, use cases, etc.).
        if (
            ranking_source
            != "cohere_rerank"
        ):

            semantic = max(
                lexical,
                metadata,
                name_score
                * 0.80,
            )

        relevance = (
            semantic
            * 0.72
            +
            lexical
            * 0.16
            +
            metadata
            * 0.10
            +
            name_score
            * 0.02
        )

        # ====================================================
        # MATCH ACCEPTANCE
        # ====================================================
        #
        # Existing threshold semantics are retained by applying
        # them to the normalized model score. A small raw-score
        # floor prevents an unrelated top document from passing
        # only because normalization made it 1.0.
        #
        # Exact structured metadata / lexical matches can still
        # rescue short queries such as:
        #
        # breakfast
        # tea time
        # smoothie
        # baking
        # dairy
        # ====================================================

        if (
            ranking_source
            == "cohere_rerank"
        ):

            semantic_pass = (
                raw_rerank
                >= MIN_RAW_RERANK_SCORE
                and semantic
                >= similarity_threshold
            )

        else:

            semantic_pass = (
                semantic
                >= max(
                    0.50,
                    similarity_threshold,
                )
            )

        lexical_pass = (
            lexical
            >= 0.50
        )

        metadata_pass = (
            metadata
            >= 0.50
        )

        name_pass = (
            name_score
            >= 0.75
        )

        if not (
            semantic_pass
            or lexical_pass
            or metadata_pass
            or name_pass
        ):

            continue

        scored.append(
            {
                "id":
                    (
                        str(
                            row.get(
                                "id"
                            )
                        )
                        if row.get(
                            "id"
                        )
                        is not None
                        else None
                    ),

                "logical_product_key":
                    row.get(
                        "logical_product_key"
                    ),

                "product_name":
                    row.get(
                        "product_name"
                    ),

                "brand":
                    row.get(
                        "brand"
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

                "category_slug":
                    row.get(
                        "category_slug"
                    ),

                "product_domain":
                    row.get(
                        "product_domain"
                    ),

                "description":
                    row.get(
                        "description"
                    ),

                "semantic_tags":
                    _array_values(
                        row.get(
                            "semantic_tags"
                        )
                    ),

                "use_cases":
                    _array_values(
                        row.get(
                            "use_cases"
                        )
                    ),

                "meal_contexts":
                    _array_values(
                        row.get(
                            "meal_contexts"
                        )
                    ),

                "health_tags":
                    _array_values(
                        row.get(
                            "health_tags"
                        )
                    ),

                "health_context":
                    row.get(
                        "health_context"
                    ),

                # Keep the existing field name for compatibility
                # with callers that already inspect "similarity".
                "similarity":
                    round(
                        semantic,
                        6,
                    ),

                # Raw Cohere score is useful for diagnostics but is
                # not authoritative product data.
                "rerank_score":
                    round(
                        raw_rerank,
                        6,
                    ),

                "semantic_backend":
                    ranking_source,

                "lexical_score":
                    round(
                        lexical,
                        6,
                    ),

                "metadata_score":
                    round(
                        metadata,
                        6,
                    ),

                "name_score":
                    round(
                        name_score,
                        6,
                    ),

                "relevance_score":
                    round(
                        relevance,
                        6,
                    ),
            }
        )

    scored.sort(
        key=lambda item: (
            -float(
                item.get(
                    "relevance_score",
                    0,
                )
            ),
            -float(
                item.get(
                    "similarity",
                    0,
                )
            ),
            -float(
                item.get(
                    "rerank_score",
                    0,
                )
            ),
            str(
                item.get(
                    "product_name"
                )
                or ""
            ).lower(),
        )
    )

    return scored[
        :candidate_limit
    ]


# ============================================================
# PUBLIC DESCRIPTION SEARCH
# ============================================================


def search_descriptions(
    *,
    query: str,
    top_k: int | None = None,
    similarity_threshold: float | None = None,
    domains: Sequence[
        str
    ] | None = None,
) -> list[
    dict[
        str,
        Any,
    ]
]:
    """
    Search product_descriptions only.

    For normal application usage, use retrieve_products().
    """

    query = (
        _validate_query(
            query
        )
    )

    top_k = (
        _validate_top_k(
            top_k
        )
    )

    similarity_threshold = (
        _validate_similarity_threshold(
            similarity_threshold
        )
    )

    return (
        _rank_description_candidates(
            query=query,
            candidate_limit=top_k,
            similarity_threshold=(
                similarity_threshold
            ),
            domains=domains,
        )
    )


# ============================================================
# LIVE PRODUCT HYDRATION
# ============================================================


def _find_live_product(
    match: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
] | None:
    """
    Hydrate one semantic description into the real logical product.
    """

    logical_key = str(
        match.get(
            "logical_product_key"
        )
        or ""
    ).strip()

    # ========================================================
    # PRIMARY: LOGICAL PRODUCT KEY
    # ========================================================

    if logical_key:

        try:

            product = (
                get_product_by_key(
                    logical_key
                )
            )

            if product is not None:

                return product

        except ProductDatabaseError:

            raise

        except Exception:

            logger.warning(
                "RAG logical product lookup failed. key=%s",
                logical_key,
            )

    # ========================================================
    # FALLBACK: PRODUCT NAME + BRAND
    # ========================================================

    product_name = str(
        match.get(
            "product_name"
        )
        or ""
    ).strip()

    brand = str(
        match.get(
            "brand"
        )
        or ""
    ).strip()

    if not product_name:

        return None

    products = (
        search_products(
            query=product_name,
            brand=(
                brand
                or None
            ),
            stock_state="any",
            limit=5,
        )
    )

    if not products:

        return None

    target_name = (
        _normalize_text(
            product_name
        )
    )

    target_brand = (
        _normalize_text(
            brand
        )
    )

    for product in products:

        product_name_value = (
            _normalize_text(
                product.get(
                    "name"
                )
            )
        )

        product_brand_value = (
            _normalize_text(
                product.get(
                    "brand"
                )
            )
        )

        if (
            product_name_value
            == target_name
            and (
                not target_brand
                or product_brand_value
                == target_brand
            )
        ):

            return product

    return products[
        0
    ]


# ============================================================
# ELIGIBLE VARIANTS
# ============================================================


def _eligible_variants(
    product: Mapping[
        str,
        Any,
    ],
    *,
    in_stock_only: bool,
    min_price: float | None,
    max_price: float | None,
) -> list[
    dict[
        str,
        Any,
    ]
]:

    variants = (
        product.get(
            "variants"
        )
    )

    if (
        not isinstance(
            variants,
            Sequence,
        )
        or isinstance(
            variants,
            (
                str,
                bytes,
            ),
        )
    ):

        return []

    results: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for variant in variants:

        if not isinstance(
            variant,
            Mapping,
        ):

            continue

        if (
            in_stock_only
            and not bool(
                variant.get(
                    "in_stock"
                )
            )
        ):

            continue

        try:

            price = float(
                variant.get(
                    "price"
                )
            )

        except (
            TypeError,
            ValueError,
        ):

            continue

        if (
            not math.isfinite(
                price
            )
        ):

            continue

        if (
            min_price is not None
            and price
            < min_price
        ):

            continue

        if (
            max_price is not None
            and price
            > max_price
        ):

            continue

        results.append(
            dict(
                variant
            )
        )

    results.sort(
        key=lambda variant: (
            float(
                variant.get(
                    "price"
                )
                or 0
            ),
            str(
                variant.get(
                    "size"
                )
                or ""
            ),
        )
    )

    return results


# ============================================================
# COMPACT VARIANT
# ============================================================


def _compact_variant(
    variant: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

    return {
        "sku_id":
            variant.get(
                "sku_id"
            ),

        "size":
            variant.get(
                "size"
            ),

        "pack_size":
            variant.get(
                "pack_size"
            ),

        "price":
            variant.get(
                "price"
            ),

        "mrp":
            variant.get(
                "mrp"
            ),

        "stock":
            variant.get(
                "stock"
            ),

        "in_stock":
            bool(
                variant.get(
                    "in_stock"
                )
            ),

        "currency":
            variant.get(
                "currency"
            ),

        "image_url":
            variant.get(
                "image_url"
            ),
    }


# ============================================================
# RETRIEVE LIVE PRODUCTS
# ============================================================


def retrieve_products(
    *,
    query: str,
    top_k: int | None = None,
    similarity_threshold: float | None = None,
    domains: Sequence[
        str
    ] | None = None,
    in_stock_only: bool = True,
    min_price: float | None = None,
    max_price: float | None = None,
) -> dict[
    str,
    Any,
]:
    """
    Semantic retrieval + live catalog hydration.

    Important improvement:

    If top_k=5, we do NOT retrieve only 5 description candidates.

    We first retrieve a larger candidate pool, because some semantic
    matches may later disappear due to stock/price filtering.
    """

    query = (
        _validate_query(
            query
        )
    )

    top_k = (
        _validate_top_k(
            top_k
        )
    )

    threshold = (
        _validate_similarity_threshold(
            similarity_threshold
        )
    )

    normalized_domains = (
        _normalize_domains(
            domains
        )
    )

    min_price = (
        _optional_price(
            min_price,
            field_name=(
                "min_price"
            ),
        )
    )

    max_price = (
        _optional_price(
            max_price,
            field_name=(
                "max_price"
            ),
        )
    )

    if (
        min_price is not None
        and max_price is not None
        and min_price
        > max_price
    ):

        raise RAGValidationError(
            "min_price cannot exceed max_price."
        )

    if not isinstance(
        in_stock_only,
        bool,
    ):

        raise RAGValidationError(
            "in_stock_only must be boolean."
        )

    candidate_limit = min(
        MAX_DESCRIPTION_CANDIDATES,
        max(
            MIN_DESCRIPTION_CANDIDATES,
            top_k
            * 5,
        ),
    )

    semantic_candidates = (
        _rank_description_candidates(
            query=query,
            candidate_limit=(
                candidate_limit
            ),
            similarity_threshold=(
                threshold
            ),
            domains=(
                list(
                    normalized_domains
                )
                if normalized_domains
                else None
            ),
        )
    )

    matches: list[
        dict[
            str,
            Any,
        ]
    ] = []

    unavailable_matches: list[
        dict[
            str,
            Any,
        ]
    ] = []

    hydrated_product_keys: set[
        str
    ] = set()

    for candidate in (
        semantic_candidates
    ):

        try:

            live_product = (
                _find_live_product(
                    candidate
                )
            )

        except ProductDatabaseError as exc:

            raise RAGDatabaseError(
                "Live product information could not be retrieved."
            ) from exc

        if live_product is None:

            logger.warning(
                "RAG description did not map to catalog product. "
                "product=%s",
                candidate.get(
                    "product_name"
                ),
            )

            continue

        product_key = str(
            live_product.get(
                "product_key"
            )
            or ""
        )

        if (
            product_key
            and product_key
            in hydrated_product_keys
        ):

            continue

        if product_key:

            hydrated_product_keys.add(
                product_key
            )

        eligible = (
            _eligible_variants(
                live_product,
                in_stock_only=(
                    in_stock_only
                ),
                min_price=(
                    min_price
                ),
                max_price=(
                    max_price
                ),
            )
        )

        # ====================================================
        # ELIGIBLE RESULT
        # ====================================================

        if eligible:

            matches.append(
                {
                    "semantic_match":
                        candidate,

                    "live_product":
                        live_product,

                    "eligible_variants":
                        eligible,
                }
            )

            if (
                len(
                    matches
                )
                >= top_k
            ):

                break

            continue

        # ====================================================
        # NO STRUCTURED FILTERS
        # ====================================================

        if (
            not in_stock_only
            and min_price is None
            and max_price is None
        ):

            all_variants = list(
                live_product.get(
                    "variants"
                )
                or []
            )

            matches.append(
                {
                    "semantic_match":
                        candidate,

                    "live_product":
                        live_product,

                    "eligible_variants":
                        all_variants,
                }
            )

            if (
                len(
                    matches
                )
                >= top_k
            ):

                break

            continue

        # ====================================================
        # OUT OF STOCK SEMANTIC MATCH
        #
        # Keep a small fallback list so we can distinguish:
        #
        # "no semantic match exists"
        #
        # from:
        #
        # "relevant product exists but is unavailable"
        # ====================================================

        if (
            in_stock_only
            and not bool(
                live_product.get(
                    "is_available"
                )
            )
            and len(
                unavailable_matches
            )
            < top_k
        ):

            unavailable_matches.append(
                {
                    "semantic_match":
                        candidate,

                    "live_product":
                        live_product,

                    "eligible_variants":
                        [],
                }
            )

    # ========================================================
    # STATUS
    # ========================================================

    if matches:

        status = (
            "found"
        )

    elif unavailable_matches:

        status = (
            "only_out_of_stock"
        )

    else:

        status = (
            "no_match"
        )

    return {
        "source":
            "product_descriptions+products",

        "status":
            status,

        "query":
            query,

        "match_count":
            len(
                matches
            ),

        "unavailable_match_count":
            len(
                unavailable_matches
            ),

        "filters": {
            "domains":
                (
                    sorted(
                        normalized_domains
                    )
                    if normalized_domains
                    else None
                ),

            "in_stock_only":
                in_stock_only,

            "min_price":
                min_price,

            "max_price":
                max_price,

            "similarity_threshold":
                threshold,
        },

        "matches":
            matches,

        "unavailable_matches":
            unavailable_matches,
    }


# ============================================================
# SHORT TEXT
# ============================================================


def _short_text(
    value: Any,
    *,
    limit: int,
) -> str | None:

    if value is None:

        return None

    text = (
        str(
            value
        ).strip()
    )

    if not text:

        return None

    if (
        len(
            text
        )
        <= limit
    ):

        return text

    return (
        text[
            :limit
        ]
        + "..."
    )


# ============================================================
# COMPACT MATCH
# ============================================================


def _compact_match(
    item: Mapping[
        str,
        Any,
    ],
    *,
    unavailable: bool = False,
) -> dict[
    str,
    Any,
]:

    semantic = (
        item.get(
            "semantic_match"
        )
        or {}
    )

    product = (
        item.get(
            "live_product"
        )
        or {}
    )

    variants = (
        item.get(
            "eligible_variants"
        )
        or []
    )

    # If product is unavailable, show its existing variants so final
    # response can correctly say "this exists but is currently out of
    # stock".
    if (
        unavailable
        and not variants
    ):

        variants = (
            product.get(
                "variants"
            )
            or []
        )

    compact_variants = [
        _compact_variant(
            variant
        )
        for variant in variants[
            :MAX_CONTEXT_VARIANTS
        ]
        if isinstance(
            variant,
            Mapping,
        )
    ]

    return {
        "product_key":
            product.get(
                "product_key"
            ),

        "product_name":
            product.get(
                "name"
            ),

        "brand":
            product.get(
                "brand"
            ),

        "category":
            product.get(
                "category"
            ),

        "image_url":
            product.get(
                "image_url"
            ),

        "is_available":
            bool(
                product.get(
                    "is_available"
                )
            ),

        "available_variant_count":
            product.get(
                "available_variant_count"
            ),

        "semantic_description":
            _short_text(
                semantic.get(
                    "description"
                ),
                limit=(
                    MAX_DESCRIPTION_CONTEXT_LENGTH
                ),
            ),

        "semantic_tags":
            list(
                semantic.get(
                    "semantic_tags"
                )
                or []
            )[
                :MAX_CONTEXT_TAGS
            ],

        "use_cases":
            list(
                semantic.get(
                    "use_cases"
                )
                or []
            )[
                :MAX_CONTEXT_TAGS
            ],

        "meal_contexts":
            list(
                semantic.get(
                    "meal_contexts"
                )
                or []
            )[
                :MAX_CONTEXT_TAGS
            ],

        "health_tags":
            list(
                semantic.get(
                    "health_tags"
                )
                or []
            )[
                :MAX_CONTEXT_TAGS
            ],

        "health_context":
            _short_text(
                semantic.get(
                    "health_context"
                ),
                limit=(
                    MAX_HEALTH_CONTEXT_LENGTH
                ),
            ),

        "similarity":
            semantic.get(
                "similarity"
            ),

        "lexical_score":
            semantic.get(
                "lexical_score"
            ),

        "metadata_score":
            semantic.get(
                "metadata_score"
            ),

        "relevance_score":
            semantic.get(
                "relevance_score"
            ),

        "variants":
            compact_variants,
    }


# ============================================================
# BUILD COMPACT RAG CONTEXT
# ============================================================


def build_rag_context(
    *,
    query: str,
    top_k: int | None = None,
    similarity_threshold: float | None = None,
    domains: Sequence[
        str
    ] | None = None,
    in_stock_only: bool = True,
    min_price: float | None = None,
    max_price: float | None = None,
) -> dict[
    str,
    Any,
]:
    """
    Compact RAG result for Cohere/Groq.

    Embeddings and raw full catalog rows are intentionally NOT included.
    """

    result = (
        retrieve_products(
            query=query,
            top_k=top_k,
            similarity_threshold=(
                similarity_threshold
            ),
            domains=domains,
            in_stock_only=(
                in_stock_only
            ),
            min_price=(
                min_price
            ),
            max_price=(
                max_price
            ),
        )
    )

    matches = [
        _compact_match(
            item
        )
        for item
        in result[
            "matches"
        ][
            :MAX_CONTEXT_MATCHES
        ]
    ]

    unavailable = [
        _compact_match(
            item,
            unavailable=True,
        )
        for item
        in result[
            "unavailable_matches"
        ][
            :3
        ]
    ]

    return {
        "source":
            "supabase_rag",

        "status":
            result[
                "status"
            ],

        "query":
            result[
                "query"
            ],

        "match_count":
            len(
                matches
            ),

        "unavailable_match_count":
            len(
                unavailable
            ),

        "filters":
            result[
                "filters"
            ],

        "matches":
            matches,

        "unavailable_matches":
            unavailable,
    }


# ============================================================
# SYNC DESCRIPTION EMBEDDINGS
# ============================================================


def sync_description_embeddings(
    *,
    force: bool = False,
) -> dict[
    str,
    Any,
]:
    """
    Compatibility maintenance endpoint.

    Runtime semantic retrieval no longer requires local 384-dimension
    sentence-transformer embeddings. Existing database vectors may remain
    in Supabase, but they are not loaded into the Render process.

    Keeping this function prevents existing admin/maintenance callers from
    breaking while making it explicit that no local embedding generation is
    required.
    """

    rows = (
        _fetch_description_rows(
            force_refresh=True
        )
    )

    clear_rag_cache()

    return {
        "updated":
            0,

        "skipped":
            len(
                rows
            ),

        "failed":
            0,

        "total":
            len(
                rows
            ),

        "embedding_model":
            None,

        "retrieval_mode":
            "cohere_rerank",

        "rerank_model":
            _cohere_rerank_model(),

        "force_requested":
            bool(
                force
            ),

        "message": (
            "Local embedding synchronization is no longer required; "
            "semantic retrieval uses Cohere Rerank."
        ),
    }


# ============================================================
# RAG HEALTH CHECK
# ============================================================


def check_rag_database() -> dict[
    str,
    Any,
]:
    """
    Development diagnostic.

    Do NOT call before every chat message.

    This health check intentionally does not call Cohere, so checking
    health does not consume rerank quota or add network latency.
    """

    try:

        rows = (
            _fetch_description_rows(
                force_refresh=True
            )
        )

        return {
            "ok":
                True,

            "description_rows":
                len(
                    rows
                ),

            # Compatibility keys retained for callers that consumed
            # the previous diagnostics.
            "stored_current_embeddings":
                None,

            "missing_embeddings":
                None,

            "stale_embeddings":
                None,

            "embedding_model":
                None,

            "embedding_dimension":
                None,

            "expected_dimension":
                None,

            "retrieval_mode":
                "cohere_rerank",

            "rerank_model":
                _cohere_rerank_model(),

            "cohere_configured":
                bool(
                    _cohere_api_key()
                ),

            "local_transformer_loaded":
                False,
        }

    except Exception as exc:

        logger.exception(
            "RAG health check failed."
        )

        return {
            "ok":
                False,

            "error":
                type(
                    exc
                ).__name__,

            "retrieval_mode":
                "cohere_rerank",
        }


# ============================================================
# TOOL RESULTS
# ============================================================


def _tool_success(
    *,
    data: Any,
    message: str,
) -> dict[
    str,
    Any,
]:

    return {
        "success":
            True,

        "source":
            "supabase_rag",

        "domain":
            "semantic_product_search",

        "tool":
            TOOL_SEMANTIC_PRODUCT_SEARCH,

        "message":
            message,

        "data":
            data,
    }


def _tool_error(
    *,
    code: str,
    message: str,
) -> dict[
    str,
    Any,
]:

    return {
        "success":
            False,

        "source":
            "supabase_rag",

        "domain":
            "semantic_product_search",

        "tool":
            TOOL_SEMANTIC_PRODUCT_SEARCH,

        "error": {
            "code":
                code,

            "message":
                message,
        },

        "data":
            None,
    }


# ============================================================
# TOOL BOOLEAN
# ============================================================


def _tool_boolean(
    value: Any,
    *,
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

        value = (
            value
            .strip()
            .lower()
        )

        if value in {
            "true",
            "yes",
            "1",
            "on",
        }:

            return True

        if value in {
            "false",
            "no",
            "0",
            "off",
        }:

            return False

    raise RAGValidationError(
        "in_stock_only must be true or false."
    )


# ============================================================
# SEMANTIC PRODUCT TOOL
# ============================================================


def tool_semantic_product_search(
    *,
    query: Any,
    domains: Any = None,
    in_stock_only: Any = True,
    min_price: Any = None,
    max_price: Any = None,
    top_k: Any = None,
) -> dict[
    str,
    Any,
]:
    """
    AI-callable semantic grocery search.
    """

    try:

        query = (
            _validate_query(
                query
            )
        )

        # ====================================================
        # DOMAINS
        # ====================================================

        if domains is None:

            normalized_domains = None

        elif isinstance(
            domains,
            str,
        ):

            normalized_domains = [
                domains
            ]

        elif (
            isinstance(
                domains,
                Sequence,
            )
            and not isinstance(
                domains,
                (
                    str,
                    bytes,
                ),
            )
        ):

            normalized_domains = [
                str(
                    value
                )
                for value in domains
            ]

        else:

            raise RAGValidationError(
                "domains must be a list of product domains."
            )

        _normalize_domains(
            normalized_domains
        )

        # ====================================================
        # STOCK
        # ====================================================

        in_stock_only = (
            _tool_boolean(
                in_stock_only,
                default=True,
            )
        )

        # ====================================================
        # TOP K
        # ====================================================

        top_k = (
            _validate_top_k(
                top_k
            )
        )

        # ====================================================
        # PRICES
        # ====================================================

        min_price = (
            _optional_price(
                min_price,
                field_name=(
                    "min_price"
                ),
            )
        )

        max_price = (
            _optional_price(
                max_price,
                field_name=(
                    "max_price"
                ),
            )
        )

        if (
            min_price is not None
            and max_price is not None
            and min_price
            > max_price
        ):

            raise RAGValidationError(
                "min_price cannot exceed max_price."
            )

        # ====================================================
        # RETRIEVE
        # ====================================================

        result = (
            build_rag_context(
                query=query,
                top_k=top_k,
                domains=(
                    normalized_domains
                ),
                in_stock_only=(
                    in_stock_only
                ),
                min_price=(
                    min_price
                ),
                max_price=(
                    max_price
                ),
            )
        )

        status = (
            result.get(
                "status"
            )
        )

        if status == "found":

            message = (
                "Suitable semantic product matches were found. "
                "The returned variants contain live catalog prices, stock "
                "and SKU information."
            )

        elif (
            status
            == "only_out_of_stock"
        ):

            message = (
                "Semantically relevant products were found, but the matched "
                "products are currently out of stock."
            )

        else:

            message = (
                "No sufficiently relevant semantic product matches were "
                "found for the current request and filters."
            )

        return _tool_success(
            data=result,
            message=message,
        )

    except RAGValidationError as exc:

        return _tool_error(
            code=(
                "INVALID_ARGUMENTS"
            ),
            message=str(
                exc
            ),
        )

    except (
        RAGDatabaseError,
        ProductDatabaseError,
    ):

        logger.exception(
            "Semantic product database failure."
        )

        return _tool_error(
            code=(
                "RAG_DATABASE_UNAVAILABLE"
            ),
            message=(
                "Semantic product recommendations could not be "
                "retrieved right now."
            ),
        )

    except RAGEmbeddingError:

        logger.exception(
            "Semantic embedding failure."
        )

        return _tool_error(
            code=(
                "EMBEDDING_UNAVAILABLE"
            ),
            message=(
                "Semantic product search is temporarily unavailable."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected semantic product search failure."
        )

        return _tool_error(
            code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Semantic product search could not be completed."
            ),
        )


# ============================================================
# COHERE RAG TOOL SCHEMA
# ============================================================


RAG_TOOL_SCHEMAS: list[
    dict[
        str,
        Any,
    ]
] = [
    {
        "type":
            "function",

        "function": {

            "name":
                TOOL_SEMANTIC_PRODUCT_SEARCH,

            "description": (
                "Recommend products from the real store according to meaning, "
                "meal context, intended use or suitability. Use this when the "
                "user is not asking for one exact product name but wants "
                "something suitable, for example: 'something good for "
                "breakfast', 'healthy snack', 'quick breakfast', 'something "
                "for tea time', 'something for smoothies', 'something for "
                "baking', 'party snack', or 'something light to eat'. "
                "This searches product_descriptions semantically and then "
                "hydrates every result from the live products catalog for "
                "real SKU, price, stock and image information. Do not use it "
                "for a straightforward named-product query such as 'show me "
                "milk'; use search_products for that. Usually one successful "
                "semantic_product_search call is enough—do not repeatedly "
                "call it with slightly different wording just to verify the "
                "same recommendation."
            ),

            "parameters": {

                "type":
                    "object",

                "properties": {

                    "query": {
                        "type":
                            "string",

                        "description": (
                            "Preserve the user's semantic need, for example "
                            "'something good for breakfast', 'healthy evening "
                            "snack' or 'something useful for baking'."
                        ),
                    },

                    "domains": {
                        "type":
                            "array",

                        "items": {
                            "type":
                                "string",

                            "enum": [
                                "food",
                                "food-beverage",
                                "personal-care",
                                "household-care",
                                "baby-care",
                            ],
                        },

                        "description": (
                            "Optional domain restriction. For meals, breakfast, "
                            "snacks, tea-time and other edible requests normally "
                            "use food and food-beverage."
                        ),
                    },

                    "in_stock_only": {
                        "type":
                            "boolean",

                        "description": (
                            "Normally true for recommendations the user wants "
                            "to purchase now. If false, semantic results may "
                            "also contain currently unavailable products."
                        ),
                    },

                    "min_price": {
                        "type":
                            "number",

                        "minimum":
                            0,

                        "description":
                            "Optional minimum live SKU price.",
                    },

                    "max_price": {
                        "type":
                            "number",

                        "minimum":
                            0,

                        "description":
                            "Optional maximum live SKU price.",
                    },

                    "top_k": {
                        "type":
                            "integer",

                        "minimum":
                            1,

                        "maximum":
                            MAX_TOP_K,

                        "description": (
                            "Maximum number of logical recommended products. "
                            "Normally 3 to 5 is sufficient."
                        ),
                    },
                },

                "required": [
                    "query",
                ],

                "additionalProperties":
                    False,
            },
        },
    }
]


# ============================================================
# SAFE TOOL REGISTRY
# ============================================================

RAGToolFunction = Callable[
    ...,
    dict[
        str,
        Any,
    ],
]


RAG_TOOL_FUNCTIONS: dict[
    str,
    RAGToolFunction,
] = {
    TOOL_SEMANTIC_PRODUCT_SEARCH:
        tool_semantic_product_search,
}


# ============================================================
# TOOL SCHEMAS
# ============================================================


def get_rag_tool_schemas() -> list[
    dict[
        str,
        Any,
    ]
]:

    return copy.deepcopy(
        RAG_TOOL_SCHEMAS
    )


# ============================================================
# TOOL NAMES
# ============================================================


def get_rag_tool_names() -> set[
    str
]:

    return set(
        RAG_TOOL_FUNCTIONS.keys()
    )


# ============================================================
# TOOL CHECK
# ============================================================


def is_rag_tool(
    tool_name: str,
) -> bool:

    if not isinstance(
        tool_name,
        str,
    ):

        return False

    return (
        tool_name.strip()
        in RAG_TOOL_FUNCTIONS
    )


# ============================================================
# SAFE EXECUTOR
# ============================================================


def execute_rag_tool(
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

    if not isinstance(
        tool_name,
        str,
    ):

        raise RAGToolNotFoundError(
            "RAG tool name must be text."
        )

    tool_name = (
        tool_name.strip()
    )

    if not tool_name:

        raise RAGToolNotFoundError(
            "RAG tool name cannot be empty."
        )

    function = (
        RAG_TOOL_FUNCTIONS.get(
            tool_name
        )
    )

    if function is None:

        raise RAGToolNotFoundError(
            f"Unsupported RAG tool: {tool_name}"
        )

    if arguments is None:

        arguments = {}

    if not isinstance(
        arguments,
        Mapping,
    ):

        return _tool_error(
            code=(
                "INVALID_ARGUMENTS"
            ),
            message=(
                "RAG tool arguments must be an object."
            ),
        )

    try:

        return function(
            **dict(
                arguments
            )
        )

    except TypeError:

        logger.warning(
            "RAG tool argument mismatch. tool=%s",
            tool_name,
        )

        return _tool_error(
            code=(
                "INVALID_ARGUMENTS"
            ),
            message=(
                "The semantic search tool received unsupported "
                "or incomplete arguments."
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected RAG tool execution failure."
        )

        return _tool_error(
            code=(
                "INTERNAL_ERROR"
            ),
            message=(
                "Semantic product search could not be completed."
            ),
        )
