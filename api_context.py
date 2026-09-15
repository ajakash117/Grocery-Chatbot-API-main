"""
api_context.py

Request-scoped authentication and conversation context for the
Grocery Shopping Assistant HTTP API.

=======================================================================
PURPOSE
=======================================================================

The existing Streamlit application uses:

    st.session_state

That is appropriate for:

    app.py

but an HTTP API must NOT depend on Streamlit session state.

A deployed frontend may be:

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

Every HTTP request therefore gets its own isolated APIRequestContext.

Flow:

    Frontend
        ↓
    Supabase login
        ↓
    Supabase access token
        ↓
    Authorization: Bearer <access_token>
        ↓
    FastAPI /api.py
        ↓
    build_api_request_context()
        ↓
    Supabase verifies token
        ↓
    APIRequestContext
        ↓
    ContextVar
        ↓
    brain / tools / database
        ↓
    same authenticated user only


=======================================================================
IMPORTANT SECURITY RULES
=======================================================================

1. Never trust a user_id supplied by the frontend.

2. User identity must come from the verified Supabase access token.

3. Never send access tokens to:
       Cohere
       Groq
       RAG
       frontend response JSON
       logs

4. Never store:
       SUPABASE_SECRET_KEY
       GROQ keys
       COHERE key
   inside this request context.

5. ContextVar provides request/task isolation.

6. The access token is available internally only so later
   auth/session.py compatibility can create a user-scoped database
   connection with Supabase RLS.

7. Product catalog requests may use public catalog access, but private
   cart/order operations must always use the authenticated identity.


=======================================================================
NO FASTAPI DEPENDENCY HERE
=======================================================================

This module deliberately does NOT import FastAPI.

api.py will handle:

    HTTP headers
    HTTP status codes
    CORS
    endpoints

This module handles only:

    token verification
    verified user identity
    request-scoped context
"""

from __future__ import annotations

import copy
import logging
import re
import uuid

from contextlib import (
    asynccontextmanager,
    contextmanager,
)

from contextvars import (
    ContextVar,
    Token,
)

from dataclasses import (
    dataclass,
    field,
)

from typing import (
    Any,
    AsyncIterator,
    Iterator,
    Mapping,
)


# ============================================================
# PROJECT
# ============================================================

from api_models import (
    APIUser,
)

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
# LIMITS
# ============================================================

MAX_ACCESS_TOKEN_LENGTH = 20_000

MAX_CONVERSATION_ID_LENGTH = 200

MAX_LOCALE_LENGTH = 30

MAX_CLIENT_CONTEXT_SIZE = 20_000

MAX_CONTEXT_DEPTH = 6

MAX_CONTEXT_LIST_ITEMS = 50

MAX_CONTEXT_STRING_LENGTH = 2_000


# ============================================================
# SECRET-LIKE FIELD NAMES
# ============================================================

_SECRET_FIELD_NAMES = {
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "api_key",
    "apikey",
    "supabase_secret_key",
    "groq_api_key",
    "groq_api_key1",
    "groq_api_key2",
    "cohere_api_key",
    "token",
    "jwt",
    "bearer",
}


# ============================================================
# EXCEPTIONS
# ============================================================


class APIContextError(
    RuntimeError
):
    """
    Base request-context exception.
    """

    pass


class APIAuthenticationError(
    APIContextError
):
    """
    Authentication failed.
    """

    pass


class APIAuthorizationHeaderError(
    APIAuthenticationError
):
    """
    Authorization header is invalid.
    """

    pass


class APIAccessTokenError(
    APIAuthenticationError
):
    """
    Supabase access token is missing or invalid.
    """

    pass


class APIUserVerificationError(
    APIAuthenticationError
):
    """
    Supabase token did not resolve to a valid user.
    """

    pass


class APIRequestContextMissingError(
    APIContextError
):
    """
    Code requested API context outside an active HTTP request.
    """

    pass


class APIContextValidationError(
    APIContextError
):
    """
    Invalid request-context input.
    """

    pass


# ============================================================
# REQUEST CONTEXT
# ============================================================


@dataclass(
    slots=True,
    frozen=True,
)
class APIRequestContext:
    """
    One authenticated API request context.

    IMPORTANT:

    access_token is deliberately internal.

    It must NEVER be:

        serialized into API output
        logged
        sent to Cohere
        sent to Groq
    """

    request_id: str

    user: APIUser

    access_token: str = field(
        repr=False
    )

    conversation_id: str

    locale: str | None = None

    client_context: dict[
        str,
        Any,
    ] = field(
        default_factory=dict,
        repr=False,
    )

    @property
    def user_id(
        self,
    ) -> str:

        return self.user.id

    @property
    def email(
        self,
    ) -> str | None:

        return self.user.email

    def safe_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:
        """
        Safe representation suitable for debugging.

        Authentication token is intentionally excluded.
        """

        return {
            "request_id":
                self.request_id,

            "user": {
                "id":
                    self.user.id,

                "email":
                    self.user.email,

                "display_name":
                    self.user.display_name,
            },

            "conversation_id":
                self.conversation_id,

            "locale":
                self.locale,

            "client_context":
                copy.deepcopy(
                    self.client_context
                ),

            "authenticated":
                True,
        }


# ============================================================
# CONTEXTVAR
# ============================================================

_CURRENT_API_CONTEXT: ContextVar[
    APIRequestContext | None
] = ContextVar(
    "grocery_chatbot_api_request_context",
    default=None,
)


# ============================================================
# TEXT NORMALIZATION
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


# ============================================================
# ACCESS TOKEN VALIDATION
# ============================================================


def _validate_access_token(
    access_token: Any,
) -> str:

    if not isinstance(
        access_token,
        str,
    ):

        raise APIAccessTokenError(
            "Authentication token is missing."
        )

    access_token = (
        access_token.strip()
    )

    if not access_token:

        raise APIAccessTokenError(
            "Authentication token is missing."
        )

    if (
        len(
            access_token
        )
        > MAX_ACCESS_TOKEN_LENGTH
    ):

        raise APIAccessTokenError(
            "Authentication token is invalid."
        )

    return access_token


# ============================================================
# AUTHORIZATION HEADER
# ============================================================


def extract_bearer_token(
    authorization_header: Any,
) -> str:
    """
    Parse:

        Authorization: Bearer <token>

    api.py can call this directly.

    Token values are never logged.
    """

    if not isinstance(
        authorization_header,
        str,
    ):

        raise APIAuthorizationHeaderError(
            "Authorization header is required."
        )

    authorization_header = (
        authorization_header.strip()
    )

    if not authorization_header:

        raise APIAuthorizationHeaderError(
            "Authorization header is required."
        )

    parts = (
        authorization_header.split(
            None,
            1,
        )
    )

    if (
        len(
            parts
        )
        != 2
    ):

        raise APIAuthorizationHeaderError(
            "Authorization header must use Bearer authentication."
        )

    scheme = (
        parts[
            0
        ]
        .strip()
        .lower()
    )

    if scheme != "bearer":

        raise APIAuthorizationHeaderError(
            "Authorization header must use Bearer authentication."
        )

    token = (
        parts[
            1
        ].strip()
    )

    return _validate_access_token(
        token
    )


# ============================================================
# CONVERSATION ID
# ============================================================


def normalize_conversation_id(
    value: Any,
) -> str:
    """
    Accept a client conversation ID or create one.

    The conversation ID is NOT trusted user identity.

    Identity always comes from Supabase.
    """

    if value is None:

        return str(
            uuid.uuid4()
        )

    value = str(
        value
    ).strip()

    if not value:

        return str(
            uuid.uuid4()
        )

    if (
        len(
            value
        )
        > MAX_CONVERSATION_ID_LENGTH
    ):

        raise APIContextValidationError(
            "conversation_id is too long."
        )

    # Keep IDs transport-safe and predictable.

    if not re.fullmatch(
        r"[A-Za-z0-9._:\-]+",
        value,
    ):

        raise APIContextValidationError(
            "conversation_id contains unsupported characters."
        )

    return value


# ============================================================
# LOCALE
# ============================================================


def normalize_locale(
    value: Any,
) -> str | None:

    if value is None:

        return None

    value = str(
        value
    ).strip()

    if not value:

        return None

    if (
        len(
            value
        )
        > MAX_LOCALE_LENGTH
    ):

        raise APIContextValidationError(
            "locale is too long."
        )

    return value


# ============================================================
# SAFE CLIENT CONTEXT
# ============================================================


def _safe_context_value(
    value: Any,
    *,
    depth: int = 0,
) -> Any:
    """
    Sanitize untrusted optional frontend context.

    client_context is convenience information only.

    It must NEVER become a source of truth for:

        user identity
        price
        stock
        cart
        orders
    """

    if depth > MAX_CONTEXT_DEPTH:

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

        if (
            len(
                value
            )
            > MAX_CONTEXT_STRING_LENGTH
        ):

            return (
                value[
                    :MAX_CONTEXT_STRING_LENGTH
                ]
                + "..."
            )

        return value

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

            # Never accept secret-like frontend data into context.

            if (
                normalized_key
                in _SECRET_FIELD_NAMES
                or "password"
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
                _safe_context_value(
                    item,
                    depth=(
                        depth + 1
                    ),
                )
            )

        return output

    if isinstance(
        value,
        (
            list,
            tuple,
            set,
        ),
    ):

        return [
            _safe_context_value(
                item,
                depth=(
                    depth + 1
                ),
            )
            for item
            in list(
                value
            )[
                :MAX_CONTEXT_LIST_ITEMS
            ]
        ]

    return str(
        value
    )[
        :MAX_CONTEXT_STRING_LENGTH
    ]


def sanitize_client_context(
    value: Any,
) -> dict[
    str,
    Any,
]:
    """
    Normalize optional frontend-provided context.

    This context remains NON-AUTHORITATIVE.
    """

    if value is None:

        return {}

    if not isinstance(
        value,
        Mapping,
    ):

        raise APIContextValidationError(
            "client_context must be an object."
        )

    sanitized = (
        _safe_context_value(
            value
        )
    )

    if not isinstance(
        sanitized,
        dict,
    ):

        return {}

    # Simple defensive size check.

    if (
        len(
            str(
                sanitized
            )
        )
        > MAX_CLIENT_CONTEXT_SIZE
    ):

        raise APIContextValidationError(
            "client_context is too large."
        )

    return sanitized


# ============================================================
# SUPABASE USER EXTRACTION
# ============================================================


def _extract_supabase_user(
    auth_response: Any,
) -> Any:
    """
    Supabase SDK response shapes can vary slightly by version.

    Support:

        response.user

    or direct user-like response.
    """

    if auth_response is None:

        return None

    user = getattr(
        auth_response,
        "user",
        None,
    )

    if user is not None:

        return user

    # Some SDK versions may return the user object directly.

    if getattr(
        auth_response,
        "id",
        None,
    ):

        return auth_response

    return None


# ============================================================
# USER METADATA
# ============================================================


def _extract_user_metadata(
    user: Any,
) -> dict[
    str,
    Any,
]:

    metadata = getattr(
        user,
        "user_metadata",
        None,
    )

    if isinstance(
        metadata,
        Mapping,
    ):

        return dict(
            metadata
        )

    metadata = getattr(
        user,
        "raw_user_meta_data",
        None,
    )

    if isinstance(
        metadata,
        Mapping,
    ):

        return dict(
            metadata
        )

    return {}


# ============================================================
# DISPLAY NAME
# ============================================================


def _extract_display_name(
    user: Any,
) -> str | None:

    metadata = (
        _extract_user_metadata(
            user
        )
    )

    candidates = [
        metadata.get(
            "full_name"
        ),

        metadata.get(
            "name"
        ),

        metadata.get(
            "display_name"
        ),
    ]

    for candidate in candidates:

        value = (
            _optional_text(
                candidate
            )
        )

        if value:

            return value

    return None


# ============================================================
# VERIFY SUPABASE ACCESS TOKEN
# ============================================================


def verify_supabase_access_token(
    access_token: str,
) -> APIUser:
    """
    Verify access token with Supabase Auth.

    IMPORTANT:

    We do NOT decode the JWT and trust its payload ourselves.

    Supabase Auth verifies the token and returns the authenticated user.

    No admin/service key is required for this operation.
    """

    access_token = (
        _validate_access_token(
            access_token
        )
    )

    try:

        supabase = (
            create_public_client()
        )

    except Exception as exc:

        logger.exception(
            "Unable to initialize Supabase public client "
            "for API authentication."
        )

        raise APIAuthenticationError(
            "Authentication service is unavailable."
        ) from exc

    try:

        auth_response = (
            supabase
            .auth
            .get_user(
                access_token
            )
        )

    except Exception as exc:

        # Never log token or raw authorization header.

        logger.warning(
            "Supabase rejected API access token. "
            "error_type=%s",
            type(
                exc
            ).__name__,
        )

        raise APIAccessTokenError(
            "Your authentication session is invalid or expired."
        ) from exc

    user = (
        _extract_supabase_user(
            auth_response
        )
    )

    if user is None:

        raise APIUserVerificationError(
            "Authenticated user could not be verified."
        )

    user_id = (
        _optional_text(
            getattr(
                user,
                "id",
                None,
            )
        )
    )

    if not user_id:

        raise APIUserVerificationError(
            "Authenticated user has no valid ID."
        )

    email = (
        _optional_text(
            getattr(
                user,
                "email",
                None,
            )
        )
    )

    display_name = (
        _extract_display_name(
            user
        )
    )

    return APIUser(
        id=user_id,
        email=email,
        display_name=display_name,
    )


# ============================================================
# BUILD REQUEST CONTEXT
# ============================================================


def build_api_request_context(
    *,
    access_token: str,
    conversation_id: str | None = None,
    locale: str | None = None,
    client_context: Mapping[
        str,
        Any,
    ] | None = None,
    request_id: str | None = None,
) -> APIRequestContext:
    """
    Create one VERIFIED API request context.

    User identity is always determined from Supabase token verification.

    The frontend cannot override:

        user.id
        email
    """

    access_token = (
        _validate_access_token(
            access_token
        )
    )

    user = (
        verify_supabase_access_token(
            access_token
        )
    )

    normalized_conversation_id = (
        normalize_conversation_id(
            conversation_id
        )
    )

    normalized_locale = (
        normalize_locale(
            locale
        )
    )

    normalized_client_context = (
        sanitize_client_context(
            client_context
        )
    )

    if request_id is None:

        request_id = str(
            uuid.uuid4()
        )

    else:

        request_id = str(
            request_id
        ).strip()

        if not request_id:

            request_id = str(
                uuid.uuid4()
            )

    return APIRequestContext(
        request_id=request_id,

        user=user,

        access_token=access_token,

        conversation_id=(
            normalized_conversation_id
        ),

        locale=(
            normalized_locale
        ),

        client_context=(
            normalized_client_context
        ),
    )


# ============================================================
# BUILD FROM AUTH HEADER
# ============================================================


def build_api_request_context_from_authorization(
    *,
    authorization_header: str,
    conversation_id: str | None = None,
    locale: str | None = None,
    client_context: Mapping[
        str,
        Any,
    ] | None = None,
    request_id: str | None = None,
) -> APIRequestContext:
    """
    Convenience function for api.py.

    Example:

        context = build_api_request_context_from_authorization(
            authorization_header=request.headers["Authorization"],
            conversation_id=body.conversation_id,
        )
    """

    access_token = (
        extract_bearer_token(
            authorization_header
        )
    )

    return build_api_request_context(
        access_token=access_token,
        conversation_id=conversation_id,
        locale=locale,
        client_context=client_context,
        request_id=request_id,
    )


# ============================================================
# ACTIVATE CONTEXT
# ============================================================


def activate_api_context(
    context: APIRequestContext,
) -> Token:
    """
    Activate request context for current async task/thread.

    Returns ContextVar token required for reset.
    """

    if not isinstance(
        context,
        APIRequestContext,
    ):

        raise APIContextValidationError(
            "context must be APIRequestContext."
        )

    return _CURRENT_API_CONTEXT.set(
        context
    )


# ============================================================
# RESET CONTEXT
# ============================================================


def reset_api_context(
    token: Token,
) -> None:
    """
    Restore previous ContextVar state.
    """

    _CURRENT_API_CONTEXT.reset(
        token
    )


# ============================================================
# CLEAR CONTEXT
# ============================================================


def clear_api_context() -> None:
    """
    Clear active API request context.

    Normally context managers should be preferred because they safely
    restore nested state.
    """

    _CURRENT_API_CONTEXT.set(
        None
    )


# ============================================================
# ACTIVE CHECK
# ============================================================


def is_api_request_active() -> bool:

    return (
        _CURRENT_API_CONTEXT.get()
        is not None
    )


# ============================================================
# GET CONTEXT
# ============================================================


def get_api_request_context(
    *,
    required: bool = False,
) -> APIRequestContext | None:

    context = (
        _CURRENT_API_CONTEXT.get()
    )

    if (
        context is None
        and required
    ):

        raise APIRequestContextMissingError(
            "No active API request context is available."
        )

    return context


def require_api_request_context() -> (
    APIRequestContext
):
    """
    Return active context or raise.
    """

    context = (
        get_api_request_context(
            required=True
        )
    )

    assert context is not None

    return context


# ============================================================
# USER GETTERS
# ============================================================


def get_api_user() -> APIUser | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        return None

    return context.user


def get_api_user_id(
    *,
    required: bool = True,
) -> str | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        if required:

            raise APIRequestContextMissingError(
                "No authenticated API user is available."
            )

        return None

    return context.user_id


def get_api_user_email() -> str | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        return None

    return context.email


# ============================================================
# ACCESS TOKEN GETTER
# ============================================================


def get_api_access_token(
    *,
    required: bool = True,
) -> str | None:
    """
    Internal only.

    NEVER return this value through API JSON.

    NEVER log it.

    This will later be used by auth/session.py compatibility code to
    create a user-scoped Supabase client for cart/order operations.
    """

    context = (
        get_api_request_context()
    )

    if context is None:

        if required:

            raise APIRequestContextMissingError(
                "No API access token is available."
            )

        return None

    return context.access_token


# ============================================================
# CONVERSATION ID
# ============================================================


def get_api_conversation_id(
    *,
    required: bool = True,
) -> str | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        if required:

            raise APIRequestContextMissingError(
                "No API conversation is active."
            )

        return None

    return context.conversation_id


# ============================================================
# REQUEST ID
# ============================================================


def get_api_request_id() -> str | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        return None

    return context.request_id


# ============================================================
# LOCALE
# ============================================================


def get_api_locale() -> str | None:

    context = (
        get_api_request_context()
    )

    if context is None:

        return None

    return context.locale


# ============================================================
# CLIENT CONTEXT
# ============================================================


def get_api_client_context() -> dict[
    str,
    Any,
]:

    context = (
        get_api_request_context()
    )

    if context is None:

        return {}

    return copy.deepcopy(
        context.client_context
    )


# ============================================================
# SAFE CONTEXT SNAPSHOT
# ============================================================


def get_safe_api_context_snapshot() -> dict[
    str,
    Any,
]:
    """
    Safe debugging snapshot.

    ACCESS TOKEN IS NOT INCLUDED.
    """

    context = (
        get_api_request_context()
    )

    if context is None:

        return {
            "active":
                False,
        }

    snapshot = (
        context.safe_dict()
    )

    snapshot[
        "active"
    ] = True

    return snapshot


# ============================================================
# SYNCHRONOUS CONTEXT MANAGER
# ============================================================


@contextmanager
def api_request_context(
    context: APIRequestContext,
) -> Iterator[
    APIRequestContext
]:
    """
    Safely activate one API request.

    Example:

        context = build_api_request_context(...)

        with api_request_context(context):

            result = process_message(
                "show my cart"
            )

    Context is automatically removed afterward.
    """

    token = (
        activate_api_context(
            context
        )
    )

    try:

        yield context

    finally:

        reset_api_context(
            token
        )


# ============================================================
# ASYNC CONTEXT MANAGER
# ============================================================


@asynccontextmanager
async def async_api_request_context(
    context: APIRequestContext,
) -> AsyncIterator[
    APIRequestContext
]:
    """
    Async equivalent for FastAPI endpoints.

    Example:

        async with async_api_request_context(context):

            result = await run_in_threadpool(
                process_message,
                body.message,
            )
    """

    token = (
        activate_api_context(
            context
        )
    )

    try:

        yield context

    finally:

        reset_api_context(
            token
        )


# ============================================================
# CONVENIENCE AUTHENTICATED SCOPE
# ============================================================


@contextmanager
def authenticated_api_scope(
    *,
    authorization_header: str,
    conversation_id: str | None = None,
    locale: str | None = None,
    client_context: Mapping[
        str,
        Any,
    ] | None = None,
    request_id: str | None = None,
) -> Iterator[
    APIRequestContext
]:
    """
    Complete synchronous flow:

        Authorization header
            ↓
        extract token
            ↓
        verify Supabase user
            ↓
        create request context
            ↓
        activate
            ↓
        execute application code
            ↓
        cleanup

    Useful for tests and non-FastAPI HTTP adapters.
    """

    context = (
        build_api_request_context_from_authorization(
            authorization_header=(
                authorization_header
            ),
            conversation_id=(
                conversation_id
            ),
            locale=(
                locale
            ),
            client_context=(
                client_context
            ),
            request_id=(
                request_id
            ),
        )
    )

    with api_request_context(
        context
    ):

        yield context


# ============================================================
# ASYNC AUTHENTICATED SCOPE
# ============================================================


@asynccontextmanager
async def authenticated_async_api_scope(
    *,
    authorization_header: str,
    conversation_id: str | None = None,
    locale: str | None = None,
    client_context: Mapping[
        str,
        Any,
    ] | None = None,
    request_id: str | None = None,
) -> AsyncIterator[
    APIRequestContext
]:
    """
    Complete asynchronous request-authentication scope for FastAPI.
    """

    context = (
        build_api_request_context_from_authorization(
            authorization_header=(
                authorization_header
            ),
            conversation_id=(
                conversation_id
            ),
            locale=(
                locale
            ),
            client_context=(
                client_context
            ),
            request_id=(
                request_id
            ),
        )
    )

    async with async_api_request_context(
        context
    ):

        yield context


# ============================================================
# DEVELOPMENT CHECK
# ============================================================


def check_api_context_configuration() -> dict[
    str,
    Any,
]:
    """
    Local configuration check.

    Does NOT verify a user.
    Does NOT make an authentication request.
    Does NOT expose secrets.
    """

    try:

        create_public_client()

        supabase_available = True

    except Exception:

        supabase_available = False

    return {
        "api_context":
            True,

        "context_isolation":
            "contextvars",

        "streamlit_dependency":
            False,

        "supabase_public_client_available":
            supabase_available,

        "identity_source":
            "verified_supabase_access_token",

        "accepts_arbitrary_user_id":
            False,

        "access_token_in_public_response":
            False,

        "frontend_agnostic":
            True,
    }