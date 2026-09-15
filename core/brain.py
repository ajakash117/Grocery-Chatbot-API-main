# """
# core/brain.py

# Central orchestration layer for Grocery Chatbot.

# FLOW
# ----

# User
#     ↓
# Cohere
#     ↓
# Automatically decides:
#     - no tool
#     - product search
#     - semantic RAG
#     - cart
#     - order
#     ↓
# Python executes approved tool
#     ↓
# Verified Supabase / RAG data
#     ↓
# Groq
#     ↓
# Final response


# IMPORTANT
# ---------

# There is NO manually maintained natural-language intent classifier.

# Do NOT add:

#     if "milk" in user_message
#     if "cart" in user_message
#     if "healthy" in user_message

# Cohere decides whether tools are needed.

# Python only:

# - validates tool calls
# - executes approved tools
# - prevents loops
# - prevents duplicate/redundant execution
# - compacts verified results
# - provides a deterministic fallback if Groq is unavailable

# UPDATED RESPONSE ROUTING
# ------------------------

# Tool-backed requests now use a dedicated response path:

#     Cohere
#         ↓
#     product / cart / order / RAG tool
#         ↓
#     verified backend data
#         ↓
#     core.database_responder
#         ↓
#     GROQ_API_KEY2 only (Groq API slot 3)
#         ↓
#     prompts/rules.txt
#         ↓
#     conversational response + structured ResponsePayload

# General no-tool conversation continues through the existing normal
# Groq response path.

# All existing helper functions below are intentionally retained for
# compatibility and fallback behavior.
# """

# from __future__ import annotations

# import inspect
# import json
# import logging

# from dataclasses import (
#     dataclass,
#     field,
# )

# from typing import (
#     Any,
#     Mapping,
#     Sequence,
# )


# # ============================================================
# # COHERE
# # ============================================================

# from core.cohere_client import (
#     CohereBrainResponse,
#     CohereClientError,
#     CohereToolCall,
#     build_messages,
#     chat_with_messages,
# )


# # ============================================================
# # GROQ
# # ============================================================

# from core.groq_client import (
#     GroqClientError,
#     generate_final_text,
# )


# # ============================================================
# # DATABASE RESPONSE ENGINE
# # ============================================================

# from core.database_responder import (
#     respond_to_database_query,
# )


# # ============================================================
# # STRUCTURED RESPONSE MODEL
# # ============================================================

# from core.response_models import (
#     ResponsePayload,
# )


# # ============================================================
# # PRODUCT TOOLS
# # ============================================================

# from tools.product_tools import (
#     execute_product_tool,
#     get_product_tool_names,
#     get_product_tool_schemas,
# )


# # ============================================================
# # CART TOOLS
# # ============================================================

# from tools.cart_tools import (
#     execute_cart_tool,
#     get_cart_tool_names,
#     get_cart_tool_schemas,
# )


# # ============================================================
# # ORDER TOOLS
# # ============================================================

# from tools.order_tools import (
#     execute_order_tool,
#     get_order_tool_names,
#     get_order_tool_schemas,
# )


# # ============================================================
# # RAG
# # ============================================================

# from rag.rag import (
#     execute_rag_tool,
#     get_rag_tool_names,
#     get_rag_tool_schemas,
# )


# # ============================================================
# # SESSION
# # ============================================================

# from auth.session import (
#     add_chat_message,
#     get_conversation_context,
#     get_last_product_context,
#     get_model_chat_history,
#     get_pending_action,
#     initialize_session,
#     set_conversation_context,
#     set_last_product_context,
# )


# # ============================================================
# # LOGGER
# # ============================================================

# logger = logging.getLogger(
#     __name__
# )


# # ============================================================
# # LIMITS
# # ============================================================

# # Enough for:
# #
# # search
# #   ↓
# # resolve SKU
# #   ↓
# # cart action
# #
# # But prevents Cohere from wandering indefinitely.

# MAX_TOOL_ROUNDS = 3

# MAX_TOOL_CALLS_PER_ROUND = 4

# MAX_TOTAL_TOOL_CALLS = 8


# # Context sent to Cohere.
# MAX_CONTEXT_JSON_LENGTH = 4_000

# MAX_COHERE_TEXT_LENGTH = 1_200

# MAX_USER_MESSAGE_LENGTH = 20_000


# # ============================================================
# # GROQ CONTEXT LIMITS
# # ============================================================

# MAX_GROQ_RESULTS = 4

# MAX_PRODUCTS_FOR_GROQ = 5

# MAX_VARIANTS_FOR_GROQ = 5

# MAX_ALTERNATIVES_FOR_GROQ = 4

# MAX_GENERIC_LIST_FOR_GROQ = 6

# MAX_STRING_FOR_GROQ = 500


# # ============================================================
# # PER-TOOL EXECUTION BUDGET
# # ============================================================
# #
# # This is NOT intent classification.
# #
# # Cohere already selected the tool.
# #
# # These limits only protect the backend from repeated calls.
# # ============================================================

# TOOL_CALL_BUDGETS: dict[
#     str,
#     int,
# ] = {

#     # Direct product search can run twice because:
#     #
#     # "show milk and bread"
#     #
#     # may legitimately require two searches.
#     "search_products":
#         2,

#     # Semantic search already performs retrieval + live hydration.
#     "semantic_product_search":
#         1,

#     "get_catalog_stats":
#         1,

#     "list_catalog_brands":
#         1,

#     "list_catalog_categories":
#         1,

#     "get_cart":
#         1,

#     "get_cart_summary":
#         1,

#     "get_my_orders":
#         1,

#     "get_latest_order":
#         1,

#     "get_order_count":
#         1,

#     "get_order_history_summary":
#         1,

#     "get_order_status":
#         1,

#     "clear_cart":
#         1,
# }


# # ============================================================
# # REDUNDANT DISCOVERY TOOLS
# # ============================================================

# CATALOG_METADATA_TOOLS = {
#     "get_catalog_stats",
#     "list_catalog_brands",
#     "list_catalog_categories",
# }


# DISCOVERY_TOOLS = {
#     "search_products",
#     "semantic_product_search",
# }


# # ============================================================
# # COHERE GUIDANCE
# # ============================================================

# ROUTING_GUIDANCE = """
# TOOL SELECTION RULES
# ====================

# You are the reasoning and tool-selection brain of a grocery shopping
# assistant.

# You do NOT need to use a tool for every request.


# GENERAL CONVERSATION
# --------------------

# Do NOT use tools when current store/user data is unnecessary.

# Examples:

# "hi"
#     -> NO TOOL

# "what is protein?"
#     -> NO TOOL

# "what are carbohydrates?"
#     -> NO TOOL

# "what is paneer?"
#     -> NO TOOL

# "give me a simple breakfast recipe"
#     -> NO TOOL

# "how do I make tea?"
#     -> NO TOOL


# DIRECT CATALOG SEARCH
# ---------------------

# Use search_products when the user asks whether the STORE carries,
# contains or sells a named product.

# Examples:

# "do you have milk?"
#     -> search_products(
#            query="milk",
#            stock_state="any"
#        )

# "show milk"
#     -> search_products(
#            query="milk",
#            stock_state="any"
#        )

# "I need Amul milk"
#     -> search_products(
#            query="Amul milk",
#            stock_state="any"
#        )

# "show oats under 200"
#     -> search_products(
#            query="oats",
#            max_price=200,
#            stock_state="any"
#        )


# IMPORTANT STOCK RULE
# --------------------

# For normal catalog search:

#     stock_state="any"

# Out-of-stock products still exist in the catalog and MUST be returned.

# Example:

# Milk exists with stock=0.

# Correct:
#     "Milk exists but is currently out of stock."

# Incorrect:
#     "We don't have milk."


# Only use:

#     stock_state="in_stock"

# when the user explicitly asks for only currently available/purchasable
# products.


# CHEAPEST / MOST EXPENSIVE PRODUCT
# ---------------------------------

# For the cheapest currently purchasable product:

#     search_products(
#         stock_state="in_stock",
#         sort_by="price",
#         sort_order="asc",
#         limit=1
#     )

# For the most expensive currently purchasable product:

#     search_products(
#         stock_state="in_stock",
#         sort_by="price",
#         sort_order="desc",
#         limit=1
#     )

# A query string is NOT required for catalog-wide ranking.


# SEMANTIC PRODUCT RECOMMENDATIONS
# --------------------------------

# Use semantic_product_search when the user wants a PRODUCT according to
# purpose, meal context, suitability or meaning.

# Examples:

# "I need something good for breakfast"
#     -> semantic_product_search

# "recommend a healthy snack"
#     -> semantic_product_search

# "something for tea time"
#     -> semantic_product_search

# "something for smoothies"
#     -> semantic_product_search

# "something useful for baking"
#     -> semantic_product_search

# "party snacks under 100"
#     -> semantic_product_search(max_price=100)


# Semantic recommendations the user intends to buy should normally use:

#     in_stock_only=true


# GENERAL IDEA VS PRODUCT RECOMMENDATION
# --------------------------------------

# "give me breakfast ideas"
#     -> NO TOOL

# "I need something good for breakfast"
#     -> semantic_product_search

# "give me a breakfast product"
#     -> semantic_product_search


# DIRECT NAME VS SEMANTIC
# -----------------------

# "show milk"
#     -> search_products

# "something I can drink for breakfast"
#     -> semantic_product_search


# CART
# ----

# "what is in my cart?"
#     -> get_cart

# "cart total"
#     -> get_cart_summary

# "clear my cart"
#     -> clear_cart


# CART MUTATION
# -------------

# Cart mutations require an exact SKU.

# If the user says:

# "add milk"

# and multiple variants exist:

#     DO NOT guess.

# Search/resolve the product and ask the user which size if necessary.

# If the exact SKU is known:

#     use add_to_cart.


# ORDERS
# ------

# "show my orders"
#     -> get_my_orders

# "latest order"
#     -> get_latest_order(
#            include_items=true
#        )

# "what was my last order?"
#     -> get_latest_order(
#            include_items=true
#        )

# "what did I order last time?"
#     -> get_latest_order(
#            include_items=true
#        )

# "latest order status"
#     -> get_latest_order(
#            include_items=false
#        )

# "how many orders do I have?"
#     -> get_order_count


# EFFICIENCY
# ----------

# One successful authoritative product search is normally enough.

# After search_products successfully returns the requested product:

# DO NOT call:

#     get_catalog_stats
#     list_catalog_categories
#     list_catalog_brands

# just to verify the result.


# semantic_product_search already:

#     searches product_descriptions
#     +
#     hydrates real products
#     +
#     returns live SKU/price/stock

# Therefore do NOT call search_products afterward merely to re-check the
# same recommendation.


# STOPPING
# --------

# Once enough backend data exists to answer the request:

#     STOP CALLING TOOLS.

# Groq will create the final wording.


# TRUTH
# -----

# Never invent:

#     price
#     stock
#     SKU
#     variant
#     cart contents
#     cart total
#     order information
#     order status

# Those must come from backend tools.
# """.strip()


# # ============================================================
# # EXCEPTIONS
# # ============================================================


# class BrainError(
#     RuntimeError
# ):
#     pass


# class BrainValidationError(
#     BrainError
# ):
#     pass


# class BrainToolError(
#     BrainError
# ):
#     pass


# # ============================================================
# # TOOL RECORD
# # ============================================================


# @dataclass(
#     slots=True
# )
# class ToolExecutionRecord:

#     tool_call_id: str

#     tool_name: str

#     arguments: dict[
#         str,
#         Any,
#     ]

#     result: dict[
#         str,
#         Any,
#     ]

#     round_number: int

#     @property
#     def success(
#         self,
#     ) -> bool:

#         return bool(
#             self.result.get(
#                 "success"
#             )
#         )

#     def safe_dict(
#         self,
#     ) -> dict[
#         str,
#         Any,
#     ]:

#         return {
#             "tool_name":
#                 self.tool_name,

#             "arguments":
#                 self.arguments,

#             "success":
#                 self.success,

#             "result":
#                 self.result,
#         }


# # ============================================================
# # BRAIN RESPONSE
# # ============================================================


# @dataclass(
#     slots=True
# )
# class BrainResponse:

#     text: str

#     success: bool = True

#     tool_executions: list[
#         ToolExecutionRecord
#     ] = field(
#         default_factory=list
#     )

#     cohere_text: str | None = None

#     rounds: int = 0

#     error_code: str | None = None

#     # Structured verified response used by app.py for:
#     #
#     # - product images
#     # - SKU / variant cards
#     # - cart cards
#     # - order cards
#     #
#     # Generated response text and structured commerce data remain separate.
#     response_payload: ResponsePayload | None = None

#     @property
#     def used_tools(
#         self,
#     ) -> bool:

#         return bool(
#             self.tool_executions
#         )

#     @property
#     def tool_names(
#         self,
#     ) -> list[str]:

#         return list(
#             dict.fromkeys(
#                 execution.tool_name
#                 for execution
#                 in self.tool_executions
#             )
#         )

#     @property
#     def has_structured_response(
#         self,
#     ) -> bool:
#         """
#         True when a database-backed request produced UI-renderable
#         structured content.
#         """

#         return (
#             self.response_payload is not None
#             and self.response_payload.has_structured_content()
#         )


# # ============================================================
# # VALIDATE USER MESSAGE
# # ============================================================


# def _validate_user_message(
#     user_message: Any,
# ) -> str:

#     if not isinstance(
#         user_message,
#         str,
#     ):

#         raise BrainValidationError(
#             "User message must be text."
#         )

#     user_message = (
#         user_message.strip()
#     )

#     if not user_message:

#         raise BrainValidationError(
#             "User message cannot be empty."
#         )

#     if (
#         len(
#             user_message
#         )
#         > MAX_USER_MESSAGE_LENGTH
#     ):

#         raise BrainValidationError(
#             "User message is too long."
#         )

#     return user_message


# # ============================================================
# # TOOL DEFINITIONS
# # ============================================================


# def get_all_tool_schemas() -> list[
#     dict[
#         str,
#         Any,
#     ]
# ]:

#     return (
#         list(
#             get_product_tool_schemas()
#         )
#         +
#         list(
#             get_cart_tool_schemas()
#         )
#         +
#         list(
#             get_order_tool_schemas()
#         )
#         +
#         list(
#             get_rag_tool_schemas()
#         )
#     )


# def get_all_tool_names() -> set[str]:

#     return (
#         set(
#             get_product_tool_names()
#         )
#         |
#         set(
#             get_cart_tool_names()
#         )
#         |
#         set(
#             get_order_tool_names()
#         )
#         |
#         set(
#             get_rag_tool_names()
#         )
#     )


# # ============================================================
# # EXECUTE APPROVED TOOL
# # ============================================================


# def execute_tool(
#     *,
#     tool_name: str,
#     arguments: Mapping[
#         str,
#         Any,
#     ] | None,
# ) -> dict[
#     str,
#     Any,
# ]:

#     tool_name = (
#         str(
#             tool_name
#             or ""
#         )
#         .strip()
#     )

#     if not tool_name:

#         raise BrainToolError(
#             "Tool name cannot be empty."
#         )

#     if (
#         tool_name
#         in get_product_tool_names()
#     ):

#         return execute_product_tool(
#             tool_name=tool_name,
#             arguments=arguments,
#         )

#     if (
#         tool_name
#         in get_cart_tool_names()
#     ):

#         return execute_cart_tool(
#             tool_name=tool_name,
#             arguments=arguments,
#         )

#     if (
#         tool_name
#         in get_order_tool_names()
#     ):

#         return execute_order_tool(
#             tool_name=tool_name,
#             arguments=arguments,
#         )

#     if (
#         tool_name
#         in get_rag_tool_names()
#     ):

#         return execute_rag_tool(
#             tool_name=tool_name,
#             arguments=arguments,
#         )

#     raise BrainToolError(
#         f"Unsupported backend tool: {tool_name}"
#     )


# # ============================================================
# # JSON
# # ============================================================


# def _json_default(
#     value: Any,
# ) -> Any:

#     if hasattr(
#         value,
#         "model_dump",
#     ):

#         try:

#             return value.model_dump()

#         except Exception:

#             pass

#     if hasattr(
#         value,
#         "__dict__",
#     ):

#         try:

#             return dict(
#                 value.__dict__
#             )

#         except Exception:

#             pass

#     return str(
#         value
#     )


# def _json_dumps(
#     value: Any,
# ) -> str:

#     return json.dumps(
#         value,
#         ensure_ascii=False,
#         default=_json_default,
#         separators=(
#             ",",
#             ":",
#         ),
#     )


# # ============================================================
# # SAFE CONTEXT
# # ============================================================


# def _safe_context_json(
#     value: Any,
# ) -> str:

#     if value is None:

#         return "null"

#     try:

#         text = json.dumps(
#             value,
#             ensure_ascii=False,
#             default=_json_default,
#             separators=(
#                 ",",
#                 ":",
#             ),
#         )

#     except Exception:

#         return "{}"

#     if (
#         len(
#             text
#         )
#         > MAX_CONTEXT_JSON_LENGTH
#     ):

#         return (
#             text[
#                 :MAX_CONTEXT_JSON_LENGTH
#             ]
#             + "..."
#         )

#     return text


# # ============================================================
# # RUNTIME CONTEXT
# # ============================================================


# def _build_runtime_system_context() -> str:

#     try:

#         conversation_context = (
#             get_conversation_context()
#         )

#     except Exception:

#         conversation_context = None

#     try:

#         product_context = (
#             get_last_product_context()
#         )

#     except Exception:

#         product_context = None

#     try:

#         pending_action = (
#             get_pending_action()
#         )

#     except Exception:

#         pending_action = None

#     return f"""
# CURRENT APPLICATION CONTEXT
# ===========================

# Conversation:
# {_safe_context_json(conversation_context)}

# Recent product context:
# {_safe_context_json(product_context)}

# Pending unresolved action:
# {_safe_context_json(pending_action)}

# {ROUTING_GUIDANCE}
# """.strip()


# # ============================================================
# # TOOL CALL FINGERPRINT
# # ============================================================


# def _tool_call_fingerprint(
#     *,
#     tool_name: str,
#     arguments: Mapping[
#         str,
#         Any,
#     ],
# ) -> str:

#     try:

#         args = json.dumps(
#             arguments,
#             sort_keys=True,
#             ensure_ascii=False,
#             default=_json_default,
#             separators=(
#                 ",",
#                 ":",
#             ),
#         )

#     except Exception:

#         args = str(
#             arguments
#         )

#     return (
#         f"{tool_name}:{args}"
#     )


# # ============================================================
# # TOOL ARGUMENTS
# # ============================================================


# def _tool_arguments(
#     call: CohereToolCall,
# ) -> dict[
#     str,
#     Any,
# ]:

#     arguments = (
#         call.arguments
#     )

#     if arguments is None:

#         return {}

#     if not isinstance(
#         arguments,
#         Mapping,
#     ):

#         raise BrainToolError(
#             "Cohere returned invalid tool arguments."
#         )

#     return dict(
#         arguments
#     )


# # ============================================================
# # COHERE ASSISTANT TOOL MESSAGE
# # ============================================================


# def _assistant_tool_message(
#     response: CohereBrainResponse,
# ) -> dict[
#     str,
#     Any,
# ]:

#     tool_calls: list[
#         dict[
#             str,
#             Any,
#         ]
#     ] = []

#     for call in (
#         response.tool_calls
#         or []
#     ):

#         arguments = (
#             call.arguments
#             if isinstance(
#                 call.arguments,
#                 Mapping,
#             )
#             else {}
#         )

#         tool_calls.append(
#             {
#                 "id":
#                     str(
#                         call.id
#                         or ""
#                     ),

#                 "type":
#                     str(
#                         call.type
#                         or "function"
#                     ),

#                 "function": {
#                     "name":
#                         str(
#                             call.name
#                             or ""
#                         ),

#                     "arguments":
#                         _json_dumps(
#                             dict(
#                                 arguments
#                             )
#                         ),
#                 },
#             }
#         )

#     message: dict[
#         str,
#         Any,
#     ] = {
#         "role":
#             "assistant",

#         "content":
#             "",
#     }

#     if tool_calls:

#         message[
#             "tool_calls"
#         ] = tool_calls

#     if (
#         isinstance(
#             response.text,
#             str,
#         )
#         and response.text.strip()
#     ):

#         message[
#             "content"
#         ] = (
#             response.text.strip()
#         )

#     return message


# # ============================================================
# # COHERE TOOL RESULT MESSAGE
# # ============================================================


# def _tool_result_message(
#     *,
#     tool_call_id: str,
#     tool_name: str,
#     result: Mapping[
#         str,
#         Any,
#     ],
# ) -> dict[
#     str,
#     Any,
# ]:

#     return {
#         "role":
#             "tool",

#         "tool_call_id":
#             tool_call_id,

#         "content": [
#             {
#                 "type":
#                     "document",

#                 "document": {
#                     "data":
#                         _json_dumps(
#                             {
#                                 "tool":
#                                     tool_name,

#                                 "result":
#                                     dict(
#                                         result
#                                     ),
#                             }
#                         ),
#                 },
#             }
#         ],
#     }


# # ============================================================
# # FAILURE RESULT
# # ============================================================


# def _safe_tool_failure(
#     *,
#     tool_name: str,
#     code: str,
#     message: str,
# ) -> dict[
#     str,
#     Any,
# ]:

#     return {
#         "success":
#             False,

#         "source":
#             "python",

#         "domain":
#             "tool_execution",

#         "tool":
#             tool_name,

#         "error": {
#             "code":
#                 code,

#             "message":
#                 message,
#         },

#         "data":
#             None,
#     }


# # ============================================================
# # TOOL COUNT
# # ============================================================


# def _tool_execution_count(
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
#     tool_name: str,
# ) -> int:

#     return sum(
#         1
#         for execution
#         in executions
#         if execution.tool_name
#         == tool_name
#     )


# # ============================================================
# # PRIOR SUCCESS
# # ============================================================


# def _has_successful_tool(
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
#     tool_name: str,
# ) -> bool:

#     return any(
#         execution.success
#         and execution.tool_name
#         == tool_name
#         for execution
#         in executions
#     )


# # ============================================================
# # REDUNDANT TOOL DETECTION
# # ============================================================


# def _is_redundant_tool_call(
#     *,
#     tool_name: str,
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> bool:
#     """
#     Prevent useless catalog verification loops.

#     This examines previously executed tool RESULTS.

#     It does not inspect or classify the user's natural language.
#     """

#     # ========================================================
#     # PRODUCT SEARCH ALREADY SUCCEEDED
#     # ========================================================

#     if (
#         tool_name
#         in CATALOG_METADATA_TOOLS
#         and _has_successful_tool(
#             executions,
#             "search_products",
#         )
#     ):

#         return True

#     # ========================================================
#     # RAG ALREADY PERFORMED DISCOVERY + LIVE HYDRATION
#     # ========================================================

#     if (
#         _has_successful_tool(
#             executions,
#             "semantic_product_search",
#         )
#         and tool_name
#         in (
#             CATALOG_METADATA_TOOLS
#             |
#             DISCOVERY_TOOLS
#         )
#     ):

#         return True

#     return False


# # ============================================================
# # TOOL BUDGET CHECK
# # ============================================================


# def _tool_budget_exhausted(
#     *,
#     tool_name: str,
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> bool:

#     budget = (
#         TOOL_CALL_BUDGETS.get(
#             tool_name
#         )
#     )

#     if budget is None:

#         return False

#     return (
#         _tool_execution_count(
#             executions,
#             tool_name,
#         )
#         >= budget
#     )


# # ============================================================
# # SESSION SETTER
# # ============================================================


# def _safe_session_setter(
#     setter: Any,
#     payload: Mapping[
#         str,
#         Any,
#     ],
# ) -> None:
#     """
#     Invoke session setter defensively.

#     Supports either:

#         setter(payload)

#     or a keyword-only single parameter.

#     This helps avoid noisy session-context warnings when helper
#     signatures differ slightly.
#     """

#     try:

#         signature = (
#             inspect.signature(
#                 setter
#             )
#         )

#         parameters = list(
#             signature.parameters.values()
#         )

#         usable = [
#             parameter
#             for parameter
#             in parameters
#             if parameter.kind
#             not in {
#                 inspect.Parameter.VAR_POSITIONAL,
#                 inspect.Parameter.VAR_KEYWORD,
#             }
#         ]

#         if (
#             len(
#                 usable
#             )
#             == 1
#             and usable[
#                 0
#             ].kind
#             == inspect.Parameter.KEYWORD_ONLY
#         ):

#             setter(
#                 **{
#                     usable[
#                         0
#                     ].name:
#                         dict(
#                             payload
#                         )
#                 }
#             )

#             return

#         setter(
#             dict(
#                 payload
#             )
#         )

#     except Exception:

#         # Context storage is useful but must never break the chatbot.
#         logger.debug(
#             "Optional session context update failed.",
#             exc_info=True,
#         )


# # ============================================================
# # PRODUCT CONTEXT
# # ============================================================


# def _update_product_context(
#     record: ToolExecutionRecord,
# ) -> None:

#     if not record.success:

#         return

#     data = (
#         record.result.get(
#             "data"
#         )
#     )

#     if not isinstance(
#         data,
#         Mapping,
#     ):

#         return

#     # ========================================================
#     # PRODUCT SEARCH
#     # ========================================================

#     products = (
#         data.get(
#             "products"
#         )
#     )

#     if (
#         isinstance(
#             products,
#             Sequence,
#         )
#         and not isinstance(
#             products,
#             (
#                 str,
#                 bytes,
#             ),
#         )
#     ):

#         safe_products = [
#             dict(
#                 product
#             )
#             for product
#             in products[
#                 :5
#             ]
#             if isinstance(
#                 product,
#                 Mapping,
#             )
#         ]

#         if safe_products:

#             _safe_session_setter(
#                 set_last_product_context,
#                 {
#                     "source_tool":
#                         record.tool_name,

#                     "products":
#                         safe_products,
#                 },
#             )

#             return

#     # ========================================================
#     # SINGLE PRODUCT
#     # ========================================================

#     product = (
#         data.get(
#             "product"
#         )
#     )

#     if isinstance(
#         product,
#         Mapping,
#     ):

#         _safe_session_setter(
#             set_last_product_context,
#             {
#                 "source_tool":
#                     record.tool_name,

#                 "product":
#                     dict(
#                         product
#                     ),
#             },
#         )

#         return

#     # ========================================================
#     # VARIANT
#     # ========================================================

#     variant = (
#         data.get(
#             "variant"
#         )
#     )

#     if isinstance(
#         variant,
#         Mapping,
#     ):

#         _safe_session_setter(
#             set_last_product_context,
#             {
#                 "source_tool":
#                     record.tool_name,

#                 "variant":
#                     dict(
#                         variant
#                     ),
#             },
#         )

#         return

#     # ========================================================
#     # RAG
#     # ========================================================

#     matches = (
#         data.get(
#             "matches"
#         )
#     )

#     if (
#         isinstance(
#             matches,
#             Sequence,
#         )
#         and not isinstance(
#             matches,
#             (
#                 str,
#                 bytes,
#             ),
#         )
#     ):

#         safe_matches = [
#             dict(
#                 match
#             )
#             for match
#             in matches[
#                 :5
#             ]
#             if isinstance(
#                 match,
#                 Mapping,
#             )
#         ]

#         if safe_matches:

#             _safe_session_setter(
#                 set_last_product_context,
#                 {
#                     "source_tool":
#                         record.tool_name,

#                     "semantic_matches":
#                         safe_matches,
#                 },
#             )


# # ============================================================
# # CONVERSATION CONTEXT
# # ============================================================


# def _update_conversation_context(
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> None:

#     if not executions:

#         return

#     latest = (
#         executions[
#             -1
#         ]
#     )

#     _safe_session_setter(
#         set_conversation_context,
#         {
#             "last_tool":
#                 latest.tool_name,

#             "last_tool_success":
#                 latest.success,

#             "recent_tools": [
#                 execution.tool_name
#                 for execution
#                 in executions[
#                     -4:
#                 ]
#             ],
#         },
#     )


# # ============================================================
# # COMPACT VARIANT
# # ============================================================


# def _compact_variant(
#     variant: Mapping[
#         str,
#         Any,
#     ],
# ) -> dict[
#     str,
#     Any,
# ]:

#     keys = (
#         "sku_id",
#         "size",
#         "pack_size",
#         "quantity",
#         "unit",
#         "price",
#         "mrp",
#         "stock",
#         "in_stock",
#         "currency",
#         "image_url",
#     )

#     return {
#         key:
#             variant.get(
#                 key
#             )
#         for key in keys
#         if key in variant
#     }


# # ============================================================
# # COMPACT PRODUCT
# # ============================================================


# def _compact_product(
#     product: Mapping[
#         str,
#         Any,
#     ],
# ) -> dict[
#     str,
#     Any,
# ]:

#     variants = (
#         product.get(
#             "variants"
#         )
#         or []
#     )

#     compact_variants = [
#         _compact_variant(
#             variant
#         )
#         for variant
#         in variants[
#             :MAX_VARIANTS_FOR_GROQ
#         ]
#         if isinstance(
#             variant,
#             Mapping,
#         )
#     ]

#     return {
#         "product_key":
#             product.get(
#                 "product_key"
#             ),

#         "name":
#             product.get(
#                 "name"
#             ),

#         "brand":
#             product.get(
#                 "brand"
#             ),

#         "category":
#             product.get(
#                 "category"
#             ),

#         "image_url":
#             product.get(
#                 "image_url"
#             ),

#         "rating":
#             product.get(
#                 "rating"
#             ),

#         "variant_count":
#             product.get(
#                 "variant_count"
#             ),

#         "available_variant_count":
#             product.get(
#                 "available_variant_count"
#             ),

#         "is_available":
#             product.get(
#                 "is_available"
#             ),

#         "all_variants_out_of_stock":
#             product.get(
#                 "all_variants_out_of_stock"
#             ),

#         "price_from":
#             product.get(
#                 "price_from"
#             ),

#         "price_to":
#             product.get(
#                 "price_to"
#             ),

#         "variants":
#             compact_variants,
#     }


# # ============================================================
# # GENERIC COMPACTION
# # ============================================================


# def _compact_for_groq(
#     value: Any,
#     *,
#     depth: int = 0,
#     parent_key: str | None = None,
# ) -> Any:

#     if depth > 5:

#         return (
#             str(
#                 value
#             )[
#                 :MAX_STRING_FOR_GROQ
#             ]
#         )

#     if value is None:

#         return None

#     if isinstance(
#         value,
#         (
#             bool,
#             int,
#             float,
#         ),
#     ):

#         return value

#     if isinstance(
#         value,
#         str,
#     ):

#         return (
#             value
#             if len(
#                 value
#             )
#             <= MAX_STRING_FOR_GROQ
#             else (
#                 value[
#                     :MAX_STRING_FOR_GROQ
#                 ]
#                 + "..."
#             )
#         )

#     if isinstance(
#         value,
#         Mapping,
#     ):

#         excluded = {
#             "raw_response",
#             "embedding",
#             "vector",
#             "rag_text",
#             "debug",
#             "internal_debug",
#             "description",
#             "short_description",
#             "specs",
#             "features",
#             "created_at",
#             "updated_at",
#             "tool_call_id",
#             "round_number",
#         }

#         compact: dict[
#             str,
#             Any,
#         ] = {}

#         for (
#             key,
#             item,
#         ) in value.items():

#             key = str(
#                 key
#             )

#             if key in excluded:

#                 continue

#             compact[
#                 key
#             ] = (
#                 _compact_for_groq(
#                     item,
#                     depth=(
#                         depth + 1
#                     ),
#                     parent_key=key,
#                 )
#             )

#         return compact

#     if (
#         isinstance(
#             value,
#             Sequence,
#         )
#         and not isinstance(
#             value,
#             (
#                 str,
#                 bytes,
#             ),
#         )
#     ):

#         if parent_key == "products":

#             limit = (
#                 MAX_PRODUCTS_FOR_GROQ
#             )

#         elif parent_key == "variants":

#             limit = (
#                 MAX_VARIANTS_FOR_GROQ
#             )

#         elif parent_key == "alternatives":

#             limit = (
#                 MAX_ALTERNATIVES_FOR_GROQ
#             )

#         else:

#             limit = (
#                 MAX_GENERIC_LIST_FOR_GROQ
#             )

#         return [
#             _compact_for_groq(
#                 item,
#                 depth=(
#                     depth + 1
#                 ),
#             )
#             for item
#             in list(
#                 value
#             )[
#                 :limit
#             ]
#         ]

#     return (
#         str(
#             value
#         )[
#             :MAX_STRING_FOR_GROQ
#         ]
#     )


# # ============================================================
# # COMPACT PRODUCT SEARCH RESULT
# # ============================================================


# def _compact_search_result(
#     data: Mapping[
#         str,
#         Any,
#     ],
# ) -> dict[
#     str,
#     Any,
# ]:

#     products = [
#         _compact_product(
#             product
#         )
#         for product
#         in (
#             data.get(
#                 "products"
#             )
#             or []
#         )[
#             :MAX_PRODUCTS_FOR_GROQ
#         ]
#         if isinstance(
#             product,
#             Mapping,
#         )
#     ]

#     alternatives = [
#         _compact_product(
#             product
#         )
#         for product
#         in (
#             data.get(
#                 "alternatives"
#             )
#             or []
#         )[
#             :MAX_ALTERNATIVES_FOR_GROQ
#         ]
#         if isinstance(
#             product,
#             Mapping,
#         )
#     ]

#     return {
#         "status":
#             data.get(
#                 "status"
#             ),

#         "query":
#             data.get(
#                 "query"
#             ),

#         "found":
#             data.get(
#                 "found"
#             ),

#         "product_count":
#             data.get(
#                 "product_count"
#             ),

#         "available_product_count":
#             data.get(
#                 "available_product_count"
#             ),

#         "unavailable_product_count":
#             data.get(
#                 "unavailable_product_count"
#             ),

#         "all_matches_out_of_stock":
#             data.get(
#                 "all_matches_out_of_stock"
#             ),

#         "response_hint":
#             data.get(
#                 "response_hint"
#             ),

#         "products":
#             products,

#         "alternatives":
#             alternatives,
#     }


# # ============================================================
# # COMPACT RAG RESULT
# # ============================================================


# def _compact_rag_result(
#     data: Mapping[
#         str,
#         Any,
#     ],
# ) -> dict[
#     str,
#     Any,
# ]:

#     matches = []

#     for match in (
#         data.get(
#             "matches"
#         )
#         or []
#     )[
#         :MAX_PRODUCTS_FOR_GROQ
#     ]:

#         if not isinstance(
#             match,
#             Mapping,
#         ):

#             continue

#         matches.append(
#             _compact_for_groq(
#                 match
#             )
#         )

#     unavailable = []

#     for match in (
#         data.get(
#             "unavailable_matches"
#         )
#         or []
#     )[
#         :3
#     ]:

#         if not isinstance(
#             match,
#             Mapping,
#         ):

#             continue

#         unavailable.append(
#             _compact_for_groq(
#                 match
#             )
#         )

#     return {
#         "status":
#             data.get(
#                 "status"
#             ),

#         "query":
#             data.get(
#                 "query"
#             ),

#         "match_count":
#             data.get(
#                 "match_count"
#             ),

#         "unavailable_match_count":
#             data.get(
#                 "unavailable_match_count"
#             ),

#         "matches":
#             matches,

#         "unavailable_matches":
#             unavailable,
#     }


# # ============================================================
# # GROQ CONTEXT
# # ============================================================


# def _build_groq_context(
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> tuple[
#     list[
#         dict[
#             str,
#             Any,
#         ]
#     ],
#     list[
#         dict[
#             str,
#             Any,
#         ]
#     ],
# ]:

#     rag_tools = set(
#         get_rag_tool_names()
#     )

#     # ========================================================
#     # DISTINCT EXECUTIONS
#     # ========================================================

#     distinct: list[
#         ToolExecutionRecord
#     ] = []

#     seen: set[
#         str
#     ] = set()

#     for execution in reversed(
#         executions
#     ):

#         fingerprint = (
#             _tool_call_fingerprint(
#                 tool_name=(
#                     execution.tool_name
#                 ),
#                 arguments=(
#                     execution.arguments
#                 ),
#             )
#         )

#         if fingerprint in seen:

#             continue

#         seen.add(
#             fingerprint
#         )

#         distinct.append(
#             execution
#         )

#         if (
#             len(
#                 distinct
#             )
#             >= MAX_GROQ_RESULTS
#         ):

#             break

#     distinct.reverse()

#     verified: list[
#         dict[
#             str,
#             Any,
#         ]
#     ] = []

#     rag: list[
#         dict[
#             str,
#             Any,
#         ]
#     ] = []

#     for execution in distinct:

#         result = (
#             execution.result
#         )

#         item: dict[
#             str,
#             Any,
#         ] = {
#             "tool":
#                 execution.tool_name,

#             "success":
#                 execution.success,
#         }

#         if isinstance(
#             result,
#             Mapping,
#         ):

#             data = (
#                 result.get(
#                     "data"
#                 )
#             )

#             if isinstance(
#                 data,
#                 Mapping,
#             ):

#                 if (
#                     execution.tool_name
#                     == "search_products"
#                 ):

#                     item[
#                         "data"
#                     ] = (
#                         _compact_search_result(
#                             data
#                         )
#                     )

#                 elif (
#                     execution.tool_name
#                     in rag_tools
#                 ):

#                     item[
#                         "data"
#                     ] = (
#                         _compact_rag_result(
#                             data
#                         )
#                     )

#                 else:

#                     item[
#                         "data"
#                     ] = (
#                         _compact_for_groq(
#                             data
#                         )
#                     )

#             elif data is not None:

#                 item[
#                     "data"
#                 ] = (
#                     _compact_for_groq(
#                         data
#                     )
#                 )

#             if result.get(
#                 "message"
#             ):

#                 item[
#                     "message"
#                 ] = str(
#                     result[
#                         "message"
#                     ]
#                 )[
#                     :500
#                 ]

#             if result.get(
#                 "error"
#             ):

#                 item[
#                     "error"
#                 ] = (
#                     _compact_for_groq(
#                         result[
#                             "error"
#                         ]
#                     )
#                 )

#         if (
#             execution.tool_name
#             in rag_tools
#         ):

#             rag.append(
#                 item
#             )

#         else:

#             verified.append(
#                 item
#             )

#     return (
#         verified,
#         rag,
#     )


# # ============================================================
# # BACKEND ACTION
# # ============================================================


# def _build_backend_action(
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> str:

#     names = list(
#         dict.fromkeys(
#             execution.tool_name
#             for execution
#             in executions
#         )
#     )

#     if not names:

#         return (
#             "direct_conversation"
#         )

#     return ", ".join(
#         names
#     )


# # ============================================================
# # COHERE TEXT
# # ============================================================


# def _clean_cohere_text(
#     value: Any,
# ) -> str | None:

#     if not isinstance(
#         value,
#         str,
#     ):

#         return None

#     value = (
#         value.strip()
#     )

#     if not value:

#         return None

#     if (
#         len(
#             value
#         )
#         > MAX_COHERE_TEXT_LENGTH
#     ):

#         return (
#             value[
#                 :MAX_COHERE_TEXT_LENGTH
#             ]
#             + "..."
#         )

#     return value


# # ============================================================
# # COHERE TOOL LOOP
# # ============================================================


# def _run_cohere_tool_loop(
#     *,
#     user_message: str,
#     chat_history: Sequence[
#         Mapping[
#             str,
#             Any,
#         ]
#     ] | None,
# ) -> tuple[
#     list[
#         ToolExecutionRecord
#     ],
#     str | None,
#     int,
# ]:

#     tools = (
#         get_all_tool_schemas()
#     )

#     messages = (
#         build_messages(
#             user_message=user_message,
#             chat_history=chat_history,
#             include_system_rules=True,
#             additional_system_context=(
#                 _build_runtime_system_context()
#             ),
#         )
#     )

#     executions: list[
#         ToolExecutionRecord
#     ] = []

#     fingerprints: set[
#         str
#     ] = set()

#     total_calls = 0

#     cohere_text: (
#         str
#         | None
#     ) = None

#     # ========================================================
#     # ROUNDS
#     # ========================================================

#     for round_number in range(
#         1,
#         MAX_TOOL_ROUNDS + 1,
#     ):

#         response = (
#             chat_with_messages(
#                 messages=messages,
#                 tools=tools,
#                 strict_tools=False,
#             )
#         )

#         cohere_text = (
#             _clean_cohere_text(
#                 response.text
#             )
#         )

#         calls = list(
#             response.tool_calls
#             or []
#         )

#         # ====================================================
#         # NO TOOL → DONE
#         # ====================================================

#         if not calls:

#             return (
#                 executions,
#                 cohere_text,
#                 round_number,
#             )

#         if (
#             len(
#                 calls
#             )
#             > MAX_TOOL_CALLS_PER_ROUND
#         ):

#             logger.warning(
#                 "Cohere requested too many tools in one round."
#             )

#             return (
#                 executions,
#                 cohere_text,
#                 round_number,
#             )

#         # ====================================================
#         # CHECK IF ENTIRE NEW ROUND IS REDUNDANT
#         # ====================================================

#         all_redundant = True

#         for call in calls:

#             tool_name = str(
#                 call.name
#                 or ""
#             ).strip()

#             if (
#                 not _is_redundant_tool_call(
#                     tool_name=tool_name,
#                     executions=executions,
#                 )
#                 and not _tool_budget_exhausted(
#                     tool_name=tool_name,
#                     executions=executions,
#                 )
#             ):

#                 all_redundant = False

#                 break

#         if (
#             all_redundant
#             and executions
#         ):

#             logger.info(
#                 "Stopping redundant Cohere tool round."
#             )

#             return (
#                 executions,
#                 cohere_text,
#                 round_number,
#             )

#         # Must send Cohere's assistant tool-call message before
#         # corresponding tool results.

#         messages.append(
#             _assistant_tool_message(
#                 response
#             )
#         )

#         executed_this_round = 0

#         # ====================================================
#         # EXECUTE CALLS
#         # ====================================================

#         for call in calls:

#             total_calls += 1

#             if (
#                 total_calls
#                 > MAX_TOTAL_TOOL_CALLS
#             ):

#                 logger.warning(
#                     "Maximum total tool calls reached."
#                 )

#                 return (
#                     executions,
#                     cohere_text,
#                     round_number,
#                 )

#             tool_name = str(
#                 call.name
#                 or ""
#             ).strip()

#             tool_call_id = str(
#                 call.id
#                 or ""
#             ).strip()

#             if not tool_call_id:

#                 continue

#             try:

#                 arguments = (
#                     _tool_arguments(
#                         call
#                     )
#                 )

#             except BrainToolError:

#                 result = (
#                     _safe_tool_failure(
#                         tool_name=(
#                             tool_name
#                             or "unknown"
#                         ),
#                         code=(
#                             "INVALID_TOOL_ARGUMENTS"
#                         ),
#                         message=(
#                             "The tool received invalid arguments."
#                         ),
#                     )
#                 )

#                 messages.append(
#                     _tool_result_message(
#                         tool_call_id=tool_call_id,
#                         tool_name=(
#                             tool_name
#                             or "unknown"
#                         ),
#                         result=result,
#                     )
#                 )

#                 continue

#             # =================================================
#             # ALLOW LIST
#             # =================================================

#             if (
#                 tool_name
#                 not in get_all_tool_names()
#             ):

#                 result = (
#                     _safe_tool_failure(
#                         tool_name=(
#                             tool_name
#                             or "unknown"
#                         ),
#                         code=(
#                             "UNKNOWN_TOOL"
#                         ),
#                         message=(
#                             "That backend capability is unavailable."
#                         ),
#                     )
#                 )

#                 messages.append(
#                     _tool_result_message(
#                         tool_call_id=tool_call_id,
#                         tool_name=(
#                             tool_name
#                             or "unknown"
#                         ),
#                         result=result,
#                     )
#                 )

#                 continue

#             fingerprint = (
#                 _tool_call_fingerprint(
#                     tool_name=tool_name,
#                     arguments=arguments,
#                 )
#             )

#             # =================================================
#             # EXACT DUPLICATE
#             # =================================================

#             if fingerprint in fingerprints:

#                 logger.info(
#                     "Stopping duplicate tool call. tool=%s",
#                     tool_name,
#                 )

#                 return (
#                     executions,
#                     cohere_text,
#                     round_number,
#                 )

#             # =================================================
#             # REDUNDANT CALL
#             # =================================================

#             if (
#                 _is_redundant_tool_call(
#                     tool_name=tool_name,
#                     executions=executions,
#                 )
#             ):

#                 skipped_result = {
#                     "success":
#                         True,

#                     "source":
#                         "python",

#                     "domain":
#                         "orchestration",

#                     "tool":
#                         tool_name,

#                     "data": {
#                         "skipped":
#                             True,

#                         "reason":
#                             (
#                                 "Sufficient authoritative "
#                                 "data was already retrieved."
#                             ),
#                     },
#                 }

#                 messages.append(
#                     _tool_result_message(
#                         tool_call_id=tool_call_id,
#                         tool_name=tool_name,
#                         result=skipped_result,
#                     )
#                 )

#                 continue

#             # =================================================
#             # TOOL BUDGET
#             # =================================================

#             if (
#                 _tool_budget_exhausted(
#                     tool_name=tool_name,
#                     executions=executions,
#                 )
#             ):

#                 skipped_result = {
#                     "success":
#                         True,

#                     "source":
#                         "python",

#                     "domain":
#                         "orchestration",

#                     "tool":
#                         tool_name,

#                     "data": {
#                         "skipped":
#                             True,

#                         "reason":
#                             (
#                                 "The tool has already been "
#                                 "executed enough times."
#                             ),
#                     },
#                 }

#                 messages.append(
#                     _tool_result_message(
#                         tool_call_id=tool_call_id,
#                         tool_name=tool_name,
#                         result=skipped_result,
#                     )
#                 )

#                 continue

#             # =================================================
#             # EXECUTE REAL TOOL
#             # =================================================

#             try:

#                 result = (
#                     execute_tool(
#                         tool_name=tool_name,
#                         arguments=arguments,
#                     )
#                 )

#             except BrainToolError:

#                 result = (
#                     _safe_tool_failure(
#                         tool_name=tool_name,
#                         code=(
#                             "TOOL_EXECUTION_REJECTED"
#                         ),
#                         message=(
#                             "The backend operation was rejected."
#                         ),
#                     )
#                 )

#             except Exception:

#                 logger.exception(
#                     "Backend tool failed. tool=%s",
#                     tool_name,
#                 )

#                 result = (
#                     _safe_tool_failure(
#                         tool_name=tool_name,
#                         code=(
#                             "TOOL_EXECUTION_ERROR"
#                         ),
#                         message=(
#                             "The backend operation could not "
#                             "be completed."
#                         ),
#                     )
#                 )

#             if not isinstance(
#                 result,
#                 Mapping,
#             ):

#                 result = (
#                     _safe_tool_failure(
#                         tool_name=tool_name,
#                         code=(
#                             "INVALID_TOOL_RESULT"
#                         ),
#                         message=(
#                             "The backend returned an invalid result."
#                         ),
#                     )
#                 )

#             result_dict = dict(
#                 result
#             )

#             record = (
#                 ToolExecutionRecord(
#                     tool_call_id=tool_call_id,
#                     tool_name=tool_name,
#                     arguments=arguments,
#                     result=result_dict,
#                     round_number=round_number,
#                 )
#             )

#             executions.append(
#                 record
#             )

#             fingerprints.add(
#                 fingerprint
#             )

#             executed_this_round += 1

#             # =================================================
#             # SAVE PRODUCT CONTEXT
#             # =================================================

#             _update_product_context(
#                 record
#             )

#             # =================================================
#             # RETURN TOOL RESULT TO COHERE
#             # =================================================

#             messages.append(
#                 _tool_result_message(
#                     tool_call_id=tool_call_id,
#                     tool_name=tool_name,
#                     result=result_dict,
#                 )
#             )

#         # If Cohere only asked for redundant/budget-blocked calls,
#         # stop instead of making another expensive model round.

#         if (
#             executed_this_round
#             == 0
#             and executions
#         ):

#             return (
#                 executions,
#                 cohere_text,
#                 round_number,
#             )

#     logger.info(
#         "Maximum Cohere rounds reached; using collected results."
#     )

#     return (
#         executions,
#         cohere_text,
#         MAX_TOOL_ROUNDS,
#     )


# # ============================================================
# # MONEY FORMATTER
# # ============================================================


# def _format_price(
#     price: Any,
#     currency: Any = None,
# ) -> str:

#     if price is None:

#         return ""

#     try:

#         number = float(
#             price
#         )

#         if number.is_integer():

#             value = str(
#                 int(
#                     number
#                 )
#             )

#         else:

#             value = (
#                 f"{number:.2f}"
#                 .rstrip(
#                     "0"
#                 )
#                 .rstrip(
#                     "."
#                 )
#             )

#     except (
#         TypeError,
#         ValueError,
#     ):

#         value = str(
#             price
#         )

#     currency = str(
#         currency
#         or ""
#     ).strip().upper()

#     if currency == "INR":

#         return (
#             "₹"
#             + value
#         )

#     if currency:

#         return (
#             f"{currency} {value}"
#         )

#     return value


# # ============================================================
# # PRODUCT LINE
# # ============================================================


# def _product_fallback_lines(
#     product: Mapping[
#         str,
#         Any,
#     ],
# ) -> list[str]:

#     name = str(
#         product.get(
#             "name"
#         )
#         or product.get(
#             "product_name"
#         )
#         or "Product"
#     )

#     brand = str(
#         product.get(
#             "brand"
#         )
#         or ""
#     )

#     title = (
#         f"{brand} {name}"
#         if (
#             brand
#             and brand.lower()
#             not in name.lower()
#         )
#         else name
#     )

#     available = bool(
#         product.get(
#             "is_available"
#         )
#     )

#     variants = list(
#         product.get(
#             "variants"
#         )
#         or []
#     )

#     lines = [
#         (
#             f"**{title}**"
#             + (
#                 ""
#                 if available
#                 else " — currently out of stock"
#             )
#         )
#     ]

#     for variant in variants[
#         :4
#     ]:

#         if not isinstance(
#             variant,
#             Mapping,
#         ):

#             continue

#         size = str(
#             variant.get(
#                 "size"
#             )
#             or variant.get(
#                 "pack_size"
#             )
#             or ""
#         )

#         price = (
#             _format_price(
#                 variant.get(
#                     "price"
#                 ),
#                 variant.get(
#                     "currency"
#                 ),
#             )
#         )

#         in_stock = bool(
#             variant.get(
#                 "in_stock"
#             )
#         )

#         pieces = [
#             item
#             for item in (
#                 size,
#                 price,
#                 (
#                     "In stock"
#                     if in_stock
#                     else "Out of stock"
#                 ),
#             )
#             if item
#         ]

#         lines.append(
#             "• "
#             + " — ".join(
#                 pieces
#             )
#         )

#     return lines


# # ============================================================
# # PRODUCT SEARCH FALLBACK
# # ============================================================


# def _search_fallback_answer(
#     result: Mapping[
#         str,
#         Any,
#     ],
# ) -> str | None:

#     data = (
#         result.get(
#             "data"
#         )
#     )

#     if not isinstance(
#         data,
#         Mapping,
#     ):

#         return None

#     status = str(
#         data.get(
#             "status"
#         )
#         or ""
#     )

#     products = [
#         product
#         for product
#         in (
#             data.get(
#                 "products"
#             )
#             or []
#         )
#         if isinstance(
#             product,
#             Mapping,
#         )
#     ]

#     alternatives = [
#         product
#         for product
#         in (
#             data.get(
#                 "alternatives"
#             )
#             or []
#         )
#         if isinstance(
#             product,
#             Mapping,
#         )
#     ]

#     lines: list[
#         str
#     ] = []

#     if status in {
#         "found",
#         "found_partially_available",
#     }:

#         lines.append(
#             "I found these matching products:"
#         )

#         for product in products[
#             :4
#         ]:

#             lines.extend(
#                 _product_fallback_lines(
#                     product
#                 )
#             )

#         return "\n".join(
#             lines
#         )

#     if status in {
#         "found_out_of_stock",
#         "requested_in_stock_but_unavailable",
#     }:

#         if products:

#             names = [
#                 str(
#                     product.get(
#                         "name"
#                     )
#                     or "the requested product"
#                 )
#                 for product
#                 in products[
#                     :2
#                 ]
#             ]

#             lines.append(
#                 (
#                     ", ".join(
#                         names
#                     )
#                     + (
#                         " is currently out of stock."
#                         if len(
#                             names
#                         )
#                         == 1
#                         else " are currently out of stock."
#                     )
#                 )
#             )

#         else:

#             lines.append(
#                 "The requested product is currently out of stock."
#             )

#         if alternatives:

#             lines.append(
#                 "\nAvailable alternatives:"
#             )

#             for product in alternatives[
#                 :3
#             ]:

#                 lines.extend(
#                     _product_fallback_lines(
#                         product
#                     )
#                 )

#         return "\n".join(
#             lines
#         )

#     if status == "not_found":

#         lines.append(
#             "I couldn't find a sufficiently close product in the catalog currently."
#         )

#         if alternatives:

#             lines.append(
#                 "\nYou could consider:"
#             )

#             for product in alternatives[
#                 :3
#             ]:

#                 lines.extend(
#                     _product_fallback_lines(
#                         product
#                     )
#                 )

#         return "\n".join(
#             lines
#         )

#     return None


# # ============================================================
# # RAG FALLBACK
# # ============================================================


# def _rag_fallback_answer(
#     result: Mapping[
#         str,
#         Any,
#     ],
# ) -> str | None:

#     data = (
#         result.get(
#             "data"
#         )
#     )

#     if not isinstance(
#         data,
#         Mapping,
#     ):

#         return None

#     matches = [
#         item
#         for item in (
#             data.get(
#                 "matches"
#             )
#             or []
#         )
#         if isinstance(
#             item,
#             Mapping,
#         )
#     ]

#     unavailable = [
#         item
#         for item in (
#             data.get(
#                 "unavailable_matches"
#             )
#             or []
#         )
#         if isinstance(
#             item,
#             Mapping,
#         )
#     ]

#     if matches:

#         lines = [
#             "Here are some suitable options:"
#         ]

#         for item in matches[
#             :4
#         ]:

#             name = str(
#                 item.get(
#                     "product_name"
#                 )
#                 or "Product"
#             )

#             brand = str(
#                 item.get(
#                     "brand"
#                 )
#                 or ""
#             )

#             title = (
#                 f"{brand} {name}"
#                 if (
#                     brand
#                     and brand.lower()
#                     not in name.lower()
#                 )
#                 else name
#             )

#             variants = list(
#                 item.get(
#                     "variants"
#                 )
#                 or []
#             )

#             detail = ""

#             if variants:

#                 variant = variants[
#                     0
#                 ]

#                 if isinstance(
#                     variant,
#                     Mapping,
#                 ):

#                     size = str(
#                         variant.get(
#                             "size"
#                         )
#                         or ""
#                     )

#                     price = (
#                         _format_price(
#                             variant.get(
#                                 "price"
#                             ),
#                             variant.get(
#                                 "currency"
#                             ),
#                         )
#                     )

#                     detail_parts = [
#                         item
#                         for item in (
#                             size,
#                             price,
#                         )
#                         if item
#                     ]

#                     if detail_parts:

#                         detail = (
#                             " — "
#                             + " — ".join(
#                                 detail_parts
#                             )
#                         )

#             lines.append(
#                 f"• **{title}**{detail}"
#             )

#         return "\n".join(
#             lines
#         )

#     if unavailable:

#         names = [
#             str(
#                 item.get(
#                     "product_name"
#                 )
#                 or "Product"
#             )
#             for item
#             in unavailable[
#                 :3
#             ]
#         ]

#         return (
#             "I found relevant options, but they are currently "
#             "out of stock: "
#             + ", ".join(
#                 names
#             )
#             + "."
#         )

#     return (
#         "I couldn't find a sufficiently suitable product for that request right now."
#     )


# # ============================================================
# # VERIFIED FALLBACK
# # ============================================================


# def _build_verified_fallback_answer(
#     *,
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
#     cohere_text: str | None,
# ) -> str:
#     """
#     If Groq is rate-limited/unavailable, do NOT throw away successful
#     Supabase/RAG results.

#     Generate a small deterministic answer from verified data.

#     This does not invent commerce facts.
#     """

#     # ========================================================
#     # LATEST SUCCESSFUL SEARCH / RAG
#     # ========================================================

#     for execution in reversed(
#         executions
#     ):

#         if not execution.success:

#             continue

#         if (
#             execution.tool_name
#             == "search_products"
#         ):

#             answer = (
#                 _search_fallback_answer(
#                     execution.result
#                 )
#             )

#             if answer:

#                 return answer

#         if (
#             execution.tool_name
#             == "semantic_product_search"
#         ):

#             answer = (
#                 _rag_fallback_answer(
#                     execution.result
#                 )
#             )

#             if answer:

#                 return answer

#     # ========================================================
#     # OTHER TOOLS
#     # ========================================================

#     for execution in reversed(
#         executions
#     ):

#         if not execution.success:

#             continue

#         message = (
#             execution.result.get(
#                 "message"
#             )
#         )

#         if (
#             isinstance(
#                 message,
#                 str,
#             )
#             and message.strip()
#         ):

#             return (
#                 message.strip()
#             )

#     # ========================================================
#     # GENERAL QUERY
#     #
#     # Cohere's no-tool response is usable if Groq is temporarily
#     # unavailable.
#     # ========================================================

#     if cohere_text:

#         return cohere_text

#     return (
#         "I couldn't generate the final wording right now. "
#         "Please try again shortly."
#     )


# # ============================================================
# # GENERATE DATABASE-BACKED RESPONSE
# # ============================================================


# def _generate_database_backed_response(
#     *,
#     user_message: str,
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
# ) -> ResponsePayload:
#     """
#     Generate the final response for any tool-backed request.

#     This path intentionally bypasses the normal rotating Groq responder.

#     database_responder.py uses:

#         GROQ_API_KEY2

#     which is the dedicated Groq API slot 3 for database-backed
#     product/cart/order/RAG responses.

#     The returned ResponsePayload preserves structured verified data for
#     Streamlit so app.py can display images, SKU details, variants, cart
#     items and orders independently of generated prose.
#     """

#     return respond_to_database_query(
#         user_message=user_message,
#         executions=executions,
#     )


# # ============================================================
# # GENERATE GROQ ANSWER
# # ============================================================


# def _generate_final_answer(
#     *,
#     user_message: str,
#     chat_history: Sequence[
#         Mapping[
#             str,
#             Any,
#         ]
#     ] | None,
#     executions: Sequence[
#         ToolExecutionRecord
#     ],
#     cohere_text: str | None,
# ) -> str:

#     (
#         verified_data,
#         rag_context,
#     ) = (
#         _build_groq_context(
#             executions
#         )
#     )

#     additional_context = {
#         "cohere_conclusion":
#             (
#                 cohere_text[
#                     :600
#                 ]
#                 if cohere_text
#                 else None
#             ),

#         "tools":
#             list(
#                 dict.fromkeys(
#                     execution.tool_name
#                     for execution
#                     in executions
#                 )
#             ),
#     }

#     return generate_final_text(
#         user_message=user_message,

#         backend_action=(
#             _build_backend_action(
#                 executions
#             )
#         ),

#         verified_data=(
#             verified_data
#             if verified_data
#             else None
#         ),

#         rag_context=(
#             rag_context
#             if rag_context
#             else None
#         ),

#         chat_history=chat_history,

#         additional_context=(
#             additional_context
#         ),
#     )


# # ============================================================
# # SAVE CHAT
# # ============================================================


# def _save_chat_message(
#     role: str,
#     content: str,
# ) -> None:

#     try:

#         add_chat_message(
#             role,
#             content,
#         )

#     except Exception:

#         logger.debug(
#             "Unable to save chat message.",
#             exc_info=True,
#         )


# # ============================================================
# # MAIN
# # ============================================================


# def process_message(
#     user_message: str,
# ) -> BrainResponse:

#     initialize_session()

#     # ========================================================
#     # VALIDATION
#     # ========================================================

#     try:

#         user_message = (
#             _validate_user_message(
#                 user_message
#             )
#         )

#     except BrainValidationError as exc:

#         return BrainResponse(
#             text=str(
#                 exc
#             ),
#             success=False,
#             error_code=(
#                 "INVALID_MESSAGE"
#             ),
#         )

#     # ========================================================
#     # HISTORY
#     # ========================================================

#     try:

#         chat_history = (
#             get_model_chat_history()
#         )

#     except Exception:

#         logger.debug(
#             "Unable to load model history.",
#             exc_info=True,
#         )

#         chat_history = []

#     executions: list[
#         ToolExecutionRecord
#     ] = []

#     cohere_text: (
#         str
#         | None
#     ) = None

#     rounds = 0

#     # ========================================================
#     # COHERE
#     # ========================================================

#     try:

#         (
#             executions,
#             cohere_text,
#             rounds,
#         ) = (
#             _run_cohere_tool_loop(
#                 user_message=user_message,
#                 chat_history=chat_history,
#             )
#         )

#     except CohereClientError:

#         logger.exception(
#             "Cohere request failed."
#         )

#         text = (
#             "I couldn't process that request right now. "
#             "Please try again."
#         )

#         _save_chat_message(
#             "user",
#             user_message,
#         )

#         _save_chat_message(
#             "assistant",
#             text,
#         )

#         return BrainResponse(
#             text=text,
#             success=False,
#             tool_executions=executions,
#             cohere_text=cohere_text,
#             rounds=rounds,
#             error_code=(
#                 "COHERE_ERROR"
#             ),
#         )

#     except Exception:

#         logger.exception(
#             "Unexpected brain orchestration failure."
#         )

#         text = (
#             "I couldn't complete that request right now. "
#             "Please try again."
#         )

#         _save_chat_message(
#             "user",
#             user_message,
#         )

#         _save_chat_message(
#             "assistant",
#             text,
#         )

#         return BrainResponse(
#             text=text,
#             success=False,
#             tool_executions=executions,
#             cohere_text=cohere_text,
#             rounds=rounds,
#             error_code=(
#                 "BRAIN_ERROR"
#             ),
#         )

#     # ========================================================
#     # DATABASE / TOOL-BACKED RESPONSE PATH
#     # ========================================================
#     #
#     # If Cohere used a backend tool, do not send that result through
#     # the normal rotating Groq client below.
#     #
#     # Instead:
#     #
#     #     verified backend result
#     #         ↓
#     #     database_responder.py
#     #         ↓
#     #     GROQ_API_KEY2 only (API slot 3)
#     #         ↓
#     #     prompts/rules.txt
#     #         ↓
#     #     natural response + structured ResponsePayload
#     #
#     # The old Groq block below is preserved and now handles only
#     # no-tool / general-chat requests.
#     # ========================================================

#     if executions:

#         try:

#             response_payload = (
#                 _generate_database_backed_response(
#                     user_message=user_message,
#                     executions=executions,
#                 )
#             )

#             final_text = str(
#                 response_payload.text
#                 or ""
#             ).strip()

#             if not final_text:

#                 final_text = (
#                     _build_verified_fallback_answer(
#                         executions=executions,
#                         cohere_text=cohere_text,
#                     )
#                 )

#             _save_chat_message(
#                 "user",
#                 user_message,
#             )

#             _save_chat_message(
#                 "assistant",
#                 final_text,
#             )

#             _update_conversation_context(
#                 executions
#             )

#             return BrainResponse(
#                 text=final_text,
#                 success=bool(
#                     response_payload.success
#                     and final_text
#                 ),
#                 tool_executions=executions,
#                 cohere_text=cohere_text,
#                 rounds=rounds,
#                 error_code=(
#                     None
#                     if response_payload.groq_used
#                     else "DATABASE_GROQ_FALLBACK_USED"
#                 ),
#                 response_payload=response_payload,
#             )

#         except Exception:

#             logger.exception(
#                 "Database response engine failed. "
#                 "Using existing verified Python fallback."
#             )

#             # Never spill a database request into the normal rotating
#             # Groq pool. Use the existing grounded Python fallback.
#             final_text = (
#                 _build_verified_fallback_answer(
#                     executions=executions,
#                     cohere_text=cohere_text,
#                 )
#             )

#             _save_chat_message(
#                 "user",
#                 user_message,
#             )

#             _save_chat_message(
#                 "assistant",
#                 final_text,
#             )

#             _update_conversation_context(
#                 executions
#             )

#             return BrainResponse(
#                 text=final_text,
#                 success=bool(
#                     final_text
#                 ),
#                 tool_executions=executions,
#                 cohere_text=cohere_text,
#                 rounds=rounds,
#                 error_code=(
#                     "DATABASE_RESPONSE_ERROR"
#                 ),
#                 response_payload=None,
#             )

#     # ========================================================
#     # GROQ
#     # ========================================================
#     #
#     # EXISTING GENERAL-CHAT PATH.
#     #
#     # This block is deliberately preserved. Because the database path
#     # above returns early, this section now runs only when Cohere did
#     # not execute a backend tool.
#     # ========================================================

#     groq_failed = False

#     try:

#         final_text = (
#             _generate_final_answer(
#                 user_message=user_message,
#                 chat_history=chat_history,
#                 executions=executions,
#                 cohere_text=cohere_text,
#             )
#         )

#         final_text = (
#             str(
#                 final_text
#             ).strip()
#         )

#         if not final_text:

#             raise GroqClientError(
#                 "Groq returned empty text."
#             )

#     except GroqClientError:

#         # ====================================================
#         # IMPORTANT CHANGE
#         #
#         # Previously:
#         #
#         # successful DB result
#         #     ↓
#         # Groq 429
#         #     ↓
#         # generic failure message
#         #
#         # Now:
#         #
#         # successful DB result
#         #     ↓
#         # Groq 429
#         #     ↓
#         # deterministic verified result
#         # ====================================================

#         groq_failed = True

#         logger.warning(
#             "Groq unavailable; using verified fallback response."
#         )

#         final_text = (
#             _build_verified_fallback_answer(
#                 executions=executions,
#                 cohere_text=cohere_text,
#             )
#         )

#     except Exception:

#         groq_failed = True

#         logger.exception(
#             "Unexpected Groq final-response failure."
#         )

#         final_text = (
#             _build_verified_fallback_answer(
#                 executions=executions,
#                 cohere_text=cohere_text,
#             )
#         )

#     # ========================================================
#     # SAVE
#     # ========================================================

#     _save_chat_message(
#         "user",
#         user_message,
#     )

#     _save_chat_message(
#         "assistant",
#         final_text,
#     )

#     _update_conversation_context(
#         executions
#     )

#     # ========================================================
#     # SUCCESS
#     #
#     # If Groq failed but verified backend data existed and Python
#     # produced a truthful fallback, the user's request still
#     # completed successfully.
#     # ========================================================

#     effective_success = bool(
#         final_text
#     ) and (
#         bool(
#             executions
#         )
#         or bool(
#             cohere_text
#         )
#         or not groq_failed
#     )

#     return BrainResponse(
#         text=final_text,
#         success=effective_success,
#         tool_executions=executions,
#         cohere_text=cohere_text,
#         rounds=rounds,
#         error_code=(
#             "GROQ_FALLBACK_USED"
#             if groq_failed
#             else None
#         ),
#         response_payload=None,
#     )


# # ============================================================
# # SIMPLE WRAPPER
# # ============================================================


# def chat(
#     user_message: str,
# ) -> str:

#     return (
#         process_message(
#             user_message
#         )
#         .text
#     )


# # ============================================================
# # CAPABILITIES
# # ============================================================


# def get_brain_capabilities() -> dict[
#     str,
#     Any,
# ]:

#     product_tools = sorted(
#         get_product_tool_names()
#     )

#     cart_tools = sorted(
#         get_cart_tool_names()
#     )

#     order_tools = sorted(
#         get_order_tool_names()
#     )

#     rag_tools = sorted(
#         get_rag_tool_names()
#     )

#     return {
#         "product_tools":
#             product_tools,

#         "cart_tools":
#             cart_tools,

#         "order_tools":
#             order_tools,

#         "rag_tools":
#             rag_tools,

#         "total_tools": (
#             len(
#                 product_tools
#             )
#             +
#             len(
#                 cart_tools
#             )
#             +
#             len(
#                 order_tools
#             )
#             +
#             len(
#                 rag_tools
#             )
#         ),

#         "max_tool_rounds":
#             MAX_TOOL_ROUNDS,

#         "max_total_tool_calls":
#             MAX_TOTAL_TOOL_CALLS,

#         "manual_intent_classifier":
#             False,

#         "general_queries_supported":
#             True,

#         "semantic_rag_supported":
#             True,

#         "out_of_stock_search_supported":
#             True,

#         "groq_verified_fallback":
#             True,

#         "compact_groq_context":
#             True,

#         "structured_response_supported":
#             True,

#         "database_response_engine":
#             "core.database_responder",

#         "database_groq_key_slot":
#             3,

#         "database_groq_rotation":
#             False,

#         "database_rules_file":
#             "prompts/rules.txt",

#         "product_image_payload_supported":
#             True,

#         "sku_payload_supported":
#             True,

#         "order_payload_supported":
#             True,

#         "general_groq_path_preserved":
#             True,
#     }

"""
core/brain.py

Central orchestration layer for Grocery Chatbot.

FLOW
----

User
    ↓
Cohere
    ↓
Automatically decides:
    - no tool
    - product search
    - semantic RAG
    - cart
    - order
    ↓
Python executes approved tool
    ↓
Verified Supabase / RAG data
    ↓
Groq
    ↓
Final response


IMPORTANT
---------

There is NO manually maintained natural-language intent classifier.

Do NOT add:

    if "milk" in user_message
    if "cart" in user_message
    if "healthy" in user_message

Cohere decides whether tools are needed.

Python only:

- validates tool calls
- executes approved tools
- prevents loops
- prevents duplicate/redundant execution
- compacts verified results
- provides a deterministic fallback if Groq is unavailable

UPDATED RESPONSE ROUTING
------------------------

Tool-backed requests now use a dedicated response path:

    Cohere
        ↓
    product / cart / order / RAG tool
        ↓
    verified backend data
        ↓
    core.database_responder
        ↓
    GROQ_API_KEY2 only (Groq API slot 3)
        ↓
    prompts/rules.txt
        ↓
    conversational response + structured ResponsePayload

General no-tool conversation continues through the existing normal
Groq response path.

All existing helper functions below are intentionally retained for
compatibility and fallback behavior.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging

from contextvars import ContextVar

from dataclasses import (
    dataclass,
    field,
)

from typing import (
    Any,
    Mapping,
    Sequence,
)


# ============================================================
# COHERE
# ============================================================

from core.cohere_client import (
    CohereBrainResponse,
    CohereClientError,
    CohereToolCall,
    build_messages,
    chat_with_messages,
)


# ============================================================
# GROQ
# ============================================================

from core.groq_client import (
    GroqClientError,
    generate_final_text,
)


# ============================================================
# DATABASE RESPONSE ENGINE
# ============================================================

from core.database_responder import (
    respond_to_database_query,
)


# ============================================================
# STRUCTURED RESPONSE MODEL
# ============================================================

from core.response_models import (
    ResponsePayload,
)


# ============================================================
# PRODUCT TOOLS
# ============================================================

from tools.product_tools import (
    execute_product_tool,
    get_product_tool_names,
    get_product_tool_schemas,
)


# ============================================================
# CART TOOLS
# ============================================================

from tools.cart_tools import (
    execute_cart_tool,
    get_cart_tool_names,
    get_cart_tool_schemas,
)


# ============================================================
# ORDER TOOLS
# ============================================================

from tools.order_tools import (
    execute_order_tool,
    get_order_tool_names,
    get_order_tool_schemas,
)


# ============================================================
# RAG
# ============================================================

from rag.rag import (
    execute_rag_tool,
    get_rag_tool_names,
    get_rag_tool_schemas,
)


# ============================================================
# API REQUEST CONTEXT
# ============================================================

from api_context import (
    get_api_client_context,
    get_api_conversation_id,
)


# ============================================================
# CONFIGURATION
# ============================================================

from config import settings


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# API-ONLY REQUEST-SCOPED CONVERSATION STATE
# ============================================================
#
# The original Streamlit UI stored conversational state in
# auth/session.py -> st.session_state.
#
# Hugging Face runs this project as an API only.  The helper names used
# by the existing brain are intentionally preserved here, but their
# storage is now a ContextVar scoped to the current API request/task.
#
# Existing orchestration below therefore remains unchanged.
# ============================================================


@dataclass(
    slots=True,
)
class _BrainRuntimeState:

    messages: list[
        dict[
            str,
            Any,
        ]
    ] = field(
        default_factory=list
    )

    conversation_context: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    last_product_context: Any = None

    pending_action: dict[
        str,
        Any,
    ] | None = None

    conversation_id: str | None = None


_BRAIN_RUNTIME_STATE: ContextVar[
    _BrainRuntimeState | None
] = ContextVar(
    "grocery_chatbot_brain_runtime_state",
    default=None,
)

_CONTEXT_VALUE_NOT_SET = object()

_ALLOWED_CHAT_ROLES = {
    "user",
    "assistant",
}


def _copy_state_value(
    value: Any,
) -> Any:

    try:
        return copy.deepcopy(
            value
        )
    except Exception:
        return value


def _safe_mapping(
    value: Any,
) -> dict[
    str,
    Any,
]:

    if not isinstance(
        value,
        Mapping,
    ):
        return {}

    return _copy_state_value(
        dict(
            value
        )
    )


def _history_limit() -> int:

    try:
        value = int(
            getattr(
                settings,
                "max_chat_history",
                10,
            )
            or 10
        )
    except (
        TypeError,
        ValueError,
    ):
        value = 10

    return max(
        1,
        value,
    )


def _normalize_client_history(
    value: Any,
) -> list[
    dict[
        str,
        Any,
    ]
]:

    if not (
        isinstance(
            value,
            Sequence,
        )
        and not isinstance(
            value,
            (
                str,
                bytes,
                bytearray,
            ),
        )
    ):
        return []

    messages: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for item in value:

        if not isinstance(
            item,
            Mapping,
        ):
            continue

        role = str(
            item.get(
                "role"
            )
            or ""
        ).strip().lower()

        content = item.get(
            "content"
        )

        if role not in _ALLOWED_CHAT_ROLES:
            continue

        if not isinstance(
            content,
            str,
        ):
            continue

        content = content.strip()

        if not content:
            continue

        messages.append(
            {
                "role":
                    role,
                "content":
                    content,
            }
        )

    return messages[
        -_history_limit():
    ]


def _new_runtime_state() -> _BrainRuntimeState:
    """
    Seed conversational state from ChatRequest.client_context.

    This is conversational context only.  It is never trusted for
    authenticated identity, price, stock, cart or order facts.
    """

    try:
        client_context = (
            get_api_client_context()
        )
    except Exception:
        client_context = {}

    if not isinstance(
        client_context,
        Mapping,
    ):
        client_context = {}

    history = (
        client_context.get(
            "chat_history"
        )
    )

    if history is None:
        history = (
            client_context.get(
                "messages"
            )
        )

    conversation_context = (
        _safe_mapping(
            client_context.get(
                "conversation_context"
            )
        )
    )

    if not conversation_context:
        conversation_context = (
            _safe_mapping(
                client_context.get(
                    "context"
                )
            )
        )

    last_product_context = (
        _copy_state_value(
            client_context.get(
                "last_product_context"
            )
        )
    )

    if last_product_context is None:
        last_product_context = (
            _copy_state_value(
                client_context.get(
                    "product_context"
                )
            )
        )

    pending_action = (
        _safe_mapping(
            client_context.get(
                "pending_action"
            )
        )
    )

    if not pending_action:
        pending_action = None

    try:
        conversation_id = (
            get_api_conversation_id(
                required=False
            )
        )
    except Exception:
        conversation_id = None

    return _BrainRuntimeState(
        messages=(
            _normalize_client_history(
                history
            )
        ),
        conversation_context=conversation_context,
        last_product_context=last_product_context,
        pending_action=pending_action,
        conversation_id=conversation_id,
    )


def initialize_session() -> None:
    """
    API replacement for the old Streamlit initializer.

    A fresh state is installed on every process_message() call.
    """

    _BRAIN_RUNTIME_STATE.set(
        _new_runtime_state()
    )


def _runtime_state() -> _BrainRuntimeState:

    state = (
        _BRAIN_RUNTIME_STATE.get()
    )

    if state is None:
        state = (
            _new_runtime_state()
        )
        _BRAIN_RUNTIME_STATE.set(
            state
        )

    return state


def add_chat_message(
    role: str,
    content: str,
    *,
    metadata: Mapping[
        str,
        Any,
    ] | None = None,
) -> None:
    """
    Preserve the brain's previous chat-history behavior without Streamlit.
    """

    role = str(
        role
        or ""
    ).strip().lower()

    if role not in _ALLOWED_CHAT_ROLES:
        raise ValueError(
            "Chat role must be user or assistant."
        )

    if not isinstance(
        content,
        str,
    ):
        raise TypeError(
            "Chat message content must be text."
        )

    content = content.strip()

    if not content:
        raise ValueError(
            "Chat message cannot be empty."
        )

    message: dict[
        str,
        Any,
    ] = {
        "role":
            role,
        "content":
            content,
    }

    if isinstance(
        metadata,
        Mapping,
    ):
        message[
            "metadata"
        ] = (
            _copy_state_value(
                dict(
                    metadata
                )
            )
        )

    state = (
        _runtime_state()
    )

    state.messages.append(
        message
    )

    state.messages = (
        state.messages[
            -_history_limit():
        ]
    )


def get_model_chat_history() -> list[
    dict[
        str,
        str,
    ]
]:

    history: list[
        dict[
            str,
            str,
        ]
    ] = []

    for message in (
        _runtime_state()
        .messages
    ):

        role = message.get(
            "role"
        )
        content = message.get(
            "content"
        )

        if (
            role in _ALLOWED_CHAT_ROLES
            and isinstance(
                content,
                str,
            )
            and content.strip()
        ):
            history.append(
                {
                    "role":
                        str(
                            role
                        ),
                    "content":
                        content.strip(),
                }
            )

    return _copy_state_value(
        history
    )


def set_conversation_context(
    name_or_context: str | Mapping[
        str,
        Any,
    ],
    value: Any = _CONTEXT_VALUE_NOT_SET,
) -> None:
    """
    Preserve both calling styles from the former session manager.
    """

    state = (
        _runtime_state()
    )

    if (
        isinstance(
            name_or_context,
            Mapping,
        )
        and value
        is _CONTEXT_VALUE_NOT_SET
    ):
        state.conversation_context = (
            _safe_mapping(
                name_or_context
            )
        )
        return

    if (
        isinstance(
            name_or_context,
            str,
        )
        and name_or_context.strip()
        and value
        is not _CONTEXT_VALUE_NOT_SET
    ):
        context = (
            _safe_mapping(
                state.conversation_context
            )
        )
        context[
            name_or_context.strip()
        ] = (
            _copy_state_value(
                value
            )
        )
        state.conversation_context = (
            context
        )
        return

    raise TypeError(
        "Invalid conversation context update."
    )


def get_conversation_context(
    name: str | None = None,
    *,
    default: Any = None,
) -> Any:

    context = (
        _runtime_state()
        .conversation_context
    )

    if name is None:
        return _copy_state_value(
            context
        )

    return _copy_state_value(
        context.get(
            name,
            default,
        )
    )


def set_last_product_context(
    value: Any,
) -> None:

    _runtime_state().last_product_context = (
        _copy_state_value(
            value
        )
    )


def get_last_product_context() -> Any:

    return _copy_state_value(
        _runtime_state()
        .last_product_context
    )


def get_pending_action() -> dict[
    str,
    Any,
] | None:

    value = (
        _runtime_state()
        .pending_action
    )

    if not isinstance(
        value,
        dict,
    ):
        return None

    return _copy_state_value(
        value
    )


def export_brain_client_state() -> dict[
    str,
    Any,
]:
    """
    Safe state the frontend can send back in client_context next time.
    No access token, refresh token or API key is included.
    """

    state = (
        _runtime_state()
    )

    return {
        "conversation_id":
            state.conversation_id,
        "chat_history":
            get_model_chat_history(),
        "conversation_context":
            _copy_state_value(
                state.conversation_context
            ),
        "last_product_context":
            _copy_state_value(
                state.last_product_context
            ),
        "pending_action":
            _copy_state_value(
                state.pending_action
            ),
    }


# ============================================================
# LIMITS
# ============================================================

# Enough for:
#
# search
#   ↓
# resolve SKU
#   ↓
# cart action
#
# But prevents Cohere from wandering indefinitely.

MAX_TOOL_ROUNDS = 3

MAX_TOOL_CALLS_PER_ROUND = 4

MAX_TOTAL_TOOL_CALLS = 8


# Context sent to Cohere.
MAX_CONTEXT_JSON_LENGTH = 4_000

MAX_COHERE_TEXT_LENGTH = 1_200

MAX_USER_MESSAGE_LENGTH = 20_000


# ============================================================
# GROQ CONTEXT LIMITS
# ============================================================

MAX_GROQ_RESULTS = 4

MAX_PRODUCTS_FOR_GROQ = 5

MAX_VARIANTS_FOR_GROQ = 5

MAX_ALTERNATIVES_FOR_GROQ = 4

MAX_GENERIC_LIST_FOR_GROQ = 6

MAX_STRING_FOR_GROQ = 500


# ============================================================
# PER-TOOL EXECUTION BUDGET
# ============================================================
#
# This is NOT intent classification.
#
# Cohere already selected the tool.
#
# These limits only protect the backend from repeated calls.
# ============================================================

TOOL_CALL_BUDGETS: dict[
    str,
    int,
] = {

    # Direct product search can run twice because:
    #
    # "show milk and bread"
    #
    # may legitimately require two searches.
    "search_products":
        2,

    # Semantic search already performs retrieval + live hydration.
    "semantic_product_search":
        1,

    "get_catalog_stats":
        1,

    "list_catalog_brands":
        1,

    "list_catalog_categories":
        1,

    "get_cart":
        1,

    "get_cart_summary":
        1,

    "get_my_orders":
        1,

    "get_latest_order":
        1,

    "get_order_count":
        1,

    "get_order_history_summary":
        1,

    "get_order_status":
        1,

    "clear_cart":
        1,
}


# ============================================================
# REDUNDANT DISCOVERY TOOLS
# ============================================================

CATALOG_METADATA_TOOLS = {
    "get_catalog_stats",
    "list_catalog_brands",
    "list_catalog_categories",
}


DISCOVERY_TOOLS = {
    "search_products",
    "semantic_product_search",
}


# ============================================================
# COHERE GUIDANCE
# ============================================================

ROUTING_GUIDANCE = """
TOOL SELECTION RULES
====================

You are the reasoning and tool-selection brain of a grocery shopping
assistant.

You do NOT need to use a tool for every request.


GENERAL CONVERSATION
--------------------

Do NOT use tools when current store/user data is unnecessary.

Examples:

"hi"
    -> NO TOOL

"what is protein?"
    -> NO TOOL

"what are carbohydrates?"
    -> NO TOOL

"what is paneer?"
    -> NO TOOL

"give me a simple breakfast recipe"
    -> NO TOOL

"how do I make tea?"
    -> NO TOOL


DIRECT CATALOG SEARCH
---------------------

Use search_products when the user asks whether the STORE carries,
contains or sells a named product.

Examples:

"do you have milk?"
    -> search_products(
           query="milk",
           stock_state="any"
       )

"show milk"
    -> search_products(
           query="milk",
           stock_state="any"
       )

"I need Amul milk"
    -> search_products(
           query="Amul milk",
           stock_state="any"
       )

"show oats under 200"
    -> search_products(
           query="oats",
           max_price=200,
           stock_state="any"
       )


IMPORTANT STOCK RULE
--------------------

For normal catalog search:

    stock_state="any"

Out-of-stock products still exist in the catalog and MUST be returned.

Example:

Milk exists with stock=0.

Correct:
    "Milk exists but is currently out of stock."

Incorrect:
    "We don't have milk."


Only use:

    stock_state="in_stock"

when the user explicitly asks for only currently available/purchasable
products.


CHEAPEST / MOST EXPENSIVE PRODUCT
---------------------------------

For the cheapest currently purchasable product:

    search_products(
        stock_state="in_stock",
        sort_by="price",
        sort_order="asc",
        limit=1
    )

For the most expensive currently purchasable product:

    search_products(
        stock_state="in_stock",
        sort_by="price",
        sort_order="desc",
        limit=1
    )

A query string is NOT required for catalog-wide ranking.


SEMANTIC PRODUCT RECOMMENDATIONS
--------------------------------

Use semantic_product_search when the user wants a PRODUCT according to
purpose, meal context, suitability or meaning.

Examples:

"I need something good for breakfast"
    -> semantic_product_search

"recommend a healthy snack"
    -> semantic_product_search

"something for tea time"
    -> semantic_product_search

"something for smoothies"
    -> semantic_product_search

"something useful for baking"
    -> semantic_product_search

"party snacks under 100"
    -> semantic_product_search(max_price=100)


Semantic recommendations the user intends to buy should normally use:

    in_stock_only=true


GENERAL IDEA VS PRODUCT RECOMMENDATION
--------------------------------------

"give me breakfast ideas"
    -> NO TOOL

"I need something good for breakfast"
    -> semantic_product_search

"give me a breakfast product"
    -> semantic_product_search


DIRECT NAME VS SEMANTIC
-----------------------

"show milk"
    -> search_products

"something I can drink for breakfast"
    -> semantic_product_search


CART
----

"what is in my cart?"
    -> get_cart

"cart total"
    -> get_cart_summary

"clear my cart"
    -> clear_cart


CART MUTATION
-------------

Cart mutations require an exact SKU.

If the user says:

"add milk"

and multiple variants exist:

    DO NOT guess.

Search/resolve the product and ask the user which size if necessary.

If the exact SKU is known:

    use add_to_cart.


ORDERS
------

"show my orders"
    -> get_my_orders

"latest order"
    -> get_latest_order(
           include_items=true
       )

"what was my last order?"
    -> get_latest_order(
           include_items=true
       )

"what did I order last time?"
    -> get_latest_order(
           include_items=true
       )

"latest order status"
    -> get_latest_order(
           include_items=false
       )

"how many orders do I have?"
    -> get_order_count


EFFICIENCY
----------

One successful authoritative product search is normally enough.

After search_products successfully returns the requested product:

DO NOT call:

    get_catalog_stats
    list_catalog_categories
    list_catalog_brands

just to verify the result.


semantic_product_search already:

    searches product_descriptions
    +
    hydrates real products
    +
    returns live SKU/price/stock

Therefore do NOT call search_products afterward merely to re-check the
same recommendation.


STOPPING
--------

Once enough backend data exists to answer the request:

    STOP CALLING TOOLS.

Groq will create the final wording.


TRUTH
-----

Never invent:

    price
    stock
    SKU
    variant
    cart contents
    cart total
    order information
    order status

Those must come from backend tools.
""".strip()


# ============================================================
# EXCEPTIONS
# ============================================================


class BrainError(
    RuntimeError
):
    pass


class BrainValidationError(
    BrainError
):
    pass


class BrainToolError(
    BrainError
):
    pass


# ============================================================
# TOOL RECORD
# ============================================================


@dataclass(
    slots=True
)
class ToolExecutionRecord:

    tool_call_id: str

    tool_name: str

    arguments: dict[
        str,
        Any,
    ]

    result: dict[
        str,
        Any,
    ]

    round_number: int

    @property
    def success(
        self,
    ) -> bool:

        return bool(
            self.result.get(
                "success"
            )
        )

    def safe_dict(
        self,
    ) -> dict[
        str,
        Any,
    ]:

        return {
            "tool_name":
                self.tool_name,

            "arguments":
                self.arguments,

            "success":
                self.success,

            "result":
                self.result,
        }


# ============================================================
# BRAIN RESPONSE
# ============================================================


@dataclass(
    slots=True
)
class BrainResponse:

    text: str

    success: bool = True

    tool_executions: list[
        ToolExecutionRecord
    ] = field(
        default_factory=list
    )

    cohere_text: str | None = None

    rounds: int = 0

    error_code: str | None = None

    # Structured verified response used by API/frontends for:
    #
    # - product images
    # - SKU / variant cards
    # - cart cards
    # - order cards
    #
    # Generated response text and structured commerce data remain separate.
    response_payload: ResponsePayload | None = None

    # Updated safe conversational state for stateless API clients.
    client_state: dict[
        str,
        Any,
    ] = field(
        default_factory=dict
    )

    def __post_init__(
        self,
    ) -> None:

        if self.client_state:
            self.client_state = (
                _copy_state_value(
                    self.client_state
                )
            )
            return

        try:
            self.client_state = (
                export_brain_client_state()
            )
        except Exception:
            logger.debug(
                "Unable to export API conversation state.",
                exc_info=True,
            )
            self.client_state = {}

    @property
    def used_tools(
        self,
    ) -> bool:

        return bool(
            self.tool_executions
        )

    @property
    def tool_names(
        self,
    ) -> list[str]:

        return list(
            dict.fromkeys(
                execution.tool_name
                for execution
                in self.tool_executions
            )
        )

    @property
    def has_structured_response(
        self,
    ) -> bool:
        """
        True when a database-backed request produced UI-renderable
        structured content.
        """

        return (
            self.response_payload is not None
            and self.response_payload.has_structured_content()
        )


# ============================================================
# VALIDATE USER MESSAGE
# ============================================================


def _validate_user_message(
    user_message: Any,
) -> str:

    if not isinstance(
        user_message,
        str,
    ):

        raise BrainValidationError(
            "User message must be text."
        )

    user_message = (
        user_message.strip()
    )

    if not user_message:

        raise BrainValidationError(
            "User message cannot be empty."
        )

    if (
        len(
            user_message
        )
        > MAX_USER_MESSAGE_LENGTH
    ):

        raise BrainValidationError(
            "User message is too long."
        )

    return user_message


# ============================================================
# TOOL DEFINITIONS
# ============================================================


def get_all_tool_schemas() -> list[
    dict[
        str,
        Any,
    ]
]:

    return (
        list(
            get_product_tool_schemas()
        )
        +
        list(
            get_cart_tool_schemas()
        )
        +
        list(
            get_order_tool_schemas()
        )
        +
        list(
            get_rag_tool_schemas()
        )
    )


def get_all_tool_names() -> set[str]:

    return (
        set(
            get_product_tool_names()
        )
        |
        set(
            get_cart_tool_names()
        )
        |
        set(
            get_order_tool_names()
        )
        |
        set(
            get_rag_tool_names()
        )
    )


# ============================================================
# EXECUTE APPROVED TOOL
# ============================================================


def execute_tool(
    *,
    tool_name: str,
    arguments: Mapping[
        str,
        Any,
    ] | None,
) -> dict[
    str,
    Any,
]:

    tool_name = (
        str(
            tool_name
            or ""
        )
        .strip()
    )

    if not tool_name:

        raise BrainToolError(
            "Tool name cannot be empty."
        )

    if (
        tool_name
        in get_product_tool_names()
    ):

        return execute_product_tool(
            tool_name=tool_name,
            arguments=arguments,
        )

    if (
        tool_name
        in get_cart_tool_names()
    ):

        return execute_cart_tool(
            tool_name=tool_name,
            arguments=arguments,
        )

    if (
        tool_name
        in get_order_tool_names()
    ):

        return execute_order_tool(
            tool_name=tool_name,
            arguments=arguments,
        )

    if (
        tool_name
        in get_rag_tool_names()
    ):

        return execute_rag_tool(
            tool_name=tool_name,
            arguments=arguments,
        )

    raise BrainToolError(
        f"Unsupported backend tool: {tool_name}"
    )


# ============================================================
# JSON
# ============================================================


def _json_default(
    value: Any,
) -> Any:

    if hasattr(
        value,
        "model_dump",
    ):

        try:

            return value.model_dump()

        except Exception:

            pass

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


def _json_dumps(
    value: Any,
) -> str:

    return json.dumps(
        value,
        ensure_ascii=False,
        default=_json_default,
        separators=(
            ",",
            ":",
        ),
    )


# ============================================================
# SAFE CONTEXT
# ============================================================


def _safe_context_json(
    value: Any,
) -> str:

    if value is None:

        return "null"

    try:

        text = json.dumps(
            value,
            ensure_ascii=False,
            default=_json_default,
            separators=(
                ",",
                ":",
            ),
        )

    except Exception:

        return "{}"

    if (
        len(
            text
        )
        > MAX_CONTEXT_JSON_LENGTH
    ):

        return (
            text[
                :MAX_CONTEXT_JSON_LENGTH
            ]
            + "..."
        )

    return text


# ============================================================
# RUNTIME CONTEXT
# ============================================================


def _build_runtime_system_context() -> str:

    try:

        conversation_context = (
            get_conversation_context()
        )

    except Exception:

        conversation_context = None

    try:

        product_context = (
            get_last_product_context()
        )

    except Exception:

        product_context = None

    try:

        pending_action = (
            get_pending_action()
        )

    except Exception:

        pending_action = None

    return f"""
CURRENT APPLICATION CONTEXT
===========================

Conversation:
{_safe_context_json(conversation_context)}

Recent product context:
{_safe_context_json(product_context)}

Pending unresolved action:
{_safe_context_json(pending_action)}

{ROUTING_GUIDANCE}
""".strip()


# ============================================================
# TOOL CALL FINGERPRINT
# ============================================================


def _tool_call_fingerprint(
    *,
    tool_name: str,
    arguments: Mapping[
        str,
        Any,
    ],
) -> str:

    try:

        args = json.dumps(
            arguments,
            sort_keys=True,
            ensure_ascii=False,
            default=_json_default,
            separators=(
                ",",
                ":",
            ),
        )

    except Exception:

        args = str(
            arguments
        )

    return (
        f"{tool_name}:{args}"
    )


# ============================================================
# TOOL ARGUMENTS
# ============================================================


def _tool_arguments(
    call: CohereToolCall,
) -> dict[
    str,
    Any,
]:

    arguments = (
        call.arguments
    )

    if arguments is None:

        return {}

    if not isinstance(
        arguments,
        Mapping,
    ):

        raise BrainToolError(
            "Cohere returned invalid tool arguments."
        )

    return dict(
        arguments
    )


# ============================================================
# COHERE ASSISTANT TOOL MESSAGE
# ============================================================


def _assistant_tool_message(
    response: CohereBrainResponse,
) -> dict[
    str,
    Any,
]:

    tool_calls: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for call in (
        response.tool_calls
        or []
    ):

        arguments = (
            call.arguments
            if isinstance(
                call.arguments,
                Mapping,
            )
            else {}
        )

        tool_calls.append(
            {
                "id":
                    str(
                        call.id
                        or ""
                    ),

                "type":
                    str(
                        call.type
                        or "function"
                    ),

                "function": {
                    "name":
                        str(
                            call.name
                            or ""
                        ),

                    "arguments":
                        _json_dumps(
                            dict(
                                arguments
                            )
                        ),
                },
            }
        )

    message: dict[
        str,
        Any,
    ] = {
        "role":
            "assistant",

        "content":
            "",
    }

    if tool_calls:

        message[
            "tool_calls"
        ] = tool_calls

    if (
        isinstance(
            response.text,
            str,
        )
        and response.text.strip()
    ):

        message[
            "content"
        ] = (
            response.text.strip()
        )

    return message


# ============================================================
# COHERE TOOL RESULT MESSAGE
# ============================================================


def _tool_result_message(
    *,
    tool_call_id: str,
    tool_name: str,
    result: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

    return {
        "role":
            "tool",

        "tool_call_id":
            tool_call_id,

        "content": [
            {
                "type":
                    "document",

                "document": {
                    "data":
                        _json_dumps(
                            {
                                "tool":
                                    tool_name,

                                "result":
                                    dict(
                                        result
                                    ),
                            }
                        ),
                },
            }
        ],
    }


# ============================================================
# FAILURE RESULT
# ============================================================


def _safe_tool_failure(
    *,
    tool_name: str,
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
            "python",

        "domain":
            "tool_execution",

        "tool":
            tool_name,

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
# TOOL COUNT
# ============================================================


def _tool_execution_count(
    executions: Sequence[
        ToolExecutionRecord
    ],
    tool_name: str,
) -> int:

    return sum(
        1
        for execution
        in executions
        if execution.tool_name
        == tool_name
    )


# ============================================================
# PRIOR SUCCESS
# ============================================================


def _has_successful_tool(
    executions: Sequence[
        ToolExecutionRecord
    ],
    tool_name: str,
) -> bool:

    return any(
        execution.success
        and execution.tool_name
        == tool_name
        for execution
        in executions
    )


# ============================================================
# REDUNDANT TOOL DETECTION
# ============================================================


def _is_redundant_tool_call(
    *,
    tool_name: str,
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> bool:
    """
    Prevent useless catalog verification loops.

    This examines previously executed tool RESULTS.

    It does not inspect or classify the user's natural language.
    """

    # ========================================================
    # PRODUCT SEARCH ALREADY SUCCEEDED
    # ========================================================

    if (
        tool_name
        in CATALOG_METADATA_TOOLS
        and _has_successful_tool(
            executions,
            "search_products",
        )
    ):

        return True

    # ========================================================
    # RAG ALREADY PERFORMED DISCOVERY + LIVE HYDRATION
    # ========================================================

    if (
        _has_successful_tool(
            executions,
            "semantic_product_search",
        )
        and tool_name
        in (
            CATALOG_METADATA_TOOLS
            |
            DISCOVERY_TOOLS
        )
    ):

        return True

    return False


# ============================================================
# TOOL BUDGET CHECK
# ============================================================


def _tool_budget_exhausted(
    *,
    tool_name: str,
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> bool:

    budget = (
        TOOL_CALL_BUDGETS.get(
            tool_name
        )
    )

    if budget is None:

        return False

    return (
        _tool_execution_count(
            executions,
            tool_name,
        )
        >= budget
    )


# ============================================================
# SESSION SETTER
# ============================================================


def _safe_session_setter(
    setter: Any,
    payload: Mapping[
        str,
        Any,
    ],
) -> None:
    """
    Invoke session setter defensively.

    Supports either:

        setter(payload)

    or a keyword-only single parameter.

    This helps avoid noisy session-context warnings when helper
    signatures differ slightly.
    """

    try:

        signature = (
            inspect.signature(
                setter
            )
        )

        parameters = list(
            signature.parameters.values()
        )

        usable = [
            parameter
            for parameter
            in parameters
            if parameter.kind
            not in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }
        ]

        if (
            len(
                usable
            )
            == 1
            and usable[
                0
            ].kind
            == inspect.Parameter.KEYWORD_ONLY
        ):

            setter(
                **{
                    usable[
                        0
                    ].name:
                        dict(
                            payload
                        )
                }
            )

            return

        setter(
            dict(
                payload
            )
        )

    except Exception:

        # Context storage is useful but must never break the chatbot.
        logger.debug(
            "Optional session context update failed.",
            exc_info=True,
        )


# ============================================================
# PRODUCT CONTEXT
# ============================================================


def _update_product_context(
    record: ToolExecutionRecord,
) -> None:

    if not record.success:

        return

    data = (
        record.result.get(
            "data"
        )
    )

    if not isinstance(
        data,
        Mapping,
    ):

        return

    # ========================================================
    # PRODUCT SEARCH
    # ========================================================

    products = (
        data.get(
            "products"
        )
    )

    if (
        isinstance(
            products,
            Sequence,
        )
        and not isinstance(
            products,
            (
                str,
                bytes,
            ),
        )
    ):

        safe_products = [
            dict(
                product
            )
            for product
            in products[
                :5
            ]
            if isinstance(
                product,
                Mapping,
            )
        ]

        if safe_products:

            _safe_session_setter(
                set_last_product_context,
                {
                    "source_tool":
                        record.tool_name,

                    "products":
                        safe_products,
                },
            )

            return

    # ========================================================
    # SINGLE PRODUCT
    # ========================================================

    product = (
        data.get(
            "product"
        )
    )

    if isinstance(
        product,
        Mapping,
    ):

        _safe_session_setter(
            set_last_product_context,
            {
                "source_tool":
                    record.tool_name,

                "product":
                    dict(
                        product
                    ),
            },
        )

        return

    # ========================================================
    # VARIANT
    # ========================================================

    variant = (
        data.get(
            "variant"
        )
    )

    if isinstance(
        variant,
        Mapping,
    ):

        _safe_session_setter(
            set_last_product_context,
            {
                "source_tool":
                    record.tool_name,

                "variant":
                    dict(
                        variant
                    ),
            },
        )

        return

    # ========================================================
    # RAG
    # ========================================================

    matches = (
        data.get(
            "matches"
        )
    )

    if (
        isinstance(
            matches,
            Sequence,
        )
        and not isinstance(
            matches,
            (
                str,
                bytes,
            ),
        )
    ):

        safe_matches = [
            dict(
                match
            )
            for match
            in matches[
                :5
            ]
            if isinstance(
                match,
                Mapping,
            )
        ]

        if safe_matches:

            _safe_session_setter(
                set_last_product_context,
                {
                    "source_tool":
                        record.tool_name,

                    "semantic_matches":
                        safe_matches,
                },
            )


# ============================================================
# CONVERSATION CONTEXT
# ============================================================


def _update_conversation_context(
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> None:

    if not executions:

        return

    latest = (
        executions[
            -1
        ]
    )

    _safe_session_setter(
        set_conversation_context,
        {
            "last_tool":
                latest.tool_name,

            "last_tool_success":
                latest.success,

            "recent_tools": [
                execution.tool_name
                for execution
                in executions[
                    -4:
                ]
            ],
        },
    )


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

    keys = (
        "sku_id",
        "size",
        "pack_size",
        "quantity",
        "unit",
        "price",
        "mrp",
        "stock",
        "in_stock",
        "currency",
        "image_url",
    )

    return {
        key:
            variant.get(
                key
            )
        for key in keys
        if key in variant
    }


# ============================================================
# COMPACT PRODUCT
# ============================================================


def _compact_product(
    product: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

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
        for variant
        in variants[
            :MAX_VARIANTS_FOR_GROQ
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

        "name":
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

        "rating":
            product.get(
                "rating"
            ),

        "variant_count":
            product.get(
                "variant_count"
            ),

        "available_variant_count":
            product.get(
                "available_variant_count"
            ),

        "is_available":
            product.get(
                "is_available"
            ),

        "all_variants_out_of_stock":
            product.get(
                "all_variants_out_of_stock"
            ),

        "price_from":
            product.get(
                "price_from"
            ),

        "price_to":
            product.get(
                "price_to"
            ),

        "variants":
            compact_variants,
    }


# ============================================================
# GENERIC COMPACTION
# ============================================================


def _compact_for_groq(
    value: Any,
    *,
    depth: int = 0,
    parent_key: str | None = None,
) -> Any:

    if depth > 5:

        return (
            str(
                value
            )[
                :MAX_STRING_FOR_GROQ
            ]
        )

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

        return (
            value
            if len(
                value
            )
            <= MAX_STRING_FOR_GROQ
            else (
                value[
                    :MAX_STRING_FOR_GROQ
                ]
                + "..."
            )
        )

    if isinstance(
        value,
        Mapping,
    ):

        excluded = {
            "raw_response",
            "embedding",
            "vector",
            "rag_text",
            "debug",
            "internal_debug",
            "description",
            "short_description",
            "specs",
            "features",
            "created_at",
            "updated_at",
            "tool_call_id",
            "round_number",
        }

        compact: dict[
            str,
            Any,
        ] = {}

        for (
            key,
            item,
        ) in value.items():

            key = str(
                key
            )

            if key in excluded:

                continue

            compact[
                key
            ] = (
                _compact_for_groq(
                    item,
                    depth=(
                        depth + 1
                    ),
                    parent_key=key,
                )
            )

        return compact

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

        if parent_key == "products":

            limit = (
                MAX_PRODUCTS_FOR_GROQ
            )

        elif parent_key == "variants":

            limit = (
                MAX_VARIANTS_FOR_GROQ
            )

        elif parent_key == "alternatives":

            limit = (
                MAX_ALTERNATIVES_FOR_GROQ
            )

        else:

            limit = (
                MAX_GENERIC_LIST_FOR_GROQ
            )

        return [
            _compact_for_groq(
                item,
                depth=(
                    depth + 1
                ),
            )
            for item
            in list(
                value
            )[
                :limit
            ]
        ]

    return (
        str(
            value
        )[
            :MAX_STRING_FOR_GROQ
        ]
    )


# ============================================================
# COMPACT PRODUCT SEARCH RESULT
# ============================================================


def _compact_search_result(
    data: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

    products = [
        _compact_product(
            product
        )
        for product
        in (
            data.get(
                "products"
            )
            or []
        )[
            :MAX_PRODUCTS_FOR_GROQ
        ]
        if isinstance(
            product,
            Mapping,
        )
    ]

    alternatives = [
        _compact_product(
            product
        )
        for product
        in (
            data.get(
                "alternatives"
            )
            or []
        )[
            :MAX_ALTERNATIVES_FOR_GROQ
        ]
        if isinstance(
            product,
            Mapping,
        )
    ]

    return {
        "status":
            data.get(
                "status"
            ),

        "query":
            data.get(
                "query"
            ),

        "found":
            data.get(
                "found"
            ),

        "product_count":
            data.get(
                "product_count"
            ),

        "available_product_count":
            data.get(
                "available_product_count"
            ),

        "unavailable_product_count":
            data.get(
                "unavailable_product_count"
            ),

        "all_matches_out_of_stock":
            data.get(
                "all_matches_out_of_stock"
            ),

        "response_hint":
            data.get(
                "response_hint"
            ),

        "products":
            products,

        "alternatives":
            alternatives,
    }


# ============================================================
# COMPACT RAG RESULT
# ============================================================


def _compact_rag_result(
    data: Mapping[
        str,
        Any,
    ],
) -> dict[
    str,
    Any,
]:

    matches = []

    for match in (
        data.get(
            "matches"
        )
        or []
    )[
        :MAX_PRODUCTS_FOR_GROQ
    ]:

        if not isinstance(
            match,
            Mapping,
        ):

            continue

        matches.append(
            _compact_for_groq(
                match
            )
        )

    unavailable = []

    for match in (
        data.get(
            "unavailable_matches"
        )
        or []
    )[
        :3
    ]:

        if not isinstance(
            match,
            Mapping,
        ):

            continue

        unavailable.append(
            _compact_for_groq(
                match
            )
        )

    return {
        "status":
            data.get(
                "status"
            ),

        "query":
            data.get(
                "query"
            ),

        "match_count":
            data.get(
                "match_count"
            ),

        "unavailable_match_count":
            data.get(
                "unavailable_match_count"
            ),

        "matches":
            matches,

        "unavailable_matches":
            unavailable,
    }


# ============================================================
# GROQ CONTEXT
# ============================================================


def _build_groq_context(
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> tuple[
    list[
        dict[
            str,
            Any,
        ]
    ],
    list[
        dict[
            str,
            Any,
        ]
    ],
]:

    rag_tools = set(
        get_rag_tool_names()
    )

    # ========================================================
    # DISTINCT EXECUTIONS
    # ========================================================

    distinct: list[
        ToolExecutionRecord
    ] = []

    seen: set[
        str
    ] = set()

    for execution in reversed(
        executions
    ):

        fingerprint = (
            _tool_call_fingerprint(
                tool_name=(
                    execution.tool_name
                ),
                arguments=(
                    execution.arguments
                ),
            )
        )

        if fingerprint in seen:

            continue

        seen.add(
            fingerprint
        )

        distinct.append(
            execution
        )

        if (
            len(
                distinct
            )
            >= MAX_GROQ_RESULTS
        ):

            break

    distinct.reverse()

    verified: list[
        dict[
            str,
            Any,
        ]
    ] = []

    rag: list[
        dict[
            str,
            Any,
        ]
    ] = []

    for execution in distinct:

        result = (
            execution.result
        )

        item: dict[
            str,
            Any,
        ] = {
            "tool":
                execution.tool_name,

            "success":
                execution.success,
        }

        if isinstance(
            result,
            Mapping,
        ):

            data = (
                result.get(
                    "data"
                )
            )

            if isinstance(
                data,
                Mapping,
            ):

                if (
                    execution.tool_name
                    == "search_products"
                ):

                    item[
                        "data"
                    ] = (
                        _compact_search_result(
                            data
                        )
                    )

                elif (
                    execution.tool_name
                    in rag_tools
                ):

                    item[
                        "data"
                    ] = (
                        _compact_rag_result(
                            data
                        )
                    )

                else:

                    item[
                        "data"
                    ] = (
                        _compact_for_groq(
                            data
                        )
                    )

            elif data is not None:

                item[
                    "data"
                ] = (
                    _compact_for_groq(
                        data
                    )
                )

            if result.get(
                "message"
            ):

                item[
                    "message"
                ] = str(
                    result[
                        "message"
                    ]
                )[
                    :500
                ]

            if result.get(
                "error"
            ):

                item[
                    "error"
                ] = (
                    _compact_for_groq(
                        result[
                            "error"
                        ]
                    )
                )

        if (
            execution.tool_name
            in rag_tools
        ):

            rag.append(
                item
            )

        else:

            verified.append(
                item
            )

    return (
        verified,
        rag,
    )


# ============================================================
# BACKEND ACTION
# ============================================================


def _build_backend_action(
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> str:

    names = list(
        dict.fromkeys(
            execution.tool_name
            for execution
            in executions
        )
    )

    if not names:

        return (
            "direct_conversation"
        )

    return ", ".join(
        names
    )


# ============================================================
# COHERE TEXT
# ============================================================


def _clean_cohere_text(
    value: Any,
) -> str | None:

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

    if (
        len(
            value
        )
        > MAX_COHERE_TEXT_LENGTH
    ):

        return (
            value[
                :MAX_COHERE_TEXT_LENGTH
            ]
            + "..."
        )

    return value


# ============================================================
# COHERE TOOL LOOP
# ============================================================


def _run_cohere_tool_loop(
    *,
    user_message: str,
    chat_history: Sequence[
        Mapping[
            str,
            Any,
        ]
    ] | None,
) -> tuple[
    list[
        ToolExecutionRecord
    ],
    str | None,
    int,
]:

    tools = (
        get_all_tool_schemas()
    )

    messages = (
        build_messages(
            user_message=user_message,
            chat_history=chat_history,
            include_system_rules=True,
            additional_system_context=(
                _build_runtime_system_context()
            ),
        )
    )

    executions: list[
        ToolExecutionRecord
    ] = []

    fingerprints: set[
        str
    ] = set()

    total_calls = 0

    cohere_text: (
        str
        | None
    ) = None

    # ========================================================
    # ROUNDS
    # ========================================================

    for round_number in range(
        1,
        MAX_TOOL_ROUNDS + 1,
    ):

        response = (
            chat_with_messages(
                messages=messages,
                tools=tools,
                strict_tools=False,
            )
        )

        cohere_text = (
            _clean_cohere_text(
                response.text
            )
        )

        calls = list(
            response.tool_calls
            or []
        )

        # ====================================================
        # NO TOOL → DONE
        # ====================================================

        if not calls:

            return (
                executions,
                cohere_text,
                round_number,
            )

        if (
            len(
                calls
            )
            > MAX_TOOL_CALLS_PER_ROUND
        ):

            logger.warning(
                "Cohere requested too many tools in one round."
            )

            return (
                executions,
                cohere_text,
                round_number,
            )

        # ====================================================
        # CHECK IF ENTIRE NEW ROUND IS REDUNDANT
        # ====================================================

        all_redundant = True

        for call in calls:

            tool_name = str(
                call.name
                or ""
            ).strip()

            if (
                not _is_redundant_tool_call(
                    tool_name=tool_name,
                    executions=executions,
                )
                and not _tool_budget_exhausted(
                    tool_name=tool_name,
                    executions=executions,
                )
            ):

                all_redundant = False

                break

        if (
            all_redundant
            and executions
        ):

            logger.info(
                "Stopping redundant Cohere tool round."
            )

            return (
                executions,
                cohere_text,
                round_number,
            )

        # Must send Cohere's assistant tool-call message before
        # corresponding tool results.

        messages.append(
            _assistant_tool_message(
                response
            )
        )

        executed_this_round = 0

        # ====================================================
        # EXECUTE CALLS
        # ====================================================

        for call in calls:

            total_calls += 1

            if (
                total_calls
                > MAX_TOTAL_TOOL_CALLS
            ):

                logger.warning(
                    "Maximum total tool calls reached."
                )

                return (
                    executions,
                    cohere_text,
                    round_number,
                )

            tool_name = str(
                call.name
                or ""
            ).strip()

            tool_call_id = str(
                call.id
                or ""
            ).strip()

            if not tool_call_id:

                continue

            try:

                arguments = (
                    _tool_arguments(
                        call
                    )
                )

            except BrainToolError:

                result = (
                    _safe_tool_failure(
                        tool_name=(
                            tool_name
                            or "unknown"
                        ),
                        code=(
                            "INVALID_TOOL_ARGUMENTS"
                        ),
                        message=(
                            "The tool received invalid arguments."
                        ),
                    )
                )

                messages.append(
                    _tool_result_message(
                        tool_call_id=tool_call_id,
                        tool_name=(
                            tool_name
                            or "unknown"
                        ),
                        result=result,
                    )
                )

                continue

            # =================================================
            # ALLOW LIST
            # =================================================

            if (
                tool_name
                not in get_all_tool_names()
            ):

                result = (
                    _safe_tool_failure(
                        tool_name=(
                            tool_name
                            or "unknown"
                        ),
                        code=(
                            "UNKNOWN_TOOL"
                        ),
                        message=(
                            "That backend capability is unavailable."
                        ),
                    )
                )

                messages.append(
                    _tool_result_message(
                        tool_call_id=tool_call_id,
                        tool_name=(
                            tool_name
                            or "unknown"
                        ),
                        result=result,
                    )
                )

                continue

            fingerprint = (
                _tool_call_fingerprint(
                    tool_name=tool_name,
                    arguments=arguments,
                )
            )

            # =================================================
            # EXACT DUPLICATE
            # =================================================

            if fingerprint in fingerprints:

                logger.info(
                    "Stopping duplicate tool call. tool=%s",
                    tool_name,
                )

                return (
                    executions,
                    cohere_text,
                    round_number,
                )

            # =================================================
            # REDUNDANT CALL
            # =================================================

            if (
                _is_redundant_tool_call(
                    tool_name=tool_name,
                    executions=executions,
                )
            ):

                skipped_result = {
                    "success":
                        True,

                    "source":
                        "python",

                    "domain":
                        "orchestration",

                    "tool":
                        tool_name,

                    "data": {
                        "skipped":
                            True,

                        "reason":
                            (
                                "Sufficient authoritative "
                                "data was already retrieved."
                            ),
                    },
                }

                messages.append(
                    _tool_result_message(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        result=skipped_result,
                    )
                )

                continue

            # =================================================
            # TOOL BUDGET
            # =================================================

            if (
                _tool_budget_exhausted(
                    tool_name=tool_name,
                    executions=executions,
                )
            ):

                skipped_result = {
                    "success":
                        True,

                    "source":
                        "python",

                    "domain":
                        "orchestration",

                    "tool":
                        tool_name,

                    "data": {
                        "skipped":
                            True,

                        "reason":
                            (
                                "The tool has already been "
                                "executed enough times."
                            ),
                    },
                }

                messages.append(
                    _tool_result_message(
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                        result=skipped_result,
                    )
                )

                continue

            # =================================================
            # EXECUTE REAL TOOL
            # =================================================

            try:

                result = (
                    execute_tool(
                        tool_name=tool_name,
                        arguments=arguments,
                    )
                )

            except BrainToolError:

                result = (
                    _safe_tool_failure(
                        tool_name=tool_name,
                        code=(
                            "TOOL_EXECUTION_REJECTED"
                        ),
                        message=(
                            "The backend operation was rejected."
                        ),
                    )
                )

            except Exception:

                logger.exception(
                    "Backend tool failed. tool=%s",
                    tool_name,
                )

                result = (
                    _safe_tool_failure(
                        tool_name=tool_name,
                        code=(
                            "TOOL_EXECUTION_ERROR"
                        ),
                        message=(
                            "The backend operation could not "
                            "be completed."
                        ),
                    )
                )

            if not isinstance(
                result,
                Mapping,
            ):

                result = (
                    _safe_tool_failure(
                        tool_name=tool_name,
                        code=(
                            "INVALID_TOOL_RESULT"
                        ),
                        message=(
                            "The backend returned an invalid result."
                        ),
                    )
                )

            result_dict = dict(
                result
            )

            record = (
                ToolExecutionRecord(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=result_dict,
                    round_number=round_number,
                )
            )

            executions.append(
                record
            )

            fingerprints.add(
                fingerprint
            )

            executed_this_round += 1

            # =================================================
            # SAVE PRODUCT CONTEXT
            # =================================================

            _update_product_context(
                record
            )

            # =================================================
            # RETURN TOOL RESULT TO COHERE
            # =================================================

            messages.append(
                _tool_result_message(
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    result=result_dict,
                )
            )

        # If Cohere only asked for redundant/budget-blocked calls,
        # stop instead of making another expensive model round.

        if (
            executed_this_round
            == 0
            and executions
        ):

            return (
                executions,
                cohere_text,
                round_number,
            )

    logger.info(
        "Maximum Cohere rounds reached; using collected results."
    )

    return (
        executions,
        cohere_text,
        MAX_TOOL_ROUNDS,
    )


# ============================================================
# MONEY FORMATTER
# ============================================================


def _format_price(
    price: Any,
    currency: Any = None,
) -> str:

    if price is None:

        return ""

    try:

        number = float(
            price
        )

        if number.is_integer():

            value = str(
                int(
                    number
                )
            )

        else:

            value = (
                f"{number:.2f}"
                .rstrip(
                    "0"
                )
                .rstrip(
                    "."
                )
            )

    except (
        TypeError,
        ValueError,
    ):

        value = str(
            price
        )

    currency = str(
        currency
        or ""
    ).strip().upper()

    if currency == "INR":

        return (
            "₹"
            + value
        )

    if currency:

        return (
            f"{currency} {value}"
        )

    return value


# ============================================================
# PRODUCT LINE
# ============================================================


def _product_fallback_lines(
    product: Mapping[
        str,
        Any,
    ],
) -> list[str]:

    name = str(
        product.get(
            "name"
        )
        or product.get(
            "product_name"
        )
        or "Product"
    )

    brand = str(
        product.get(
            "brand"
        )
        or ""
    )

    title = (
        f"{brand} {name}"
        if (
            brand
            and brand.lower()
            not in name.lower()
        )
        else name
    )

    available = bool(
        product.get(
            "is_available"
        )
    )

    variants = list(
        product.get(
            "variants"
        )
        or []
    )

    lines = [
        (
            f"**{title}**"
            + (
                ""
                if available
                else " — currently out of stock"
            )
        )
    ]

    for variant in variants[
        :4
    ]:

        if not isinstance(
            variant,
            Mapping,
        ):

            continue

        size = str(
            variant.get(
                "size"
            )
            or variant.get(
                "pack_size"
            )
            or ""
        )

        price = (
            _format_price(
                variant.get(
                    "price"
                ),
                variant.get(
                    "currency"
                ),
            )
        )

        in_stock = bool(
            variant.get(
                "in_stock"
            )
        )

        pieces = [
            item
            for item in (
                size,
                price,
                (
                    "In stock"
                    if in_stock
                    else "Out of stock"
                ),
            )
            if item
        ]

        lines.append(
            "• "
            + " — ".join(
                pieces
            )
        )

    return lines


# ============================================================
# PRODUCT SEARCH FALLBACK
# ============================================================


def _search_fallback_answer(
    result: Mapping[
        str,
        Any,
    ],
) -> str | None:

    data = (
        result.get(
            "data"
        )
    )

    if not isinstance(
        data,
        Mapping,
    ):

        return None

    status = str(
        data.get(
            "status"
        )
        or ""
    )

    products = [
        product
        for product
        in (
            data.get(
                "products"
            )
            or []
        )
        if isinstance(
            product,
            Mapping,
        )
    ]

    alternatives = [
        product
        for product
        in (
            data.get(
                "alternatives"
            )
            or []
        )
        if isinstance(
            product,
            Mapping,
        )
    ]

    lines: list[
        str
    ] = []

    if status in {
        "found",
        "found_partially_available",
    }:

        lines.append(
            "I found these matching products:"
        )

        for product in products[
            :4
        ]:

            lines.extend(
                _product_fallback_lines(
                    product
                )
            )

        return "\n".join(
            lines
        )

    if status in {
        "found_out_of_stock",
        "requested_in_stock_but_unavailable",
    }:

        if products:

            names = [
                str(
                    product.get(
                        "name"
                    )
                    or "the requested product"
                )
                for product
                in products[
                    :2
                ]
            ]

            lines.append(
                (
                    ", ".join(
                        names
                    )
                    + (
                        " is currently out of stock."
                        if len(
                            names
                        )
                        == 1
                        else " are currently out of stock."
                    )
                )
            )

        else:

            lines.append(
                "The requested product is currently out of stock."
            )

        if alternatives:

            lines.append(
                "\nAvailable alternatives:"
            )

            for product in alternatives[
                :3
            ]:

                lines.extend(
                    _product_fallback_lines(
                        product
                    )
                )

        return "\n".join(
            lines
        )

    if status == "not_found":

        lines.append(
            "I couldn't find a sufficiently close product in the catalog currently."
        )

        if alternatives:

            lines.append(
                "\nYou could consider:"
            )

            for product in alternatives[
                :3
            ]:

                lines.extend(
                    _product_fallback_lines(
                        product
                    )
                )

        return "\n".join(
            lines
        )

    return None


# ============================================================
# RAG FALLBACK
# ============================================================


def _rag_fallback_answer(
    result: Mapping[
        str,
        Any,
    ],
) -> str | None:

    data = (
        result.get(
            "data"
        )
    )

    if not isinstance(
        data,
        Mapping,
    ):

        return None

    matches = [
        item
        for item in (
            data.get(
                "matches"
            )
            or []
        )
        if isinstance(
            item,
            Mapping,
        )
    ]

    unavailable = [
        item
        for item in (
            data.get(
                "unavailable_matches"
            )
            or []
        )
        if isinstance(
            item,
            Mapping,
        )
    ]

    if matches:

        lines = [
            "Here are some suitable options:"
        ]

        for item in matches[
            :4
        ]:

            name = str(
                item.get(
                    "product_name"
                )
                or "Product"
            )

            brand = str(
                item.get(
                    "brand"
                )
                or ""
            )

            title = (
                f"{brand} {name}"
                if (
                    brand
                    and brand.lower()
                    not in name.lower()
                )
                else name
            )

            variants = list(
                item.get(
                    "variants"
                )
                or []
            )

            detail = ""

            if variants:

                variant = variants[
                    0
                ]

                if isinstance(
                    variant,
                    Mapping,
                ):

                    size = str(
                        variant.get(
                            "size"
                        )
                        or ""
                    )

                    price = (
                        _format_price(
                            variant.get(
                                "price"
                            ),
                            variant.get(
                                "currency"
                            ),
                        )
                    )

                    detail_parts = [
                        item
                        for item in (
                            size,
                            price,
                        )
                        if item
                    ]

                    if detail_parts:

                        detail = (
                            " — "
                            + " — ".join(
                                detail_parts
                            )
                        )

            lines.append(
                f"• **{title}**{detail}"
            )

        return "\n".join(
            lines
        )

    if unavailable:

        names = [
            str(
                item.get(
                    "product_name"
                )
                or "Product"
            )
            for item
            in unavailable[
                :3
            ]
        ]

        return (
            "I found relevant options, but they are currently "
            "out of stock: "
            + ", ".join(
                names
            )
            + "."
        )

    return (
        "I couldn't find a sufficiently suitable product for that request right now."
    )


# ============================================================
# VERIFIED FALLBACK
# ============================================================


def _build_verified_fallback_answer(
    *,
    executions: Sequence[
        ToolExecutionRecord
    ],
    cohere_text: str | None,
) -> str:
    """
    If Groq is rate-limited/unavailable, do NOT throw away successful
    Supabase/RAG results.

    Generate a small deterministic answer from verified data.

    This does not invent commerce facts.
    """

    # ========================================================
    # LATEST SUCCESSFUL SEARCH / RAG
    # ========================================================

    for execution in reversed(
        executions
    ):

        if not execution.success:

            continue

        if (
            execution.tool_name
            == "search_products"
        ):

            answer = (
                _search_fallback_answer(
                    execution.result
                )
            )

            if answer:

                return answer

        if (
            execution.tool_name
            == "semantic_product_search"
        ):

            answer = (
                _rag_fallback_answer(
                    execution.result
                )
            )

            if answer:

                return answer

    # ========================================================
    # OTHER TOOLS
    # ========================================================

    for execution in reversed(
        executions
    ):

        if not execution.success:

            continue

        message = (
            execution.result.get(
                "message"
            )
        )

        if (
            isinstance(
                message,
                str,
            )
            and message.strip()
        ):

            return (
                message.strip()
            )

    # ========================================================
    # GENERAL QUERY
    #
    # Cohere's no-tool response is usable if Groq is temporarily
    # unavailable.
    # ========================================================

    if cohere_text:

        return cohere_text

    return (
        "I couldn't generate the final wording right now. "
        "Please try again shortly."
    )


# ============================================================
# GENERATE DATABASE-BACKED RESPONSE
# ============================================================


def _generate_database_backed_response(
    *,
    user_message: str,
    executions: Sequence[
        ToolExecutionRecord
    ],
) -> ResponsePayload:
    """
    Generate the final response for any tool-backed request.

    This path intentionally bypasses the normal rotating Groq responder.

    database_responder.py uses:

        GROQ_API_KEY2

    which is the dedicated Groq API slot 3 for database-backed
    product/cart/order/RAG responses.

    The returned ResponsePayload preserves structured verified data for
    Streamlit so app.py can display images, SKU details, variants, cart
    items and orders independently of generated prose.
    """

    return respond_to_database_query(
        user_message=user_message,
        executions=executions,
    )


# ============================================================
# GENERATE GROQ ANSWER
# ============================================================


def _generate_final_answer(
    *,
    user_message: str,
    chat_history: Sequence[
        Mapping[
            str,
            Any,
        ]
    ] | None,
    executions: Sequence[
        ToolExecutionRecord
    ],
    cohere_text: str | None,
) -> str:

    (
        verified_data,
        rag_context,
    ) = (
        _build_groq_context(
            executions
        )
    )

    additional_context = {
        "cohere_conclusion":
            (
                cohere_text[
                    :600
                ]
                if cohere_text
                else None
            ),

        "tools":
            list(
                dict.fromkeys(
                    execution.tool_name
                    for execution
                    in executions
                )
            ),
    }

    return generate_final_text(
        user_message=user_message,

        backend_action=(
            _build_backend_action(
                executions
            )
        ),

        verified_data=(
            verified_data
            if verified_data
            else None
        ),

        rag_context=(
            rag_context
            if rag_context
            else None
        ),

        chat_history=chat_history,

        additional_context=(
            additional_context
        ),
    )


# ============================================================
# SAVE CHAT
# ============================================================


def _save_chat_message(
    role: str,
    content: str,
) -> None:

    try:

        add_chat_message(
            role,
            content,
        )

    except Exception:

        logger.debug(
            "Unable to save chat message.",
            exc_info=True,
        )


# ============================================================
# MAIN
# ============================================================


def process_message(
    user_message: str,
) -> BrainResponse:

    initialize_session()

    # ========================================================
    # VALIDATION
    # ========================================================

    try:

        user_message = (
            _validate_user_message(
                user_message
            )
        )

    except BrainValidationError as exc:

        return BrainResponse(
            text=str(
                exc
            ),
            success=False,
            error_code=(
                "INVALID_MESSAGE"
            ),
        )

    # ========================================================
    # HISTORY
    # ========================================================

    try:

        chat_history = (
            get_model_chat_history()
        )

    except Exception:

        logger.debug(
            "Unable to load model history.",
            exc_info=True,
        )

        chat_history = []

    executions: list[
        ToolExecutionRecord
    ] = []

    cohere_text: (
        str
        | None
    ) = None

    rounds = 0

    # ========================================================
    # COHERE
    # ========================================================

    try:

        (
            executions,
            cohere_text,
            rounds,
        ) = (
            _run_cohere_tool_loop(
                user_message=user_message,
                chat_history=chat_history,
            )
        )

    except CohereClientError:

        logger.exception(
            "Cohere request failed."
        )

        text = (
            "I couldn't process that request right now. "
            "Please try again."
        )

        _save_chat_message(
            "user",
            user_message,
        )

        _save_chat_message(
            "assistant",
            text,
        )

        return BrainResponse(
            text=text,
            success=False,
            tool_executions=executions,
            cohere_text=cohere_text,
            rounds=rounds,
            error_code=(
                "COHERE_ERROR"
            ),
        )

    except Exception:

        logger.exception(
            "Unexpected brain orchestration failure."
        )

        text = (
            "I couldn't complete that request right now. "
            "Please try again."
        )

        _save_chat_message(
            "user",
            user_message,
        )

        _save_chat_message(
            "assistant",
            text,
        )

        return BrainResponse(
            text=text,
            success=False,
            tool_executions=executions,
            cohere_text=cohere_text,
            rounds=rounds,
            error_code=(
                "BRAIN_ERROR"
            ),
        )

    # ========================================================
    # DATABASE / TOOL-BACKED RESPONSE PATH
    # ========================================================
    #
    # If Cohere used a backend tool, do not send that result through
    # the normal rotating Groq client below.
    #
    # Instead:
    #
    #     verified backend result
    #         ↓
    #     database_responder.py
    #         ↓
    #     GROQ_API_KEY2 only (API slot 3)
    #         ↓
    #     prompts/rules.txt
    #         ↓
    #     natural response + structured ResponsePayload
    #
    # The old Groq block below is preserved and now handles only
    # no-tool / general-chat requests.
    # ========================================================

    if executions:

        try:

            response_payload = (
                _generate_database_backed_response(
                    user_message=user_message,
                    executions=executions,
                )
            )

            final_text = str(
                response_payload.text
                or ""
            ).strip()

            if not final_text:

                final_text = (
                    _build_verified_fallback_answer(
                        executions=executions,
                        cohere_text=cohere_text,
                    )
                )

            _save_chat_message(
                "user",
                user_message,
            )

            _save_chat_message(
                "assistant",
                final_text,
            )

            _update_conversation_context(
                executions
            )

            return BrainResponse(
                text=final_text,
                success=bool(
                    response_payload.success
                    and final_text
                ),
                tool_executions=executions,
                cohere_text=cohere_text,
                rounds=rounds,
                error_code=(
                    None
                    if response_payload.groq_used
                    else "DATABASE_GROQ_FALLBACK_USED"
                ),
                response_payload=response_payload,
            )

        except Exception:

            logger.exception(
                "Database response engine failed. "
                "Using existing verified Python fallback."
            )

            # Never spill a database request into the normal rotating
            # Groq pool. Use the existing grounded Python fallback.
            final_text = (
                _build_verified_fallback_answer(
                    executions=executions,
                    cohere_text=cohere_text,
                )
            )

            _save_chat_message(
                "user",
                user_message,
            )

            _save_chat_message(
                "assistant",
                final_text,
            )

            _update_conversation_context(
                executions
            )

            return BrainResponse(
                text=final_text,
                success=bool(
                    final_text
                ),
                tool_executions=executions,
                cohere_text=cohere_text,
                rounds=rounds,
                error_code=(
                    "DATABASE_RESPONSE_ERROR"
                ),
                response_payload=None,
            )

    # ========================================================
    # GROQ
    # ========================================================
    #
    # EXISTING GENERAL-CHAT PATH.
    #
    # This block is deliberately preserved. Because the database path
    # above returns early, this section now runs only when Cohere did
    # not execute a backend tool.
    # ========================================================

    groq_failed = False

    try:

        final_text = (
            _generate_final_answer(
                user_message=user_message,
                chat_history=chat_history,
                executions=executions,
                cohere_text=cohere_text,
            )
        )

        final_text = (
            str(
                final_text
            ).strip()
        )

        if not final_text:

            raise GroqClientError(
                "Groq returned empty text."
            )

    except GroqClientError:

        # ====================================================
        # IMPORTANT CHANGE
        #
        # Previously:
        #
        # successful DB result
        #     ↓
        # Groq 429
        #     ↓
        # generic failure message
        #
        # Now:
        #
        # successful DB result
        #     ↓
        # Groq 429
        #     ↓
        # deterministic verified result
        # ====================================================

        groq_failed = True

        logger.warning(
            "Groq unavailable; using verified fallback response."
        )

        final_text = (
            _build_verified_fallback_answer(
                executions=executions,
                cohere_text=cohere_text,
            )
        )

    except Exception:

        groq_failed = True

        logger.exception(
            "Unexpected Groq final-response failure."
        )

        final_text = (
            _build_verified_fallback_answer(
                executions=executions,
                cohere_text=cohere_text,
            )
        )

    # ========================================================
    # SAVE
    # ========================================================

    _save_chat_message(
        "user",
        user_message,
    )

    _save_chat_message(
        "assistant",
        final_text,
    )

    _update_conversation_context(
        executions
    )

    # ========================================================
    # SUCCESS
    #
    # If Groq failed but verified backend data existed and Python
    # produced a truthful fallback, the user's request still
    # completed successfully.
    # ========================================================

    effective_success = bool(
        final_text
    ) and (
        bool(
            executions
        )
        or bool(
            cohere_text
        )
        or not groq_failed
    )

    return BrainResponse(
        text=final_text,
        success=effective_success,
        tool_executions=executions,
        cohere_text=cohere_text,
        rounds=rounds,
        error_code=(
            "GROQ_FALLBACK_USED"
            if groq_failed
            else None
        ),
        response_payload=None,
    )


# ============================================================
# SIMPLE WRAPPER
# ============================================================


def chat(
    user_message: str,
) -> str:

    return (
        process_message(
            user_message
        )
        .text
    )


# ============================================================
# CAPABILITIES
# ============================================================


def get_brain_capabilities() -> dict[
    str,
    Any,
]:

    product_tools = sorted(
        get_product_tool_names()
    )

    cart_tools = sorted(
        get_cart_tool_names()
    )

    order_tools = sorted(
        get_order_tool_names()
    )

    rag_tools = sorted(
        get_rag_tool_names()
    )

    return {
        "product_tools":
            product_tools,

        "cart_tools":
            cart_tools,

        "order_tools":
            order_tools,

        "rag_tools":
            rag_tools,

        "total_tools": (
            len(
                product_tools
            )
            +
            len(
                cart_tools
            )
            +
            len(
                order_tools
            )
            +
            len(
                rag_tools
            )
        ),

        "max_tool_rounds":
            MAX_TOOL_ROUNDS,

        "max_total_tool_calls":
            MAX_TOTAL_TOOL_CALLS,

        "manual_intent_classifier":
            False,

        "general_queries_supported":
            True,

        "semantic_rag_supported":
            True,

        "out_of_stock_search_supported":
            True,

        "groq_verified_fallback":
            True,

        "compact_groq_context":
            True,

        "structured_response_supported":
            True,

        "database_response_engine":
            "core.database_responder",

        "database_groq_key_slot":
            3,

        "database_groq_rotation":
            False,

        "database_rules_file":
            "prompts/rules.txt",

        "product_image_payload_supported":
            True,

        "sku_payload_supported":
            True,

        "order_payload_supported":
            True,

        "general_groq_path_preserved":
            True,

        "api_only_runtime":
            True,

        "streamlit_session_dependency":
            False,

        "request_scoped_conversation_state":
            True,

        "client_state_export_supported":
            True,
    }