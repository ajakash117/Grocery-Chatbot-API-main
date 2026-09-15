# """
# database/supabase.py

# Central Supabase client management for the Grocery Chatbot.

# ========================================================================
# PURPOSE
# ========================================================================

# This module is responsible ONLY for creating and validating Supabase
# clients.

# It does NOT contain:

# - product queries
# - cart queries
# - order queries
# - shopping-list queries
# - Streamlit UI
# - login forms
# - business logic
# - AI logic

# Those responsibilities belong to other files.

# ========================================================================
# WHY THIS FILE IS IMPORTANT
# ========================================================================

# Supabase Python clients keep authentication/session state inside the
# client instance.

# In a multi-user Streamlit application, this means we must NOT create
# one globally cached authenticated Supabase client and share it between
# users.

# BAD:

#     @lru_cache
#     def get_supabase():
#         return create_client(...)

#     # User A logs into the shared client.
#     # User B later uses the same client.
#     #
#     # This creates a serious session-isolation risk.

# GOOD:

#     User A
#         -> own publishable-key client
#         -> own JWT
#         -> RLS sees User A

#     User B
#         -> own publishable-key client
#         -> own JWT
#         -> RLS sees User B


# We therefore use THREE client patterns:

# 1. PUBLIC CLIENT
#    -------------
#    Uses SUPABASE_PUBLISHABLE_KEY.

#    Has no authenticated user attached.

#    Useful for:
#        - sign up
#        - sign in
#        - OAuth initiation
#        - public database operations permitted by RLS


# 2. USER-SCOPED CLIENT
#    ------------------
#    Uses SUPABASE_PUBLISHABLE_KEY
#    + user's access token
#    + user's refresh token

#    Useful for:
#        - cart
#        - shopping list
#        - orders
#        - authenticated profile data

#    This client is NEVER globally cached.


# 3. ADMIN CLIENT
#    ------------
#    Uses SUPABASE_SECRET_KEY.

#    This is privileged server-side access.

#    It bypasses Row Level Security.

#    It must NEVER be:
#        - sent to the browser
#        - passed to the AI
#        - stored in st.session_state
#        - used for normal user cart/order access
#        - exposed in logs
#        - included in chatbot responses


# ========================================================================
# SECURITY MODEL
# ========================================================================

# For normal user data:

# Authenticated user JWT
#         |
#         v
# Publishable-key Supabase client
#         |
#         v
# PostgREST
#         |
#         v
# RLS
#         |
#         v
# Only rows belonging to auth.uid()


# For trusted admin/server operations:

# Python server
#         |
#         v
# Secret-key Supabase client
#         |
#         v
# Elevated access / RLS bypass


# ========================================================================
# IMPORTANT
# ========================================================================

# Never trust a user-supplied user_id.

# For example:

#     User says:
#         "Show cart of user abc123"

# We DO NOT do:

#     get_cart(user_id="abc123")

# Instead:

#     authenticated JWT
#           |
#           v
#     Supabase verifies JWT
#           |
#           v
#     user_id = verified auth user
#           |
#           v
#     RLS applies

# The authenticated identity always comes from the verified Supabase
# session.
# """

# from __future__ import annotations

# import logging

# from dataclasses import dataclass, field
# from functools import lru_cache
# from typing import Any


# # ============================================================
# # SUPABASE IMPORTS
# # ============================================================

# from supabase import Client, create_client


# # ClientOptions has moved between package locations across
# # supabase-py versions.
# #
# # Supporting both paths makes this module less brittle when updating
# # the dependency.
# try:
#     from supabase.client import ClientOptions

# except ImportError:  # pragma: no cover - compatibility fallback

#     from supabase.lib.client_options import ClientOptions


# # ============================================================
# # PROJECT IMPORTS
# # ============================================================

# from config import settings


# # ============================================================
# # LOGGER
# # ============================================================

# logger = logging.getLogger(__name__)


# # ============================================================
# # CONSTANTS
# # ============================================================

# DEFAULT_POSTGREST_TIMEOUT = 20

# DEFAULT_STORAGE_TIMEOUT = 30

# DEFAULT_DATABASE_SCHEMA = "public"


# # ============================================================
# # CUSTOM EXCEPTIONS
# # ============================================================


# class SupabaseClientError(RuntimeError):
#     """
#     Base exception for Supabase client-related failures.
#     """

#     pass


# class SupabaseConfigurationError(SupabaseClientError):
#     """
#     Raised when Supabase configuration is missing or invalid.
#     """

#     pass


# class SupabaseConnectionError(SupabaseClientError):
#     """
#     Raised when a Supabase client cannot be created or reached.
#     """

#     pass


# class SupabaseAuthenticationError(SupabaseClientError):
#     """
#     Raised when a user's Supabase authentication session is invalid.
#     """

#     pass


# class SupabaseAuthorizationError(SupabaseClientError):
#     """
#     Raised when Supabase rejects an operation due to authorization.
#     """

#     pass


# # ============================================================
# # VERIFIED USER IDENTITY
# # ============================================================


# @dataclass(slots=True)
# class VerifiedUserIdentity:
#     """
#     Server-verified Supabase user identity.

#     We intentionally store the raw SDK user object with repr=False
#     because it may contain metadata we don't want printed accidentally.

#     The user_id is safe to use internally as the authenticated identity.

#     IMPORTANT:
#     ---------
#     This user_id comes from Supabase authentication.

#     It does NOT come from:
#         - user text
#         - query parameters
#         - Groq
#         - Cohere
#         - request payload supplied by the chatbot
#     """

#     user_id: str

#     email: str | None = None

#     raw_user: Any = field(
#         default=None,
#         repr=False,
#     )

#     def safe_dict(self) -> dict[str, Any]:
#         """
#         Return safe information suitable for debugging.

#         Authentication tokens are intentionally not included.
#         """

#         return {
#             "user_id": self.user_id,
#             "email": self.email,
#         }


# # ============================================================
# # USER-SCOPED SUPABASE CLIENT
# # ============================================================


# @dataclass(slots=True)
# class UserScopedSupabaseClient:
#     """
#     A Supabase client tied to exactly one authenticated user.

#     This object contains:

#         client
#         verified user identity
#         access token
#         refresh token
#         expiration information

#     Tokens are deliberately hidden from repr().
#     """

#     client: Client = field(
#         repr=False
#     )

#     user: VerifiedUserIdentity

#     access_token: str = field(
#         repr=False
#     )

#     refresh_token: str = field(
#         repr=False
#     )

#     expires_at: int | None = None

#     expires_in: int | None = None

#     token_type: str | None = None

#     # --------------------------------------------------------
#     # Convenience properties
#     # --------------------------------------------------------

#     @property
#     def user_id(self) -> str:
#         """
#         Return authenticated Supabase user ID.
#         """

#         return self.user.user_id

#     @property
#     def email(self) -> str | None:
#         """
#         Return authenticated user's email.
#         """

#         return self.user.email

#     def safe_dict(self) -> dict[str, Any]:
#         """
#         Return safe diagnostic information.

#         Tokens and client internals are never exposed.
#         """

#         return {
#             "user_id": self.user_id,
#             "email": self.email,
#             "expires_at": self.expires_at,
#             "expires_in": self.expires_in,
#             "token_type": self.token_type,
#         }


# # ============================================================
# # CONFIGURATION VALIDATION
# # ============================================================


# def _validate_supabase_configuration() -> None:
#     """
#     Validate Supabase environment configuration.

#     This performs local validation only.

#     It does NOT make a network request.
#     """

#     # --------------------------------------------------------
#     # URL
#     # --------------------------------------------------------

#     if not settings.supabase_url:

#         raise SupabaseConfigurationError(
#             "SUPABASE_URL is missing."
#         )

#     url = settings.supabase_url.strip()

#     if not (
#         url.startswith("https://")
#         or (
#             settings.is_development
#             and url.startswith("http://")
#         )
#     ):
#         raise SupabaseConfigurationError(
#             "SUPABASE_URL must be a valid HTTP/HTTPS URL."
#         )

#     # --------------------------------------------------------
#     # Publishable key
#     # --------------------------------------------------------

#     if not settings.supabase_publishable_key:

#         raise SupabaseConfigurationError(
#             "SUPABASE_PUBLISHABLE_KEY is missing."
#         )

#     # --------------------------------------------------------
#     # Secret key
#     # --------------------------------------------------------

#     if not settings.supabase_secret_key:

#         raise SupabaseConfigurationError(
#             "SUPABASE_SECRET_KEY is missing."
#         )

#     # --------------------------------------------------------
#     # Defensive key mismatch checks
#     # --------------------------------------------------------

#     publishable_key = (
#         settings.supabase_publishable_key.strip()
#     )

#     secret_key = (
#         settings.supabase_secret_key.strip()
#     )

#     if publishable_key == secret_key:

#         raise SupabaseConfigurationError(
#             "SUPABASE_PUBLISHABLE_KEY and "
#             "SUPABASE_SECRET_KEY must not be identical."
#         )

#     # New Supabase keys normally begin with:
#     #
#     # sb_publishable_
#     # sb_secret_
#     #
#     # We intentionally don't reject legacy JWT-based keys here,
#     # because the application may later need to support migration
#     # scenarios.

#     if (
#         publishable_key.startswith("sb_secret_")
#     ):
#         raise SupabaseConfigurationError(
#             "SUPABASE_PUBLISHABLE_KEY appears to contain "
#             "a secret Supabase key."
#         )

#     if (
#         secret_key.startswith("sb_publishable_")
#     ):
#         raise SupabaseConfigurationError(
#             "SUPABASE_SECRET_KEY appears to contain "
#             "a publishable Supabase key."
#         )


# # ============================================================
# # CLIENT OPTIONS
# # ============================================================


# def _create_client_options(
#     *,
#     auto_refresh_token: bool,
#     persist_session: bool,
# ) -> ClientOptions:
#     """
#     Create Supabase ClientOptions.

#     Why persist_session=False?
#     --------------------------

#     Streamlit manages the user's session separately.

#     We do not want the Supabase SDK to become our main source of
#     persistent cross-rerun authentication state.

#     auth/session.py will explicitly own:

#         access_token
#         refresh_token
#         user_id
#         authentication state

#     This gives us deterministic behavior.

#     Why auto_refresh_token?
#     -----------------------

#     User clients:
#         True

#     Admin client:
#         False

#     Admin credentials do not represent a normal user login session,
#     so there is nothing to refresh.
#     """

#     try:

#         return ClientOptions(
#             schema=DEFAULT_DATABASE_SCHEMA,
#             auto_refresh_token=auto_refresh_token,
#             persist_session=persist_session,
#             postgrest_client_timeout=(
#                 DEFAULT_POSTGREST_TIMEOUT
#             ),
#             storage_client_timeout=(
#                 DEFAULT_STORAGE_TIMEOUT
#             ),
#         )

#     except TypeError:

#         # ----------------------------------------------------
#         # Compatibility fallback
#         # ----------------------------------------------------
#         #
#         # Some older supabase-py versions expose fewer option
#         # parameters.
#         #
#         # We retain the security-critical auth settings.
#         # ----------------------------------------------------

#         logger.warning(
#             "Installed supabase-py version exposes a reduced "
#             "ClientOptions constructor. Falling back to basic "
#             "authentication options."
#         )

#         return ClientOptions(
#             auto_refresh_token=auto_refresh_token,
#             persist_session=persist_session,
#         )


# # ============================================================
# # PUBLIC CLIENT
# # ============================================================


# def create_public_client() -> Client:
#     """
#     Create a NEW unauthenticated Supabase client.

#     Uses:
#         SUPABASE_PUBLISHABLE_KEY

#     IMPORTANT:
#     ----------
#     This function deliberately does NOT use @lru_cache.

#     A public client may later become authenticated during:

#         sign_in_with_password()
#         sign_up()
#         set_session()

#     If we globally cached it, authentication state could accidentally
#     leak across Streamlit users.

#     Therefore:

#         create_public_client()

#     always gives the caller its own Supabase client instance.

#     Typical uses:
#     -------------

#         auth.py:
#             sign up
#             sign in

#         database access:
#             public product catalog operations permitted by RLS
#     """

#     _validate_supabase_configuration()

#     try:

#         options = _create_client_options(
#             auto_refresh_token=True,
#             persist_session=False,
#         )

#         client = create_client(
#             settings.supabase_url,
#             settings.supabase_publishable_key,
#             options=options,
#         )

#         return client

#     except SupabaseClientError:
#         raise

#     except Exception as exc:

#         # Do NOT log:
#         #
#         # settings.supabase_publishable_key
#         # settings.supabase_secret_key
#         #
#         # Even though the publishable key is less sensitive, there is
#         # no reason to include credentials in application logs.

#         logger.error(
#             "Failed to create public Supabase client. error_type=%s",
#             type(exc).__name__,
#         )

#         raise SupabaseConnectionError(
#             "Unable to initialize Supabase."
#         ) from exc


# # ============================================================
# # VERIFY ACCESS TOKEN
# # ============================================================


# def verify_access_token(
#     access_token: str,
# ) -> VerifiedUserIdentity:
#     """
#     Verify a Supabase access token with the Supabase Auth server.

#     Parameters
#     ----------
#     access_token:
#         JWT received after authentication.

#     Returns
#     -------
#     VerifiedUserIdentity
#         Trusted server-verified identity.

#     Security
#     --------

#     We do NOT simply decode the JWT ourselves and trust the payload.

#     Supabase auth.get_user(access_token) verifies the token through
#     Supabase Auth and returns the authenticated user.

#     That verified identity becomes the source for user_id.
#     """

#     access_token = _validate_token(
#         access_token,
#         token_name="access_token",
#     )

#     client = create_public_client()

#     try:

#         response = client.auth.get_user(
#             access_token
#         )

#         user = getattr(
#             response,
#             "user",
#             None,
#         )

#         if user is None:

#             raise SupabaseAuthenticationError(
#                 "Supabase did not return an authenticated user."
#             )

#         user_id = getattr(
#             user,
#             "id",
#             None,
#         )

#         if not user_id:

#             raise SupabaseAuthenticationError(
#                 "Authenticated Supabase user has no ID."
#             )

#         email = getattr(
#             user,
#             "email",
#             None,
#         )

#         return VerifiedUserIdentity(
#             user_id=str(user_id),
#             email=(
#                 str(email)
#                 if email
#                 else None
#             ),
#             raw_user=user,
#         )

#     except SupabaseAuthenticationError:
#         raise

#     except Exception as exc:

#         logger.warning(
#             "Supabase access-token verification failed. "
#             "error_type=%s",
#             type(exc).__name__,
#         )

#         raise SupabaseAuthenticationError(
#             "Your login session is invalid or has expired."
#         ) from exc


# # ============================================================
# # TOKEN VALIDATION
# # ============================================================


# def _validate_token(
#     token: str,
#     *,
#     token_name: str,
# ) -> str:
#     """
#     Validate token input without logging or exposing it.
#     """

#     if not isinstance(
#         token,
#         str,
#     ):

#         raise SupabaseAuthenticationError(
#             f"{token_name} must be a string."
#         )

#     token = token.strip()

#     if not token:

#         raise SupabaseAuthenticationError(
#             f"{token_name} is missing."
#         )

#     # We deliberately avoid strict JWT-format validation.
#     #
#     # The Supabase server is responsible for determining whether
#     # the token is actually valid.

#     return token


# # ============================================================
# # SESSION RESPONSE EXTRACTION
# # ============================================================


# def _extract_session(
#     auth_response: Any,
# ) -> Any | None:
#     """
#     Extract the session object from a Supabase auth response.

#     Different supabase-py versions can expose slightly different
#     response structures.

#     Usually:

#         response.session

#     Defensive fallback:

#         response itself behaves like a session.
#     """

#     if auth_response is None:
#         return None

#     session = getattr(
#         auth_response,
#         "session",
#         None,
#     )

#     if session is not None:
#         return session

#     # --------------------------------------------------------
#     # Compatibility fallback
#     # --------------------------------------------------------

#     if getattr(
#         auth_response,
#         "access_token",
#         None,
#     ):
#         return auth_response

#     return None


# # ============================================================
# # SESSION TOKEN EXTRACTION
# # ============================================================


# def _session_value(
#     session: Any,
#     field_name: str,
#     fallback: Any = None,
# ) -> Any:
#     """
#     Safely retrieve a field from a Supabase session object.
#     """

#     if session is None:
#         return fallback

#     value = getattr(
#         session,
#         field_name,
#         None,
#     )

#     if value is None:
#         return fallback

#     return value


# # ============================================================
# # CREATE AUTHENTICATED USER CLIENT
# # ============================================================


# def create_user_client(
#     *,
#     access_token: str,
#     refresh_token: str,
#     verify_user: bool = True,
# ) -> UserScopedSupabaseClient:
#     """
#     Create a Supabase client scoped to ONE authenticated user.

#     This is one of the most important functions in the application.

#     Parameters
#     ----------
#     access_token:
#         Current Supabase access JWT.

#     refresh_token:
#         Current Supabase refresh token.

#     verify_user:
#         Whether to verify the resulting identity through Supabase Auth.

#         Default:
#             True

#         Keep this True for normal application usage.

#     Returns
#     -------
#     UserScopedSupabaseClient

#     Example
#     -------

#         user_db = create_user_client(
#             access_token=session_access_token,
#             refresh_token=session_refresh_token,
#         )

#         client = user_db.client

#         user_id = user_db.user_id


#     IMPORTANT TOKEN REFRESH BEHAVIOR
#     --------------------------------

#     Supabase set_session() may refresh an expired access token.

#     If that happens, the returned access_token and refresh_token can
#     differ from the values supplied to this function.

#     Therefore this function returns the CURRENT tokens.

#     auth/session.py must later update Streamlit session state:

#         st.session_state.access_token = user_db.access_token
#         st.session_state.refresh_token = user_db.refresh_token

#     Never assume old tokens remain current forever.
#     """

#     access_token = _validate_token(
#         access_token,
#         token_name="access_token",
#     )

#     refresh_token = _validate_token(
#         refresh_token,
#         token_name="refresh_token",
#     )

#     # --------------------------------------------------------
#     # Fresh client per authenticated context
#     # --------------------------------------------------------

#     client = create_public_client()

#     try:

#         # ----------------------------------------------------
#         # Attach user's Supabase auth session
#         # ----------------------------------------------------

#         auth_response = client.auth.set_session(
#             access_token,
#             refresh_token,
#         )

#         session = _extract_session(
#             auth_response
#         )

#         # ----------------------------------------------------
#         # Capture potentially refreshed tokens
#         # ----------------------------------------------------

#         current_access_token = (
#             _session_value(
#                 session,
#                 "access_token",
#                 access_token,
#             )
#         )

#         current_refresh_token = (
#             _session_value(
#                 session,
#                 "refresh_token",
#                 refresh_token,
#             )
#         )

#         current_access_token = (
#             _validate_token(
#                 current_access_token,
#                 token_name="access_token",
#             )
#         )

#         current_refresh_token = (
#             _validate_token(
#                 current_refresh_token,
#                 token_name="refresh_token",
#             )
#         )

#         # ----------------------------------------------------
#         # Verify user identity
#         # ----------------------------------------------------

#         if verify_user:

#             verified_user = (
#                 _verify_user_with_client(
#                     client=client,
#                     access_token=(
#                         current_access_token
#                     ),
#                 )
#             )

#         else:

#             # ------------------------------------------------
#             # We strongly prefer verification.
#             #
#             # This fallback exists mainly for controlled testing.
#             # ------------------------------------------------

#             auth_user = getattr(
#                 auth_response,
#                 "user",
#                 None,
#             )

#             if auth_user is None:

#                 raise SupabaseAuthenticationError(
#                     "Authenticated user could not be determined."
#                 )

#             user_id = getattr(
#                 auth_user,
#                 "id",
#                 None,
#             )

#             if not user_id:

#                 raise SupabaseAuthenticationError(
#                     "Authenticated user has no ID."
#                 )

#             email = getattr(
#                 auth_user,
#                 "email",
#                 None,
#             )

#             verified_user = VerifiedUserIdentity(
#                 user_id=str(user_id),
#                 email=(
#                     str(email)
#                     if email
#                     else None
#                 ),
#                 raw_user=auth_user,
#             )

#         # ----------------------------------------------------
#         # Session metadata
#         # ----------------------------------------------------

#         expires_at = _session_value(
#             session,
#             "expires_at",
#         )

#         expires_in = _session_value(
#             session,
#             "expires_in",
#         )

#         token_type = _session_value(
#             session,
#             "token_type",
#         )

#         # ----------------------------------------------------
#         # Final user-scoped wrapper
#         # ----------------------------------------------------

#         return UserScopedSupabaseClient(
#             client=client,
#             user=verified_user,
#             access_token=(
#                 current_access_token
#             ),
#             refresh_token=(
#                 current_refresh_token
#             ),
#             expires_at=(
#                 int(expires_at)
#                 if expires_at is not None
#                 else None
#             ),
#             expires_in=(
#                 int(expires_in)
#                 if expires_in is not None
#                 else None
#             ),
#             token_type=(
#                 str(token_type)
#                 if token_type
#                 else None
#             ),
#         )

#     except SupabaseAuthenticationError:
#         raise

#     except Exception as exc:

#         logger.warning(
#             "Unable to create authenticated Supabase client. "
#             "error_type=%s",
#             type(exc).__name__,
#         )

#         raise SupabaseAuthenticationError(
#             "Your Supabase login session is invalid or has expired."
#         ) from exc


# # ============================================================
# # VERIFY USER USING EXISTING CLIENT
# # ============================================================


# def _verify_user_with_client(
#     *,
#     client: Client,
#     access_token: str,
# ) -> VerifiedUserIdentity:
#     """
#     Verify authenticated identity through Supabase Auth using an
#     existing client.

#     Internal helper used by create_user_client().
#     """

#     try:

#         response = client.auth.get_user(
#             access_token
#         )

#         user = getattr(
#             response,
#             "user",
#             None,
#         )

#         if user is None:

#             raise SupabaseAuthenticationError(
#                 "Supabase authentication returned no user."
#             )

#         user_id = getattr(
#             user,
#             "id",
#             None,
#         )

#         if not user_id:

#             raise SupabaseAuthenticationError(
#                 "Supabase user ID is missing."
#             )

#         email = getattr(
#             user,
#             "email",
#             None,
#         )

#         return VerifiedUserIdentity(
#             user_id=str(user_id),
#             email=(
#                 str(email)
#                 if email
#                 else None
#             ),
#             raw_user=user,
#         )

#     except SupabaseAuthenticationError:
#         raise

#     except Exception as exc:

#         logger.warning(
#             "Authenticated Supabase identity verification failed. "
#             "error_type=%s",
#             type(exc).__name__,
#         )

#         raise SupabaseAuthenticationError(
#             "Unable to verify the authenticated user."
#         ) from exc


# # ============================================================
# # ADMIN CLIENT
# # ============================================================


# @lru_cache(maxsize=1)
# def get_admin_client() -> Client:
#     """
#     Return the trusted server-side Supabase admin client.

#     Uses:
#         SUPABASE_SECRET_KEY

#     WARNING
#     =======

#     This client has elevated privileges and can bypass Row Level
#     Security.

#     DO NOT use this for:

#         get_user_cart()
#         get_user_orders()
#         get_user_shopping_list()

#     because doing so would defeat our user-isolation architecture.

#     Correct normal-user flow:

#         User JWT
#             |
#             v
#         create_user_client(...)
#             |
#             v
#         RLS


#     Appropriate admin-client uses might include:

#         - admin dashboard
#         - controlled data seeding
#         - backend maintenance
#         - server-side inventory management
#         - administrative user operations

#     This client is cached because:

#         1. it NEVER signs in as a normal user
#         2. it NEVER receives user access tokens
#         3. its auth state is never mutated

#     That makes sharing this server-only client safe from the
#     cross-user session-state problem that applies to authenticated
#     user clients.
#     """

#     _validate_supabase_configuration()

#     try:

#         options = _create_client_options(
#             auto_refresh_token=False,
#             persist_session=False,
#         )

#         client = create_client(
#             settings.supabase_url,
#             settings.supabase_secret_key,
#             options=options,
#         )

#         return client

#     except Exception as exc:

#         logger.error(
#             "Failed to create Supabase admin client. "
#             "error_type=%s",
#             type(exc).__name__,
#         )

#         raise SupabaseConnectionError(
#             "Unable to initialize the privileged Supabase client."
#         ) from exc


# # ============================================================
# # GET AUTHENTICATED USER ID
# # ============================================================


# def get_verified_user_id(
#     access_token: str,
# ) -> str:
#     """
#     Convenience function returning the verified authenticated user ID.

#     Example:

#         user_id = get_verified_user_id(access_token)

#     Never replace this with:

#         user_id = user_message["user_id"]

#     or:

#         user_id = cohere_result["user_id"]

#     AI/user-supplied identity is not trusted.
#     """

#     identity = verify_access_token(
#         access_token
#     )

#     return identity.user_id


# # ============================================================
# # SAFE CONFIGURATION SUMMARY
# # ============================================================


# def get_supabase_safe_summary() -> dict[str, Any]:
#     """
#     Return non-sensitive Supabase configuration information.

#     Useful during development.

#     Secret values are NEVER returned.
#     """

#     _validate_supabase_configuration()

#     return {
#         "url": settings.supabase_url,
#         "schema": DEFAULT_DATABASE_SCHEMA,
#         "publishable_key_configured": bool(
#             settings.supabase_publishable_key
#         ),
#         "secret_key_configured": bool(
#             settings.supabase_secret_key
#         ),
#         "environment": settings.app_env,
#     }


# # ============================================================
# # CLIENT FACTORY ALIAS
# # ============================================================


# def get_public_client() -> Client:
#     """
#     Alias for create_public_client().

#     IMPORTANT:
#     Despite the word 'get', this still creates a NEW client.

#     It is intentionally NOT globally cached.
#     """

#     return create_public_client()


# # ============================================================
# # TEST / DEVELOPMENT CACHE RESET
# # ============================================================


# def clear_supabase_client_cache() -> None:
#     """
#     Clear cached server-only Supabase clients.

#     Currently only the admin client is cached.

#     This function is useful for:

#         - automated tests
#         - environment-variable changes during local development

#     It should rarely be needed during normal application execution.
#     """

#     get_admin_client.cache_clear()


# # ============================================================
# # LOCAL CONFIGURATION HEALTH CHECK
# # ============================================================


# def check_supabase_configuration() -> bool:
#     """
#     Check whether Supabase configuration can be loaded and clients
#     can be constructed.

#     NOTE:
#     -----
#     This is not a complete database connectivity test.

#     It intentionally avoids assuming that a particular table exists.

#     Actual table access will be tested in:

#         database/products.py
#         database/users.py
#     """

#     try:

#         _validate_supabase_configuration()

#         # Create public client.
#         create_public_client()

#         # Create / retrieve admin client.
#         get_admin_client()

#         return True

#     except Exception:

#         logger.exception(
#             "Supabase configuration check failed."
#         )

#         return False

"""
database/supabase.py

Central Supabase client management for the Grocery Chatbot.

========================================================================
PURPOSE
========================================================================

This module is responsible ONLY for creating and validating Supabase
clients.

It does NOT contain:

- product queries
- cart queries
- order queries
- shopping-list queries
- Streamlit UI
- login forms
- business logic
- AI logic

Those responsibilities belong to other files.

========================================================================
WHY THIS FILE IS IMPORTANT
========================================================================

Supabase Python clients keep authentication/session state inside the
client instance.

In a multi-user Streamlit application, this means we must NOT create
one globally cached authenticated Supabase client and share it between
users.

BAD:

    @lru_cache
    def get_supabase():
        return create_client(...)

    # User A logs into the shared client.
    # User B later uses the same client.
    #
    # This creates a serious session-isolation risk.

GOOD:

    User A
        -> own publishable-key client
        -> own JWT
        -> RLS sees User A

    User B
        -> own publishable-key client
        -> own JWT
        -> RLS sees User B


We therefore use FOUR client patterns:

1. PUBLIC CLIENT
   -------------
   Uses SUPABASE_PUBLISHABLE_KEY.

   Has no authenticated user attached.

   Useful for:
       - sign up
       - sign in
       - OAuth initiation
       - public database operations permitted by RLS


2. USER-SCOPED CLIENT
   ------------------
   Uses SUPABASE_PUBLISHABLE_KEY
   + user's access token
   + user's refresh token

   Useful for the original Streamlit/session flow where the backend owns
   both access and refresh tokens.

   This client is NEVER globally cached.


3. API USER-SCOPED CLIENT
   ----------------------
   Uses SUPABASE_PUBLISHABLE_KEY
   + VERIFIED user's access token only

   Intended for the API-only Hugging Face backend.

   Flow:

       Frontend Supabase login
           ↓
       frontend receives Supabase session
           ↓
       frontend sends:
           Authorization: Bearer <access_token>
           ↓
       Hugging Face API verifies token with Supabase
           ↓
       create_user_client_from_access_token()
           ↓
       PostgREST Authorization header uses that JWT
           ↓
       RLS applies auth.uid()

   The refresh token stays with the frontend/Supabase client and is NOT
   sent to the Hugging Face chat API.

   This client is NEVER globally cached.


4. ADMIN CLIENT
   ------------
   Uses SUPABASE_SECRET_KEY.

   This is privileged server-side access.

   It bypasses Row Level Security.

   It must NEVER be:
       - sent to the browser
       - passed to the AI
       - stored in st.session_state
       - used for normal user cart/order access
       - exposed in logs
       - included in chatbot responses


========================================================================
SECURITY MODEL
========================================================================

For normal user data:

Authenticated user JWT
        |
        v
Publishable-key Supabase client
        |
        v
PostgREST
        |
        v
RLS
        |
        v
Only rows belonging to auth.uid()


For trusted admin/server operations:

Python server
        |
        v
Secret-key Supabase client
        |
        v
Elevated access / RLS bypass


========================================================================
IMPORTANT
========================================================================

Never trust a user-supplied user_id.

For example:

    User says:
        "Show cart of user abc123"

We DO NOT do:

    get_cart(user_id="abc123")

Instead:

    authenticated JWT
          |
          v
    Supabase verifies JWT
          |
          v
    user_id = verified auth user
          |
          v
    RLS applies

The authenticated identity always comes from the verified Supabase
session.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, MutableMapping


# ============================================================
# SUPABASE IMPORTS
# ============================================================

from supabase import Client, create_client


# ClientOptions has moved between package locations across
# supabase-py versions.
#
# Supporting both paths makes this module less brittle when updating
# the dependency.
try:
    from supabase.client import ClientOptions

except ImportError:  # pragma: no cover - compatibility fallback

    from supabase.lib.client_options import ClientOptions


# ============================================================
# PROJECT IMPORTS
# ============================================================

from config import settings


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_POSTGREST_TIMEOUT = 20

DEFAULT_STORAGE_TIMEOUT = 30

DEFAULT_DATABASE_SCHEMA = "public"


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================


class SupabaseClientError(RuntimeError):
    """
    Base exception for Supabase client-related failures.
    """

    pass


class SupabaseConfigurationError(SupabaseClientError):
    """
    Raised when Supabase configuration is missing or invalid.
    """

    pass


class SupabaseConnectionError(SupabaseClientError):
    """
    Raised when a Supabase client cannot be created or reached.
    """

    pass


class SupabaseAuthenticationError(SupabaseClientError):
    """
    Raised when a user's Supabase authentication session is invalid.
    """

    pass


class SupabaseAuthorizationError(SupabaseClientError):
    """
    Raised when Supabase rejects an operation due to authorization.
    """

    pass


# ============================================================
# VERIFIED USER IDENTITY
# ============================================================


@dataclass(slots=True)
class VerifiedUserIdentity:
    """
    Server-verified Supabase user identity.

    We intentionally store the raw SDK user object with repr=False
    because it may contain metadata we don't want printed accidentally.

    The user_id is safe to use internally as the authenticated identity.

    IMPORTANT:
    ---------
    This user_id comes from Supabase authentication.

    It does NOT come from:
        - user text
        - query parameters
        - Groq
        - Cohere
        - request payload supplied by the chatbot
    """

    user_id: str

    email: str | None = None

    raw_user: Any = field(
        default=None,
        repr=False,
    )

    def safe_dict(self) -> dict[str, Any]:
        """
        Return safe information suitable for debugging.

        Authentication tokens are intentionally not included.
        """

        return {
            "user_id": self.user_id,
            "email": self.email,
        }


# ============================================================
# USER-SCOPED SUPABASE CLIENT
# ============================================================


@dataclass(slots=True)
class UserScopedSupabaseClient:
    """
    A Supabase client tied to exactly one authenticated user.

    This object contains:

        client
        verified user identity
        access token
        optional refresh token
        expiration information

    Tokens are deliberately hidden from repr().
    """

    client: Client = field(
        repr=False
    )

    user: VerifiedUserIdentity

    access_token: str = field(
        repr=False
    )

    refresh_token: str | None = field(
        default=None,
        repr=False,
    )

    expires_at: int | None = None

    expires_in: int | None = None

    token_type: str | None = None

    # --------------------------------------------------------
    # Convenience properties
    # --------------------------------------------------------

    @property
    def user_id(self) -> str:
        """
        Return authenticated Supabase user ID.
        """

        return self.user.user_id

    @property
    def email(self) -> str | None:
        """
        Return authenticated user's email.
        """

        return self.user.email

    def safe_dict(self) -> dict[str, Any]:
        """
        Return safe diagnostic information.

        Tokens and client internals are never exposed.
        """

        return {
            "user_id": self.user_id,
            "email": self.email,
            "expires_at": self.expires_at,
            "expires_in": self.expires_in,
            "token_type": self.token_type,
        }


# ============================================================
# CONFIGURATION VALIDATION
# ============================================================


def _validate_supabase_configuration() -> None:
    """
    Validate Supabase environment configuration.

    This performs local validation only.

    It does NOT make a network request.
    """

    # --------------------------------------------------------
    # URL
    # --------------------------------------------------------

    if not settings.supabase_url:

        raise SupabaseConfigurationError(
            "SUPABASE_URL is missing."
        )

    url = settings.supabase_url.strip()

    if not (
        url.startswith("https://")
        or (
            settings.is_development
            and url.startswith("http://")
        )
    ):
        raise SupabaseConfigurationError(
            "SUPABASE_URL must be a valid HTTP/HTTPS URL."
        )

    # --------------------------------------------------------
    # Publishable key
    # --------------------------------------------------------

    if not settings.supabase_publishable_key:

        raise SupabaseConfigurationError(
            "SUPABASE_PUBLISHABLE_KEY is missing."
        )

    # --------------------------------------------------------
    # Secret key
    # --------------------------------------------------------

    if not settings.supabase_secret_key:

        raise SupabaseConfigurationError(
            "SUPABASE_SECRET_KEY is missing."
        )

    # --------------------------------------------------------
    # Defensive key mismatch checks
    # --------------------------------------------------------

    publishable_key = (
        settings.supabase_publishable_key.strip()
    )

    secret_key = (
        settings.supabase_secret_key.strip()
    )

    if publishable_key == secret_key:

        raise SupabaseConfigurationError(
            "SUPABASE_PUBLISHABLE_KEY and "
            "SUPABASE_SECRET_KEY must not be identical."
        )

    # New Supabase keys normally begin with:
    #
    # sb_publishable_
    # sb_secret_
    #
    # We intentionally don't reject legacy JWT-based keys here,
    # because the application may later need to support migration
    # scenarios.

    if (
        publishable_key.startswith("sb_secret_")
    ):
        raise SupabaseConfigurationError(
            "SUPABASE_PUBLISHABLE_KEY appears to contain "
            "a secret Supabase key."
        )

    if (
        secret_key.startswith("sb_publishable_")
    ):
        raise SupabaseConfigurationError(
            "SUPABASE_SECRET_KEY appears to contain "
            "a publishable Supabase key."
        )


# ============================================================
# CLIENT OPTIONS
# ============================================================


def _create_client_options(
    *,
    auto_refresh_token: bool,
    persist_session: bool,
) -> ClientOptions:
    """
    Create Supabase ClientOptions.

    Why persist_session=False?
    --------------------------

    Streamlit manages the user's session separately.

    We do not want the Supabase SDK to become our main source of
    persistent cross-rerun authentication state.

    auth/session.py will explicitly own:

        access_token
        refresh_token
        user_id
        authentication state

    This gives us deterministic behavior.

    Why auto_refresh_token?
    -----------------------

    User clients:
        True

    Admin client:
        False

    Admin credentials do not represent a normal user login session,
    so there is nothing to refresh.
    """

    try:

        return ClientOptions(
            schema=DEFAULT_DATABASE_SCHEMA,
            auto_refresh_token=auto_refresh_token,
            persist_session=persist_session,
            postgrest_client_timeout=(
                DEFAULT_POSTGREST_TIMEOUT
            ),
            storage_client_timeout=(
                DEFAULT_STORAGE_TIMEOUT
            ),
        )

    except TypeError:

        # ----------------------------------------------------
        # Compatibility fallback
        # ----------------------------------------------------
        #
        # Some older supabase-py versions expose fewer option
        # parameters.
        #
        # We retain the security-critical auth settings.
        # ----------------------------------------------------

        logger.warning(
            "Installed supabase-py version exposes a reduced "
            "ClientOptions constructor. Falling back to basic "
            "authentication options."
        )

        return ClientOptions(
            auto_refresh_token=auto_refresh_token,
            persist_session=persist_session,
        )


# ============================================================
# PUBLIC CLIENT
# ============================================================


def create_public_client() -> Client:
    """
    Create a NEW unauthenticated Supabase client.

    Uses:
        SUPABASE_PUBLISHABLE_KEY

    IMPORTANT:
    ----------
    This function deliberately does NOT use @lru_cache.

    A public client may later become authenticated during:

        sign_in_with_password()
        sign_up()
        set_session()

    If we globally cached it, authentication state could accidentally
    leak across Streamlit users.

    Therefore:

        create_public_client()

    always gives the caller its own Supabase client instance.

    Typical uses:
    -------------

        auth.py:
            sign up
            sign in

        database access:
            public product catalog operations permitted by RLS
    """

    _validate_supabase_configuration()

    try:

        options = _create_client_options(
            auto_refresh_token=True,
            persist_session=False,
        )

        client = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
            options=options,
        )

        return client

    except SupabaseClientError:
        raise

    except Exception as exc:

        # Do NOT log:
        #
        # settings.supabase_publishable_key
        # settings.supabase_secret_key
        #
        # Even though the publishable key is less sensitive, there is
        # no reason to include credentials in application logs.

        logger.error(
            "Failed to create public Supabase client. error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseConnectionError(
            "Unable to initialize Supabase."
        ) from exc


# ============================================================
# ATTACH ACCESS TOKEN TO POSTGREST
# ============================================================


def _attach_access_token_to_postgrest(
    *,
    client: Client,
    access_token: str,
) -> None:
    """
    Attach a verified Supabase JWT to the client's PostgREST layer.

    The API-only Hugging Face backend receives only:

        Authorization: Bearer <access_token>

    The frontend keeps the Supabase refresh token.

    Therefore this API path does not use auth.set_session(), because
    set_session() requires both access and refresh tokens.

    The verified access token is attached to PostgREST so Supabase RLS
    still evaluates auth.uid() for cart/order/profile operations.
    """

    access_token = _validate_token(
        access_token,
        token_name="access_token",
    )

    postgrest = getattr(
        client,
        "postgrest",
        None,
    )

    if postgrest is None:

        postgrest = getattr(
            client,
            "rest",
            None,
        )

    if postgrest is None:

        raise SupabaseConnectionError(
            "Supabase PostgREST client is unavailable."
        )

    auth_method = getattr(
        postgrest,
        "auth",
        None,
    )

    if callable(
        auth_method
    ):

        try:

            auth_method(
                access_token
            )

            return

        except TypeError:

            try:

                auth_method(
                    token=access_token
                )

                return

            except Exception:
                pass

        except Exception as exc:

            logger.warning(
                "Unable to attach authenticated JWT to PostgREST. "
                "error_type=%s",
                type(exc).__name__,
            )

            raise SupabaseConnectionError(
                "Unable to initialize authenticated database access."
            ) from exc

    bearer_value = (
        "Bearer "
        + access_token
    )

    for header_container_name in (
        "headers",
        "_headers",
    ):

        headers = getattr(
            postgrest,
            header_container_name,
            None,
        )

        if isinstance(
            headers,
            MutableMapping,
        ):

            headers[
                "Authorization"
            ] = bearer_value

            return

    raise SupabaseConnectionError(
        "Installed Supabase/PostgREST client does not expose a supported "
        "authentication interface."
    )


# ============================================================
# VERIFY ACCESS TOKEN
# ============================================================


def verify_access_token(
    access_token: str,
) -> VerifiedUserIdentity:
    """
    Verify a Supabase access token with the Supabase Auth server.

    Parameters
    ----------
    access_token:
        JWT received after authentication.

    Returns
    -------
    VerifiedUserIdentity
        Trusted server-verified identity.

    Security
    --------

    We do NOT simply decode the JWT ourselves and trust the payload.

    Supabase auth.get_user(access_token) verifies the token through
    Supabase Auth and returns the authenticated user.

    That verified identity becomes the source for user_id.
    """

    access_token = _validate_token(
        access_token,
        token_name="access_token",
    )

    client = create_public_client()

    try:

        response = client.auth.get_user(
            access_token
        )

        user = getattr(
            response,
            "user",
            None,
        )

        if user is None:

            raise SupabaseAuthenticationError(
                "Supabase did not return an authenticated user."
            )

        user_id = getattr(
            user,
            "id",
            None,
        )

        if not user_id:

            raise SupabaseAuthenticationError(
                "Authenticated Supabase user has no ID."
            )

        email = getattr(
            user,
            "email",
            None,
        )

        return VerifiedUserIdentity(
            user_id=str(user_id),
            email=(
                str(email)
                if email
                else None
            ),
            raw_user=user,
        )

    except SupabaseAuthenticationError:
        raise

    except Exception as exc:

        logger.warning(
            "Supabase access-token verification failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseAuthenticationError(
            "Your login session is invalid or has expired."
        ) from exc


# ============================================================
# TOKEN VALIDATION
# ============================================================


def _validate_token(
    token: str,
    *,
    token_name: str,
) -> str:
    """
    Validate token input without logging or exposing it.
    """

    if not isinstance(
        token,
        str,
    ):

        raise SupabaseAuthenticationError(
            f"{token_name} must be a string."
        )

    token = token.strip()

    if not token:

        raise SupabaseAuthenticationError(
            f"{token_name} is missing."
        )

    # We deliberately avoid strict JWT-format validation.
    #
    # The Supabase server is responsible for determining whether
    # the token is actually valid.

    return token


# ============================================================
# SESSION RESPONSE EXTRACTION
# ============================================================


def _extract_session(
    auth_response: Any,
) -> Any | None:
    """
    Extract the session object from a Supabase auth response.

    Different supabase-py versions can expose slightly different
    response structures.

    Usually:

        response.session

    Defensive fallback:

        response itself behaves like a session.
    """

    if auth_response is None:
        return None

    session = getattr(
        auth_response,
        "session",
        None,
    )

    if session is not None:
        return session

    # --------------------------------------------------------
    # Compatibility fallback
    # --------------------------------------------------------

    if getattr(
        auth_response,
        "access_token",
        None,
    ):
        return auth_response

    return None


# ============================================================
# SESSION TOKEN EXTRACTION
# ============================================================


def _session_value(
    session: Any,
    field_name: str,
    fallback: Any = None,
) -> Any:
    """
    Safely retrieve a field from a Supabase session object.
    """

    if session is None:
        return fallback

    value = getattr(
        session,
        field_name,
        None,
    )

    if value is None:
        return fallback

    return value


# ============================================================
# CREATE AUTHENTICATED USER CLIENT
# ============================================================


def create_user_client(
    *,
    access_token: str,
    refresh_token: str,
    verify_user: bool = True,
) -> UserScopedSupabaseClient:
    """
    Create a Supabase client scoped to ONE authenticated user.

    This is one of the most important functions in the application.

    Parameters
    ----------
    access_token:
        Current Supabase access JWT.

    refresh_token:
        Current Supabase refresh token.

    verify_user:
        Whether to verify the resulting identity through Supabase Auth.

        Default:
            True

        Keep this True for normal application usage.

    Returns
    -------
    UserScopedSupabaseClient

    Example
    -------

        user_db = create_user_client(
            access_token=session_access_token,
            refresh_token=session_refresh_token,
        )

        client = user_db.client

        user_id = user_db.user_id


    IMPORTANT TOKEN REFRESH BEHAVIOR
    --------------------------------

    Supabase set_session() may refresh an expired access token.

    If that happens, the returned access_token and refresh_token can
    differ from the values supplied to this function.

    Therefore this function returns the CURRENT tokens.

    auth/session.py must later update Streamlit session state:

        st.session_state.access_token = user_db.access_token
        st.session_state.refresh_token = user_db.refresh_token

    Never assume old tokens remain current forever.
    """

    access_token = _validate_token(
        access_token,
        token_name="access_token",
    )

    refresh_token = _validate_token(
        refresh_token,
        token_name="refresh_token",
    )

    # --------------------------------------------------------
    # Fresh client per authenticated context
    # --------------------------------------------------------

    client = create_public_client()

    try:

        # ----------------------------------------------------
        # Attach user's Supabase auth session
        # ----------------------------------------------------

        auth_response = client.auth.set_session(
            access_token,
            refresh_token,
        )

        session = _extract_session(
            auth_response
        )

        # ----------------------------------------------------
        # Capture potentially refreshed tokens
        # ----------------------------------------------------

        current_access_token = (
            _session_value(
                session,
                "access_token",
                access_token,
            )
        )

        current_refresh_token = (
            _session_value(
                session,
                "refresh_token",
                refresh_token,
            )
        )

        current_access_token = (
            _validate_token(
                current_access_token,
                token_name="access_token",
            )
        )

        current_refresh_token = (
            _validate_token(
                current_refresh_token,
                token_name="refresh_token",
            )
        )

        # ----------------------------------------------------
        # Verify user identity
        # ----------------------------------------------------

        if verify_user:

            verified_user = (
                _verify_user_with_client(
                    client=client,
                    access_token=(
                        current_access_token
                    ),
                )
            )

        else:

            # ------------------------------------------------
            # We strongly prefer verification.
            #
            # This fallback exists mainly for controlled testing.
            # ------------------------------------------------

            auth_user = getattr(
                auth_response,
                "user",
                None,
            )

            if auth_user is None:

                raise SupabaseAuthenticationError(
                    "Authenticated user could not be determined."
                )

            user_id = getattr(
                auth_user,
                "id",
                None,
            )

            if not user_id:

                raise SupabaseAuthenticationError(
                    "Authenticated user has no ID."
                )

            email = getattr(
                auth_user,
                "email",
                None,
            )

            verified_user = VerifiedUserIdentity(
                user_id=str(user_id),
                email=(
                    str(email)
                    if email
                    else None
                ),
                raw_user=auth_user,
            )

        # ----------------------------------------------------
        # Session metadata
        # ----------------------------------------------------

        expires_at = _session_value(
            session,
            "expires_at",
        )

        expires_in = _session_value(
            session,
            "expires_in",
        )

        token_type = _session_value(
            session,
            "token_type",
        )

        # ----------------------------------------------------
        # Final user-scoped wrapper
        # ----------------------------------------------------

        return UserScopedSupabaseClient(
            client=client,
            user=verified_user,
            access_token=(
                current_access_token
            ),
            refresh_token=(
                current_refresh_token
            ),
            expires_at=(
                int(expires_at)
                if expires_at is not None
                else None
            ),
            expires_in=(
                int(expires_in)
                if expires_in is not None
                else None
            ),
            token_type=(
                str(token_type)
                if token_type
                else None
            ),
        )

    except SupabaseAuthenticationError:
        raise

    except Exception as exc:

        logger.warning(
            "Unable to create authenticated Supabase client. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseAuthenticationError(
            "Your Supabase login session is invalid or has expired."
        ) from exc


# ============================================================
# CREATE API USER CLIENT FROM ACCESS TOKEN ONLY
# ============================================================


def create_user_client_from_access_token(
    *,
    access_token: str,
    verify_user: bool = True,
) -> UserScopedSupabaseClient:
    """
    Create a NEW Supabase client scoped to one authenticated API user
    using only the current Supabase access token.

    Intended flow:

        frontend Supabase login
            ↓
        Authorization: Bearer <access_token>
            ↓
        Hugging Face API
            ↓
        create_user_client_from_access_token()
            ↓
        verified Supabase user
            ↓
        PostgREST bearer JWT
            ↓
        RLS / auth.uid()

    The backend does NOT refresh expired access tokens.

    If the access token expires, the API should return 401 and the
    frontend Supabase SDK should refresh its own session and retry.
    """

    access_token = _validate_token(
        access_token,
        token_name="access_token",
    )

    _validate_supabase_configuration()

    try:

        options = _create_client_options(
            auto_refresh_token=False,
            persist_session=False,
        )

        client = create_client(
            settings.supabase_url,
            settings.supabase_publishable_key,
            options=options,
        )

    except SupabaseClientError:
        raise

    except Exception as exc:

        logger.error(
            "Failed to create API user Supabase client. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseConnectionError(
            "Unable to initialize authenticated database access."
        ) from exc

    try:

        if verify_user:

            verified_user = (
                _verify_user_with_client(
                    client=client,
                    access_token=access_token,
                )
            )

        else:

            response = (
                client
                .auth
                .get_user(
                    access_token
                )
            )

            auth_user = getattr(
                response,
                "user",
                None,
            )

            if auth_user is None:

                raise SupabaseAuthenticationError(
                    "Authenticated user could not be determined."
                )

            user_id = getattr(
                auth_user,
                "id",
                None,
            )

            if not user_id:

                raise SupabaseAuthenticationError(
                    "Authenticated user has no ID."
                )

            email = getattr(
                auth_user,
                "email",
                None,
            )

            verified_user = (
                VerifiedUserIdentity(
                    user_id=str(
                        user_id
                    ),
                    email=(
                        str(
                            email
                        )
                        if email
                        else None
                    ),
                    raw_user=auth_user,
                )
            )

        _attach_access_token_to_postgrest(
            client=client,
            access_token=access_token,
        )

        return UserScopedSupabaseClient(
            client=client,
            user=verified_user,
            access_token=access_token,
            refresh_token=None,
            expires_at=None,
            expires_in=None,
            token_type="bearer",
        )

    except SupabaseAuthenticationError:
        raise

    except SupabaseConnectionError:
        raise

    except Exception as exc:

        logger.warning(
            "Unable to create access-token-scoped Supabase client. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseAuthenticationError(
            "Your Supabase login session is invalid or has expired."
        ) from exc


def create_api_user_client(
    *,
    access_token: str,
    verify_user: bool = True,
) -> UserScopedSupabaseClient:
    """
    API-oriented alias for create_user_client_from_access_token().

    This is the preferred factory for user-private API operations such as
    cart, orders and authenticated profile access.
    """

    return create_user_client_from_access_token(
        access_token=access_token,
        verify_user=verify_user,
    )


# ============================================================
# VERIFY USER USING EXISTING CLIENT
# ============================================================


def _verify_user_with_client(
    *,
    client: Client,
    access_token: str,
) -> VerifiedUserIdentity:
    """
    Verify authenticated identity through Supabase Auth using an
    existing client.

    Internal helper used by create_user_client().
    """

    try:

        response = client.auth.get_user(
            access_token
        )

        user = getattr(
            response,
            "user",
            None,
        )

        if user is None:

            raise SupabaseAuthenticationError(
                "Supabase authentication returned no user."
            )

        user_id = getattr(
            user,
            "id",
            None,
        )

        if not user_id:

            raise SupabaseAuthenticationError(
                "Supabase user ID is missing."
            )

        email = getattr(
            user,
            "email",
            None,
        )

        return VerifiedUserIdentity(
            user_id=str(user_id),
            email=(
                str(email)
                if email
                else None
            ),
            raw_user=user,
        )

    except SupabaseAuthenticationError:
        raise

    except Exception as exc:

        logger.warning(
            "Authenticated Supabase identity verification failed. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseAuthenticationError(
            "Unable to verify the authenticated user."
        ) from exc


# ============================================================
# ADMIN CLIENT
# ============================================================


@lru_cache(maxsize=1)
def get_admin_client() -> Client:
    """
    Return the trusted server-side Supabase admin client.

    Uses:
        SUPABASE_SECRET_KEY

    WARNING
    =======

    This client has elevated privileges and can bypass Row Level
    Security.

    DO NOT use this for:

        get_user_cart()
        get_user_orders()
        get_user_shopping_list()

    because doing so would defeat our user-isolation architecture.

    Correct normal-user flow:

        User JWT
            |
            v
        create_user_client(...)
            |
            v
        RLS


    Appropriate admin-client uses might include:

        - admin dashboard
        - controlled data seeding
        - backend maintenance
        - server-side inventory management
        - administrative user operations

    This client is cached because:

        1. it NEVER signs in as a normal user
        2. it NEVER receives user access tokens
        3. its auth state is never mutated

    That makes sharing this server-only client safe from the
    cross-user session-state problem that applies to authenticated
    user clients.
    """

    _validate_supabase_configuration()

    try:

        options = _create_client_options(
            auto_refresh_token=False,
            persist_session=False,
        )

        client = create_client(
            settings.supabase_url,
            settings.supabase_secret_key,
            options=options,
        )

        return client

    except Exception as exc:

        logger.error(
            "Failed to create Supabase admin client. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise SupabaseConnectionError(
            "Unable to initialize the privileged Supabase client."
        ) from exc


# ============================================================
# GET AUTHENTICATED USER ID
# ============================================================


def get_verified_user_id(
    access_token: str,
) -> str:
    """
    Convenience function returning the verified authenticated user ID.

    Example:

        user_id = get_verified_user_id(access_token)

    Never replace this with:

        user_id = user_message["user_id"]

    or:

        user_id = cohere_result["user_id"]

    AI/user-supplied identity is not trusted.
    """

    identity = verify_access_token(
        access_token
    )

    return identity.user_id


# ============================================================
# SAFE CONFIGURATION SUMMARY
# ============================================================


def get_supabase_safe_summary() -> dict[str, Any]:
    """
    Return non-sensitive Supabase configuration information.

    Useful during development.

    Secret values are NEVER returned.
    """

    _validate_supabase_configuration()

    return {
        "url": settings.supabase_url,
        "schema": DEFAULT_DATABASE_SCHEMA,
        "publishable_key_configured": bool(
            settings.supabase_publishable_key
        ),
        "secret_key_configured": bool(
            settings.supabase_secret_key
        ),
        "environment": settings.app_env,
        "api_access_token_client_supported": True,
        "api_refresh_token_required": False,
        "api_user_client_cached": False,
        "api_user_data_rls": True,
    }


# ============================================================
# CLIENT FACTORY ALIAS
# ============================================================


def get_public_client() -> Client:
    """
    Alias for create_public_client().

    IMPORTANT:
    Despite the word 'get', this still creates a NEW client.

    It is intentionally NOT globally cached.
    """

    return create_public_client()


# ============================================================
# TEST / DEVELOPMENT CACHE RESET
# ============================================================


def clear_supabase_client_cache() -> None:
    """
    Clear cached server-only Supabase clients.

    Currently only the admin client is cached.

    This function is useful for:

        - automated tests
        - environment-variable changes during local development

    It should rarely be needed during normal application execution.
    """

    get_admin_client.cache_clear()


# ============================================================
# LOCAL CONFIGURATION HEALTH CHECK
# ============================================================


def check_supabase_configuration() -> bool:
    """
    Check whether Supabase configuration can be loaded and clients
    can be constructed.

    NOTE:
    -----
    This is not a complete database connectivity test.

    It intentionally avoids assuming that a particular table exists.

    Actual table access will be tested in:

        database/products.py
        database/users.py

    API access-token user clients cannot be fully tested here because
    doing so requires a real current user's Supabase access token.
    """

    try:

        _validate_supabase_configuration()

        # Create public client.
        create_public_client()

        # Create / retrieve admin client.
        get_admin_client()

        return True

    except Exception:

        logger.exception(
            "Supabase configuration check failed."
        )

        return False