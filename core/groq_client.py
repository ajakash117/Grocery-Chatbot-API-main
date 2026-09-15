"""
core/groq_client.py

Final-response client for the Grocery Shopping Assistant.

Architecture
------------

User
    ↓
Cohere
    ↓
Safe Python tools
    ↓
Supabase / RAG
    ↓
Verified compact data
    ↓
Groq
    ↓
Final user-facing response


Groq does NOT:

- decide which backend tool to use
- query Supabase
- modify cart data
- modify orders
- calculate trusted totals
- invent stock
- invent SKU IDs
- invent prices

Groq only converts already verified backend information into a
natural response.

For general questions where no tool is required, Groq may answer
normally using its own knowledge.


UPDATED KEY ROUTING
-------------------

The project now has two separate Groq paths.

GENERAL / NO-TOOL CHAT:

    GROQ_API_KEY
    GROQ_API_KEY1

These two keys form the normal general-response pool handled by this
module.

DATABASE / TOOL-BACKED RESPONSES:

    GROQ_API_KEY2

GROQ_API_KEY2 is RESERVED for:

    core/database_responder.py

and must NOT be consumed by the normal rotating key pool in this file.

Therefore a database query cannot accidentally exhaust API slot 3
through the ordinary general-response path.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time

from dataclasses import (
    asdict,
    dataclass,
    is_dataclass,
)

from datetime import (
    date,
    datetime,
)

from decimal import Decimal

from functools import lru_cache

from typing import (
    Any,
    Mapping,
    Sequence,
)

from uuid import UUID


# ============================================================
# GROQ
# ============================================================

from groq import Groq


# ============================================================
# PROJECT
# ============================================================

from config import settings

from rules import (
    get_groq_rules,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# GROQ KEY ROUTING
# ============================================================

# Normal/general response pool.
#
# API slot 1:
#
#     GROQ_API_KEY
#
# API slot 2:
#
#     GROQ_API_KEY1
#
# API slot 3 (GROQ_API_KEY2) is intentionally excluded here because
# it is reserved for core/database_responder.py.

GENERAL_GROQ_ENV_NAMES = (
    "GROQ_API_KEY",
    "GROQ_API_KEY1",
)

DATABASE_GROQ_ENV_NAME = (
    "GROQ_API_KEY2"
)

DATABASE_GROQ_KEY_SLOT = 3


# ============================================================
# TOKEN / CONTEXT LIMITS
# ============================================================

# Your current Groq tier showed an 8K TPM limit.
#
# Therefore the final-response prompt must remain substantially
# below that instead of sending huge tool output.

DEFAULT_MAX_COMPLETION_TOKENS = 450


# ------------------------------------------------------------
# Prompt sections
# ------------------------------------------------------------

MAX_SYSTEM_RULE_CHARACTERS = 5_000

MAX_VERIFIED_DATA_CHARACTERS = 7_000

MAX_RAG_CONTEXT_CHARACTERS = 4_500

MAX_ADDITIONAL_CONTEXT_CHARACTERS = 1_500

MAX_HISTORY_MESSAGE_CHARACTERS = 1_000

MAX_HISTORY_MESSAGES = 4


# ------------------------------------------------------------
# API attempts
# ------------------------------------------------------------

# Per API key.
#
# Rate-limited keys are NOT retried on the same key.
DEFAULT_TRANSIENT_RETRIES_PER_KEY = 2

DEFAULT_RETRY_BASE_DELAY_SECONDS = 0.75

DEFAULT_RETRY_MAX_DELAY_SECONDS = 4.0


# ============================================================
# HTTP STATUS
# ============================================================

RETRYABLE_HTTP_STATUS_CODES = {
    408,
    409,
    500,
    502,
    503,
    504,
}


# These should not be blindly retried.
NON_RETRYABLE_HTTP_STATUS_CODES = {
    400,
    401,
    403,
    404,
    413,
    422,
}


# ============================================================
# EXCEPTIONS
# ============================================================


class GroqClientError(
    RuntimeError
):
    """
    Base Groq exception.
    """

    pass


class GroqConfigurationError(
    GroqClientError
):
    """
    Invalid Groq configuration.
    """

    pass


class GroqRequestError(
    GroqClientError
):
    """
    Groq request failed.
    """

    pass


class GroqResponseError(
    GroqClientError
):
    """
    Groq returned unusable output.
    """

    pass


class GroqContextError(
    GroqClientError
):
    """
    Invalid context supplied to Groq.
    """

    pass


# ============================================================
# NORMALIZED RESPONSE
# ============================================================


@dataclass(
    slots=True
)
class GroqResponse:
    """
    Application-friendly Groq response.
    """

    text: str

    model: str | None = None

    finish_reason: str | None = None

    response_id: str | None = None

    prompt_tokens: int | None = None

    completion_tokens: int | None = None

    total_tokens: int | None = None

    raw_response: Any = None

    @property
    def has_text(
        self,
    ) -> bool:

        return bool(
            isinstance(
                self.text,
                str,
            )
            and self.text.strip()
        )


# ============================================================
# API KEY POOL
# ============================================================


def _clean_secret(
    value: Any,
) -> str | None:
    """
    Normalize a secret without logging it.
    """

    if not isinstance(
        value,
        str,
    ):

        return None

    value = (
        value.strip()
    )

    if not value:

        return None

    return value


def get_groq_api_keys() -> list[str]:
    """
    Return unique Groq keys for GENERAL / NO-TOOL responses only.

    GENERAL POOL
    ------------

        API slot 1:
            GROQ_API_KEY

        API slot 2:
            GROQ_API_KEY1

    RESERVED DATABASE KEY
    ---------------------

        API slot 3:
            GROQ_API_KEY2

    GROQ_API_KEY2 is intentionally NOT returned here.

    Database-backed product/cart/order/RAG responses use
    core/database_responder.py, which reads GROQ_API_KEY2 directly.

    This separation prevents normal Groq key rotation from consuming
    the dedicated database-response key.

    No key value is ever logged.
    """

    candidates = [
        # --------------------------------------------------------
        # API SLOT 1
        # --------------------------------------------------------
        #
        # Prefer the already-loaded Settings value because config.py
        # owns normal application configuration.
        # --------------------------------------------------------

        _clean_secret(
            getattr(
                settings,
                "groq_api_key",
                None,
            )
        ),

        # --------------------------------------------------------
        # API SLOT 2
        # --------------------------------------------------------

        _clean_secret(
            os.getenv(
                "GROQ_API_KEY1"
            )
        ),
    ]

    unique_keys: list[
        str
    ] = []

    seen: set[
        str
    ] = set()

    for key in candidates:

        if not key:

            continue

        if key in seen:

            continue

        seen.add(
            key
        )

        unique_keys.append(
            key
        )

    if not unique_keys:

        raise GroqConfigurationError(
            "No general-response Groq API key is configured. "
            "Configure GROQ_API_KEY and/or GROQ_API_KEY1."
        )

    return unique_keys


def get_database_groq_api_key() -> str | None:
    """
    Return the dedicated database-response Groq key if configured.

    This helper DOES NOT add the key to the normal rotation pool.

    core/database_responder.py remains responsible for actually using
    GROQ_API_KEY2.

    The helper exists only for configuration diagnostics and to make
    the key separation explicit in one place.
    """

    return _clean_secret(
        os.getenv(
            DATABASE_GROQ_ENV_NAME
        )
    )


def is_database_groq_key_configured() -> bool:
    """
    Return True when dedicated API slot 3 is configured.

    This does not perform a network request.
    """

    return bool(
        get_database_groq_api_key()
    )


# ============================================================
# CONFIG VALIDATION
# ============================================================


def _validate_configuration() -> None:
    """
    Validate model configuration.
    """

    get_groq_api_keys()

    model = (
        getattr(
            settings,
            "groq_model",
            None,
        )
    )

    if not model:

        raise GroqConfigurationError(
            "GROQ_MODEL is missing."
        )

    temperature = (
        settings.groq_temperature
    )

    if (
        temperature < 0.0
        or temperature > 2.0
    ):

        raise GroqConfigurationError(
            "GROQ_TEMPERATURE must be between 0.0 and 2.0."
        )


# ============================================================
# CLIENT CREATION
# ============================================================


@lru_cache(
    maxsize=8
)
def _get_client_for_key(
    api_key: str,
) -> Groq:
    """
    Return cached client for one API key.

    Never log api_key.
    """

    try:

        return Groq(
            api_key=api_key
        )

    except Exception as exc:

        logger.exception(
            "Unable to initialize Groq client."
        )

        raise GroqConfigurationError(
            "Unable to initialize Groq client."
        ) from exc


def get_groq_client() -> Groq:
    """
    Compatibility helper.

    Returns client using the first configured GENERAL-response key.

    Normal requests use the API-slot-1/API-slot-2 rotation mechanism
    in chat().

    GROQ_API_KEY2 is not part of this pool.
    """

    _validate_configuration()

    keys = (
        get_groq_api_keys()
    )

    return (
        _get_client_for_key(
            keys[0]
        )
    )


# ============================================================
# USER MESSAGE
# ============================================================


def _validate_user_message(
    user_message: Any,
) -> str:
    """
    Validate user text.
    """

    if not isinstance(
        user_message,
        str,
    ):

        raise GroqContextError(
            "user_message must be text."
        )

    user_message = (
        user_message.strip()
    )

    if not user_message:

        raise GroqContextError(
            "user_message cannot be empty."
        )

    return user_message


# ============================================================
# JSON SERIALIZATION
# ============================================================


def _json_default(
    value: Any,
) -> Any:
    """
    Convert common backend values to JSON.
    """

    if isinstance(
        value,
        Decimal,
    ):

        return str(
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

        return asdict(
            value
        )

    if isinstance(
        value,
        (
            set,
            tuple,
        ),
    ):

        return list(
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

            return model_dump()

        except Exception:

            pass

    return str(
        value
    )


def serialize_context(
    value: Any,
    *,
    max_characters: int,
) -> str:
    """
    Serialize runtime context with a strict size limit.
    """

    if value is None:

        return "null"

    try:

        serialized = (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(
                    ",",
                    ":",
                ),
                default=_json_default,
            )
        )

    except Exception as exc:

        logger.exception(
            "Unable to serialize Groq context."
        )

        raise GroqContextError(
            "Unable to serialize model context."
        ) from exc

    if (
        len(
            serialized
        )
        > max_characters
    ):

        logger.info(
            "Truncating Groq context from %s to %s characters.",
            len(
                serialized
            ),
            max_characters,
        )

        serialized = (
            serialized[
                :max_characters
            ]
            + "...[TRUNCATED]"
        )

    return serialized


# ============================================================
# CHAT HISTORY
# ============================================================


def sanitize_chat_history(
    chat_history: (
        Sequence[
            Mapping[
                str,
                Any,
            ]
        ]
        | None
    ),
) -> list[
    dict[
        str,
        str,
    ]
]:
    """
    Keep only a few short recent user/assistant turns.

    Full Streamlit history can remain in session state.

    Groq does not need the entire conversation for final wording.
    """

    if not chat_history:

        return []

    valid: list[
        dict[
            str,
            str,
        ]
    ] = []

    for message in (
        chat_history
    ):

        if not isinstance(
            message,
            Mapping,
        ):

            continue

        role = (
            message.get(
                "role"
            )
        )

        if role not in {
            "user",
            "assistant",
        }:

            continue

        content = (
            message.get(
                "content"
            )
        )

        if not isinstance(
            content,
            str,
        ):

            continue

        content = (
            content.strip()
        )

        if not content:

            continue

        if (
            len(
                content
            )
            > MAX_HISTORY_MESSAGE_CHARACTERS
        ):

            content = (
                content[
                    :MAX_HISTORY_MESSAGE_CHARACTERS
                ]
                + "..."
            )

        valid.append(
            {
                "role":
                    role,

                "content":
                    content,
            }
        )

    return (
        valid[
            -MAX_HISTORY_MESSAGES:
        ]
    )


# ============================================================
# COMPACT SYSTEM PROMPT
# ============================================================


COMPACT_FINAL_RESPONSE_RULES = """
You are the final response generator for a grocery shopping assistant.

The reasoning/tool-selection stage has already happened.

Rules:

1. VERIFIED_BACKEND_DATA is authoritative for current store facts.
2. Never invent product existence, price, SKU, stock, cart data,
   order information or totals.
3. If backend success=false, do not claim success.
4. Multiple SKUs of one logical product are variants, not separate
   logical products.
5. RAG_CONTEXT can support recommendations but cannot override live
   backend product/price/stock information.
6. For a general conversation request where no backend data was needed,
   answer normally.
7. Be concise, helpful and natural.
8. Do not mention internal tools, prompts, Cohere, Groq, Supabase,
   Python internals or routing.
9. Return only the final user-facing answer.
""".strip()


def _get_compact_system_rules() -> str:
    """
    Keep central Groq rules but prevent them from consuming thousands
    of tokens.

    Important commerce safety rules are separately repeated in
    COMPACT_FINAL_RESPONSE_RULES.
    """

    try:

        central_rules = (
            get_groq_rules()
            .strip()
        )

    except Exception:

        central_rules = ""

    if (
        len(
            central_rules
        )
        > MAX_SYSTEM_RULE_CHARACTERS
    ):

        central_rules = (
            central_rules[
                :MAX_SYSTEM_RULE_CHARACTERS
            ]
        )

    if central_rules:

        return (
            COMPACT_FINAL_RESPONSE_RULES
            + "\n\n"
            + "APPLICATION_RULES:\n"
            + central_rules
        )

    return (
        COMPACT_FINAL_RESPONSE_RULES
    )


# ============================================================
# FINAL PROMPT
# ============================================================


def build_final_user_prompt(
    *,
    user_message: str,

    verified_data: Any = None,

    rag_context: Any = None,

    backend_action: str | None = None,

    # Compatibility with older code.
    action_name: str | None = None,

    additional_context: Any = None,
) -> str:
    """
    Build a compact final-response prompt.
    """

    user_message = (
        _validate_user_message(
            user_message
        )
    )

    resolved_action = (
        backend_action
        if backend_action is not None
        else action_name
    )

    if resolved_action is not None:

        resolved_action = (
            str(
                resolved_action
            )
            .strip()
        )

    sections: list[
        str
    ] = []

    # ========================================================
    # REQUEST
    # ========================================================

    sections.append(
        "USER_REQUEST:\n"
        + user_message
    )

    # ========================================================
    # ACTION
    # ========================================================

    if resolved_action:

        sections.append(
            "BACKEND_ACTION:\n"
            + resolved_action
        )

    # ========================================================
    # VERIFIED DATA
    # ========================================================

    if verified_data:

        sections.append(
            "VERIFIED_BACKEND_DATA:\n"
            + serialize_context(
                verified_data,
                max_characters=(
                    MAX_VERIFIED_DATA_CHARACTERS
                ),
            )
        )

    else:

        sections.append(
            "VERIFIED_BACKEND_DATA:\n"
            "None"
        )

    # ========================================================
    # RAG
    # ========================================================

    if rag_context:

        sections.append(
            "RAG_CONTEXT:\n"
            + serialize_context(
                rag_context,
                max_characters=(
                    MAX_RAG_CONTEXT_CHARACTERS
                ),
            )
        )

    # ========================================================
    # ADDITIONAL CONTEXT
    # ========================================================

    if additional_context is not None:

        sections.append(
            "RUNTIME_CONTEXT:\n"
            + serialize_context(
                additional_context,
                max_characters=(
                    MAX_ADDITIONAL_CONTEXT_CHARACTERS
                ),
            )
        )

    # ========================================================
    # FINAL INSTRUCTION
    # ========================================================

    sections.append(
        """
TASK:

Answer the user's request.

If verified store data is present, use it exactly.

If no backend data was required because this is a general question,
answer normally.

Do not invent live store facts.

Return only the answer.
""".strip()
    )

    return (
        "\n\n".join(
            sections
        )
    )


# ============================================================
# MESSAGES
# ============================================================


def build_messages(
    *,
    user_message: str,

    verified_data: Any = None,

    rag_context: Any = None,

    chat_history: (
        Sequence[
            Mapping[
                str,
                Any,
            ]
        ]
        | None
    ) = None,

    backend_action: str | None = None,

    action_name: str | None = None,

    additional_context: Any = None,
) -> list[
    dict[
        str,
        str,
    ]
]:
    """
    Build Groq message list.
    """

    messages: list[
        dict[
            str,
            str,
        ]
    ] = [
        {
            "role":
                "system",

            "content":
                _get_compact_system_rules(),
        }
    ]

    # Keep history small.
    messages.extend(
        sanitize_chat_history(
            chat_history
        )
    )

    messages.append(
        {
            "role":
                "user",

            "content":
                build_final_user_prompt(
                    user_message=(
                        user_message
                    ),

                    verified_data=(
                        verified_data
                    ),

                    rag_context=(
                        rag_context
                    ),

                    backend_action=(
                        backend_action
                    ),

                    action_name=(
                        action_name
                    ),

                    additional_context=(
                        additional_context
                    ),
                ),
        }
    )

    return messages


# ============================================================
# RESPONSE NORMALIZATION
# ============================================================


def normalize_response(
    response: Any,
) -> GroqResponse:
    """
    Normalize Groq ChatCompletion response.
    """

    if response is None:

        raise GroqResponseError(
            "Groq returned no response."
        )

    try:

        choices = getattr(
            response,
            "choices",
            None,
        )

        if not choices:

            raise GroqResponseError(
                "Groq response contained no choices."
            )

        choice = (
            choices[0]
        )

        message = getattr(
            choice,
            "message",
            None,
        )

        if message is None:

            raise GroqResponseError(
                "Groq response contained no message."
            )

        content = getattr(
            message,
            "content",
            None,
        )

        if content is None:

            raise GroqResponseError(
                "Groq response contained no text."
            )

        if not isinstance(
            content,
            str,
        ):

            content = str(
                content
            )

        content = (
            content.strip()
        )

        if not content:

            raise GroqResponseError(
                "Groq returned an empty response."
            )

        finish_reason = getattr(
            choice,
            "finish_reason",
            None,
        )

        usage = getattr(
            response,
            "usage",
            None,
        )

        prompt_tokens = None

        completion_tokens = None

        total_tokens = None

        if usage is not None:

            prompt_tokens = getattr(
                usage,
                "prompt_tokens",
                None,
            )

            completion_tokens = getattr(
                usage,
                "completion_tokens",
                None,
            )

            total_tokens = getattr(
                usage,
                "total_tokens",
                None,
            )

        return GroqResponse(
            text=
                content,

            model=(
                str(
                    getattr(
                        response,
                        "model",
                        "",
                    )
                )
                or None
            ),

            finish_reason=(
                str(
                    finish_reason
                )
                if finish_reason is not None
                else None
            ),

            response_id=(
                str(
                    getattr(
                        response,
                        "id",
                        "",
                    )
                )
                or None
            ),

            prompt_tokens=
                prompt_tokens,

            completion_tokens=
                completion_tokens,

            total_tokens=
                total_tokens,

            raw_response=
                response,
        )

    except GroqResponseError:

        raise

    except Exception as exc:

        logger.exception(
            "Unable to normalize Groq response."
        )

        raise GroqResponseError(
            "Unable to process Groq response."
        ) from exc


# ============================================================
# HTTP STATUS
# ============================================================


def _extract_status_code(
    exception: Exception,
) -> int | None:
    """
    Extract HTTP status.
    """

    status = getattr(
        exception,
        "status_code",
        None,
    )

    if isinstance(
        status,
        int,
    ):

        return status

    response = getattr(
        exception,
        "response",
        None,
    )

    if response is not None:

        status = getattr(
            response,
            "status_code",
            None,
        )

        if isinstance(
            status,
            int,
        ):

            return status

    return None


# ============================================================
# RETRY-AFTER
# ============================================================


def _extract_retry_after(
    exception: Exception,
) -> float | None:
    """
    Read Retry-After header when provided.
    """

    response = getattr(
        exception,
        "response",
        None,
    )

    if response is None:

        return None

    headers = getattr(
        response,
        "headers",
        None,
    )

    if headers is None:

        return None

    try:

        value = (
            headers.get(
                "retry-after"
            )
            or headers.get(
                "Retry-After"
            )
        )

        if value is None:

            return None

        seconds = float(
            value
        )

        if seconds < 0:

            return None

        return seconds

    except (
        TypeError,
        ValueError,
        AttributeError,
    ):

        return None


# ============================================================
# RETRY DELAY
# ============================================================


def _retry_delay(
    attempt: int,
) -> float:
    """
    Small exponential backoff.
    """

    delay = (
        DEFAULT_RETRY_BASE_DELAY_SECONDS
        * (
            2 ** attempt
        )
    )

    jitter = (
        random.uniform(
            0.0,
            0.2,
        )
    )

    return min(
        delay + jitter,
        DEFAULT_RETRY_MAX_DELAY_SECONDS,
    )


# ============================================================
# ERROR CLASSIFICATION
# ============================================================


def _is_rate_limit(
    exception: Exception,
) -> bool:

    return (
        _extract_status_code(
            exception
        )
        == 429
    )


def _should_retry_same_key(
    exception: Exception,
) -> bool:
    """
    429 should NOT retry the same key.

    Instead the caller moves immediately to another configured key.
    """

    status = (
        _extract_status_code(
            exception
        )
    )

    if status == 429:

        return False

    if (
        status
        in NON_RETRYABLE_HTTP_STATUS_CODES
    ):

        return False

    if (
        status
        in RETRYABLE_HTTP_STATUS_CODES
    ):

        return True

    if (
        status is not None
        and 500 <= status < 600
    ):

        return True

    # Network failures may be temporary.
    if status is None:

        return True

    return False


# ============================================================
# FRIENDLY ERROR
# ============================================================


def _friendly_error_message(
    status_code: int | None,
) -> str:

    if status_code == 400:

        return (
            "The final-response request was invalid."
        )

    if status_code == 401:

        return (
            "Groq authentication failed."
        )

    if status_code == 403:

        return (
            "The Groq account is not authorized for this request."
        )

    if status_code == 404:

        return (
            "The configured Groq model could not be found."
        )

    if status_code == 413:

        return (
            "The final-response context was too large."
        )

    if status_code == 429:

        return (
            "The response service is temporarily rate limited."
        )

    if (
        status_code is not None
        and 500 <= status_code < 600
    ):

        return (
            "The response service is temporarily unavailable."
        )

    return (
        "Unable to generate the final response."
    )


# ============================================================
# RAW GROQ CHAT
# ============================================================


def chat(
    *,
    messages: Sequence[
        Mapping[
            str,
            str,
        ]
    ],

    max_completion_tokens: int = (
        DEFAULT_MAX_COMPLETION_TOKENS
    ),

    transient_retries_per_key: int = (
        DEFAULT_TRANSIENT_RETRIES_PER_KEY
    ),
) -> GroqResponse:
    """
    Send request to Groq.

    General-key behavior:

    API SLOT 1
        ↓
    success -> return

    if 429
        ↓
    do NOT retry API slot 1
        ↓
    API SLOT 2

    if API slot 2 also fails
        ↓
    raise GroqRequestError

    IMPORTANT:

    API slot 3 / GROQ_API_KEY2 is NOT tried here.

    That key is reserved for core/database_responder.py.

    This prevents database-response capacity from being consumed by
    ordinary general-chat rotation.
    """

    if not messages:

        raise GroqContextError(
            "messages cannot be empty."
        )

    if (
        max_completion_tokens < 1
    ):

        raise GroqContextError(
            "max_completion_tokens must be greater than zero."
        )

    # ========================================================
    # VALIDATE MESSAGES
    # ========================================================

    normalized_messages: list[
        dict[
            str,
            str,
        ]
    ] = []

    allowed_roles = {
        "system",
        "user",
        "assistant",
    }

    for index, message in enumerate(
        messages
    ):

        if not isinstance(
            message,
            Mapping,
        ):

            raise GroqContextError(
                f"Groq message {index} must be a mapping."
            )

        role = (
            message.get(
                "role"
            )
        )

        content = (
            message.get(
                "content"
            )
        )

        if role not in allowed_roles:

            raise GroqContextError(
                f"Invalid Groq role {role!r}."
            )

        if not isinstance(
            content,
            str,
        ):

            raise GroqContextError(
                f"Groq message {index} content must be text."
            )

        content = (
            content.strip()
        )

        if not content:

            continue

        normalized_messages.append(
            {
                "role":
                    role,

                "content":
                    content,
            }
        )

    if not normalized_messages:

        raise GroqContextError(
            "No valid Groq messages remained."
        )

    # ========================================================
    # KEY POOL
    # ========================================================

    _validate_configuration()

    api_keys = (
        get_groq_api_keys()
    )

    last_exception: (
        Exception
        | None
    ) = None

    last_status: (
        int
        | None
    ) = None

    # ========================================================
    # TRY EACH KEY
    # ========================================================

    for key_index, api_key in enumerate(
        api_keys,
        start=1,
    ):

        client = (
            _get_client_for_key(
                api_key
            )
        )

        # ----------------------------------------------------
        # Only retry true transient errors on this same key.
        # ----------------------------------------------------

        for attempt in range(
            transient_retries_per_key
        ):

            try:

                logger.info(
                    "Sending Groq request. "
                    "key_slot=%s/%s "
                    "attempt=%s/%s "
                    "model=%s",
                    key_index,
                    len(
                        api_keys
                    ),
                    attempt + 1,
                    transient_retries_per_key,
                    settings.groq_model,
                )

                response = (
                    client.chat.completions.create(
                        model=(
                            settings.groq_model
                        ),

                        messages=(
                            normalized_messages
                        ),

                        temperature=(
                            settings.groq_temperature
                        ),

                        max_completion_tokens=(
                            max_completion_tokens
                        ),

                        include_reasoning=False,

                        stream=False,
                    )
                )

                normalized = (
                    normalize_response(
                        response
                    )
                )

                logger.info(
                    "Groq response successful. "
                    "key_slot=%s "
                    "prompt_tokens=%s "
                    "completion_tokens=%s "
                    "total_tokens=%s",
                    key_index,
                    normalized.prompt_tokens,
                    normalized.completion_tokens,
                    normalized.total_tokens,
                )

                return normalized

            except GroqResponseError:

                raise

            except Exception as exc:

                last_exception = (
                    exc
                )

                last_status = (
                    _extract_status_code(
                        exc
                    )
                )

                logger.warning(
                    "Groq request failed. "
                    "key_slot=%s/%s "
                    "attempt=%s/%s "
                    "status=%s "
                    "error_type=%s",
                    key_index,
                    len(
                        api_keys
                    ),
                    attempt + 1,
                    transient_retries_per_key,
                    last_status,
                    type(
                        exc
                    ).__name__,
                )

                # =============================================
                # RATE LIMIT
                #
                # Immediately switch key.
                # =============================================

                if (
                    _is_rate_limit(
                        exc
                    )
                ):

                    logger.warning(
                        "Groq key_slot=%s is rate limited. "
                        "Trying next configured key.",
                        key_index,
                    )

                    break

                # =============================================
                # NON-RETRYABLE
                # =============================================

                if not _should_retry_same_key(
                    exc
                ):

                    # Authentication failure may apply only to one key.
                    #
                    # Try another key for 401/403 before giving up.

                    if (
                        last_status
                        in {
                            401,
                            403,
                        }
                        and key_index
                        < len(
                            api_keys
                        )
                    ):

                        logger.warning(
                            "Groq key_slot=%s rejected. "
                            "Trying next configured key.",
                            key_index,
                        )

                        break

                    raise GroqRequestError(
                        _friendly_error_message(
                            last_status
                        )
                    ) from exc

                # =============================================
                # TRANSIENT RETRY
                # =============================================

                if (
                    attempt
                    >= transient_retries_per_key - 1
                ):

                    break

                retry_after = (
                    _extract_retry_after(
                        exc
                    )
                )

                if retry_after is not None:

                    delay = min(
                        retry_after,
                        DEFAULT_RETRY_MAX_DELAY_SECONDS,
                    )

                else:

                    delay = (
                        _retry_delay(
                            attempt
                        )
                    )

                time.sleep(
                    delay
                )

    # ========================================================
    # ALL KEYS FAILED
    # ========================================================

    logger.error(
        "All configured Groq API keys failed. "
        "keys_tried=%s last_status=%s",
        len(
            api_keys
        ),
        last_status,
    )

    raise GroqRequestError(
        _friendly_error_message(
            last_status
        )
    ) from last_exception


# ============================================================
# FINAL RESPONSE
# ============================================================


def generate_final_response(
    *,
    user_message: str,

    verified_data: Any = None,

    rag_context: Any = None,

    chat_history: (
        Sequence[
            Mapping[
                str,
                Any,
            ]
        ]
        | None
    ) = None,

    backend_action: str | None = None,

    # Backward compatibility.
    action_name: str | None = None,

    additional_context: Any = None,

    max_completion_tokens: int = (
        DEFAULT_MAX_COMPLETION_TOKENS
    ),
) -> GroqResponse:
    """
    Generate final chatbot response.
    """

    user_message = (
        _validate_user_message(
            user_message
        )
    )

    messages = (
        build_messages(
            user_message=(
                user_message
            ),

            verified_data=(
                verified_data
            ),

            rag_context=(
                rag_context
            ),

            chat_history=(
                chat_history
            ),

            backend_action=(
                backend_action
            ),

            action_name=(
                action_name
            ),

            additional_context=(
                additional_context
            ),
        )
    )

    return chat(
        messages=
            messages,

        max_completion_tokens=(
            max_completion_tokens
        ),
    )


# ============================================================
# TEXT ONLY
# ============================================================


def generate_final_text(
    *,
    user_message: str,

    verified_data: Any = None,

    rag_context: Any = None,

    chat_history: (
        Sequence[
            Mapping[
                str,
                Any,
            ]
        ]
        | None
    ) = None,

    backend_action: str | None = None,

    action_name: str | None = None,

    additional_context: Any = None,
) -> str:
    """
    Convenience wrapper for brain.py.
    """

    response = (
        generate_final_response(
            user_message=(
                user_message
            ),

            verified_data=(
                verified_data
            ),

            rag_context=(
                rag_context
            ),

            chat_history=(
                chat_history
            ),

            backend_action=(
                backend_action
            ),

            action_name=(
                action_name
            ),

            additional_context=(
                additional_context
            ),
        )
    )

    return (
        response.text
    )


# ============================================================
# CONNECTION CHECK
# ============================================================


def check_groq_connection() -> bool:
    """
    Check Groq using the configured GENERAL-response key pool.

    It stops on the first successful key.

    This intentionally does NOT test GROQ_API_KEY2 because API slot 3
    is reserved for database_responder.py.
    """

    try:

        response = (
            chat(
                messages=[
                    {
                        "role":
                            "user",

                        "content":
                            "Reply exactly with: OK",
                    }
                ],

                max_completion_tokens=10,

                transient_retries_per_key=1,
            )
        )

        return (
            response.text
            .strip()
            .upper()
            == "OK"
        )

    except Exception:

        logger.exception(
            "Groq connection check failed."
        )

        return False


# ============================================================
# KEY ROUTING DIAGNOSTIC
# ============================================================


def get_groq_key_routing_status() -> dict[
    str,
    Any,
]:
    """
    Return safe Groq routing information for development diagnostics.

    No secret values are returned.
    """

    try:

        general_count = len(
            get_groq_api_keys()
        )

    except GroqConfigurationError:

        general_count = 0

    return {
        "general_key_slots":
            general_count,

        "general_key_envs": [
            "GROQ_API_KEY",
            "GROQ_API_KEY1",
        ],

        "database_key_slot":
            DATABASE_GROQ_KEY_SLOT,

        "database_key_env":
            DATABASE_GROQ_ENV_NAME,

        "database_key_configured":
            is_database_groq_key_configured(),

        "database_key_in_general_rotation":
            False,

        "database_responder":
            "core.database_responder",

        "general_rate_limit_rotation":
            (
                "API slot 1 -> API slot 2"
            ),

        "database_rate_limit_rotation":
            False,
    }

