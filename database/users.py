# """
# database/users.py

# Private user-data access layer for the Grocery Chatbot.

# ========================================================================
# RESPONSIBILITIES
# ========================================================================

# This module handles authenticated Supabase data belonging to the
# currently signed-in user:

#     profiles
#     cart_items
#     orders
#     order_items

# It provides trusted database primitives for:

#     tools/cart_tools.py
#     tools/order_tools.py
#     auth/app UI

# ========================================================================
# THIS MODULE DOES NOT
# ========================================================================

# - talk to Cohere
# - talk to Groq
# - perform RAG
# - decide chatbot intent
# - calculate cart totals
# - select product variants
# - validate stock
# - generate conversational responses
# - accept arbitrary user_id values

# Those belong to higher-level layers.

# ========================================================================
# SECURITY MODEL
# ========================================================================

# User
#  |
#  v
# Streamlit Session
#  |
#  | access_token
#  | refresh_token
#  |
#  v
# get_user_supabase_client()
#  |
#  v
# Supabase verifies JWT
#  |
#  v
# verified user_id
#  |
#  v
# RLS
#  |
#  v
# User's own rows


# CRITICAL:

# Functions in this file NEVER accept:

#     user_id="some-user"

# from the chatbot.

# The authenticated Supabase session determines the user.

# Even when RLS is enabled, we additionally filter by the verified
# user_id where appropriate. This provides defense in depth.

# ========================================================================
# DATABASE MODEL
# ========================================================================

# Existing user-related tables:

# profiles
# --------
# id
# email
# full_name
# phone
# role
# avatar_url
# address
# created_at
# updated_at


# cart_items
# ----------
# id
# user_id
# product_id
# quantity
# created_at
# updated_at

# IMPORTANT:

# product_id references a row in products.

# Since the existing products table stores SKU rows, product_id is
# effectively the SKU ID used by the cart.


# orders
# ------
# id
# user_id
# ...
# total_amount
# payment_status
# order_status
# created_at
# updated_at


# order_items
# -----------
# id
# order_id
# product_id
# ...
# quantity
# unit_price
# total_price
# created_at

# We deliberately use select("*") for orders/order_items because the
# commerce schema may contain additional fields such as:

#     subtotal
#     delivery_fee
#     discount_amount
#     payment_method
#     delivery_address
#     notes
#     payment_id

# and the database remains the source of truth.

# ========================================================================
# IMPORTANT ARCHITECTURAL RULE
# ========================================================================

# This is a DATABASE layer.

# For example:

#     create_cart_item()

# does NOT decide whether stock is sufficient.

# The future cart tool does:

#     cart_tools.py
#         ↓
#     get_sku_by_id()
#         ↓
#     validate stock
#         ↓
#     calculate desired quantity
#         ↓
#     database/users.py
#         ↓
#     write verified quantity

# This keeps database access separate from business logic.
# """

# from __future__ import annotations

# import logging

# from datetime import datetime, timezone
# from typing import Any, Mapping, Sequence


# # ============================================================
# # APPLICATION IMPORTS
# # ============================================================

# from auth.session import (
#     NotAuthenticatedError,
#     get_user_supabase_client,
# )


# # ============================================================
# # LOGGER
# # ============================================================

# logger = logging.getLogger(__name__)


# # ============================================================
# # TABLE NAMES
# # ============================================================

# PROFILES_TABLE = "profiles"

# CART_ITEMS_TABLE = "cart_items"

# ORDERS_TABLE = "orders"

# ORDER_ITEMS_TABLE = "order_items"


# # ============================================================
# # LIMITS
# # ============================================================

# MAX_CART_QUANTITY = 999

# MAX_ORDER_QUERY_LIMIT = 100

# MAX_PROFILE_TEXT_LENGTH = 5000


# # ============================================================
# # CUSTOM EXCEPTIONS
# # ============================================================


# class UserDatabaseError(RuntimeError):
#     """
#     Base exception for authenticated user-data operations.
#     """

#     pass


# class UserDatabaseAuthenticationError(
#     UserDatabaseError
# ):
#     """
#     Raised when a private database operation is attempted without
#     valid authentication.
#     """

#     pass


# class UserDatabaseValidationError(
#     UserDatabaseError
# ):
#     """
#     Raised when supplied database arguments are invalid.
#     """

#     pass


# class UserDatabaseNotFoundError(
#     UserDatabaseError
# ):
#     """
#     Raised when an explicitly requested private record does not exist.
#     """

#     pass


# class UserDatabaseConflictError(
#     UserDatabaseError
# ):
#     """
#     Raised when a write cannot proceed because existing state conflicts
#     with the requested operation.
#     """

#     pass


# # ============================================================
# # TIME
# # ============================================================


# def _utc_iso() -> str:
#     """
#     Return UTC timestamp suitable for database updated_at fields.
#     """

#     return datetime.now(
#         timezone.utc
#     ).isoformat()


# # ============================================================
# # BASIC VALIDATION
# # ============================================================


# def _required_identifier(
#     value: Any,
#     *,
#     field_name: str,
# ) -> str:
#     """
#     Validate IDs without assuming whether the database uses UUID,
#     integer-like strings or another identifier representation.

#     Supabase itself ultimately validates the DB type.
#     """

#     if value is None:

#         raise UserDatabaseValidationError(
#             f"{field_name} is required."
#         )

#     value = str(
#         value
#     ).strip()

#     if not value:

#         raise UserDatabaseValidationError(
#             f"{field_name} cannot be empty."
#         )

#     if len(value) > 200:

#         raise UserDatabaseValidationError(
#             f"{field_name} is invalid."
#         )

#     return value


# def _validate_quantity(
#     quantity: Any,
# ) -> int:
#     """
#     Validate cart quantity.

#     Cart quantities must always be positive integers.
#     """

#     # bool is technically an int subclass in Python.
#     # We don't want True becoming quantity=1.

#     if isinstance(
#         quantity,
#         bool,
#     ):

#         raise UserDatabaseValidationError(
#             "Cart quantity must be an integer."
#         )

#     try:

#         quantity = int(
#             quantity
#         )

#     except (
#         TypeError,
#         ValueError,
#     ) as exc:

#         raise UserDatabaseValidationError(
#             "Cart quantity must be an integer."
#         ) from exc

#     if quantity < 1:

#         raise UserDatabaseValidationError(
#             "Cart quantity must be at least 1."
#         )

#     if quantity > MAX_CART_QUANTITY:

#         raise UserDatabaseValidationError(
#             f"Cart quantity cannot exceed "
#             f"{MAX_CART_QUANTITY}."
#         )

#     return quantity


# # ============================================================
# # AUTHENTICATED DATABASE CONTEXT
# # ============================================================


# def _get_private_context():
#     """
#     Return:

#         authenticated Supabase client
#         verified user_id

#     IMPORTANT:

#     user_id comes from Supabase authentication.

#     It NEVER comes from an LLM or request argument.
#     """

#     try:

#         user_db = (
#             get_user_supabase_client(
#                 verify_user=True
#             )
#         )

#     except NotAuthenticatedError as exc:

#         raise UserDatabaseAuthenticationError(
#             "You must be signed in to access private data."
#         ) from exc

#     except Exception as exc:

#         logger.warning(
#             "Unable to establish authenticated database context. "
#             "error_type=%s",
#             type(exc).__name__,
#         )

#         raise UserDatabaseAuthenticationError(
#             "Your authentication session could not be verified."
#         ) from exc

#     if not user_db.user_id:

#         raise UserDatabaseAuthenticationError(
#             "Authenticated user identity is unavailable."
#         )

#     return (
#         user_db.client,
#         user_db.user_id,
#     )


# # ============================================================
# # SAFE RESPONSE DATA
# # ============================================================


# def _response_rows(
#     response: Any,
# ) -> list[dict[str, Any]]:
#     """
#     Convert Supabase response.data into list[dict].

#     Never return SDK-specific row objects to higher application layers.
#     """

#     if response is None:

#         return []

#     data = getattr(
#         response,
#         "data",
#         None,
#     )

#     if not data:

#         return []

#     if isinstance(
#         data,
#         Mapping,
#     ):

#         return [
#             dict(data)
#         ]

#     if isinstance(
#         data,
#         Sequence,
#     ) and not isinstance(
#         data,
#         (str, bytes),
#     ):

#         return [
#             dict(row)
#             for row in data
#             if isinstance(
#                 row,
#                 Mapping,
#             )
#         ]

#     return []


# # ============================================================
# # PROFILE
# # ============================================================


# def get_current_profile() -> dict[
#     str,
#     Any
# ] | None:
#     """
#     Return current authenticated user's profile.

#     This function cannot retrieve another user's profile.

#     Example
#     -------

#     profile = get_current_profile()

#     {
#         "id": "...",
#         "email": "...",
#         "full_name": "...",
#         ...
#     }
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         response = (
#             client
#             .table(
#                 PROFILES_TABLE
#             )
#             .select("*")
#             .eq(
#                 "id",
#                 user_id,
#             )
#             .limit(1)
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Profile query failed. user_id=%s error_type=%s",
#             user_id,
#             type(exc).__name__,
#         )

#         raise UserDatabaseError(
#             "Unable to retrieve your profile."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if not rows:
#         return None

#     return rows[0]


# # ============================================================
# # ENSURE PROFILE
# # ============================================================


# def ensure_current_profile(
#     *,
#     email: str | None = None,
#     full_name: str | None = None,
# ) -> dict[str, Any]:
#     """
#     Ensure the authenticated Supabase user has a profiles row.

#     Useful after:

#         email signup
#         Google OAuth signup

#     Ideally a Supabase auth trigger creates profiles automatically.

#     However this method provides a safe application-level fallback.

#     It NEVER allows callers to set:

#         role
#         id
#         user_id

#     to arbitrary values.
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     existing = (
#         get_current_profile()
#     )

#     if existing is not None:

#         return existing

#     payload: dict[
#         str,
#         Any
#     ] = {
#         "id": user_id,
#     }

#     if email:

#         payload[
#             "email"
#         ] = str(
#             email
#         ).strip().lower()

#     if full_name:

#         value = " ".join(
#             str(
#                 full_name
#             )
#             .strip()
#             .split()
#         )

#         if value:

#             payload[
#                 "full_name"
#             ] = value

#     try:

#         response = (
#             client
#             .table(
#                 PROFILES_TABLE
#             )
#             .insert(
#                 payload
#             )
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Profile creation failed. "
#             "user_id=%s error_type=%s",
#             user_id,
#             type(exc).__name__,
#         )

#         # Race-safe fallback:
#         #
#         # The profile might have been created by a DB trigger between
#         # our check and our insert.

#         retry_profile = (
#             get_current_profile()
#         )

#         if retry_profile is not None:
#             return retry_profile

#         raise UserDatabaseError(
#             "Unable to initialize your profile."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if rows:
#         return rows[0]

#     # Supabase insert may return no representation depending on
#     # configuration. Re-read authoritative row.

#     created = (
#         get_current_profile()
#     )

#     if created is None:

#         raise UserDatabaseError(
#             "Profile creation could not be verified."
#         )

#     return created


# # ============================================================
# # UPDATE PROFILE
# # ============================================================


# def update_current_profile(
#     *,
#     full_name: str | None = None,
#     phone: str | None = None,
#     avatar_url: str | None = None,
#     address: str | None = None,
# ) -> dict[str, Any]:
#     """
#     Update allowed profile information.

#     SECURITY:

#     Callers CANNOT modify:

#         id
#         role
#         email
#         created_at

#     through this function.

#     Role changes should only happen in controlled admin code.
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     payload: dict[
#         str,
#         Any
#     ] = {}

#     values = {
#         "full_name": full_name,
#         "phone": phone,
#         "avatar_url": avatar_url,
#         "address": address,
#     }

#     for field_name, value in (
#         values.items()
#     ):

#         if value is None:
#             continue

#         if not isinstance(
#             value,
#             str,
#         ):

#             raise UserDatabaseValidationError(
#                 f"{field_name} must be text."
#             )

#         value = value.strip()

#         if len(value) > (
#             MAX_PROFILE_TEXT_LENGTH
#         ):

#             raise UserDatabaseValidationError(
#                 f"{field_name} is too long."
#             )

#         payload[
#             field_name
#         ] = (
#             value
#             if value
#             else None
#         )

#     if not payload:

#         existing = (
#             get_current_profile()
#         )

#         if existing is None:

#             raise UserDatabaseNotFoundError(
#                 "Profile does not exist."
#             )

#         return existing

#     payload[
#         "updated_at"
#     ] = _utc_iso()

#     try:

#         response = (
#             client
#             .table(
#                 PROFILES_TABLE
#             )
#             .update(
#                 payload
#             )
#             .eq(
#                 "id",
#                 user_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to update your profile."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if rows:

#         return rows[0]

#     result = (
#         get_current_profile()
#     )

#     if result is None:

#         raise UserDatabaseNotFoundError(
#             "Profile could not be found."
#         )

#     return result


# # ============================================================
# # CART — READ ALL
# # ============================================================


# def get_cart_items() -> list[
#     dict[str, Any]
# ]:
#     """
#     Return raw cart rows belonging to current authenticated user.

#     These rows contain product_id, which points to an exact SKU.

#     This database function intentionally does NOT calculate totals or
#     fetch product prices.

#     tools/cart_tools.py will combine these rows with live SKU data from
#     database/products.py.
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         response = (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .order(
#                 "created_at",
#                 desc=False,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Cart retrieval failed. "
#             "user_id=%s error_type=%s",
#             user_id,
#             type(exc).__name__,
#         )

#         raise UserDatabaseError(
#             "Unable to retrieve your cart."
#         ) from exc

#     return _response_rows(
#         response
#     )


# # ============================================================
# # CART — GET ONE SKU
# # ============================================================


# def get_cart_item_by_sku(
#     sku_id: str,
# ) -> dict[
#     str,
#     Any
# ] | None:
#     """
#     Retrieve current user's cart row for one SKU.

#     `product_id` in the existing cart schema refers to the SKU row in
#     products.
#     """

#     sku_id = _required_identifier(
#         sku_id,
#         field_name="sku_id",
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         response = (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .eq(
#                 "product_id",
#                 sku_id,
#             )
#             .limit(1)
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to retrieve cart item."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     return (
#         rows[0]
#         if rows
#         else None
#     )


# # ============================================================
# # CART — GET BY CART ITEM ID
# # ============================================================


# def get_cart_item_by_id(
#     cart_item_id: str,
# ) -> dict[
#     str,
#     Any
# ] | None:
#     """
#     Retrieve one cart row by cart_items.id.

#     Defense in depth:

#     We require BOTH:

#         id = cart_item_id
#         user_id = authenticated user
#     """

#     cart_item_id = (
#         _required_identifier(
#             cart_item_id,
#             field_name="cart_item_id",
#         )
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         response = (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "id",
#                 cart_item_id,
#             )
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .limit(1)
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to retrieve cart item."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     return (
#         rows[0]
#         if rows
#         else None
#     )


# # ============================================================
# # CART — CREATE
# # ============================================================


# def create_cart_item(
#     *,
#     sku_id: str,
#     quantity: int,
# ) -> dict[str, Any]:
#     """
#     Insert one SKU into current user's cart.

#     IMPORTANT:

#     This is a LOW-LEVEL database primitive.

#     It does NOT:

#         validate stock
#         determine which variant the user meant
#         increase an existing cart quantity automatically

#     cart_tools.py will perform those business rules BEFORE calling
#     this function.

#     If the SKU already exists, cart_tools.py should normally call
#     update_cart_item_quantity().
#     """

#     sku_id = _required_identifier(
#         sku_id,
#         field_name="sku_id",
#     )

#     quantity = _validate_quantity(
#         quantity
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     # --------------------------------------------------------
#     # Avoid duplicate cart rows at application level
#     # --------------------------------------------------------

#     existing = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     if existing is not None:

#         raise UserDatabaseConflictError(
#             "This SKU is already present in the cart."
#         )

#     payload = {
#         "user_id": user_id,

#         # products.id = SKU identifier
#         "product_id": sku_id,

#         "quantity": quantity,
#     }

#     try:

#         response = (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .insert(
#                 payload
#             )
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Cart insert failed. "
#             "user_id=%s sku_id=%s error_type=%s",
#             user_id,
#             sku_id,
#             type(exc).__name__,
#         )

#         raise UserDatabaseError(
#             "Unable to add the item to your cart."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if rows:

#         return rows[0]

#     # Verify write from database.

#     created = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     if created is None:

#         raise UserDatabaseError(
#             "Cart update could not be verified."
#         )

#     return created


# # ============================================================
# # CART — UPDATE QUANTITY
# # ============================================================


# def update_cart_item_quantity(
#     *,
#     sku_id: str,
#     quantity: int,
# ) -> dict[str, Any]:
#     """
#     Set absolute quantity for one SKU in current user's cart.

#     Example:

#         old quantity = 2

#         update_cart_item_quantity(
#             sku_id="...",
#             quantity=5
#         )

#     produces quantity=5.

#     It does NOT mean "+5".

#     cart_tools.py will determine desired quantity first.
#     """

#     sku_id = _required_identifier(
#         sku_id,
#         field_name="sku_id",
#     )

#     quantity = _validate_quantity(
#         quantity
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     existing = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     if existing is None:

#         raise UserDatabaseNotFoundError(
#             "This item is not in your cart."
#         )

#     payload = {
#         "quantity": quantity,
#         "updated_at": _utc_iso(),
#     }

#     try:

#         response = (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .update(
#                 payload
#             )
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .eq(
#                 "product_id",
#                 sku_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to update cart quantity."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if rows:

#         return rows[0]

#     updated = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     if updated is None:

#         raise UserDatabaseError(
#             "Updated cart item could not be verified."
#         )

#     return updated


# # ============================================================
# # CART — DELETE SKU
# # ============================================================


# def delete_cart_item(
#     *,
#     sku_id: str,
# ) -> bool:
#     """
#     Remove one SKU from current user's cart.

#     Returns:

#         True  -> item existed and was deleted
#         False -> item was not in this user's cart

#     We never delete using sku_id alone.

#     Query includes:

#         user_id = authenticated user
#         product_id = sku_id
#     """

#     sku_id = _required_identifier(
#         sku_id,
#         field_name="sku_id",
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     existing = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     if existing is None:

#         return False

#     try:

#         (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .delete()
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .eq(
#                 "product_id",
#                 sku_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to remove the item from your cart."
#         ) from exc

#     # --------------------------------------------------------
#     # Verify deletion
#     # --------------------------------------------------------

#     remaining = (
#         get_cart_item_by_sku(
#             sku_id
#         )
#     )

#     return (
#         remaining is None
#     )


# # ============================================================
# # CART — CLEAR
# # ============================================================


# def clear_cart() -> int:
#     """
#     Delete all cart rows belonging to current user.

#     Returns number of rows that existed before deletion.

#     IMPORTANT:

#     No other user's rows are touched.
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     existing = (
#         get_cart_items()
#     )

#     if not existing:

#         return 0

#     count = len(
#         existing
#     )

#     try:

#         (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .delete()
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to clear your cart."
#         ) from exc

#     remaining = (
#         get_cart_items()
#     )

#     if remaining:

#         raise UserDatabaseError(
#             "Cart clearing could not be fully verified."
#         )

#     return count


# # ============================================================
# # CART COUNT
# # ============================================================


# def get_cart_row_count() -> int:
#     """
#     Return number of distinct SKU rows in current user's cart.

#     Example:

#         Milk 1L x 3
#         Bread x 1

#     returns:

#         2 cart rows

#     NOT:

#         4 total units

#     cart_tools.py can calculate total units separately.
#     """

#     return len(
#         get_cart_items()
#     )


# # ============================================================
# # ORDERS — LIST
# # ============================================================


# def list_orders(
#     *,
#     limit: int = 20,
#     offset: int = 0,
#     order_status: str | None = None,
#     payment_status: str | None = None,
# ) -> list[
#     dict[str, Any]
# ]:
#     """
#     Retrieve orders belonging only to current authenticated user.

#     Newest orders are returned first.
#     """

#     if (
#         limit < 1
#         or limit > MAX_ORDER_QUERY_LIMIT
#     ):

#         raise UserDatabaseValidationError(
#             f"limit must be between 1 and "
#             f"{MAX_ORDER_QUERY_LIMIT}."
#         )

#     if offset < 0:

#         raise UserDatabaseValidationError(
#             "offset cannot be negative."
#         )

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         query = (
#             client
#             .table(
#                 ORDERS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#         )

#         if order_status:

#             query = query.eq(
#                 "order_status",
#                 str(
#                     order_status
#                 ).strip(),
#             )

#         if payment_status:

#             query = query.eq(
#                 "payment_status",
#                 str(
#                     payment_status
#                 ).strip(),
#             )

#         response = (
#             query
#             .order(
#                 "created_at",
#                 desc=True,
#             )
#             .range(
#                 offset,
#                 offset + limit - 1,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Order list query failed. "
#             "user_id=%s error_type=%s",
#             user_id,
#             type(exc).__name__,
#         )

#         raise UserDatabaseError(
#             "Unable to retrieve your orders."
#         ) from exc

#     return _response_rows(
#         response
#     )


# # ============================================================
# # ORDER — GET OWN ORDER
# # ============================================================


# def get_order(
#     order_id: str,
#     *,
#     include_items: bool = True,
# ) -> dict[str, Any] | None:
#     """
#     Retrieve one order belonging to current authenticated user.

#     SECURITY
#     --------

#     We first verify:

#         orders.id = requested ID
#         orders.user_id = authenticated user

#     ONLY after ownership is established do we query order_items.

#     This prevents someone from retrieving line items by guessing an
#     order ID.
#     """

#     order_id = (
#         _required_identifier(
#             order_id,
#             field_name="order_id",
#         )
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     # --------------------------------------------------------
#     # Verify order ownership
#     # --------------------------------------------------------

#     try:

#         response = (
#             client
#             .table(
#                 ORDERS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "id",
#                 order_id,
#             )
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .limit(1)
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to retrieve this order."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if not rows:

#         return None

#     order = rows[0]

#     # --------------------------------------------------------
#     # Order only
#     # --------------------------------------------------------

#     if not include_items:

#         return order

#     # --------------------------------------------------------
#     # Now it is safe to retrieve line items because order ownership
#     # has already been proven.
#     # --------------------------------------------------------

#     try:

#         item_response = (
#             client
#             .table(
#                 ORDER_ITEMS_TABLE
#             )
#             .select("*")
#             .eq(
#                 "order_id",
#                 order_id,
#             )
#             .order(
#                 "created_at",
#                 desc=False,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to retrieve order items."
#         ) from exc

#     order[
#         "items"
#     ] = _response_rows(
#         item_response
#     )

#     return order


# # ============================================================
# # ORDER EXISTS
# # ============================================================


# def order_exists(
#     order_id: str,
# ) -> bool:
#     """
#     Return whether order exists for CURRENT user.

#     This does not reveal whether the same ID belongs to somebody else.
#     """

#     return (
#         get_order(
#             order_id,
#             include_items=False,
#         )
#         is not None
#     )


# # ============================================================
# # ORDER COUNT
# # ============================================================


# def get_order_count() -> int:
#     """
#     Return current user's order count.

#     Uses a normal read for broad supabase-py compatibility.

#     We can later optimize this with exact count headers if desired.
#     """

#     client, user_id = (
#         _get_private_context()
#     )

#     try:

#         response = (
#             client
#             .table(
#                 ORDERS_TABLE
#             )
#             .select(
#                 "id"
#             )
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to retrieve order count."
#         ) from exc

#     return len(
#         _response_rows(
#             response
#         )
#     )


# # ============================================================
# # ORDER WRITE PRIMITIVE
# # ============================================================


# def create_order_record(
#     order_data: Mapping[
#         str,
#         Any
#     ],
# ) -> dict[str, Any]:
#     """
#     LOW-LEVEL order-header insertion.

#     This function is intentionally NOT exposed directly to Cohere.

#     order_tools.py will later:

#         verify cart
#         verify SKUs
#         verify stock
#         calculate totals in Python
#         construct safe order payload

#     and only then call this function.

#     SECURITY
#     --------

#     Any user_id supplied inside order_data is ignored.

#     Authenticated user_id is always inserted by this function.
#     """

#     if not isinstance(
#         order_data,
#         Mapping,
#     ):

#         raise UserDatabaseValidationError(
#             "order_data must be a mapping."
#         )

#     client, user_id = (
#         _get_private_context()
#     )

#     payload = dict(
#         order_data
#     )

#     # --------------------------------------------------------
#     # Never trust caller-supplied ownership
#     # --------------------------------------------------------

#     payload.pop(
#         "id",
#         None,
#     )

#     payload.pop(
#         "user_id",
#         None,
#     )

#     payload[
#         "user_id"
#     ] = user_id

#     if not payload:

#         raise UserDatabaseValidationError(
#             "Order payload is empty."
#         )

#     try:

#         response = (
#             client
#             .table(
#                 ORDERS_TABLE
#             )
#             .insert(
#                 payload
#             )
#             .execute()
#         )

#     except Exception as exc:

#         logger.warning(
#             "Order creation failed. "
#             "user_id=%s error_type=%s",
#             user_id,
#             type(exc).__name__,
#         )

#         raise UserDatabaseError(
#             "Unable to create the order."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if not rows:

#         raise UserDatabaseError(
#             "Order creation could not be verified."
#         )

#     order = rows[0]

#     # --------------------------------------------------------
#     # Ownership integrity check
#     # --------------------------------------------------------

#     returned_user_id = (
#         order.get(
#             "user_id"
#         )
#     )

#     if (
#         returned_user_id is not None
#         and str(
#             returned_user_id
#         )
#         != user_id
#     ):

#         logger.critical(
#             "Unexpected order ownership returned from database."
#         )

#         raise UserDatabaseError(
#             "Order ownership validation failed."
#         )

#     return order


# # ============================================================
# # ORDER ITEMS WRITE
# # ============================================================


# def create_order_items(
#     *,
#     order_id: str,
#     items: Sequence[
#         Mapping[str, Any]
#     ],
# ) -> list[
#     dict[str, Any]
# ]:
#     """
#     Insert verified line-item snapshots into an order.

#     SECURITY:

#     Before inserting items we verify that the order belongs to the
#     currently authenticated user.

#     The caller cannot add items to another user's order.


#     IMPORTANT
#     ---------

#     This is a low-level database primitive.

#     Values such as:

#         unit_price
#         total_price
#         product_name

#     must be calculated/verified by order_tools.py BEFORE calling this
#     function.

#     Groq/Cohere must NEVER provide trusted prices directly.
#     """

#     order_id = _required_identifier(
#         order_id,
#         field_name="order_id",
#     )

#     if not isinstance(
#         items,
#         Sequence,
#     ) or isinstance(
#         items,
#         (str, bytes),
#     ):

#         raise UserDatabaseValidationError(
#             "items must be a sequence."
#         )

#     if not items:

#         raise UserDatabaseValidationError(
#             "At least one order item is required."
#         )

#     # --------------------------------------------------------
#     # Verify current user owns the order first
#     # --------------------------------------------------------

#     owned_order = (
#         get_order(
#             order_id,
#             include_items=False,
#         )
#     )

#     if owned_order is None:

#         raise UserDatabaseNotFoundError(
#             "Order could not be found."
#         )

#     client, _ = (
#         _get_private_context()
#     )

#     payloads: list[
#         dict[str, Any]
#     ] = []

#     for index, item in enumerate(
#         items
#     ):

#         if not isinstance(
#             item,
#             Mapping,
#         ):

#             raise UserDatabaseValidationError(
#                 f"Order item {index} is invalid."
#             )

#         payload = dict(
#             item
#         )

#         # Caller cannot redirect an item into another order.
#         payload.pop(
#             "id",
#             None,
#         )

#         payload.pop(
#             "order_id",
#             None,
#         )

#         payload[
#             "order_id"
#         ] = order_id

#         payloads.append(
#             payload
#         )

#     try:

#         response = (
#             client
#             .table(
#                 ORDER_ITEMS_TABLE
#             )
#             .insert(
#                 payloads
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to save order items."
#         ) from exc

#     rows = _response_rows(
#         response
#     )

#     if len(rows) != len(
#         payloads
#     ):

#         logger.warning(
#             "Order-items insertion returned unexpected row count. "
#             "expected=%s returned=%s",
#             len(payloads),
#             len(rows),
#         )

#     return rows


# # ============================================================
# # DELETE UNFINISHED ORDER
# # ============================================================


# def delete_owned_order(
#     order_id: str,
# ) -> bool:
#     """
#     Low-level cleanup primitive.

#     Intended ONLY for compensation when order creation fails midway.

#     Example:

#         order header inserted
#             ↓
#         order item insertion fails
#             ↓
#         order_tools.py
#             ↓
#         delete_owned_order()

#     This is NOT intended as a normal customer "delete order" feature.

#     Ownership is verified before deletion.
#     """

#     order_id = (
#         _required_identifier(
#             order_id,
#             field_name="order_id",
#         )
#     )

#     client, user_id = (
#         _get_private_context()
#     )

#     owned_order = (
#         get_order(
#             order_id,
#             include_items=False,
#         )
#     )

#     if owned_order is None:

#         return False

#     try:

#         (
#             client
#             .table(
#                 ORDERS_TABLE
#             )
#             .delete()
#             .eq(
#                 "id",
#                 order_id,
#             )
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .execute()
#         )

#     except Exception as exc:

#         raise UserDatabaseError(
#             "Unable to clean up incomplete order."
#         ) from exc

#     return (
#         get_order(
#             order_id,
#             include_items=False,
#         )
#         is None
#     )


# # ============================================================
# # USER DATA SUMMARY
# # ============================================================


# def get_user_data_summary() -> dict[
#     str,
#     Any
# ]:
#     """
#     Return small safe authenticated-user data summary.

#     No access/refresh tokens are returned.

#     Useful later for UI/sidebar.

#     Do NOT send this automatically to the LLM unless needed.
#     """

#     _, user_id = (
#         _get_private_context()
#     )

#     profile = (
#         get_current_profile()
#     )

#     return {
#         "user_id": user_id,

#         "email": (
#             profile.get(
#                 "email"
#             )
#             if profile
#             else None
#         ),

#         "full_name": (
#             profile.get(
#                 "full_name"
#             )
#             if profile
#             else None
#         ),

#         "cart_item_count": (
#             get_cart_row_count()
#         ),

#         "order_count": (
#             get_order_count()
#         ),
#     }


# # ============================================================
# # PRIVATE DATABASE HEALTH CHECK
# # ============================================================


# def check_private_user_database() -> bool:
#     """
#     Verify authenticated user can access private tables.

#     This requires an active login session.

#     Do NOT call on every chat request.

#     Intended only for development diagnostics.
#     """

#     try:

#         client, user_id = (
#             _get_private_context()
#         )

#         (
#             client
#             .table(
#                 CART_ITEMS_TABLE
#             )
#             .select("id")
#             .eq(
#                 "user_id",
#                 user_id,
#             )
#             .limit(1)
#             .execute()
#         )

#         return True

#     except Exception:

#         logger.exception(
#             "Private user database check failed."
#         )

#         return False

"""
database/users.py

Private user-data access layer for the Grocery Chatbot.

========================================================================
RESPONSIBILITIES
========================================================================

This module handles authenticated Supabase data belonging to the
current API-authenticated user:

    profiles
    cart_items
    orders
    order_items

It provides trusted database primitives for:

    tools/cart_tools.py
    tools/order_tools.py
    API endpoints / backend services

========================================================================
THIS MODULE DOES NOT
========================================================================

- talk to Cohere
- talk to Groq
- perform RAG
- decide chatbot intent
- calculate cart totals
- select product variants
- validate stock
- generate conversational responses
- accept arbitrary user_id values

Those belong to higher-level layers.

========================================================================
SECURITY MODEL
========================================================================

Frontend
 |
 v
Supabase Auth
 |
 | access_token
 |
 v
Hugging Face API
 |
 v
api_context.py
 |
 | verified access_token
 | verified user_id
 |
 v
create_api_user_client()
 |
 v
Supabase verifies JWT
 |
 v
PostgREST + RLS
 |
 v
auth.uid()
 |
 v
User's own rows


CRITICAL:

Functions in this file NEVER accept:

    user_id="some-user"

from the chatbot.

The verified Supabase access token determines the user.

The frontend never sends an arbitrary trusted user_id to this module.
The API request context derives identity from Supabase authentication.

Even when RLS is enabled, we additionally filter by the verified
user_id where appropriate. This provides defense in depth.

========================================================================
DATABASE MODEL
========================================================================

Existing user-related tables:

profiles
--------
id
email
full_name
phone
role
avatar_url
address
created_at
updated_at


cart_items
----------
id
user_id
product_id
quantity
created_at
updated_at

IMPORTANT:

product_id references a row in products.

Since the existing products table stores SKU rows, product_id is
effectively the SKU ID used by the cart.


orders
------
id
user_id
...
total_amount
payment_status
order_status
created_at
updated_at


order_items
-----------
id
order_id
product_id
...
quantity
unit_price
total_price
created_at

We deliberately use select("*") for orders/order_items because the
commerce schema may contain additional fields such as:

    subtotal
    delivery_fee
    discount_amount
    payment_method
    delivery_address
    notes
    payment_id

and the database remains the source of truth.

========================================================================
IMPORTANT ARCHITECTURAL RULE
========================================================================

This is a DATABASE layer.

For example:

    create_cart_item()

does NOT decide whether stock is sufficient.

The future cart tool does:

    cart_tools.py
        ↓
    get_sku_by_id()
        ↓
    validate stock
        ↓
    calculate desired quantity
        ↓
    database/users.py
        ↓
    write verified quantity

This keeps database access separate from business logic.
"""

from __future__ import annotations

import logging

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence


# ============================================================
# APPLICATION IMPORTS
# ============================================================

from api_context import (
    APIContextError,
    get_api_access_token,
    get_api_user_id,
)

from database.supabase import (
    SupabaseAuthenticationError,
    SupabaseClientError,
    create_api_user_client,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# TABLE NAMES
# ============================================================

PROFILES_TABLE = "profiles"

CART_ITEMS_TABLE = "cart_items"

ORDERS_TABLE = "orders"

ORDER_ITEMS_TABLE = "order_items"


# ============================================================
# LIMITS
# ============================================================

MAX_CART_QUANTITY = 999

MAX_ORDER_QUERY_LIMIT = 100

MAX_PROFILE_TEXT_LENGTH = 5000


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================


class UserDatabaseError(RuntimeError):
    """
    Base exception for authenticated user-data operations.
    """

    pass


class UserDatabaseAuthenticationError(
    UserDatabaseError
):
    """
    Raised when a private database operation is attempted without
    valid authentication.
    """

    pass


class UserDatabaseValidationError(
    UserDatabaseError
):
    """
    Raised when supplied database arguments are invalid.
    """

    pass


class UserDatabaseNotFoundError(
    UserDatabaseError
):
    """
    Raised when an explicitly requested private record does not exist.
    """

    pass


class UserDatabaseConflictError(
    UserDatabaseError
):
    """
    Raised when a write cannot proceed because existing state conflicts
    with the requested operation.
    """

    pass


# ============================================================
# TIME
# ============================================================


def _utc_iso() -> str:
    """
    Return UTC timestamp suitable for database updated_at fields.
    """

    return datetime.now(
        timezone.utc
    ).isoformat()


# ============================================================
# BASIC VALIDATION
# ============================================================


def _required_identifier(
    value: Any,
    *,
    field_name: str,
) -> str:
    """
    Validate IDs without assuming whether the database uses UUID,
    integer-like strings or another identifier representation.

    Supabase itself ultimately validates the DB type.
    """

    if value is None:

        raise UserDatabaseValidationError(
            f"{field_name} is required."
        )

    value = str(
        value
    ).strip()

    if not value:

        raise UserDatabaseValidationError(
            f"{field_name} cannot be empty."
        )

    if len(value) > 200:

        raise UserDatabaseValidationError(
            f"{field_name} is invalid."
        )

    return value


def _validate_quantity(
    quantity: Any,
) -> int:
    """
    Validate cart quantity.

    Cart quantities must always be positive integers.
    """

    # bool is technically an int subclass in Python.
    # We don't want True becoming quantity=1.

    if isinstance(
        quantity,
        bool,
    ):

        raise UserDatabaseValidationError(
            "Cart quantity must be an integer."
        )

    try:

        quantity = int(
            quantity
        )

    except (
        TypeError,
        ValueError,
    ) as exc:

        raise UserDatabaseValidationError(
            "Cart quantity must be an integer."
        ) from exc

    if quantity < 1:

        raise UserDatabaseValidationError(
            "Cart quantity must be at least 1."
        )

    if quantity > MAX_CART_QUANTITY:

        raise UserDatabaseValidationError(
            f"Cart quantity cannot exceed "
            f"{MAX_CART_QUANTITY}."
        )

    return quantity


# ============================================================
# AUTHENTICATED DATABASE CONTEXT
# ============================================================


def _get_private_context():
    """
    Return:

        authenticated Supabase client
        verified user_id

    API-ONLY AUTHENTICATION FLOW
    ----------------------------

    Frontend:
        Supabase login

    Frontend -> Hugging Face API:
        Authorization: Bearer <access_token>

    api_context.py:
        verifies the token with Supabase
        stores request-scoped verified identity

    database/users.py:
        reads the active API request context
        creates a fresh access-token-scoped Supabase client
        verifies the Supabase identity again
        uses PostgREST under that JWT

    Supabase:
        applies RLS / auth.uid()

    IMPORTANT
    ---------

    user_id NEVER comes from:

        chatbot text
        Cohere
        Groq
        request JSON
        query parameters

    It comes only from the verified Supabase access token.

    Defense in depth:
        api_context verified user_id
        MUST equal
        create_api_user_client verified user_id
    """

    # --------------------------------------------------------
    # Read verified request-scoped API identity
    # --------------------------------------------------------

    try:

        access_token = (
            get_api_access_token(
                required=True
            )
        )

        context_user_id = (
            get_api_user_id(
                required=True
            )
        )

    except APIContextError as exc:

        raise UserDatabaseAuthenticationError(
            "You must be signed in to access private data."
        ) from exc

    except Exception as exc:

        logger.warning(
            "Unable to read authenticated API request context. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise UserDatabaseAuthenticationError(
            "Your authentication context could not be verified."
        ) from exc

    if not access_token:

        raise UserDatabaseAuthenticationError(
            "Authenticated access token is unavailable."
        )

    if not context_user_id:

        raise UserDatabaseAuthenticationError(
            "Authenticated user identity is unavailable."
        )

    # --------------------------------------------------------
    # Create a NEW per-request/per-user Supabase client.
    #
    # No refresh token is required by the API backend.
    # No authenticated user client is globally cached.
    # --------------------------------------------------------

    try:

        user_db = (
            create_api_user_client(
                access_token=access_token,
                verify_user=True,
            )
        )

    except SupabaseAuthenticationError as exc:

        logger.warning(
            "Supabase rejected authenticated API user token."
        )

        raise UserDatabaseAuthenticationError(
            "Your authentication session is invalid or has expired."
        ) from exc

    except SupabaseClientError as exc:

        logger.warning(
            "Unable to establish private Supabase database client. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Private account data is temporarily unavailable."
        ) from exc

    except Exception as exc:

        logger.warning(
            "Unexpected private database context failure. "
            "error_type=%s",
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Private account data could not be accessed."
        ) from exc

    verified_user_id = str(
        user_db.user_id
        or ""
    ).strip()

    if not verified_user_id:

        raise UserDatabaseAuthenticationError(
            "Authenticated user identity is unavailable."
        )

    context_user_id = str(
        context_user_id
    ).strip()

    # --------------------------------------------------------
    # Identity integrity check
    # --------------------------------------------------------

    if (
        verified_user_id
        != context_user_id
    ):

        logger.critical(
            "API/Supabase authenticated identity mismatch. "
            "Private database access denied."
        )

        raise UserDatabaseAuthenticationError(
            "Authenticated user identity could not be verified."
        )

    return (
        user_db.client,
        verified_user_id,
    )


# ============================================================
# SAFE RESPONSE DATA
# ============================================================


def _response_rows(
    response: Any,
) -> list[dict[str, Any]]:
    """
    Convert Supabase response.data into list[dict].

    Never return SDK-specific row objects to higher application layers.
    """

    if response is None:

        return []

    data = getattr(
        response,
        "data",
        None,
    )

    if not data:

        return []

    if isinstance(
        data,
        Mapping,
    ):

        return [
            dict(data)
        ]

    if isinstance(
        data,
        Sequence,
    ) and not isinstance(
        data,
        (str, bytes),
    ):

        return [
            dict(row)
            for row in data
            if isinstance(
                row,
                Mapping,
            )
        ]

    return []


# ============================================================
# PROFILE
# ============================================================


def get_current_profile() -> dict[
    str,
    Any
] | None:
    """
    Return current authenticated user's profile.

    This function cannot retrieve another user's profile.

    Example
    -------

    profile = get_current_profile()

    {
        "id": "...",
        "email": "...",
        "full_name": "...",
        ...
    }
    """

    client, user_id = (
        _get_private_context()
    )

    try:

        response = (
            client
            .table(
                PROFILES_TABLE
            )
            .select("*")
            .eq(
                "id",
                user_id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Profile query failed. user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Unable to retrieve your profile."
        ) from exc

    rows = _response_rows(
        response
    )

    if not rows:
        return None

    return rows[0]


# ============================================================
# ENSURE PROFILE
# ============================================================


def ensure_current_profile(
    *,
    email: str | None = None,
    full_name: str | None = None,
) -> dict[str, Any]:
    """
    Ensure the authenticated Supabase user has a profiles row.

    Useful after:

        email signup
        Google OAuth signup

    Ideally a Supabase auth trigger creates profiles automatically.

    However this method provides a safe application-level fallback.

    It NEVER allows callers to set:

        role
        id
        user_id

    to arbitrary values.
    """

    client, user_id = (
        _get_private_context()
    )

    existing = (
        get_current_profile()
    )

    if existing is not None:

        return existing

    payload: dict[
        str,
        Any
    ] = {
        "id": user_id,
    }

    if email:

        payload[
            "email"
        ] = str(
            email
        ).strip().lower()

    if full_name:

        value = " ".join(
            str(
                full_name
            )
            .strip()
            .split()
        )

        if value:

            payload[
                "full_name"
            ] = value

    try:

        response = (
            client
            .table(
                PROFILES_TABLE
            )
            .insert(
                payload
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Profile creation failed. "
            "user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )

        # Race-safe fallback:
        #
        # The profile might have been created by a DB trigger between
        # our check and our insert.

        retry_profile = (
            get_current_profile()
        )

        if retry_profile is not None:
            return retry_profile

        raise UserDatabaseError(
            "Unable to initialize your profile."
        ) from exc

    rows = _response_rows(
        response
    )

    if rows:
        return rows[0]

    # Supabase insert may return no representation depending on
    # configuration. Re-read authoritative row.

    created = (
        get_current_profile()
    )

    if created is None:

        raise UserDatabaseError(
            "Profile creation could not be verified."
        )

    return created


# ============================================================
# UPDATE PROFILE
# ============================================================


def update_current_profile(
    *,
    full_name: str | None = None,
    phone: str | None = None,
    avatar_url: str | None = None,
    address: str | None = None,
) -> dict[str, Any]:
    """
    Update allowed profile information.

    SECURITY:

    Callers CANNOT modify:

        id
        role
        email
        created_at

    through this function.

    Role changes should only happen in controlled admin code.
    """

    client, user_id = (
        _get_private_context()
    )

    payload: dict[
        str,
        Any
    ] = {}

    values = {
        "full_name": full_name,
        "phone": phone,
        "avatar_url": avatar_url,
        "address": address,
    }

    for field_name, value in (
        values.items()
    ):

        if value is None:
            continue

        if not isinstance(
            value,
            str,
        ):

            raise UserDatabaseValidationError(
                f"{field_name} must be text."
            )

        value = value.strip()

        if len(value) > (
            MAX_PROFILE_TEXT_LENGTH
        ):

            raise UserDatabaseValidationError(
                f"{field_name} is too long."
            )

        payload[
            field_name
        ] = (
            value
            if value
            else None
        )

    if not payload:

        existing = (
            get_current_profile()
        )

        if existing is None:

            raise UserDatabaseNotFoundError(
                "Profile does not exist."
            )

        return existing

    payload[
        "updated_at"
    ] = _utc_iso()

    try:

        response = (
            client
            .table(
                PROFILES_TABLE
            )
            .update(
                payload
            )
            .eq(
                "id",
                user_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to update your profile."
        ) from exc

    rows = _response_rows(
        response
    )

    if rows:

        return rows[0]

    result = (
        get_current_profile()
    )

    if result is None:

        raise UserDatabaseNotFoundError(
            "Profile could not be found."
        )

    return result


# ============================================================
# CART — READ ALL
# ============================================================


def get_cart_items() -> list[
    dict[str, Any]
]:
    """
    Return raw cart rows belonging to current authenticated user.

    These rows contain product_id, which points to an exact SKU.

    This database function intentionally does NOT calculate totals or
    fetch product prices.

    tools/cart_tools.py will combine these rows with live SKU data from
    database/products.py.
    """

    client, user_id = (
        _get_private_context()
    )

    try:

        response = (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .select("*")
            .eq(
                "user_id",
                user_id,
            )
            .order(
                "created_at",
                desc=False,
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Cart retrieval failed. "
            "user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Unable to retrieve your cart."
        ) from exc

    return _response_rows(
        response
    )


# ============================================================
# CART — GET ONE SKU
# ============================================================


def get_cart_item_by_sku(
    sku_id: str,
) -> dict[
    str,
    Any
] | None:
    """
    Retrieve current user's cart row for one SKU.

    `product_id` in the existing cart schema refers to the SKU row in
    products.
    """

    sku_id = _required_identifier(
        sku_id,
        field_name="sku_id",
    )

    client, user_id = (
        _get_private_context()
    )

    try:

        response = (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .select("*")
            .eq(
                "user_id",
                user_id,
            )
            .eq(
                "product_id",
                sku_id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to retrieve cart item."
        ) from exc

    rows = _response_rows(
        response
    )

    return (
        rows[0]
        if rows
        else None
    )


# ============================================================
# CART — GET BY CART ITEM ID
# ============================================================


def get_cart_item_by_id(
    cart_item_id: str,
) -> dict[
    str,
    Any
] | None:
    """
    Retrieve one cart row by cart_items.id.

    Defense in depth:

    We require BOTH:

        id = cart_item_id
        user_id = authenticated user
    """

    cart_item_id = (
        _required_identifier(
            cart_item_id,
            field_name="cart_item_id",
        )
    )

    client, user_id = (
        _get_private_context()
    )

    try:

        response = (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .select("*")
            .eq(
                "id",
                cart_item_id,
            )
            .eq(
                "user_id",
                user_id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to retrieve cart item."
        ) from exc

    rows = _response_rows(
        response
    )

    return (
        rows[0]
        if rows
        else None
    )


# ============================================================
# CART — CREATE
# ============================================================


def create_cart_item(
    *,
    sku_id: str,
    quantity: int,
) -> dict[str, Any]:
    """
    Insert one SKU into current user's cart.

    IMPORTANT:

    This is a LOW-LEVEL database primitive.

    It does NOT:

        validate stock
        determine which variant the user meant
        increase an existing cart quantity automatically

    cart_tools.py will perform those business rules BEFORE calling
    this function.

    If the SKU already exists, cart_tools.py should normally call
    update_cart_item_quantity().
    """

    sku_id = _required_identifier(
        sku_id,
        field_name="sku_id",
    )

    quantity = _validate_quantity(
        quantity
    )

    client, user_id = (
        _get_private_context()
    )

    # --------------------------------------------------------
    # Avoid duplicate cart rows at application level
    # --------------------------------------------------------

    existing = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    if existing is not None:

        raise UserDatabaseConflictError(
            "This SKU is already present in the cart."
        )

    payload = {
        "user_id": user_id,

        # products.id = SKU identifier
        "product_id": sku_id,

        "quantity": quantity,
    }

    try:

        response = (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .insert(
                payload
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Cart insert failed. "
            "user_id=%s sku_id=%s error_type=%s",
            user_id,
            sku_id,
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Unable to add the item to your cart."
        ) from exc

    rows = _response_rows(
        response
    )

    if rows:

        return rows[0]

    # Verify write from database.

    created = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    if created is None:

        raise UserDatabaseError(
            "Cart update could not be verified."
        )

    return created


# ============================================================
# CART — UPDATE QUANTITY
# ============================================================


def update_cart_item_quantity(
    *,
    sku_id: str,
    quantity: int,
) -> dict[str, Any]:
    """
    Set absolute quantity for one SKU in current user's cart.

    Example:

        old quantity = 2

        update_cart_item_quantity(
            sku_id="...",
            quantity=5
        )

    produces quantity=5.

    It does NOT mean "+5".

    cart_tools.py will determine desired quantity first.
    """

    sku_id = _required_identifier(
        sku_id,
        field_name="sku_id",
    )

    quantity = _validate_quantity(
        quantity
    )

    client, user_id = (
        _get_private_context()
    )

    existing = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    if existing is None:

        raise UserDatabaseNotFoundError(
            "This item is not in your cart."
        )

    payload = {
        "quantity": quantity,
        "updated_at": _utc_iso(),
    }

    try:

        response = (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .update(
                payload
            )
            .eq(
                "user_id",
                user_id,
            )
            .eq(
                "product_id",
                sku_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to update cart quantity."
        ) from exc

    rows = _response_rows(
        response
    )

    if rows:

        return rows[0]

    updated = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    if updated is None:

        raise UserDatabaseError(
            "Updated cart item could not be verified."
        )

    return updated


# ============================================================
# CART — DELETE SKU
# ============================================================


def delete_cart_item(
    *,
    sku_id: str,
) -> bool:
    """
    Remove one SKU from current user's cart.

    Returns:

        True  -> item existed and was deleted
        False -> item was not in this user's cart

    We never delete using sku_id alone.

    Query includes:

        user_id = authenticated user
        product_id = sku_id
    """

    sku_id = _required_identifier(
        sku_id,
        field_name="sku_id",
    )

    client, user_id = (
        _get_private_context()
    )

    existing = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    if existing is None:

        return False

    try:

        (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .delete()
            .eq(
                "user_id",
                user_id,
            )
            .eq(
                "product_id",
                sku_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to remove the item from your cart."
        ) from exc

    # --------------------------------------------------------
    # Verify deletion
    # --------------------------------------------------------

    remaining = (
        get_cart_item_by_sku(
            sku_id
        )
    )

    return (
        remaining is None
    )


# ============================================================
# CART — CLEAR
# ============================================================


def clear_cart() -> int:
    """
    Delete all cart rows belonging to current user.

    Returns number of rows that existed before deletion.

    IMPORTANT:

    No other user's rows are touched.
    """

    client, user_id = (
        _get_private_context()
    )

    existing = (
        get_cart_items()
    )

    if not existing:

        return 0

    count = len(
        existing
    )

    try:

        (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .delete()
            .eq(
                "user_id",
                user_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to clear your cart."
        ) from exc

    remaining = (
        get_cart_items()
    )

    if remaining:

        raise UserDatabaseError(
            "Cart clearing could not be fully verified."
        )

    return count


# ============================================================
# CART COUNT
# ============================================================


def get_cart_row_count() -> int:
    """
    Return number of distinct SKU rows in current user's cart.

    Example:

        Milk 1L x 3
        Bread x 1

    returns:

        2 cart rows

    NOT:

        4 total units

    cart_tools.py can calculate total units separately.
    """

    return len(
        get_cart_items()
    )


# ============================================================
# ORDERS — LIST
# ============================================================


def list_orders(
    *,
    limit: int = 20,
    offset: int = 0,
    order_status: str | None = None,
    payment_status: str | None = None,
) -> list[
    dict[str, Any]
]:
    """
    Retrieve orders belonging only to current authenticated user.

    Newest orders are returned first.
    """

    if (
        limit < 1
        or limit > MAX_ORDER_QUERY_LIMIT
    ):

        raise UserDatabaseValidationError(
            f"limit must be between 1 and "
            f"{MAX_ORDER_QUERY_LIMIT}."
        )

    if offset < 0:

        raise UserDatabaseValidationError(
            "offset cannot be negative."
        )

    client, user_id = (
        _get_private_context()
    )

    try:

        query = (
            client
            .table(
                ORDERS_TABLE
            )
            .select("*")
            .eq(
                "user_id",
                user_id,
            )
        )

        if order_status:

            query = query.eq(
                "order_status",
                str(
                    order_status
                ).strip(),
            )

        if payment_status:

            query = query.eq(
                "payment_status",
                str(
                    payment_status
                ).strip(),
            )

        response = (
            query
            .order(
                "created_at",
                desc=True,
            )
            .range(
                offset,
                offset + limit - 1,
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Order list query failed. "
            "user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Unable to retrieve your orders."
        ) from exc

    return _response_rows(
        response
    )


# ============================================================
# ORDER — GET OWN ORDER
# ============================================================


def get_order(
    order_id: str,
    *,
    include_items: bool = True,
) -> dict[str, Any] | None:
    """
    Retrieve one order belonging to current authenticated user.

    SECURITY
    --------

    We first verify:

        orders.id = requested ID
        orders.user_id = authenticated user

    ONLY after ownership is established do we query order_items.

    This prevents someone from retrieving line items by guessing an
    order ID.
    """

    order_id = (
        _required_identifier(
            order_id,
            field_name="order_id",
        )
    )

    client, user_id = (
        _get_private_context()
    )

    # --------------------------------------------------------
    # Verify order ownership
    # --------------------------------------------------------

    try:

        response = (
            client
            .table(
                ORDERS_TABLE
            )
            .select("*")
            .eq(
                "id",
                order_id,
            )
            .eq(
                "user_id",
                user_id,
            )
            .limit(1)
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to retrieve this order."
        ) from exc

    rows = _response_rows(
        response
    )

    if not rows:

        return None

    order = rows[0]

    # --------------------------------------------------------
    # Order only
    # --------------------------------------------------------

    if not include_items:

        return order

    # --------------------------------------------------------
    # Now it is safe to retrieve line items because order ownership
    # has already been proven.
    # --------------------------------------------------------

    try:

        item_response = (
            client
            .table(
                ORDER_ITEMS_TABLE
            )
            .select("*")
            .eq(
                "order_id",
                order_id,
            )
            .order(
                "created_at",
                desc=False,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to retrieve order items."
        ) from exc

    order[
        "items"
    ] = _response_rows(
        item_response
    )

    return order


# ============================================================
# ORDER EXISTS
# ============================================================


def order_exists(
    order_id: str,
) -> bool:
    """
    Return whether order exists for CURRENT user.

    This does not reveal whether the same ID belongs to somebody else.
    """

    return (
        get_order(
            order_id,
            include_items=False,
        )
        is not None
    )


# ============================================================
# ORDER COUNT
# ============================================================


def get_order_count() -> int:
    """
    Return current user's order count.

    Uses a normal read for broad supabase-py compatibility.

    We can later optimize this with exact count headers if desired.
    """

    client, user_id = (
        _get_private_context()
    )

    try:

        response = (
            client
            .table(
                ORDERS_TABLE
            )
            .select(
                "id"
            )
            .eq(
                "user_id",
                user_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to retrieve order count."
        ) from exc

    return len(
        _response_rows(
            response
        )
    )


# ============================================================
# ORDER WRITE PRIMITIVE
# ============================================================


def create_order_record(
    order_data: Mapping[
        str,
        Any
    ],
) -> dict[str, Any]:
    """
    LOW-LEVEL order-header insertion.

    This function is intentionally NOT exposed directly to Cohere.

    order_tools.py will later:

        verify cart
        verify SKUs
        verify stock
        calculate totals in Python
        construct safe order payload

    and only then call this function.

    SECURITY
    --------

    Any user_id supplied inside order_data is ignored.

    Authenticated user_id is always inserted by this function.
    """

    if not isinstance(
        order_data,
        Mapping,
    ):

        raise UserDatabaseValidationError(
            "order_data must be a mapping."
        )

    client, user_id = (
        _get_private_context()
    )

    payload = dict(
        order_data
    )

    # --------------------------------------------------------
    # Never trust caller-supplied ownership
    # --------------------------------------------------------

    payload.pop(
        "id",
        None,
    )

    payload.pop(
        "user_id",
        None,
    )

    payload[
        "user_id"
    ] = user_id

    if not payload:

        raise UserDatabaseValidationError(
            "Order payload is empty."
        )

    try:

        response = (
            client
            .table(
                ORDERS_TABLE
            )
            .insert(
                payload
            )
            .execute()
        )

    except Exception as exc:

        logger.warning(
            "Order creation failed. "
            "user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )

        raise UserDatabaseError(
            "Unable to create the order."
        ) from exc

    rows = _response_rows(
        response
    )

    if not rows:

        raise UserDatabaseError(
            "Order creation could not be verified."
        )

    order = rows[0]

    # --------------------------------------------------------
    # Ownership integrity check
    # --------------------------------------------------------

    returned_user_id = (
        order.get(
            "user_id"
        )
    )

    if (
        returned_user_id is not None
        and str(
            returned_user_id
        )
        != user_id
    ):

        logger.critical(
            "Unexpected order ownership returned from database."
        )

        raise UserDatabaseError(
            "Order ownership validation failed."
        )

    return order


# ============================================================
# ORDER ITEMS WRITE
# ============================================================


def create_order_items(
    *,
    order_id: str,
    items: Sequence[
        Mapping[str, Any]
    ],
) -> list[
    dict[str, Any]
]:
    """
    Insert verified line-item snapshots into an order.

    SECURITY:

    Before inserting items we verify that the order belongs to the
    currently authenticated user.

    The caller cannot add items to another user's order.


    IMPORTANT
    ---------

    This is a low-level database primitive.

    Values such as:

        unit_price
        total_price
        product_name

    must be calculated/verified by order_tools.py BEFORE calling this
    function.

    Groq/Cohere must NEVER provide trusted prices directly.
    """

    order_id = _required_identifier(
        order_id,
        field_name="order_id",
    )

    if not isinstance(
        items,
        Sequence,
    ) or isinstance(
        items,
        (str, bytes),
    ):

        raise UserDatabaseValidationError(
            "items must be a sequence."
        )

    if not items:

        raise UserDatabaseValidationError(
            "At least one order item is required."
        )

    # --------------------------------------------------------
    # Verify current user owns the order first
    # --------------------------------------------------------

    owned_order = (
        get_order(
            order_id,
            include_items=False,
        )
    )

    if owned_order is None:

        raise UserDatabaseNotFoundError(
            "Order could not be found."
        )

    client, _ = (
        _get_private_context()
    )

    payloads: list[
        dict[str, Any]
    ] = []

    for index, item in enumerate(
        items
    ):

        if not isinstance(
            item,
            Mapping,
        ):

            raise UserDatabaseValidationError(
                f"Order item {index} is invalid."
            )

        payload = dict(
            item
        )

        # Caller cannot redirect an item into another order.
        payload.pop(
            "id",
            None,
        )

        payload.pop(
            "order_id",
            None,
        )

        payload[
            "order_id"
        ] = order_id

        payloads.append(
            payload
        )

    try:

        response = (
            client
            .table(
                ORDER_ITEMS_TABLE
            )
            .insert(
                payloads
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to save order items."
        ) from exc

    rows = _response_rows(
        response
    )

    if len(rows) != len(
        payloads
    ):

        logger.warning(
            "Order-items insertion returned unexpected row count. "
            "expected=%s returned=%s",
            len(payloads),
            len(rows),
        )

    return rows


# ============================================================
# DELETE UNFINISHED ORDER
# ============================================================


def delete_owned_order(
    order_id: str,
) -> bool:
    """
    Low-level cleanup primitive.

    Intended ONLY for compensation when order creation fails midway.

    Example:

        order header inserted
            ↓
        order item insertion fails
            ↓
        order_tools.py
            ↓
        delete_owned_order()

    This is NOT intended as a normal customer "delete order" feature.

    Ownership is verified before deletion.
    """

    order_id = (
        _required_identifier(
            order_id,
            field_name="order_id",
        )
    )

    client, user_id = (
        _get_private_context()
    )

    owned_order = (
        get_order(
            order_id,
            include_items=False,
        )
    )

    if owned_order is None:

        return False

    try:

        (
            client
            .table(
                ORDERS_TABLE
            )
            .delete()
            .eq(
                "id",
                order_id,
            )
            .eq(
                "user_id",
                user_id,
            )
            .execute()
        )

    except Exception as exc:

        raise UserDatabaseError(
            "Unable to clean up incomplete order."
        ) from exc

    return (
        get_order(
            order_id,
            include_items=False,
        )
        is None
    )


# ============================================================
# USER DATA SUMMARY
# ============================================================


def get_user_data_summary() -> dict[
    str,
    Any
]:
    """
    Return small safe authenticated-user data summary.

    No access/refresh tokens are returned.

    Useful later for UI/sidebar.

    Do NOT send this automatically to the LLM unless needed.
    """

    _, user_id = (
        _get_private_context()
    )

    profile = (
        get_current_profile()
    )

    return {
        "user_id": user_id,

        "email": (
            profile.get(
                "email"
            )
            if profile
            else None
        ),

        "full_name": (
            profile.get(
                "full_name"
            )
            if profile
            else None
        ),

        "cart_item_count": (
            get_cart_row_count()
        ),

        "order_count": (
            get_order_count()
        ),
    }


# ============================================================
# PRIVATE DATABASE HEALTH CHECK
# ============================================================


def check_private_user_database() -> bool:
    """
    Verify authenticated user can access private tables.

    This requires an active authenticated API request context.

    Do NOT call on every chat request.

    Intended only for development diagnostics.
    """

    try:

        client, user_id = (
            _get_private_context()
        )

        (
            client
            .table(
                CART_ITEMS_TABLE
            )
            .select("id")
            .eq(
                "user_id",
                user_id,
            )
            .limit(1)
            .execute()
        )

        return True

    except Exception:

        logger.exception(
            "Private user database check failed."
        )

        return False