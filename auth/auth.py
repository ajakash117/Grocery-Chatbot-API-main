"""
auth/auth.py

Unified authentication service for the Grocery Chatbot.

========================================================================
SUPPORTED AUTHENTICATION FEATURES
========================================================================

1. Email + password signup
2. Email + password login
3. Google OAuth THROUGH Supabase
4. Google OAuth PKCE callback handling
5. Email confirmation handling
6. Resend confirmation email
7. Forgot-password email
8. Password-recovery PKCE callback
9. Update password
10. Validate current session
11. Refresh / restore current session
12. Local logout
13. Global logout
14. Safe current-user information

========================================================================
IMPORTANT ARCHITECTURE
========================================================================

                     SUPABASE AUTH
                          |
              +-----------+-----------+
              |                       |
       Email / Password             Google
              |                       |
     sign_in_with_password     sign_in_with_oauth
              |                       |
              |                 Google Provider
              |                       |
              +-----------+-----------+
                          |
                    Supabase Session
                          |
                 access + refresh JWT
                          |
                          v
                   auth/session.py
                          |
                          v
                 User-specific client
                          |
                          v
                     Supabase RLS


========================================================================
GOOGLE AUTH
========================================================================

Google Client ID and Google Client Secret are NOT stored in this
application's .env.

They are configured in:

    Supabase Dashboard
        -> Authentication
        -> Providers
        -> Google

Our application only knows:

    SUPABASE_URL
    SUPABASE_PUBLISHABLE_KEY

Google OAuth therefore happens THROUGH Supabase.

========================================================================
WHY PKCE STORAGE EXISTS
========================================================================

For a Python/Streamlit application, the Google OAuth process redirects
the browser away from Streamlit and later sends it back.

PKCE requires:

    authorization code
        +
    original code verifier

The code verifier must survive the redirect.

Streamlit session_state cannot be treated as reliable storage across an
external OAuth redirect because a new browser/server connection may be
created.

Therefore this file stores temporary PKCE data in a small SERVER-SIDE
SQLite database.

The SQLite file stores only temporary Supabase PKCE state and is removed
after a completed authentication flow.

It NEVER stores:

    GROQ_API_KEY
    COHERE_API_KEY
    SUPABASE_SECRET_KEY
    passwords

========================================================================
SECURITY RULES
========================================================================

- Never log passwords.
- Never log access tokens.
- Never log refresh tokens.
- Never expose raw Supabase exceptions to users.
- Never use SUPABASE_SECRET_KEY for normal authentication.
- Never trust user-provided user_id.
- Identity always comes from Supabase Auth.
"""

from __future__ import annotations

import logging
import re
import secrets
import sqlite3
import threading
import time

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import (
    parse_qsl,
    urlencode,
    urlsplit,
    urlunsplit,
)


# ============================================================
# SUPABASE
# ============================================================

from supabase import create_client

try:
    from supabase.client import ClientOptions

except ImportError:
    # Compatibility fallback for some supabase-py versions.
    from supabase.lib.client_options import ClientOptions


# ============================================================
# APPLICATION
# ============================================================

from config import settings

from auth.session import (
    NotAuthenticatedError,
    clear_authentication,
    get_user_email,
    get_user_id,
    get_user_supabase_client,
    initialize_session,
    is_authenticated,
    store_auth_response,
)

from database.supabase import (
    SupabaseAuthenticationError,
    create_public_client,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# GENERAL AUTH CONSTANTS
# ============================================================

MIN_PASSWORD_LENGTH = 8

MAX_PASSWORD_LENGTH = 128

MAX_EMAIL_LENGTH = 254

MAX_DISPLAY_NAME_LENGTH = 100


EMAIL_PATTERN = re.compile(
    r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
)


# ============================================================
# OAUTH / PKCE CONSTANTS
# ============================================================

# Added to callback URL so that after Google sends the user back
# we can find the correct PKCE verifier.

OAUTH_FLOW_QUERY_PARAMETER = "oauth_flow"


# Temporary OAuth state expires quickly.
OAUTH_FLOW_TTL_SECONDS = 10 * 60


PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parent
    .parent
)


OAUTH_STORAGE_FILE = (
    PROJECT_ROOT
    / ".oauth_pkce.sqlite3"
)


_oauth_storage_lock = (
    threading.RLock()
)


# ============================================================
# AUTH STATUS
# ============================================================


class AuthStatus(str, Enum):
    """
    Application-level authentication statuses.

    UI code should use these rather than trying to understand
    Supabase SDK internals.
    """

    SUCCESS = "success"

    EMAIL_CONFIRMATION_REQUIRED = (
        "email_confirmation_required"
    )

    INVALID_CREDENTIALS = (
        "invalid_credentials"
    )

    NOT_AUTHENTICATED = (
        "not_authenticated"
    )

    SESSION_EXPIRED = (
        "session_expired"
    )

    VALIDATION_ERROR = (
        "validation_error"
    )

    RATE_LIMITED = (
        "rate_limited"
    )

    EMAIL_SENT = (
        "email_sent"
    )

    OAUTH_REDIRECT = (
        "oauth_redirect"
    )

    OAUTH_ERROR = (
        "oauth_error"
    )

    PASSWORD_UPDATED = (
        "password_updated"
    )

    ERROR = "error"


# ============================================================
# STANDARD AUTH RESULT
# ============================================================


@dataclass(slots=True)
class AuthResult:
    """
    Standard result returned by authentication functions.

    This prevents app.py from depending on raw Supabase SDK response
    objects.
    """

    success: bool

    status: AuthStatus

    message: str

    user_id: str | None = None

    email: str | None = None

    requires_email_confirmation: bool = False

    raw_response: Any = field(
        default=None,
        repr=False,
    )

    @property
    def authenticated(self) -> bool:
        """
        Return True only for successful authenticated sessions.
        """

        return (
            self.success
            and self.status == AuthStatus.SUCCESS
        )

    def safe_dict(self) -> dict[str, Any]:
        """
        Return information safe to display/log.

        Tokens are deliberately excluded.
        """

        return {
            "success": self.success,
            "status": self.status.value,
            "message": self.message,
            "user_id": self.user_id,
            "email": self.email,
            "requires_email_confirmation": (
                self.requires_email_confirmation
            ),
        }


# ============================================================
# OAUTH START RESULT
# ============================================================


@dataclass(slots=True)
class OAuthStartResult:
    """
    Result returned when Google OAuth is started.

    redirect_url:
        Supabase/Google authorization URL.

    flow_id:
        Random identifier used to retrieve the correct PKCE verifier.
    """

    success: bool

    message: str

    redirect_url: str | None = None

    flow_id: str | None = None

    raw_response: Any = field(
        default=None,
        repr=False,
    )


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================


class AuthenticationServiceError(
    RuntimeError
):
    """
    Base authentication service exception.
    """

    pass


class AuthenticationValidationError(
    AuthenticationServiceError
):
    """
    Invalid user input.
    """

    pass


class OAuthStateError(
    AuthenticationServiceError
):
    """
    Missing/invalid OAuth state.
    """

    pass


# ============================================================
# EMAIL VALIDATION
# ============================================================


def normalize_email(
    email: str,
) -> str:
    """
    Normalize and validate email.

    Example:

        "  USER@Example.COM "

    becomes:

        "user@example.com"
    """

    if not isinstance(
        email,
        str,
    ):
        raise AuthenticationValidationError(
            "Email must be a string."
        )

    email = email.strip().lower()

    if not email:
        raise AuthenticationValidationError(
            "Email is required."
        )

    if len(email) > MAX_EMAIL_LENGTH:
        raise AuthenticationValidationError(
            "Email address is too long."
        )

    if not EMAIL_PATTERN.match(email):
        raise AuthenticationValidationError(
            "Please enter a valid email address."
        )

    return email


# ============================================================
# PASSWORD VALIDATION
# ============================================================


def validate_password(
    password: str,
    *,
    enforce_strength: bool,
) -> str:
    """
    Validate password.

    Signup/update:
        enforce_strength=True

    Login:
        enforce_strength=False

    Passwords are NOT stripped because spaces may intentionally be
    part of a password.
    """

    if not isinstance(
        password,
        str,
    ):
        raise AuthenticationValidationError(
            "Password must be a string."
        )

    if not password:
        raise AuthenticationValidationError(
            "Password is required."
        )

    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthenticationValidationError(
            "Password is too long."
        )

    if (
        enforce_strength
        and len(password) < MIN_PASSWORD_LENGTH
    ):
        raise AuthenticationValidationError(
            f"Password must contain at least "
            f"{MIN_PASSWORD_LENGTH} characters."
        )

    return password


# ============================================================
# DISPLAY NAME
# ============================================================


def normalize_display_name(
    name: str | None,
) -> str | None:
    """
    Validate optional signup display name.
    """

    if name is None:
        return None

    if not isinstance(
        name,
        str,
    ):
        raise AuthenticationValidationError(
            "Display name must be a string."
        )

    name = " ".join(
        name.strip().split()
    )

    if not name:
        return None

    if len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise AuthenticationValidationError(
            "Display name is too long."
        )

    return name


# ============================================================
# DEFAULT CALLBACK URL
# ============================================================


def get_default_callback_url() -> str:
    """
    Build callback URL using your EXISTING APP_PORT.

    Your .env already contains:

        APP_PORT=5173

    We intentionally use localhost rather than:

        APP_HOST=0.0.0.0

    because 0.0.0.0 is a server bind address, not a browser
    destination.

    Local callback:

        http://localhost:5173

    Production
    ----------

    app.py can explicitly provide:

        redirect_to="https://your-deployed-app.example"

    Therefore NO additional environment variable is required.
    """

    return (
        f"http://localhost:"
        f"{settings.app_port}"
    )


# ============================================================
# CALLBACK URL VALIDATION
# ============================================================


def _validate_redirect_url(
    redirect_url: str,
) -> str:
    """
    Basic redirect URL validation.
    """

    if not isinstance(
        redirect_url,
        str,
    ):
        raise AuthenticationValidationError(
            "Redirect URL must be a string."
        )

    redirect_url = redirect_url.strip()

    if not redirect_url:
        raise AuthenticationValidationError(
            "Redirect URL cannot be empty."
        )

    parsed = urlsplit(
        redirect_url
    )

    if parsed.scheme not in {
        "http",
        "https",
    }:
        raise AuthenticationValidationError(
            "Redirect URL must use http or https."
        )

    if not parsed.netloc:
        raise AuthenticationValidationError(
            "Redirect URL is invalid."
        )

    return redirect_url


# ============================================================
# ADD QUERY PARAMETER
# ============================================================


def _append_query_parameter(
    url: str,
    *,
    name: str,
    value: str,
) -> str:
    """
    Add one query parameter without destroying existing parameters.

    Example:

        http://localhost:5173

    becomes:

        http://localhost:5173?oauth_flow=abc123
    """

    parsed = urlsplit(
        url
    )

    query = dict(
        parse_qsl(
            parsed.query,
            keep_blank_values=True,
        )
    )

    query[name] = value

    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


# ============================================================
# SQLITE CONNECTION
# ============================================================


def _oauth_db_connection() -> sqlite3.Connection:
    """
    Create SQLite connection for temporary PKCE state.
    """

    connection = sqlite3.connect(
        str(OAUTH_STORAGE_FILE),
        timeout=5.0,
    )

    return connection


# ============================================================
# INITIALIZE PKCE STORAGE
# ============================================================


def _initialize_oauth_storage() -> None:
    """
    Create temporary PKCE table.

    Safe to call repeatedly.
    """

    OAUTH_STORAGE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with _oauth_storage_lock:

        connection = (
            _oauth_db_connection()
        )

        try:

            connection.execute(
                """
                PRAGMA journal_mode=WAL
                """
            )

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_pkce_storage (
                    flow_id TEXT NOT NULL,
                    storage_key TEXT NOT NULL,
                    storage_value TEXT NOT NULL,
                    created_at INTEGER NOT NULL,

                    PRIMARY KEY (
                        flow_id,
                        storage_key
                    )
                )
                """
            )

            connection.commit()

        finally:

            connection.close()


# ============================================================
# CLEANUP OLD FLOWS
# ============================================================


def _cleanup_expired_oauth_flows() -> None:
    """
    Remove expired OAuth state.
    """

    _initialize_oauth_storage()

    cutoff = (
        int(time.time())
        - OAUTH_FLOW_TTL_SECONDS
    )

    with _oauth_storage_lock:

        connection = (
            _oauth_db_connection()
        )

        try:

            connection.execute(
                """
                DELETE FROM oauth_pkce_storage
                WHERE created_at < ?
                """,
                (cutoff,),
            )

            connection.commit()

        finally:

            connection.close()


# ============================================================
# PKCE STORAGE ADAPTER
# ============================================================


class SQLitePKCEStorage:
    """
    Storage adapter used by Supabase's PKCE implementation.

    Supabase expects:

        get_item(key)
        set_item(key, value)
        remove_item(key)

    Every OAuth flow receives a random flow_id.

    Therefore two simultaneous users cannot overwrite each other's
    PKCE verifier.

    Example:

        flow A
            -> code verifier A

        flow B
            -> code verifier B
    """

    def __init__(
        self,
        flow_id: str,
    ) -> None:

        if not isinstance(
            flow_id,
            str,
        ):
            raise OAuthStateError(
                "OAuth flow ID is invalid."
            )

        flow_id = flow_id.strip()

        if not flow_id:
            raise OAuthStateError(
                "OAuth flow ID is missing."
            )

        self.flow_id = flow_id

        _initialize_oauth_storage()

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    def get_item(
        self,
        key: str,
    ) -> str | None:

        if not key:
            return None

        _cleanup_expired_oauth_flows()

        with _oauth_storage_lock:

            connection = (
                _oauth_db_connection()
            )

            try:

                cursor = connection.execute(
                    """
                    SELECT storage_value
                    FROM oauth_pkce_storage
                    WHERE flow_id = ?
                      AND storage_key = ?
                    """,
                    (
                        self.flow_id,
                        str(key),
                    ),
                )

                row = cursor.fetchone()

                if row is None:
                    return None

                return str(
                    row[0]
                )

            finally:

                connection.close()

    # --------------------------------------------------------
    # SET
    # --------------------------------------------------------

    def set_item(
        self,
        key: str,
        value: str,
    ) -> None:

        if not key:
            raise OAuthStateError(
                "OAuth storage key is missing."
            )

        if value is None:
            raise OAuthStateError(
                "OAuth storage value is missing."
            )

        with _oauth_storage_lock:

            connection = (
                _oauth_db_connection()
            )

            try:

                connection.execute(
                    """
                    INSERT INTO oauth_pkce_storage (
                        flow_id,
                        storage_key,
                        storage_value,
                        created_at
                    )
                    VALUES (?, ?, ?, ?)

                    ON CONFLICT (
                        flow_id,
                        storage_key
                    )

                    DO UPDATE SET
                        storage_value =
                            excluded.storage_value,
                        created_at =
                            excluded.created_at
                    """,
                    (
                        self.flow_id,
                        str(key),
                        str(value),
                        int(time.time()),
                    ),
                )

                connection.commit()

            finally:

                connection.close()

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    def remove_item(
        self,
        key: str,
    ) -> None:

        if not key:
            return

        with _oauth_storage_lock:

            connection = (
                _oauth_db_connection()
            )

            try:

                connection.execute(
                    """
                    DELETE
                    FROM oauth_pkce_storage
                    WHERE flow_id = ?
                      AND storage_key = ?
                    """,
                    (
                        self.flow_id,
                        str(key),
                    ),
                )

                connection.commit()

            finally:

                connection.close()

    # --------------------------------------------------------
    # CLEAR COMPLETE FLOW
    # --------------------------------------------------------

    def clear_flow(
        self,
    ) -> None:
        """
        Remove every temporary record for this flow.
        """

        with _oauth_storage_lock:

            connection = (
                _oauth_db_connection()
            )

            try:

                connection.execute(
                    """
                    DELETE
                    FROM oauth_pkce_storage
                    WHERE flow_id = ?
                    """,
                    (
                        self.flow_id,
                    ),
                )

                connection.commit()

            finally:

                connection.close()


# ============================================================
# CREATE PKCE SUPABASE CLIENT
# ============================================================


def _create_pkce_client(
    flow_id: str,
):
    """
    Create a Supabase client configured for PKCE.

    IMPORTANT:
    ---------

    Uses:

        SUPABASE_PUBLISHABLE_KEY

    NEVER:

        SUPABASE_SECRET_KEY
    """

    storage = SQLitePKCEStorage(
        flow_id
    )

    try:

        options = ClientOptions(
            flow_type="pkce",
            storage=storage,
            persist_session=True,
            auto_refresh_token=False,
        )

    except TypeError as exc:

        raise AuthenticationServiceError(
            "The installed supabase Python package does not "
            "support the required PKCE ClientOptions. "
            "Use a current supabase-py version."
        ) from exc

    try:

        client = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
            options=options,
        )

    except Exception as exc:

        logger.error(
            "PKCE Supabase client creation failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise AuthenticationServiceError(
            "Authentication could not be initialized."
        ) from exc

    return client, storage


# ============================================================
# RESPONSE HELPERS
# ============================================================


def _extract_user(
    response: Any,
) -> Any | None:
    """
    Extract user from Supabase AuthResponse.
    """

    if response is None:
        return None

    user = getattr(
        response,
        "user",
        None,
    )

    if user is not None:
        return user

    session = getattr(
        response,
        "session",
        None,
    )

    if session is None:
        return None

    return getattr(
        session,
        "user",
        None,
    )


def _extract_user_id(
    response: Any,
) -> str | None:
    """
    Extract user UUID from response.
    """

    user = _extract_user(
        response
    )

    if user is None:
        return None

    user_id = getattr(
        user,
        "id",
        None,
    )

    if user_id is None:
        return None

    return str(
        user_id
    )


def _extract_email(
    response: Any,
) -> str | None:
    """
    Extract email from response.
    """

    user = _extract_user(
        response
    )

    if user is None:
        return None

    email = getattr(
        user,
        "email",
        None,
    )

    if not email:
        return None

    return str(
        email
    )


def _has_session(
    response: Any,
) -> bool:
    """
    Determine whether Supabase returned an authenticated session.
    """

    session = getattr(
        response,
        "session",
        None,
    )

    if session is None:
        return False

    access_token = getattr(
        session,
        "access_token",
        None,
    )

    refresh_token = getattr(
        session,
        "refresh_token",
        None,
    )

    return bool(
        access_token
        and refresh_token
    )


# ============================================================
# ERROR HELPERS
# ============================================================


def _error_text(
    exception: Exception,
) -> str:
    """
    Extract exception text ONLY for internal classification.

    Never display this raw value to the user.
    """

    try:
        return str(
            exception
        ).strip().lower()

    except Exception:
        return ""


def _status_code(
    exception: Exception,
) -> int | None:
    """
    Try to extract HTTP status from Supabase exception.
    """

    for attribute in (
        "status_code",
        "status",
    ):

        value = getattr(
            exception,
            attribute,
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


def _safe_auth_error(
    exception: Exception,
    *,
    operation: str,
) -> AuthResult:
    """
    Convert Supabase exception into user-safe AuthResult.
    """

    text = _error_text(
        exception
    )

    status = _status_code(
        exception
    )

    logger.warning(
        "Supabase auth operation failed. "
        "operation=%s status=%s type=%s",
        operation,
        status,
        type(exception).__name__,
    )

    # --------------------------------------------------------
    # Rate limit
    # --------------------------------------------------------

    if (
        status == 429
        or "rate limit" in text
        or "too many requests" in text
    ):

        return AuthResult(
            success=False,
            status=AuthStatus.RATE_LIMITED,
            message=(
                "Too many authentication attempts. "
                "Please try again shortly."
            ),
        )

    # --------------------------------------------------------
    # Email confirmation
    # --------------------------------------------------------

    if (
        "email not confirmed"
        in text
    ):

        return AuthResult(
            success=False,
            status=(
                AuthStatus.EMAIL_CONFIRMATION_REQUIRED
            ),
            message=(
                "Please confirm your email address "
                "before signing in."
            ),
            requires_email_confirmation=True,
        )

    # --------------------------------------------------------
    # Invalid credentials
    # --------------------------------------------------------

    if (
        "invalid login credentials"
        in text
        or "invalid credentials"
        in text
    ):

        return AuthResult(
            success=False,
            status=(
                AuthStatus.INVALID_CREDENTIALS
            ),
            message=(
                "The email or password is incorrect."
            ),
        )

    # --------------------------------------------------------
    # Duplicate account
    # --------------------------------------------------------

    if (
        "already registered"
        in text
        or "user already exists"
        in text
    ):

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "An account with this email already exists. "
                "Please sign in instead."
            ),
        )

    # --------------------------------------------------------
    # Weak password
    # --------------------------------------------------------

    if (
        "password" in text
        and (
            "weak" in text
            or "at least" in text
        )
    ):

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=(
                "The password does not meet "
                "the required security rules."
            ),
        )

    # --------------------------------------------------------
    # Expired / invalid session
    # --------------------------------------------------------

    if (
        "refresh token" in text
        or "session" in text
        and "expired" in text
    ):

        return AuthResult(
            success=False,
            status=AuthStatus.SESSION_EXPIRED,
            message=(
                "Your session has expired. "
                "Please sign in again."
            ),
        )

    # --------------------------------------------------------
    # Generic
    # --------------------------------------------------------

    return AuthResult(
        success=False,
        status=AuthStatus.ERROR,
        message=(
            "Authentication could not be completed. "
            "Please try again."
        ),
    )


# ============================================================
# EMAIL / PASSWORD SIGNUP
# ============================================================


def sign_up_with_password(
    *,
    email: str,
    password: str,
    display_name: str | None = None,
    redirect_to: str | None = None,
    additional_metadata: Mapping[
        str,
        Any,
    ] | None = None,
) -> AuthResult:
    """
    Register new user through Supabase Auth.

    If Supabase Confirm Email is enabled:

        user returned
        session=None

    If Confirm Email is disabled:

        user returned
        session returned
        user logged in immediately
    """

    initialize_session()

    try:

        normalized_email = (
            normalize_email(
                email
            )
        )

        validated_password = (
            validate_password(
                password,
                enforce_strength=True,
            )
        )

        normalized_name = (
            normalize_display_name(
                display_name
            )
        )

    except AuthenticationValidationError as exc:

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=str(exc),
        )

    metadata: dict[
        str,
        Any
    ] = {}

    if normalized_name:

        metadata[
            "display_name"
        ] = normalized_name

    # --------------------------------------------------------
    # Safe custom metadata
    # --------------------------------------------------------

    if additional_metadata:

        forbidden_keys = {
            "id",
            "user_id",
            "role",
            "is_admin",
            "access_token",
            "refresh_token",
        }

        for key, value in (
            additional_metadata.items()
        ):

            name = str(
                key
            ).strip()

            if not name:
                continue

            if (
                name.lower()
                in forbidden_keys
            ):
                continue

            metadata[
                name
            ] = value

    credentials: dict[
        str,
        Any
    ] = {
        "email": normalized_email,
        "password": validated_password,
    }

    options: dict[
        str,
        Any
    ] = {}

    if metadata:

        options[
            "data"
        ] = metadata

    if redirect_to:

        try:

            options[
                "email_redirect_to"
            ] = _validate_redirect_url(
                redirect_to
            )

        except AuthenticationValidationError as exc:

            return AuthResult(
                success=False,
                status=(
                    AuthStatus.VALIDATION_ERROR
                ),
                message=str(exc),
            )

    if options:

        credentials[
            "options"
        ] = options

    try:

        client = (
            create_public_client()
        )

        response = (
            client.auth.sign_up(
                credentials
            )
        )

        user_id = (
            _extract_user_id(
                response
            )
        )

        response_email = (
            _extract_email(
                response
            )
            or normalized_email
        )

        # ----------------------------------------------------
        # Immediate login
        # ----------------------------------------------------

        if _has_session(
            response
        ):

            stored = (
                store_auth_response(
                    response,
                    auth_provider="email",
                )
            )

            if not stored:

                raise AuthenticationServiceError(
                    "Supabase session could not be stored."
                )

            return AuthResult(
                success=True,
                status=AuthStatus.SUCCESS,
                message=(
                    "Account created successfully."
                ),
                user_id=user_id,
                email=response_email,
                raw_response=response,
            )

        # ----------------------------------------------------
        # Email confirmation required
        # ----------------------------------------------------

        return AuthResult(
            success=True,
            status=(
                AuthStatus.EMAIL_CONFIRMATION_REQUIRED
            ),
            message=(
                "Account created. Please check your email "
                "and confirm your account before signing in."
            ),
            user_id=user_id,
            email=response_email,
            requires_email_confirmation=True,
            raw_response=response,
        )

    except Exception as exc:

        return _safe_auth_error(
            exc,
            operation="signup",
        )


# ============================================================
# EMAIL / PASSWORD LOGIN
# ============================================================


def sign_in_with_password(
    *,
    email: str,
    password: str,
) -> AuthResult:
    """
    Login through Supabase Auth using email + password.
    """

    initialize_session()

    try:

        normalized_email = (
            normalize_email(
                email
            )
        )

        validated_password = (
            validate_password(
                password,
                enforce_strength=False,
            )
        )

    except AuthenticationValidationError as exc:

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=str(exc),
        )

    try:

        client = (
            create_public_client()
        )

        response = (
            client.auth.sign_in_with_password(
                {
                    "email": normalized_email,
                    "password": validated_password,
                }
            )
        )

        if not _has_session(
            response
        ):

            return AuthResult(
                success=False,
                status=AuthStatus.ERROR,
                message=(
                    "Supabase did not return "
                    "an authentication session."
                ),
            )

        stored = (
            store_auth_response(
                response,
                auth_provider="email",
            )
        )

        if not stored:

            return AuthResult(
                success=False,
                status=AuthStatus.ERROR,
                message=(
                    "Unable to store authentication session."
                ),
            )

        # ----------------------------------------------------
        # Verify user against Supabase Auth server
        # ----------------------------------------------------

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        return AuthResult(
            success=True,
            status=AuthStatus.SUCCESS,
            message=(
                "Signed in successfully."
            ),
            user_id=user_client.user_id,
            email=(
                user_client.email
                or normalized_email
            ),
            raw_response=response,
        )

    except Exception as exc:

        if is_authenticated():
            clear_authentication()

        return _safe_auth_error(
            exc,
            operation="password_login",
        )


# ============================================================
# START GOOGLE LOGIN
# ============================================================


def start_google_sign_in(
    *,
    redirect_to: str | None = None,
) -> OAuthStartResult:
    """
    Start Google authentication THROUGH Supabase.

    No Google credentials appear in Python.

    Google Client ID + Secret live inside Supabase.

    Returns:

        OAuthStartResult.redirect_url

    app.py should navigate the browser to that URL.
    """

    initialize_session()

    _cleanup_expired_oauth_flows()

    # --------------------------------------------------------
    # Random secure flow ID
    # --------------------------------------------------------

    flow_id = secrets.token_urlsafe(
        32
    )

    try:

        base_callback = (
            redirect_to
            or get_default_callback_url()
        )

        base_callback = (
            _validate_redirect_url(
                base_callback
            )
        )

        callback_url = (
            _append_query_parameter(
                base_callback,
                name=(
                    OAUTH_FLOW_QUERY_PARAMETER
                ),
                value=flow_id,
            )
        )

        client, storage = (
            _create_pkce_client(
                flow_id
            )
        )

        response = (
            client.auth.sign_in_with_oauth(
                {
                    "provider": "google",

                    "options": {
                        "redirect_to": (
                            callback_url
                        ),
                    },
                }
            )
        )

        authorization_url = getattr(
            response,
            "url",
            None,
        )

        if not authorization_url:

            storage.clear_flow()

            raise AuthenticationServiceError(
                "Supabase returned no Google authorization URL."
            )

        return OAuthStartResult(
            success=True,
            message=(
                "Google authentication started."
            ),
            redirect_url=str(
                authorization_url
            ),
            flow_id=flow_id,
            raw_response=response,
        )

    except Exception as exc:

        logger.warning(
            "Google OAuth initialization failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        return OAuthStartResult(
            success=False,
            message=(
                "Google sign-in could not be started."
            ),
        )


# ============================================================
# COMPLETE GOOGLE LOGIN
# ============================================================


def complete_google_sign_in(
    *,
    auth_code: str,
    flow_id: str,
) -> AuthResult:
    """
    Complete Google PKCE authentication.

    Expected Streamlit callback:

        http://localhost:5173/
            ?code=...
            &oauth_flow=...

    Supabase then exchanges:

        authorization code
        +
        original PKCE verifier

    for:

        access_token
        refresh_token
        user
    """

    initialize_session()

    if not isinstance(
        auth_code,
        str,
    ) or not auth_code.strip():

        return AuthResult(
            success=False,
            status=AuthStatus.OAUTH_ERROR,
            message=(
                "Google callback is missing "
                "the authorization code."
            ),
        )

    if not isinstance(
        flow_id,
        str,
    ) or not flow_id.strip():

        return AuthResult(
            success=False,
            status=AuthStatus.OAUTH_ERROR,
            message=(
                "Google authentication state is missing."
            ),
        )

    auth_code = auth_code.strip()
    flow_id = flow_id.strip()

    storage: (
        SQLitePKCEStorage
        | None
    ) = None

    try:

        client, storage = (
            _create_pkce_client(
                flow_id
            )
        )

        response = (
            client.auth.exchange_code_for_session(
                {
                    "auth_code": auth_code,
                }
            )
        )

        if not _has_session(
            response
        ):

            raise AuthenticationServiceError(
                "Supabase did not return a session "
                "after Google authentication."
            )

        stored = (
            store_auth_response(
                response,
                auth_provider="google",
            )
        )

        if not stored:

            raise AuthenticationServiceError(
                "Google authentication session "
                "could not be stored."
            )

        # ----------------------------------------------------
        # Verify Supabase identity
        # ----------------------------------------------------

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        return AuthResult(
            success=True,
            status=AuthStatus.SUCCESS,
            message=(
                "Signed in with Google successfully."
            ),
            user_id=user_client.user_id,
            email=user_client.email,
            raw_response=response,
        )

    except Exception as exc:

        logger.warning(
            "Google OAuth callback failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        clear_authentication()

        return AuthResult(
            success=False,
            status=AuthStatus.OAUTH_ERROR,
            message=(
                "Google sign-in could not be completed. "
                "Please try again."
            ),
        )

    finally:

        # ----------------------------------------------------
        # PKCE verifier is single-use.
        # ----------------------------------------------------

        if storage is not None:

            try:
                storage.clear_flow()

            except Exception:

                logger.warning(
                    "Unable to clean completed "
                    "Google OAuth state."
                )


# ============================================================
# RESEND CONFIRMATION EMAIL
# ============================================================


def resend_confirmation_email(
    *,
    email: str,
    redirect_to: str | None = None,
) -> AuthResult:
    """
    Resend signup confirmation email.

    Response is intentionally generic to reduce account enumeration.
    """

    try:

        normalized_email = (
            normalize_email(
                email
            )
        )

    except AuthenticationValidationError as exc:

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=str(exc),
        )

    payload: dict[
        str,
        Any
    ] = {
        "type": "signup",
        "email": normalized_email,
    }

    if redirect_to:

        try:

            payload[
                "options"
            ] = {
                "email_redirect_to": (
                    _validate_redirect_url(
                        redirect_to
                    )
                )
            }

        except AuthenticationValidationError as exc:

            return AuthResult(
                success=False,
                status=(
                    AuthStatus.VALIDATION_ERROR
                ),
                message=str(exc),
            )

    try:

        client = (
            create_public_client()
        )

        client.auth.resend(
            payload
        )

        return AuthResult(
            success=True,
            status=AuthStatus.EMAIL_SENT,
            message=(
                "If confirmation is available for this "
                "address, a new email has been sent."
            ),
            email=normalized_email,
        )

    except Exception as exc:

        return _safe_auth_error(
            exc,
            operation="resend_confirmation",
        )


# ============================================================
# PASSWORD RESET — START
# ============================================================


def request_password_reset(
    *,
    email: str,
    redirect_to: str | None = None,
) -> AuthResult:
    """
    Start password recovery using PKCE.

    We use the same persistent PKCE mechanism as Google because
    Streamlit needs the recovery session after the external redirect.

    IMPORTANT
    ---------

    This function embeds a random oauth_flow value in the recovery
    callback URL.

    After the user clicks the password-reset email:

        Streamlit receives:
            ?code=...
            &oauth_flow=...

    app.py can then call:

        complete_password_recovery(...)
    """

    try:

        normalized_email = (
            normalize_email(
                email
            )
        )

    except AuthenticationValidationError as exc:

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=str(exc),
        )

    _cleanup_expired_oauth_flows()

    flow_id = secrets.token_urlsafe(
        32
    )

    storage: (
        SQLitePKCEStorage
        | None
    ) = None

    try:

        callback = (
            redirect_to
            or get_default_callback_url()
        )

        callback = (
            _validate_redirect_url(
                callback
            )
        )

        callback = (
            _append_query_parameter(
                callback,
                name=(
                    OAUTH_FLOW_QUERY_PARAMETER
                ),
                value=flow_id,
            )
        )

        client, storage = (
            _create_pkce_client(
                flow_id
            )
        )

        client.auth.reset_password_for_email(
            normalized_email,
            {
                "redirect_to": callback,
            },
        )

        return AuthResult(
            success=True,
            status=AuthStatus.EMAIL_SENT,
            message=(
                "If an account exists for this email, "
                "password-reset instructions have been sent."
            ),
            email=normalized_email,
        )

    except Exception as exc:

        if storage is not None:

            try:
                storage.clear_flow()

            except Exception:
                pass

        logger.warning(
            "Password reset request failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        # Generic response prevents account enumeration.

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "The password-reset request could not "
                "be completed. Please try again."
            ),
        )


# ============================================================
# PASSWORD RESET — CALLBACK
# ============================================================


def complete_password_recovery(
    *,
    auth_code: str,
    flow_id: str,
) -> AuthResult:
    """
    Exchange password-recovery PKCE authorization code for
    authenticated Supabase recovery session.

    After success, app.py can show a "New password" form and call:

        update_current_password(...)
    """

    initialize_session()

    if not isinstance(
        auth_code,
        str,
    ) or not auth_code.strip():

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "Password recovery code is missing."
            ),
        )

    if not isinstance(
        flow_id,
        str,
    ) or not flow_id.strip():

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "Password recovery state is missing."
            ),
        )

    storage: (
        SQLitePKCEStorage
        | None
    ) = None

    try:

        client, storage = (
            _create_pkce_client(
                flow_id.strip()
            )
        )

        response = (
            client.auth.exchange_code_for_session(
                {
                    "auth_code": (
                        auth_code.strip()
                    ),
                }
            )
        )

        if not _has_session(
            response
        ):

            raise AuthenticationServiceError(
                "No recovery session returned."
            )

        stored = (
            store_auth_response(
                response,
                auth_provider="recovery",
            )
        )

        if not stored:

            raise AuthenticationServiceError(
                "Recovery session could not be stored."
            )

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        return AuthResult(
            success=True,
            status=AuthStatus.SUCCESS,
            message=(
                "Your recovery session is ready. "
                "You can now choose a new password."
            ),
            user_id=user_client.user_id,
            email=user_client.email,
            raw_response=response,
        )

    except Exception as exc:

        logger.warning(
            "Password recovery callback failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        clear_authentication()

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "The password-reset link is invalid "
                "or has expired."
            ),
        )

    finally:

        if storage is not None:

            try:
                storage.clear_flow()

            except Exception:
                pass


# ============================================================
# UPDATE PASSWORD
# ============================================================


def update_current_password(
    *,
    new_password: str,
) -> AuthResult:
    """
    Update password for currently authenticated user.

    This works after:

        normal login

    or:

        successful password-recovery code exchange.
    """

    initialize_session()

    if not is_authenticated():

        return AuthResult(
            success=False,
            status=(
                AuthStatus.NOT_AUTHENTICATED
            ),
            message=(
                "Please authenticate before "
                "changing your password."
            ),
        )

    try:

        password = (
            validate_password(
                new_password,
                enforce_strength=True,
            )
        )

    except AuthenticationValidationError as exc:

        return AuthResult(
            success=False,
            status=(
                AuthStatus.VALIDATION_ERROR
            ),
            message=str(exc),
        )

    try:

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        response = (
            user_client.client
            .auth
            .update_user(
                {
                    "password": password,
                }
            )
        )

        return AuthResult(
            success=True,
            status=(
                AuthStatus.PASSWORD_UPDATED
            ),
            message=(
                "Your password has been updated."
            ),
            user_id=user_client.user_id,
            email=user_client.email,
            raw_response=response,
        )

    except Exception as exc:

        return _safe_auth_error(
            exc,
            operation="update_password",
        )


# ============================================================
# VALIDATE CURRENT SESSION
# ============================================================


def validate_current_session() -> AuthResult:
    """
    Verify current user against Supabase Auth server.

    We do NOT trust local session_state alone.
    """

    initialize_session()

    if not is_authenticated():

        return AuthResult(
            success=False,
            status=(
                AuthStatus.NOT_AUTHENTICATED
            ),
            message=(
                "You are not signed in."
            ),
        )

    try:

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        return AuthResult(
            success=True,
            status=AuthStatus.SUCCESS,
            message=(
                "Authentication session is valid."
            ),
            user_id=user_client.user_id,
            email=user_client.email,
        )

    except (
        NotAuthenticatedError,
        SupabaseAuthenticationError,
    ):

        clear_authentication()

        return AuthResult(
            success=False,
            status=(
                AuthStatus.SESSION_EXPIRED
            ),
            message=(
                "Your session has expired. "
                "Please sign in again."
            ),
        )

    except Exception as exc:

        logger.warning(
            "Session validation failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        return AuthResult(
            success=False,
            status=AuthStatus.ERROR,
            message=(
                "Your authentication session "
                "could not be verified."
            ),
        )


# ============================================================
# REFRESH CURRENT SESSION
# ============================================================


def refresh_current_session() -> AuthResult:
    """
    Restore/refresh currently stored Supabase session.

    database/supabase.py's create_user_client() calls set_session(),
    which automatically handles expired access tokens using the
    refresh token.
    """

    initialize_session()

    if not is_authenticated():

        return AuthResult(
            success=False,
            status=(
                AuthStatus.NOT_AUTHENTICATED
            ),
            message=(
                "No authentication session exists."
            ),
        )

    try:

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        return AuthResult(
            success=True,
            status=AuthStatus.SUCCESS,
            message=(
                "Authentication session refreshed."
            ),
            user_id=user_client.user_id,
            email=user_client.email,
        )

    except Exception:

        clear_authentication()

        return AuthResult(
            success=False,
            status=(
                AuthStatus.SESSION_EXPIRED
            ),
            message=(
                "Your session has expired. "
                "Please sign in again."
            ),
        )


# ============================================================
# LOGOUT
# ============================================================


def sign_out(
    *,
    everywhere: bool = False,
) -> AuthResult:
    """
    Sign user out.

    everywhere=False
        Sign out current browser/session only.

    everywhere=True
        Revoke all refresh-token sessions for this user.

    Local Streamlit authentication is ALWAYS cleared even if the
    Supabase request fails.
    """

    initialize_session()

    if not is_authenticated():

        clear_authentication()

        return AuthResult(
            success=True,
            status=(
                AuthStatus.NOT_AUTHENTICATED
            ),
            message=(
                "You are already signed out."
            ),
        )

    user_id = get_user_id(
        required=False
    )

    email = get_user_email()

    try:

        user_client = (
            get_user_supabase_client(
                verify_user=True
            )
        )

        scope = (
            "global"
            if everywhere
            else "local"
        )

        user_client.client.auth.sign_out(
            {
                "scope": scope,
            }
        )

    except Exception as exc:

        logger.warning(
            "Supabase remote logout failed. "
            "error_type=%s",
            type(exc).__name__,
        )

    finally:

        # ----------------------------------------------------
        # ALWAYS remove local private state.
        # ----------------------------------------------------

        clear_authentication()

    return AuthResult(
        success=True,
        status=AuthStatus.SUCCESS,
        message=(
            "Signed out successfully."
        ),
        user_id=user_id,
        email=email,
    )


# ============================================================
# CURRENT USER
# ============================================================


def get_current_user() -> dict[str, Any] | None:
    """
    Return safe current-user information.

    Tokens are NEVER returned.
    """

    initialize_session()

    if not is_authenticated():
        return None

    return {
        "id": get_user_id(
            required=False
        ),
        "email": get_user_email(),
        "authenticated": True,
    }


# ============================================================
# AUTH STATUS
# ============================================================


def get_auth_status() -> dict[str, Any]:
    """
    Return safe auth state for app.py/sidebar.
    """

    initialize_session()

    return {
        "authenticated": (
            is_authenticated()
        ),

        "user_id": (
            get_user_id(
                required=False
            )
        ),

        "email": (
            get_user_email()
        ),
    }


# ============================================================
# REQUIRE LOGIN
# ============================================================


def ensure_logged_in() -> AuthResult:
    """
    Convenient login guard for controller/UI code.

    Supabase RLS remains the real database security layer.
    """

    initialize_session()

    if not is_authenticated():

        return AuthResult(
            success=False,
            status=(
                AuthStatus.NOT_AUTHENTICATED
            ),
            message=(
                "Please sign in to continue."
            ),
        )

    return AuthResult(
        success=True,
        status=AuthStatus.SUCCESS,
        message="Authenticated.",
        user_id=(
            get_user_id(
                required=False
            )
        ),
        email=get_user_email(),
    )