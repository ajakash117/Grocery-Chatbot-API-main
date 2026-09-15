"""
auth/session.py

Central Streamlit session manager for the Grocery Chatbot.

========================================================================
RESPONSIBILITY
========================================================================

This module owns ALL per-user Streamlit session state.

It manages:

- authentication state
- authenticated Supabase user ID
- email
- access token
- refresh token
- token expiration metadata
- chat history
- per-user conversation context
- pending chatbot actions
- current product/SKU context
- session-safe Supabase client reconstruction
- token synchronization after refresh
- logout/local cleanup

Other files should NOT directly scatter authentication values through:

    st.session_state["something"]

Instead, they should use the functions in this module.

========================================================================
WHY THIS MATTERS
========================================================================

Streamlit reruns app.py from top to bottom whenever the user interacts
with the UI.

Normal Python variables therefore disappear between reruns.

Streamlit Session State persists values for that user's session.

Example:

Browser/User A
    |
    +-- session_state
            |
            +-- User A access token
            +-- User A refresh token
            +-- User A user_id
            +-- User A messages


Browser/User B
    |
    +-- DIFFERENT session_state
            |
            +-- User B access token
            +-- User B refresh token
            +-- User B user_id
            +-- User B messages


========================================================================
IMPORTANT SECURITY DESIGN
========================================================================

We NEVER store a globally authenticated Supabase client.

Instead, Session State stores only primitive authentication data:

    access_token
    refresh_token
    user_id
    email
    token expiry

Whenever database access is required:

    session tokens
        |
        v
    create_user_client()
        |
        v
    fresh Supabase client
        |
        v
    Supabase verifies user
        |
        v
    RLS applies


This prevents a cached authenticated client from accidentally being
shared between Streamlit users.

========================================================================
DO NOT STORE
========================================================================

The following must NEVER be placed into st.session_state:

- SUPABASE_SECRET_KEY
- GROQ_API_KEY
- COHERE_API_KEY
- database passwords
- admin Supabase clients
- raw application environment variables

The authenticated user's own access/refresh tokens ARE stored because
they are necessary to maintain their Supabase session.

They must never be displayed in the UI or sent to Cohere/Groq.
"""

from __future__ import annotations

import copy
import logging
import time
import uuid

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import streamlit as st

from config import settings
from database.supabase import (
    SupabaseAuthenticationError,
    UserScopedSupabaseClient,
    create_user_client,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# SESSION KEY NAMESPACE
# ============================================================

# Using a prefix prevents collisions with Streamlit widget keys.
#
# For example:
#
#     st.text_input(..., key="email")
#
# should not accidentally overwrite our authenticated email value.

SESSION_PREFIX = "_grocery_chatbot_"


def _key(name: str) -> str:
    """
    Create a namespaced Streamlit Session State key.
    """

    return f"{SESSION_PREFIX}{name}"


# ============================================================
# AUTH SESSION KEYS
# ============================================================

KEY_INITIALIZED = _key("initialized")

KEY_SESSION_ID = _key("session_id")

KEY_AUTHENTICATED = _key("authenticated")

KEY_USER_ID = _key("user_id")

KEY_USER_EMAIL = _key("user_email")

KEY_ACCESS_TOKEN = _key("access_token")

KEY_REFRESH_TOKEN = _key("refresh_token")

KEY_EXPIRES_AT = _key("expires_at")

KEY_EXPIRES_IN = _key("expires_in")

KEY_TOKEN_TYPE = _key("token_type")

KEY_AUTH_PROVIDER = _key("auth_provider")

KEY_AUTH_GENERATION = _key("auth_generation")

KEY_LAST_ACTIVITY = _key("last_activity")


# ============================================================
# CHAT SESSION KEYS
# ============================================================

KEY_MESSAGES = _key("messages")

KEY_PENDING_ACTION = _key("pending_action")

KEY_CONVERSATION_CONTEXT = _key(
    "conversation_context"
)

KEY_LAST_PRODUCT_CONTEXT = _key(
    "last_product_context"
)


# ============================================================
# CHAT CONSTANTS
# ============================================================

ALLOWED_CHAT_ROLES = {
    "user",
    "assistant",
}

MAX_MESSAGE_LENGTH = 20_000

MAX_METADATA_DEPTH_SAFE_SIZE = 20_000


# Distinguishes an omitted context value from an explicit None value.
_CONTEXT_VALUE_NOT_SET = object()


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================


class SessionManagerError(RuntimeError):
    """
    Base exception for Streamlit session-manager failures.
    """

    pass


class NotAuthenticatedError(SessionManagerError):
    """
    Raised when an authenticated session is required but unavailable.
    """

    pass


class InvalidSessionError(SessionManagerError):
    """
    Raised when authentication/session data is malformed.
    """

    pass


class SessionIdentityMismatchError(
    SessionManagerError
):
    """
    Raised when locally stored user identity does not match the identity
    verified by Supabase.

    This should never happen during normal operation.

    If it does happen, we immediately clear local user state.
    """

    pass


# ============================================================
# SAFE SESSION SNAPSHOT
# ============================================================


@dataclass(slots=True, frozen=True)
class SessionSnapshot:
    """
    Safe representation of the current Streamlit session.

    Authentication tokens are deliberately excluded.

    This object may be used in debugging or logs.
    """

    session_id: str

    initialized: bool

    authenticated: bool

    user_id: str | None

    email: str | None

    auth_provider: str | None

    expires_at: int | None

    token_expired: bool

    chat_message_count: int

    auth_generation: int

    last_activity: str | None


# ============================================================
# TIME HELPERS
# ============================================================


def _utc_timestamp() -> int:
    """
    Return current UTC Unix timestamp.
    """

    return int(
        time.time()
    )


def _utc_iso() -> str:
    """
    Return current UTC time as ISO-8601.
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# DEFAULT SESSION STATE
# ============================================================


def _default_state() -> dict[str, Any]:
    """
    Return default Streamlit session values.

    A function is used instead of a constant because mutable values
    such as lists/dictionaries must be recreated for each session.
    """

    return {
        KEY_INITIALIZED: True,

        KEY_SESSION_ID: str(
            uuid.uuid4()
        ),

        KEY_AUTHENTICATED: False,

        KEY_USER_ID: None,

        KEY_USER_EMAIL: None,

        KEY_ACCESS_TOKEN: None,

        KEY_REFRESH_TOKEN: None,

        KEY_EXPIRES_AT: None,

        KEY_EXPIRES_IN: None,

        KEY_TOKEN_TYPE: None,

        KEY_AUTH_PROVIDER: None,

        KEY_AUTH_GENERATION: 0,

        KEY_LAST_ACTIVITY: _utc_iso(),

        KEY_MESSAGES: [],

        KEY_PENDING_ACTION: None,

        KEY_CONVERSATION_CONTEXT: {},

        KEY_LAST_PRODUCT_CONTEXT: None,
    }


# ============================================================
# INITIALIZATION
# ============================================================


def initialize_session() -> None:
    """
    Initialize Streamlit Session State.

    Safe to call repeatedly.

    Recommended usage
    -----------------

    At the beginning of app.py:

        from auth.session import initialize_session

        initialize_session()

    Streamlit reruns the script frequently, so this function checks
    whether values already exist before creating them.

    Existing session values are never overwritten.
    """

    defaults = _default_state()

    for key, value in defaults.items():

        if key not in st.session_state:

            # Deep copy mutable structures so no shared mutable object
            # can accidentally be reused.

            st.session_state[key] = copy.deepcopy(
                value
            )


# ============================================================
# INTERNAL INITIALIZATION GUARD
# ============================================================


def _ensure_initialized() -> None:
    """
    Ensure our application session keys exist.
    """

    if (
        KEY_INITIALIZED
        not in st.session_state
    ):
        initialize_session()


# ============================================================
# ACTIVITY TRACKING
# ============================================================


def touch_session() -> None:
    """
    Update last activity timestamp.

    This does not currently enforce inactivity logout.

    We store the timestamp now because it may later be useful for:

        - session timeout policies
        - debugging
        - inactivity logout
        - session analytics
    """

    _ensure_initialized()

    st.session_state[
        KEY_LAST_ACTIVITY
    ] = _utc_iso()


# ============================================================
# BASIC SESSION INFORMATION
# ============================================================


def get_session_id() -> str:
    """
    Return random application session identifier.

    This identifier is NOT:
        - Supabase user ID
        - authentication token
        - browser cookie

    It exists only to distinguish application sessions in safe logs.
    """

    _ensure_initialized()

    return str(
        st.session_state[
            KEY_SESSION_ID
        ]
    )


def is_authenticated() -> bool:
    """
    Return whether local Streamlit state contains a complete
    authenticated user session.

    IMPORTANT
    ---------

    This is a local-state check.

    Sensitive database operations still reconstruct the Supabase client
    and allow Supabase to validate the session.
    """

    _ensure_initialized()

    authenticated = bool(
        st.session_state.get(
            KEY_AUTHENTICATED,
            False,
        )
    )

    if not authenticated:
        return False

    # A valid application auth state must contain all required pieces.

    required_values = (
        st.session_state.get(
            KEY_USER_ID
        ),
        st.session_state.get(
            KEY_ACCESS_TOKEN
        ),
        st.session_state.get(
            KEY_REFRESH_TOKEN
        ),
    )

    return all(
        isinstance(value, str)
        and bool(value.strip())
        for value in required_values
    )


# ============================================================
# REQUIRE AUTHENTICATION
# ============================================================


def require_authenticated() -> None:
    """
    Raise NotAuthenticatedError unless the user is logged in.

    Useful at the beginning of protected operations.
    """

    if not is_authenticated():

        raise NotAuthenticatedError(
            "You must be logged in to perform this action."
        )


# ============================================================
# AUTHENTICATED USER GETTERS
# ============================================================


def get_user_id(
    *,
    required: bool = True,
) -> str | None:
    """
    Return the authenticated user's Supabase UUID.

    Parameters
    ----------
    required:
        True:
            Raise if not authenticated.

        False:
            Return None when unauthenticated.
    """

    _ensure_initialized()

    if not is_authenticated():

        if required:

            raise NotAuthenticatedError(
                "No authenticated user is available."
            )

        return None

    user_id = st.session_state.get(
        KEY_USER_ID
    )

    if not isinstance(
        user_id,
        str,
    ) or not user_id.strip():

        if required:

            raise InvalidSessionError(
                "Authenticated session has no valid user ID."
            )

        return None

    return user_id.strip()


def get_user_email() -> str | None:
    """
    Return current authenticated user's email.
    """

    _ensure_initialized()

    if not is_authenticated():
        return None

    value = st.session_state.get(
        KEY_USER_EMAIL
    )

    if value is None:
        return None

    return str(value)


def get_access_token(
    *,
    required: bool = True,
) -> str | None:
    """
    Return current access token.

    NEVER print the returned value.
    NEVER send it to Cohere/Groq.
    """

    _ensure_initialized()

    value = st.session_state.get(
        KEY_ACCESS_TOKEN
    )

    if (
        isinstance(value, str)
        and value.strip()
    ):
        return value.strip()

    if required:

        raise NotAuthenticatedError(
            "No access token is available."
        )

    return None


def get_refresh_token(
    *,
    required: bool = True,
) -> str | None:
    """
    Return current refresh token.

    NEVER print or expose this value.
    """

    _ensure_initialized()

    value = st.session_state.get(
        KEY_REFRESH_TOKEN
    )

    if (
        isinstance(value, str)
        and value.strip()
    ):
        return value.strip()

    if required:

        raise NotAuthenticatedError(
            "No refresh token is available."
        )

    return None


# ============================================================
# TOKEN EXPIRATION
# ============================================================


def get_expires_at() -> int | None:
    """
    Return Supabase access-token expiration timestamp.
    """

    _ensure_initialized()

    value = st.session_state.get(
        KEY_EXPIRES_AT
    )

    if value is None:
        return None

    try:
        return int(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


def seconds_until_token_expiry() -> int | None:
    """
    Return approximate seconds remaining before token expiration.

    Returns None if expiration metadata is unavailable.
    """

    expires_at = get_expires_at()

    if expires_at is None:
        return None

    return expires_at - _utc_timestamp()


def is_access_token_expired(
    *,
    leeway_seconds: int = 30,
) -> bool:
    """
    Determine whether access token is expired or about to expire.

    The small leeway avoids starting an operation with a token that may
    expire during the request.

    This function does NOT itself refresh the token.

    create_user_supabase_client() will reconstruct the Supabase session,
    and Supabase set_session() can refresh expired sessions.
    """

    if leeway_seconds < 0:

        raise ValueError(
            "leeway_seconds cannot be negative."
        )

    remaining = (
        seconds_until_token_expiry()
    )

    if remaining is None:

        # Unknown does not mean expired.
        return False

    return remaining <= leeway_seconds


# ============================================================
# CLEAR USER-SCOPED APPLICATION DATA
# ============================================================


def _clear_user_scoped_state() -> None:
    """
    Clear data that must never survive when switching users.

    Called during:

        login as another account
        logout
        authentication failure
        identity mismatch
    """

    st.session_state[
        KEY_MESSAGES
    ] = []

    st.session_state[
        KEY_PENDING_ACTION
    ] = None

    st.session_state[
        KEY_CONVERSATION_CONTEXT
    ] = {}

    st.session_state[
        KEY_LAST_PRODUCT_CONTEXT
    ] = None


# ============================================================
# SAVE AUTHENTICATED SESSION
# ============================================================


def set_authenticated_session(
    *,
    user_id: str,
    access_token: str,
    refresh_token: str,
    email: str | None = None,
    expires_at: int | None = None,
    expires_in: int | None = None,
    token_type: str | None = None,
    auth_provider: str | None = None,
) -> None:
    """
    Save a verified Supabase authentication session into Streamlit.

    IMPORTANT
    ---------

    Call this only using values returned by Supabase authentication.

    Do NOT call it using values generated by:
        - Cohere
        - Groq
        - user messages
        - query parameters


    User switching protection
    -------------------------

    If Streamlit currently contains User A and this function receives
    User B, all user-specific conversation state is cleared before
    storing User B.

    This guarantees that User B cannot inherit User A's:

        chat messages
        pending actions
        product context
    """

    _ensure_initialized()

    # --------------------------------------------------------
    # Validate user ID
    # --------------------------------------------------------

    if not isinstance(
        user_id,
        str,
    ) or not user_id.strip():

        raise InvalidSessionError(
            "Supabase user_id is missing."
        )

    user_id = user_id.strip()

    # --------------------------------------------------------
    # Validate access token
    # --------------------------------------------------------

    if not isinstance(
        access_token,
        str,
    ) or not access_token.strip():

        raise InvalidSessionError(
            "Supabase access token is missing."
        )

    access_token = access_token.strip()

    # --------------------------------------------------------
    # Validate refresh token
    # --------------------------------------------------------

    if not isinstance(
        refresh_token,
        str,
    ) or not refresh_token.strip():

        raise InvalidSessionError(
            "Supabase refresh token is missing."
        )

    refresh_token = refresh_token.strip()

    # --------------------------------------------------------
    # Detect user switch
    # --------------------------------------------------------

    previous_user_id = (
        st.session_state.get(
            KEY_USER_ID
        )
    )

    user_changed = (
        previous_user_id is not None
        and str(previous_user_id)
        != user_id
    )

    if user_changed:

        logger.info(
            "Authenticated account changed within "
            "Streamlit session %s. "
            "Clearing user-scoped state.",
            get_session_id(),
        )

        _clear_user_scoped_state()

    # --------------------------------------------------------
    # Store authentication data
    # --------------------------------------------------------

    st.session_state[
        KEY_USER_ID
    ] = user_id

    st.session_state[
        KEY_USER_EMAIL
    ] = (
        str(email)
        if email
        else None
    )

    st.session_state[
        KEY_ACCESS_TOKEN
    ] = access_token

    st.session_state[
        KEY_REFRESH_TOKEN
    ] = refresh_token

    st.session_state[
        KEY_EXPIRES_AT
    ] = (
        int(expires_at)
        if expires_at is not None
        else None
    )

    st.session_state[
        KEY_EXPIRES_IN
    ] = (
        int(expires_in)
        if expires_in is not None
        else None
    )

    st.session_state[
        KEY_TOKEN_TYPE
    ] = (
        str(token_type)
        if token_type
        else None
    )

    st.session_state[
        KEY_AUTH_PROVIDER
    ] = (
        str(auth_provider)
        if auth_provider
        else None
    )

    st.session_state[
        KEY_AUTHENTICATED
    ] = True

    # Increment authentication generation.
    #
    # Useful later if cached resources need to know whether
    # authentication changed.

    current_generation = int(
        st.session_state.get(
            KEY_AUTH_GENERATION,
            0,
        )
    )

    st.session_state[
        KEY_AUTH_GENERATION
    ] = (
        current_generation + 1
    )

    touch_session()

    logger.info(
        "Authenticated Streamlit session established. "
        "session_id=%s user_id=%s",
        get_session_id(),
        user_id,
    )


# ============================================================
# SAVE SUPABASE AUTH RESPONSE
# ============================================================


def store_auth_response(
    auth_response: Any,
    *,
    auth_provider: str | None = None,
) -> bool:
    """
    Extract a Supabase authentication response and store it safely.

    This function will be used by auth/auth.py after:

        sign_in_with_password()
        sign_up() when email confirmation is disabled
        exchange_code_for_session()
        other session-producing auth flows


    Supabase response normally resembles:

        response.user
        response.session

    Where session contains:

        access_token
        refresh_token
        expires_at
        expires_in
        token_type


    Returns
    -------
    bool

        True:
            Authentication session was stored.

        False:
            Supabase returned a user but no session.

            This commonly happens after signup when email confirmation
            is enabled.
    """

    _ensure_initialized()

    if auth_response is None:

        raise InvalidSessionError(
            "Supabase authentication returned no response."
        )

    # --------------------------------------------------------
    # Extract session
    # --------------------------------------------------------

    auth_session = getattr(
        auth_response,
        "session",
        None,
    )

    # Email-confirmation signup can legitimately return no session.
    if auth_session is None:

        logger.info(
            "Supabase auth response contains no active session."
        )

        return False

    # --------------------------------------------------------
    # Extract user
    # --------------------------------------------------------

    user = getattr(
        auth_response,
        "user",
        None,
    )

    if user is None:

        # Some SDK response shapes may expose user from session.

        user = getattr(
            auth_session,
            "user",
            None,
        )

    if user is None:

        raise InvalidSessionError(
            "Supabase authentication response has no user."
        )

    # --------------------------------------------------------
    # User ID
    # --------------------------------------------------------

    user_id = getattr(
        user,
        "id",
        None,
    )

    if not user_id:

        raise InvalidSessionError(
            "Supabase authenticated user has no ID."
        )

    # --------------------------------------------------------
    # Email
    # --------------------------------------------------------

    email = getattr(
        user,
        "email",
        None,
    )

    # --------------------------------------------------------
    # Tokens
    # --------------------------------------------------------

    access_token = getattr(
        auth_session,
        "access_token",
        None,
    )

    refresh_token = getattr(
        auth_session,
        "refresh_token",
        None,
    )

    if not access_token:

        raise InvalidSessionError(
            "Supabase auth session contains no access token."
        )

    if not refresh_token:

        raise InvalidSessionError(
            "Supabase auth session contains no refresh token."
        )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    set_authenticated_session(
        user_id=str(user_id),
        email=(
            str(email)
            if email
            else None
        ),
        access_token=str(
            access_token
        ),
        refresh_token=str(
            refresh_token
        ),
        expires_at=getattr(
            auth_session,
            "expires_at",
            None,
        ),
        expires_in=getattr(
            auth_session,
            "expires_in",
            None,
        ),
        token_type=getattr(
            auth_session,
            "token_type",
            None,
        ),
        auth_provider=auth_provider,
    )

    return True


# ============================================================
# SYNCHRONIZE REFRESHED TOKENS
# ============================================================


def sync_from_user_client(
    user_client: UserScopedSupabaseClient,
) -> None:
    """
    Synchronize Streamlit state with a user-scoped Supabase client.

    database/supabase.py uses:

        supabase.auth.set_session(
            access_token,
            refresh_token
        )

    Supabase may refresh expired credentials.

    When that occurs, UserScopedSupabaseClient contains the NEW tokens.

    We immediately save those tokens here.


    SECURITY CHECK
    --------------

    Supabase's verified user ID must equal our locally stored user ID.

    If not, the session is cleared immediately.
    """

    require_authenticated()

    if not isinstance(
        user_client,
        UserScopedSupabaseClient,
    ):

        raise TypeError(
            "user_client must be UserScopedSupabaseClient."
        )

    current_user_id = get_user_id()

    verified_user_id = (
        user_client.user_id
    )

    # --------------------------------------------------------
    # Identity integrity check
    # --------------------------------------------------------

    if (
        current_user_id
        != verified_user_id
    ):

        logger.error(
            "CRITICAL session identity mismatch. "
            "session_id=%s",
            get_session_id(),
        )

        clear_authentication()

        raise SessionIdentityMismatchError(
            "Authentication identity changed unexpectedly. "
            "Please sign in again."
        )

    # --------------------------------------------------------
    # Synchronize refreshed credentials
    # --------------------------------------------------------

    st.session_state[
        KEY_ACCESS_TOKEN
    ] = user_client.access_token

    st.session_state[
        KEY_REFRESH_TOKEN
    ] = user_client.refresh_token

    st.session_state[
        KEY_EXPIRES_AT
    ] = user_client.expires_at

    st.session_state[
        KEY_EXPIRES_IN
    ] = user_client.expires_in

    st.session_state[
        KEY_TOKEN_TYPE
    ] = user_client.token_type

    # Supabase-verified email wins over stale local information.

    if user_client.email:

        st.session_state[
            KEY_USER_EMAIL
        ] = user_client.email

    touch_session()


# ============================================================
# CREATE CURRENT USER SUPABASE CLIENT
# ============================================================


def get_user_supabase_client(
    *,
    verify_user: bool = True,
) -> UserScopedSupabaseClient:
    """
    Reconstruct a Supabase client for the currently authenticated user.

    This should be the standard entry point used by database modules.

    Example
    -------

    from auth.session import get_user_supabase_client

    user_db = get_user_supabase_client()

    response = (
        user_db.client
        .table("cart_items")
        .select("*")
        .execute()
    )


    Flow
    ----

    Streamlit session
        |
        +-- access token
        +-- refresh token
        |
        v
    create_user_client()
        |
        v
    Supabase set_session()
        |
        +-- validates session
        +-- refreshes if required
        |
        v
    Supabase verifies user
        |
        v
    sync refreshed credentials
        |
        v
    return user-scoped client


    IMPORTANT
    ---------

    If session validation fails, local authentication is cleared.

    We do not keep pretending the user is authenticated after Supabase
    rejects their session.
    """

    require_authenticated()

    access_token = get_access_token()

    refresh_token = get_refresh_token()

    try:

        user_client = create_user_client(
            access_token=access_token,
            refresh_token=refresh_token,
            verify_user=verify_user,
        )

    except SupabaseAuthenticationError as exc:

        logger.warning(
            "Supabase rejected current Streamlit auth session. "
            "session_id=%s",
            get_session_id(),
        )

        clear_authentication()

        raise NotAuthenticatedError(
            "Your session has expired. Please sign in again."
        ) from exc

    # --------------------------------------------------------
    # Synchronize refreshed tokens and verify identity
    # --------------------------------------------------------

    sync_from_user_client(
        user_client
    )

    return user_client


# ============================================================
# CLEAR AUTHENTICATION
# ============================================================


def clear_authentication() -> None:
    """
    Clear all local authenticated-user state.

    This performs LOCAL session cleanup only.

    auth/auth.py will later handle remote Supabase sign_out() first,
    then call this function.

    We preserve:

        application session_id
        initialized flag

    but clear everything belonging to the authenticated user.
    """

    _ensure_initialized()

    previous_user_id = (
        st.session_state.get(
            KEY_USER_ID
        )
    )

    # --------------------------------------------------------
    # Clear user-specific app data FIRST
    # --------------------------------------------------------

    _clear_user_scoped_state()

    # --------------------------------------------------------
    # Clear credentials
    # --------------------------------------------------------

    st.session_state[
        KEY_AUTHENTICATED
    ] = False

    st.session_state[
        KEY_USER_ID
    ] = None

    st.session_state[
        KEY_USER_EMAIL
    ] = None

    st.session_state[
        KEY_ACCESS_TOKEN
    ] = None

    st.session_state[
        KEY_REFRESH_TOKEN
    ] = None

    st.session_state[
        KEY_EXPIRES_AT
    ] = None

    st.session_state[
        KEY_EXPIRES_IN
    ] = None

    st.session_state[
        KEY_TOKEN_TYPE
    ] = None

    st.session_state[
        KEY_AUTH_PROVIDER
    ] = None

    # Authentication generation changes on logout too.

    current_generation = int(
        st.session_state.get(
            KEY_AUTH_GENERATION,
            0,
        )
    )

    st.session_state[
        KEY_AUTH_GENERATION
    ] = (
        current_generation + 1
    )

    touch_session()

    logger.info(
        "Local authentication cleared. "
        "session_id=%s previous_user=%s",
        get_session_id(),
        previous_user_id,
    )


# ============================================================
# FULL SESSION RESET
# ============================================================


def reset_entire_session() -> None:
    """
    Completely reset our grocery-chatbot Session State namespace.

    This is stronger than clear_authentication().

    Use only when we want to recreate the application session itself.

    It does NOT delete unrelated Streamlit widget keys belonging to
    other components.
    """

    keys_to_delete = [
        key
        for key in list(
            st.session_state.keys()
        )
        if str(key).startswith(
            SESSION_PREFIX
        )
    ]

    for key in keys_to_delete:

        del st.session_state[key]

    initialize_session()


# ============================================================
# CHAT HISTORY
# ============================================================


def add_chat_message(
    role: str,
    content: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """
    Add a user or assistant message to current user's chat history.

    Parameters
    ----------
    role:
        "user" or "assistant"

    content:
        Message text.

    metadata:
        Optional INTERNAL metadata.

        Examples:

            {
                "action": "search_products"
            }

            {
                "tool_success": True
            }

    IMPORTANT
    ---------

    Never place credentials or API keys inside metadata.
    """

    require_authenticated()

    if role not in ALLOWED_CHAT_ROLES:

        raise ValueError(
            f"Chat role must be one of "
            f"{sorted(ALLOWED_CHAT_ROLES)}."
        )

    if not isinstance(
        content,
        str,
    ):

        raise TypeError(
            "Chat message content must be a string."
        )

    content = content.strip()

    if not content:

        raise ValueError(
            "Chat message cannot be empty."
        )

    if len(
        content
    ) > MAX_MESSAGE_LENGTH:

        raise ValueError(
            f"Chat message exceeds maximum length "
            f"of {MAX_MESSAGE_LENGTH} characters."
        )

    message: dict[str, Any] = {
        "role": role,
        "content": content,
        "created_at": _utc_iso(),
    }

    # --------------------------------------------------------
    # Optional metadata
    # --------------------------------------------------------

    if metadata is not None:

        if not isinstance(
            metadata,
            Mapping,
        ):

            raise TypeError(
                "Chat metadata must be a mapping."
            )

        safe_metadata = copy.deepcopy(
            dict(metadata)
        )

        # Very basic protection against accidentally putting huge
        # objects into session state.

        metadata_length = len(
            repr(safe_metadata)
        )

        if (
            metadata_length
            > MAX_METADATA_DEPTH_SAFE_SIZE
        ):

            raise ValueError(
                "Chat metadata is too large."
            )

        message[
            "metadata"
        ] = safe_metadata

    # --------------------------------------------------------
    # Append
    # --------------------------------------------------------

    messages = st.session_state.get(
        KEY_MESSAGES
    )

    if not isinstance(
        messages,
        list,
    ):

        messages = []

    messages.append(
        message
    )

    # --------------------------------------------------------
    # History limit
    # --------------------------------------------------------
    #
    # Your .env currently contains:
    #
    # MAX_CHAT_HISTORY=10
    #
    # So we keep the latest 10 messages.
    #
    # This avoids unbounded memory growth and also aligns the stored
    # conversation with what Cohere/Groq are configured to consume.
    # --------------------------------------------------------

    if len(
        messages
    ) > settings.max_chat_history:

        messages = messages[
            -settings.max_chat_history:
        ]

    st.session_state[
        KEY_MESSAGES
    ] = messages

    touch_session()


def get_chat_messages() -> list[dict[str, Any]]:
    """
    Return a defensive copy of current user's complete chat messages.

    A copy is returned so callers cannot accidentally mutate internal
    Session State without using this manager.
    """

    _ensure_initialized()

    messages = st.session_state.get(
        KEY_MESSAGES,
        [],
    )

    if not isinstance(
        messages,
        list,
    ):
        return []

    return copy.deepcopy(
        messages
    )


def get_model_chat_history() -> list[dict[str, str]]:
    """
    Return chat history in the format Cohere/Groq need.

    Internal metadata and timestamps are removed.

    Output example:

        [
            {
                "role": "user",
                "content": "Show me milk"
            },
            {
                "role": "assistant",
                "content": "Here are..."
            }
        ]
    """

    messages = get_chat_messages()

    history: list[
        dict[str, str]
    ] = []

    for message in messages:

        role = message.get(
            "role"
        )

        content = message.get(
            "content"
        )

        if (
            role in ALLOWED_CHAT_ROLES
            and isinstance(
                content,
                str,
            )
            and content.strip()
        ):

            history.append(
                {
                    "role": role,
                    "content": (
                        content.strip()
                    ),
                }
            )

    return history


def clear_chat_history() -> None:
    """
    Clear only the current user's conversation history.

    Authentication remains active.
    """

    _ensure_initialized()

    st.session_state[
        KEY_MESSAGES
    ] = []

    st.session_state[
        KEY_PENDING_ACTION
    ] = None

    st.session_state[
        KEY_CONVERSATION_CONTEXT
    ] = {}

    st.session_state[
        KEY_LAST_PRODUCT_CONTEXT
    ] = None

    touch_session()


# ============================================================
# CONVERSATION CONTEXT
# ============================================================


def set_conversation_context(
    name_or_context: str | Mapping[str, Any],
    value: Any = _CONTEXT_VALUE_NOT_SET,
) -> None:
    """
    Save user-scoped conversational state.

    Two calling styles are supported intentionally.

    Replace the COMPLETE context dictionary:

        set_conversation_context(
            {
                "last_tool": "search_products",
                "last_tool_success": True,
            }
        )

        set_conversation_context({})

    Set/update ONE named context value:

        set_conversation_context(
            "last_search_query",
            "milk",
        )

    Why support both?
    -----------------

    brain.py stores a compact context dictionary in one call, while some
    older callers store individual named values. Supporting both forms keeps
    app.py, brain.py and existing helper code compatible.

    IMPORTANT
    ---------

    Never store passwords, access tokens, refresh tokens, API keys or other
    secrets here.
    """

    require_authenticated()

    # --------------------------------------------------------
    # STYLE 1:
    #
    #     set_conversation_context({...})
    #
    # Replace the complete context dictionary.
    # --------------------------------------------------------

    if (
        isinstance(
            name_or_context,
            Mapping,
        )
        and value is _CONTEXT_VALUE_NOT_SET
    ):

        new_context = copy.deepcopy(
            dict(
                name_or_context
            )
        )

        if (
            len(
                repr(
                    new_context
                )
            )
            > MAX_METADATA_DEPTH_SAFE_SIZE
        ):

            raise ValueError(
                "Conversation context is too large."
            )

        st.session_state[
            KEY_CONVERSATION_CONTEXT
        ] = new_context

        touch_session()

        return

    # --------------------------------------------------------
    # STYLE 2:
    #
    #     set_conversation_context("name", value)
    #
    # Update one key inside the existing context.
    # --------------------------------------------------------

    if (
        isinstance(
            name_or_context,
            str,
        )
        and name_or_context.strip()
        and value is not _CONTEXT_VALUE_NOT_SET
    ):

        context = st.session_state.get(
            KEY_CONVERSATION_CONTEXT
        )

        if not isinstance(
            context,
            dict,
        ):

            context = {}

        context = copy.deepcopy(
            context
        )

        context[
            name_or_context.strip()
        ] = copy.deepcopy(
            value
        )

        if (
            len(
                repr(
                    context
                )
            )
            > MAX_METADATA_DEPTH_SAFE_SIZE
        ):

            raise ValueError(
                "Conversation context is too large."
            )

        st.session_state[
            KEY_CONVERSATION_CONTEXT
        ] = context

        touch_session()

        return

    # --------------------------------------------------------
    # INVALID COMBINATIONS
    # --------------------------------------------------------

    if isinstance(
        name_or_context,
        Mapping,
    ):

        raise TypeError(
            "When setting the full conversation context, "
            "pass only the mapping."
        )

    if not isinstance(
        name_or_context,
        str,
    ) or not name_or_context.strip():

        raise ValueError(
            "Context name must be a non-empty string, "
            "or pass a mapping to replace the full context."
        )

    raise TypeError(
        "A value is required when setting a named conversation context."
    )


def get_conversation_context(
    name: str | None = None,
    *,
    default: Any = None,
) -> Any:
    """
    Retrieve conversation context.

    name=None:
        return full context dictionary.

    name="last_product_id":
        return one value.
    """

    _ensure_initialized()

    context = st.session_state.get(
        KEY_CONVERSATION_CONTEXT,
        {},
    )

    if not isinstance(
        context,
        dict,
    ):

        context = {}

    if name is None:

        return copy.deepcopy(
            context
        )

    return copy.deepcopy(
        context.get(
            name,
            default,
        )
    )


def remove_conversation_context(
    name: str,
) -> None:
    """
    Remove one conversation-context value.
    """

    _ensure_initialized()

    context = st.session_state.get(
        KEY_CONVERSATION_CONTEXT
    )

    if not isinstance(
        context,
        dict,
    ):
        return

    context.pop(
        name,
        None,
    )

    st.session_state[
        KEY_CONVERSATION_CONTEXT
    ] = context

    touch_session()


def clear_conversation_context() -> None:
    """
    Clear the complete conversation-context dictionary.

    Authentication and chat history remain unchanged.
    """

    _ensure_initialized()

    st.session_state[
        KEY_CONVERSATION_CONTEXT
    ] = {}

    touch_session()


# ============================================================
# PRODUCT CONTEXT
# ============================================================


def set_last_product_context(
    value: Any,
) -> None:
    """
    Store context from the most recent product search.

    Example structure:

        {
            "products": [
                {
                    "product_id": "...",
                    "name": "Amul Taaza Milk",
                    "variants": [
                        {
                            "sku_id": "...",
                            "size": "500 ml"
                        },
                        {
                            "sku_id": "...",
                            "size": "1 L"
                        }
                    ]
                }
            ]
        }

    This allows a follow-up like:

        "add the 1 litre one"

    to be resolved safely from the previously displayed options.

    The exact schema will later be produced by product_tools.py.
    """

    require_authenticated()

    st.session_state[
        KEY_LAST_PRODUCT_CONTEXT
    ] = copy.deepcopy(
        value
    )

    touch_session()


def get_last_product_context() -> Any:
    """
    Return most recently stored product context.
    """

    _ensure_initialized()

    return copy.deepcopy(
        st.session_state.get(
            KEY_LAST_PRODUCT_CONTEXT
        )
    )


def clear_last_product_context() -> None:
    """
    Clear product reference context.
    """

    _ensure_initialized()

    st.session_state[
        KEY_LAST_PRODUCT_CONTEXT
    ] = None

    touch_session()


# ============================================================
# PENDING ACTION
# ============================================================


def set_pending_action(
    action: Mapping[str, Any] | None,
) -> None:
    """
    Save an incomplete chatbot action requiring user clarification.

    Example:

        User:
            "Add Amul milk."

        The product has several SKUs, so the assistant stores a small
        pending action and asks which size the user wants.

    Passing None clears the pending action.

    Secrets must never be stored here.
    """

    require_authenticated()

    if action is None:

        st.session_state[
            KEY_PENDING_ACTION
        ] = None

        touch_session()

        return

    if not isinstance(
        action,
        Mapping,
    ):

        raise TypeError(
            "Pending action must be a mapping or None."
        )

    safe_action = copy.deepcopy(
        dict(
            action
        )
    )

    if (
        len(
            repr(
                safe_action
            )
        )
        > MAX_METADATA_DEPTH_SAFE_SIZE
    ):

        raise ValueError(
            "Pending action is too large."
        )

    st.session_state[
        KEY_PENDING_ACTION
    ] = safe_action

    touch_session()


def get_pending_action() -> dict[str, Any] | None:
    """
    Return current pending action.
    """

    _ensure_initialized()

    value = st.session_state.get(
        KEY_PENDING_ACTION
    )

    if not isinstance(
        value,
        dict,
    ):
        return None

    return copy.deepcopy(
        value
    )


def clear_pending_action() -> None:
    """
    Clear incomplete action.
    """

    _ensure_initialized()

    st.session_state[
        KEY_PENDING_ACTION
    ] = None

    touch_session()


# ============================================================
# SAFE SESSION SNAPSHOT
# ============================================================


def get_safe_session_snapshot() -> SessionSnapshot:
    """
    Return session information safe for logs/debugging.

    Access token and refresh token are intentionally omitted.

    Example
    -------

        snapshot = get_safe_session_snapshot()

        logger.debug(
            "Session: %s",
            snapshot,
        )
    """

    _ensure_initialized()

    messages = get_chat_messages()

    return SessionSnapshot(
        session_id=get_session_id(),

        initialized=bool(
            st.session_state.get(
                KEY_INITIALIZED,
                False,
            )
        ),

        authenticated=is_authenticated(),

        user_id=get_user_id(
            required=False
        ),

        email=get_user_email(),

        auth_provider=(
            st.session_state.get(
                KEY_AUTH_PROVIDER
            )
        ),

        expires_at=get_expires_at(),

        token_expired=(
            is_access_token_expired()
        ),

        chat_message_count=len(
            messages
        ),

        auth_generation=int(
            st.session_state.get(
                KEY_AUTH_GENERATION,
                0,
            )
        ),

        last_activity=(
            st.session_state.get(
                KEY_LAST_ACTIVITY
            )
        ),
    )


# ============================================================
# AUTH GENERATION
# ============================================================


def get_auth_generation() -> int:
    """
    Return authentication-state generation number.

    It increases whenever login/logout state changes.

    Useful later if some local cache needs invalidation.
    """

    _ensure_initialized()

    return int(
        st.session_state.get(
            KEY_AUTH_GENERATION,
            0,
        )
    )