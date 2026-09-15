# """
# api.py

# Main FastAPI controller for the Grocery Chatbot / Voice Command
# Shopping Assistant.

# ========================================================================
# ARCHITECTURE
# ========================================================================

# Frontend
#     ↓
# Supabase Auth
#     ↓
# Supabase access_token
#     ↓
# Authorization: Bearer <access_token>
#     ↓
# FastAPI /chat
#     ↓
# api_context.py
#     ↓
# Supabase verifies authenticated user
#     ↓
# core/brain.py
#     ↓
# Cohere
#     ↓
# Approved backend tools
#     ├── product tools
#     ├── cart tools
#     ├── order tools
#     └── RAG tools
#     ↓
# Verified Supabase / RAG data
#     ↓
# Groq response generation
#     ↓
# ResponsePayload
#     ↓
# response_mapper.py
#     ↓
# ChatResponse JSON
#     ↓
# Frontend


# ========================================================================
# IMPORTANT
# ========================================================================

# This API is API-ONLY.

# It does NOT:

#     - render Streamlit
#     - perform frontend login forms
#     - perform Google OAuth UI
#     - store st.session_state
#     - manually classify user intents
#     - calculate product prices itself
#     - invent product data
#     - generate image URLs

# The frontend performs authentication through Supabase.

# Every protected API request sends:

#     Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

# The API verifies that access token through Supabase before processing the
# chat request.


# ========================================================================
# FRONTEND EXAMPLE
# ========================================================================

# const {
#     data: { session }
# } = await supabase.auth.getSession();

# const response = await fetch(
#     API_URL + "/chat",
#     {
#         method: "POST",
#         headers: {
#             "Content-Type": "application/json",
#             "Authorization": `Bearer ${session.access_token}`
#         },
#         body: JSON.stringify({
#             message: "show milk",
#             conversation_id: conversationId,
#             client_context: previousClientState
#         })
#     }
# );

# const data = await response.json();


# ========================================================================
# HUGGING FACE
# ========================================================================

# Docker / Uvicorn will run:

#     uvicorn api:app --host 0.0.0.0 --port 7860

# Port configuration belongs to Docker/runtime deployment.

# It is NOT handled by this file.
# """

# from __future__ import annotations


# # ============================================================
# # STANDARD LIBRARY
# # ============================================================

# import logging
# import os
# import re
# import uuid

# from typing import (
#     Any,
# )


# # ============================================================
# # FASTAPI
# # ============================================================

# from fastapi import (
#     FastAPI,
#     Header,
#     Request,
#     Response,
# )

# from fastapi.exceptions import (
#     RequestValidationError,
# )

# from fastapi.middleware.cors import (
#     CORSMiddleware,
# )

# from fastapi.responses import (
#     JSONResponse,
# )

# from starlette.concurrency import (
#     run_in_threadpool,
# )

# from starlette.middleware.gzip import (
#     GZipMiddleware,
# )


# # ============================================================
# # API MODELS
# # ============================================================

# from api_models import (
#     API_VERSION,
#     ChatRequest,
#     ChatResponse,
# )


# # ============================================================
# # API AUTH / REQUEST CONTEXT
# # ============================================================

# from api_context import (
#     APIAccessTokenError,
#     APIAuthenticationError,
#     APIAuthorizationHeaderError,
#     APIContextError,
#     APIContextValidationError,
#     APIUserVerificationError,
#     api_request_context,
#     build_api_request_context_from_authorization,
# )


# # ============================================================
# # CHAT BRAIN
# # ============================================================

# from core.brain import (
#     BrainResponse,
#     process_message,
# )


# # ============================================================
# # RESPONSE MAPPER
# # ============================================================

# from response_mapper import (
#     build_error_chat_response_dict,
#     map_brain_response,
# )


# # ============================================================
# # USER PROFILE
# # ============================================================

# from database.users import (
#     UserDatabaseError,
#     ensure_current_profile,
# )


# # ============================================================
# # LOGGER
# # ============================================================

# logger = logging.getLogger(
#     __name__
# )


# # ============================================================
# # APPLICATION INFORMATION
# # ============================================================

# APP_TITLE = (
#     "Voice Command Shopping Assistant API"
# )

# APP_DESCRIPTION = """
# API backend for the Grocery Chatbot / Voice Command Shopping Assistant.

# Authentication is handled using Supabase access tokens.

# Send:

#     Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

# with requests to `/chat`.
# """.strip()

# APP_VERSION = API_VERSION


# # ============================================================
# # REQUEST LIMITS
# # ============================================================

# MAX_REQUEST_ID_LENGTH = 200


# # ============================================================
# # FASTAPI APPLICATION
# # ============================================================

# app = FastAPI(
#     title=APP_TITLE,

#     description=APP_DESCRIPTION,

#     version=APP_VERSION,

#     docs_url="/docs",

#     redoc_url="/redoc",

#     openapi_url="/openapi.json",
# )


# # ============================================================
# # CORS
# # ============================================================


# def _get_cors_origins() -> list[str]:
#     """
#     Determine allowed frontend origins.

#     Environment variable:

#         CORS_ALLOWED_ORIGINS

#     Examples:

#         CORS_ALLOWED_ORIGINS=*

#     or:

#         CORS_ALLOWED_ORIGINS=https://myapp.vercel.app

#     or:

#         CORS_ALLOWED_ORIGINS=https://app1.com,https://app2.com

#     Because authentication uses an explicit Authorization Bearer header
#     rather than browser cookies, wildcard CORS can operate with:

#         allow_credentials=False

#     For production, specifying the actual frontend URL is preferable.
#     """

#     raw = (
#         os.getenv(
#             "CORS_ALLOWED_ORIGINS",
#             "*",
#         )
#         .strip()
#     )

#     if not raw:

#         return [
#             "*"
#         ]

#     if raw == "*":

#         return [
#             "*"
#         ]

#     origins = [
#         item.strip()
#         for item
#         in raw.split(
#             ","
#         )
#         if item.strip()
#     ]

#     if not origins:

#         return [
#             "*"
#         ]

#     return list(
#         dict.fromkeys(
#             origins
#         )
#     )


# CORS_ORIGINS = (
#     _get_cors_origins()
# )


# app.add_middleware(
#     CORSMiddleware,

#     allow_origins=CORS_ORIGINS,

#     # We authenticate through the Authorization header rather than
#     # browser cookies.
#     allow_credentials=False,

#     allow_methods=[
#         "GET",
#         "POST",
#         "OPTIONS",
#     ],

#     allow_headers=[
#         "Authorization",
#         "Content-Type",
#         "Accept",
#         "X-Request-ID",
#     ],

#     expose_headers=[
#         "X-Request-ID",
#     ],

#     max_age=600,
# )


# # ============================================================
# # RESPONSE COMPRESSION
# # ============================================================

# app.add_middleware(
#     GZipMiddleware,

#     min_size=1_000,
# )


# # ============================================================
# # REQUEST ID
# # ============================================================


# def _normalize_request_id(
#     value: Any,
# ) -> str:
#     """
#     Accept a safe client request ID or generate one.

#     Request IDs are useful for debugging but are NOT authentication.
#     """

#     if value is None:

#         return str(
#             uuid.uuid4()
#         )

#     value = str(
#         value
#     ).strip()

#     if not value:

#         return str(
#             uuid.uuid4()
#         )

#     if (
#         len(
#             value
#         )
#         > MAX_REQUEST_ID_LENGTH
#     ):

#         return str(
#             uuid.uuid4()
#         )

#     if not re.fullmatch(
#         r"[A-Za-z0-9._:\-]+",
#         value,
#     ):

#         return str(
#             uuid.uuid4()
#         )

#     return value


# # ============================================================
# # ERROR RESPONSE
# # ============================================================


# def _error_response(
#     *,
#     status_code: int,
#     message: str,
#     error_code: str,
#     conversation_id: str | None = None,
#     request_id: str | None = None,
#     authenticate_header: bool = False,
# ) -> JSONResponse:
#     """
#     Build a consistent public error response.

#     Never expose:

#         stack traces
#         Supabase tokens
#         API keys
#         raw database exceptions
#     """

#     payload = (
#         build_error_chat_response_dict(
#             message=message,
#             error_code=error_code,
#             conversation_id=conversation_id,
#         )
#     )

#     headers: dict[
#         str,
#         str,
#     ] = {}

#     if request_id:

#         headers[
#             "X-Request-ID"
#         ] = request_id

#     if authenticate_header:

#         headers[
#             "WWW-Authenticate"
#         ] = "Bearer"

#     return JSONResponse(
#         status_code=status_code,
#         content=payload,
#         headers=headers,
#     )


# # ============================================================
# # FASTAPI VALIDATION ERROR
# # ============================================================


# @app.exception_handler(
#     RequestValidationError
# )
# async def request_validation_exception_handler(
#     request: Request,
#     exc: RequestValidationError,
# ) -> JSONResponse:
#     """
#     Return the same public response structure when Pydantic/FastAPI
#     rejects malformed request JSON.

#     Raw validation details are intentionally not exposed.
#     """

#     request_id = (
#         _normalize_request_id(
#             request.headers.get(
#                 "X-Request-ID"
#             )
#         )
#     )

#     logger.info(
#         "Request validation failed. request_id=%s",
#         request_id,
#     )

#     return _error_response(
#         status_code=422,

#         message=(
#             "The request body is invalid. "
#             "Please check the message and request fields."
#         ),

#         error_code=(
#             "REQUEST_VALIDATION_ERROR"
#         ),

#         request_id=request_id,
#     )


# # ============================================================
# # UNHANDLED API ERROR
# # ============================================================


# @app.exception_handler(
#     Exception
# )
# async def unhandled_exception_handler(
#     request: Request,
#     exc: Exception,
# ) -> JSONResponse:
#     """
#     Last-resort error boundary.

#     We log only the error type and request ID here.

#     Sensitive headers and access tokens are never logged.
#     """

#     request_id = (
#         _normalize_request_id(
#             request.headers.get(
#                 "X-Request-ID"
#             )
#         )
#     )

#     logger.exception(
#         "Unhandled API error. "
#         "request_id=%s error_type=%s",
#         request_id,
#         type(
#             exc
#         ).__name__,
#     )

#     return _error_response(
#         status_code=500,

#         message=(
#             "The request could not be completed right now. "
#             "Please try again."
#         ),

#         error_code="INTERNAL_API_ERROR",

#         request_id=request_id,
#     )


# # ============================================================
# # ROOT
# # ============================================================


# @app.get(
#     "/",
#     tags=[
#         "system"
#     ],
# )
# async def root() -> dict[
#     str,
#     Any,
# ]:
#     """
#     Basic API discovery endpoint.
#     """

#     return {
#         "success":
#             True,

#         "service":
#             APP_TITLE,

#         "api_version":
#             API_VERSION,

#         "status":
#             "running",

#         "runtime":
#             "api_only",

#         "authentication":
#             "supabase_bearer_token",

#         "endpoints": {
#             "health":
#                 "/health",

#             "chat":
#                 "/chat",

#             "docs":
#                 "/docs",

#             "openapi":
#                 "/openapi.json",
#         },
#     }


# # ============================================================
# # HEALTH
# # ============================================================


# @app.get(
#     "/health",
#     tags=[
#         "system"
#     ],
# )
# async def health() -> dict[
#     str,
#     Any,
# ]:
#     """
#     Lightweight health endpoint.

#     This intentionally does not make expensive model/database calls.

#     Hugging Face or another hosting platform can use this endpoint to
#     determine whether the API process itself is alive.
#     """

#     return {
#         "success":
#             True,

#         "status":
#             "healthy",

#         "service":
#             APP_TITLE,

#         "api_version":
#             API_VERSION,

#         "runtime":
#             "api_only",

#         "authentication":
#             "supabase",

#         "chat_endpoint":
#             "/chat",
#     }


# # ============================================================
# # ENSURE PROFILE
# # ============================================================


# def _ensure_authenticated_profile() -> None:
#     """
#     Preserve the useful profile-initialization behavior from the
#     previous application.

#     Authentication itself has already been verified by api_context.

#     Profile creation is best-effort because general/product chat should
#     not be completely unavailable merely because profile synchronization
#     temporarily fails.

#     Private cart/order operations remain protected independently by
#     database/users.py and Supabase RLS.
#     """

#     try:

#         ensure_current_profile()

#     except UserDatabaseError as exc:

#         logger.warning(
#             "Unable to synchronize authenticated profile. "
#             "error_type=%s",
#             type(
#                 exc
#             ).__name__,
#         )

#     except Exception as exc:

#         logger.warning(
#             "Unexpected profile synchronization failure. "
#             "error_type=%s",
#             type(
#                 exc
#             ).__name__,
#         )


# # ============================================================
# # SYNCHRONOUS CHAT PIPELINE
# # ============================================================


# def _execute_chat_request(
#     *,
#     body: ChatRequest,
#     authorization_header: str,
#     request_id: str,
# ) -> tuple[
#     ChatResponse,
#     str,
# ]:
#     """
#     Execute ONE complete authenticated chat request.

#     IMPORTANT
#     ---------

#     This entire function runs inside one worker thread.

#     That keeps:

#         api_context ContextVar
#         brain ContextVar

#     within the same execution context.

#     Flow:

#         Authorization header
#             ↓
#         verify Supabase access token
#             ↓
#         APIRequestContext
#             ↓
#         activate request context
#             ↓
#         ensure profile
#             ↓
#         process_message()
#             ↓
#         ResponsePayload
#             ↓
#         response_mapper
#             ↓
#         ChatResponse
#     """

#     # ========================================================
#     # AUTHENTICATE + CREATE REQUEST CONTEXT
#     # ========================================================

#     context = (
#         build_api_request_context_from_authorization(
#             authorization_header=(
#                 authorization_header
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             locale=(
#                 body.locale
#             ),

#             client_context=(
#                 body.client_context
#             ),

#             request_id=request_id,
#         )
#     )

#     # ========================================================
#     # ACTIVATE REQUEST CONTEXT
#     # ========================================================

#     with api_request_context(
#         context
#     ):

#         # ----------------------------------------------------
#         # Preserve authenticated profile behavior.
#         # ----------------------------------------------------

#         _ensure_authenticated_profile()

#         # ----------------------------------------------------
#         # CENTRAL CHATBOT BRAIN
#         # ----------------------------------------------------

#         result = (
#             process_message(
#                 body.message
#             )
#         )

#         # ----------------------------------------------------
#         # Defensive response contract check.
#         # ----------------------------------------------------

#         if not (
#             isinstance(
#                 result,
#                 BrainResponse,
#             )
#             or (
#                 hasattr(
#                     result,
#                     "text",
#                 )
#                 and hasattr(
#                     result,
#                     "success",
#                 )
#             )
#         ):

#             logger.error(
#                 "brain.process_message returned an unexpected object. "
#                 "request_id=%s",
#                 request_id,
#             )

#             raise RuntimeError(
#                 "Invalid chatbot response."
#             )

#         # ----------------------------------------------------
#         # PUBLIC API RESPONSE
#         # ----------------------------------------------------

#         response = (
#             map_brain_response(
#                 result,

#                 conversation_id=(
#                     context.conversation_id
#                 ),
#             )
#         )

#         return (
#             response,
#             context.request_id,
#         )


# # ============================================================
# # CHAT ENDPOINT
# # ============================================================


# @app.post(
#     "/chat",

#     response_model=ChatResponse,

#     tags=[
#         "chat"
#     ],

#     summary=(
#         "Send a message to the grocery assistant"
#     ),

#     responses={
#         400: {
#             "description":
#                 "Invalid request context",
#         },

#         401: {
#             "description":
#                 "Missing, invalid or expired Supabase session",
#         },

#         422: {
#             "description":
#                 "Invalid request body",
#         },

#         500: {
#             "description":
#                 "Internal API error",
#         },

#         503: {
#             "description":
#                 "Authentication service unavailable",
#         },
#     },
# )
# async def chat(
#     body: ChatRequest,

#     response: Response,

#     authorization: str | None = Header(
#         default=None,
#         alias="Authorization",
#     ),

#     x_request_id: str | None = Header(
#         default=None,
#         alias="X-Request-ID",
#     ),
# ) -> ChatResponse | JSONResponse:
#     """
#     Main chatbot API endpoint.

#     Required header:

#         Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

#     Example request:

#         {
#             "message": "show milk",
#             "conversation_id": "my-chat-123",
#             "client_context": {}
#         }

#     Example structured response:

#         {
#             "success": true,
#             "text": "...",
#             "response_type": "products",
#             "conversation_id": "my-chat-123",
#             "products": [
#                 {
#                     "name": "Amul Taaza",
#                     "image_url": "https://...",
#                     "variants": [
#                         {
#                             "sku_id": "...",
#                             "size": "500 ml",
#                             "price": 29,
#                             "stock": 4,
#                             "in_stock": true
#                         }
#                     ]
#                 }
#             ]
#         }
#     """

#     request_id = (
#         _normalize_request_id(
#             x_request_id
#         )
#     )

#     # ========================================================
#     # AUTHORIZATION HEADER REQUIRED
#     # ========================================================

#     if not authorization:

#         return _error_response(
#             status_code=401,

#             message=(
#                 "You must be signed in to use the shopping assistant."
#             ),

#             error_code=(
#                 "AUTHORIZATION_REQUIRED"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,

#             authenticate_header=True,
#         )

#     # ========================================================
#     # EXECUTE COMPLETE PIPELINE IN WORKER THREAD
#     # ========================================================

#     try:

#         (
#             result,
#             verified_request_id,
#         ) = await run_in_threadpool(
#             _execute_chat_request,

#             body=body,

#             authorization_header=(
#                 authorization
#             ),

#             request_id=request_id,
#         )

#     # ========================================================
#     # INVALID AUTH HEADER
#     # ========================================================

#     except APIAuthorizationHeaderError:

#         return _error_response(
#             status_code=401,

#             message=(
#                 "Authorization must use a valid Bearer token."
#             ),

#             error_code=(
#                 "INVALID_AUTHORIZATION_HEADER"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,

#             authenticate_header=True,
#         )

#     # ========================================================
#     # EXPIRED / INVALID ACCESS TOKEN
#     # ========================================================

#     except APIAccessTokenError:

#         return _error_response(
#             status_code=401,

#             message=(
#                 "Your Supabase login session is invalid or has expired. "
#                 "Please refresh your session and try again."
#             ),

#             error_code=(
#                 "INVALID_OR_EXPIRED_ACCESS_TOKEN"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,

#             authenticate_header=True,
#         )

#     # ========================================================
#     # USER COULD NOT BE VERIFIED
#     # ========================================================

#     except APIUserVerificationError:

#         return _error_response(
#             status_code=401,

#             message=(
#                 "Your authenticated user could not be verified."
#             ),

#             error_code=(
#                 "USER_VERIFICATION_FAILED"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,

#             authenticate_header=True,
#         )

#     # ========================================================
#     # INVALID CLIENT CONTEXT / CONVERSATION ID
#     # ========================================================

#     except APIContextValidationError:

#         return _error_response(
#             status_code=400,

#             message=(
#                 "The conversation context is invalid."
#             ),

#             error_code=(
#                 "INVALID_API_CONTEXT"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,
#         )

#     # ========================================================
#     # AUTH SERVICE FAILURE
#     # ========================================================

#     except APIAuthenticationError:

#         logger.warning(
#             "Authentication service failure. "
#             "request_id=%s",
#             request_id,
#         )

#         return _error_response(
#             status_code=503,

#             message=(
#                 "Authentication is temporarily unavailable. "
#                 "Please try again."
#             ),

#             error_code=(
#                 "AUTHENTICATION_SERVICE_UNAVAILABLE"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,
#         )

#     # ========================================================
#     # GENERIC REQUEST CONTEXT FAILURE
#     # ========================================================

#     except APIContextError:

#         return _error_response(
#             status_code=400,

#             message=(
#                 "The request context could not be initialized."
#             ),

#             error_code=(
#                 "API_CONTEXT_ERROR"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,
#         )

#     # ========================================================
#     # CHAT PIPELINE FAILURE
#     # ========================================================

#     except Exception as exc:

#         logger.exception(
#             "Chat request failed. "
#             "request_id=%s error_type=%s",
#             request_id,
#             type(
#                 exc
#             ).__name__,
#         )

#         return _error_response(
#             status_code=500,

#             message=(
#                 "I couldn't process that request right now. "
#                 "Please try again."
#             ),

#             error_code=(
#                 "CHAT_PROCESSING_ERROR"
#             ),

#             conversation_id=(
#                 body.conversation_id
#             ),

#             request_id=request_id,
#         )

#     # ========================================================
#     # REQUEST ID RESPONSE HEADER
#     # ========================================================

#     response.headers[
#         "X-Request-ID"
#     ] = verified_request_id

#     return result


# # ============================================================
# # LOCAL DEVELOPMENT ENTRY POINT
# # ============================================================

# if __name__ == "__main__":

#     import uvicorn

#     uvicorn.run(
#         "api:app",

#         host="0.0.0.0",

#         port=7860,

#         reload=False,

#         log_level="info",
#     )



"""
api.py

Main FastAPI controller for the Grocery Chatbot / Voice Command
Shopping Assistant.

========================================================================
ARCHITECTURE
========================================================================

Frontend
    ↓
Supabase Auth
    ↓
Supabase access_token
    ↓
Authorization: Bearer <access_token>
    ↓
FastAPI /chat
    ↓
api_context.py
    ↓
Supabase verifies authenticated user
    ↓
core/brain.py
    ↓
Cohere
    ↓
Approved backend tools
    ├── product tools
    ├── cart tools
    ├── order tools
    └── RAG tools
    ↓
Verified Supabase / RAG data
    ↓
Groq response generation
    ↓
ResponsePayload
    ↓
response_mapper.py
    ↓
ChatResponse JSON
    ↓
Frontend


========================================================================
IMPORTANT
========================================================================

This API is API-ONLY.

It does NOT:

    - render Streamlit
    - perform frontend login forms
    - perform Google OAuth UI
    - store st.session_state
    - manually classify user intents
    - calculate product prices itself
    - invent product data
    - generate image URLs

The frontend performs authentication through Supabase.

Every protected API request sends:

    Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

The API verifies that access token through Supabase before processing the
chat request.


========================================================================
FRONTEND EXAMPLE
========================================================================

const {
    data: { session }
} = await supabase.auth.getSession();

const response = await fetch(
    API_URL + "/chat",
    {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${session.access_token}`
        },
        body: JSON.stringify({
            message: "show milk",
            conversation_id: conversationId,
            client_context: previousClientState
        })
    }
);

const data = await response.json();


========================================================================
HUGGING FACE
========================================================================

Docker / Uvicorn will run:

    uvicorn api:app --host 0.0.0.0 --port 7860

Port configuration belongs to Docker/runtime deployment.

It is NOT handled by this file.
"""

from __future__ import annotations


# ============================================================
# STANDARD LIBRARY
# ============================================================

import logging
import os
import re
import uuid

from typing import (
    Any,
)


# ============================================================
# FASTAPI
# ============================================================

from fastapi import (
    FastAPI,
    Header,
    Request,
    Response,
)

from fastapi.exceptions import (
    RequestValidationError,
)

from fastapi.middleware.cors import (
    CORSMiddleware,
)

from fastapi.responses import (
    JSONResponse,
)

from starlette.concurrency import (
    run_in_threadpool,
)

from starlette.middleware.gzip import (
    GZipMiddleware,
)


# ============================================================
# API MODELS
# ============================================================

from api_models import (
    API_VERSION,
    ChatRequest,
    ChatResponse,
)


# ============================================================
# API AUTH / REQUEST CONTEXT
# ============================================================

from api_context import (
    APIAccessTokenError,
    APIAuthenticationError,
    APIAuthorizationHeaderError,
    APIContextError,
    APIContextValidationError,
    APIUserVerificationError,
    api_request_context,
    build_api_request_context_from_authorization,
)


# ============================================================
# CHAT BRAIN
# ============================================================

from core.brain import (
    BrainResponse,
    process_message,
)


# ============================================================
# RESPONSE MAPPER
# ============================================================

from response_mapper import (
    build_error_chat_response_dict,
    map_brain_response,
)


# ============================================================
# USER PROFILE
# ============================================================

from database.users import (
    UserDatabaseError,
    ensure_current_profile,
)


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(
    __name__
)


# ============================================================
# APPLICATION INFORMATION
# ============================================================

APP_TITLE = (
    "Voice Command Shopping Assistant API"
)

APP_DESCRIPTION = """
API backend for the Grocery Chatbot / Voice Command Shopping Assistant.

Authentication is handled using Supabase access tokens.

Send:

    Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

with requests to `/chat`.
""".strip()

APP_VERSION = API_VERSION


# ============================================================
# REQUEST LIMITS
# ============================================================

MAX_REQUEST_ID_LENGTH = 200


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title=APP_TITLE,

    description=APP_DESCRIPTION,

    version=APP_VERSION,

    docs_url="/docs",

    redoc_url="/redoc",

    openapi_url="/openapi.json",
)


# ============================================================
# CORS
# ============================================================


def _get_cors_origins() -> list[str]:
    """
    Determine allowed frontend origins.

    Environment variable:

        CORS_ALLOWED_ORIGINS

    Examples:

        CORS_ALLOWED_ORIGINS=*

    or:

        CORS_ALLOWED_ORIGINS=https://myapp.vercel.app

    or:

        CORS_ALLOWED_ORIGINS=https://app1.com,https://app2.com

    Because authentication uses an explicit Authorization Bearer header
    rather than browser cookies, wildcard CORS can operate with:

        allow_credentials=False

    For production, specifying the actual frontend URL is preferable.
    """

    raw = (
        os.getenv(
            "CORS_ALLOWED_ORIGINS",
            "*",
        )
        .strip()
    )

    if not raw:

        return [
            "*"
        ]

    if raw == "*":

        return [
            "*"
        ]

    origins = [
        item.strip()
        for item
        in raw.split(
            ","
        )
        if item.strip()
    ]

    if not origins:

        return [
            "*"
        ]

    return list(
        dict.fromkeys(
            origins
        )
    )


CORS_ORIGINS = (
    _get_cors_origins()
)


app.add_middleware(
    CORSMiddleware,

    allow_origins=CORS_ORIGINS,

    # We authenticate through the Authorization header rather than
    # browser cookies.
    allow_credentials=False,

    allow_methods=[
        "GET",
        "POST",
        "OPTIONS",
    ],

    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "X-Request-ID",
    ],

    expose_headers=[
        "X-Request-ID",
    ],

    max_age=600,
)


# ============================================================
# RESPONSE COMPRESSION
# ============================================================

app.add_middleware(
    GZipMiddleware,

    minimum_size=1_000,
)


# ============================================================
# REQUEST ID
# ============================================================


def _normalize_request_id(
    value: Any,
) -> str:
    """
    Accept a safe client request ID or generate one.

    Request IDs are useful for debugging but are NOT authentication.
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
        > MAX_REQUEST_ID_LENGTH
    ):

        return str(
            uuid.uuid4()
        )

    if not re.fullmatch(
        r"[A-Za-z0-9._:\-]+",
        value,
    ):

        return str(
            uuid.uuid4()
        )

    return value


# ============================================================
# ERROR RESPONSE
# ============================================================


def _error_response(
    *,
    status_code: int,
    message: str,
    error_code: str,
    conversation_id: str | None = None,
    request_id: str | None = None,
    authenticate_header: bool = False,
) -> JSONResponse:
    """
    Build a consistent public error response.

    Never expose:

        stack traces
        Supabase tokens
        API keys
        raw database exceptions
    """

    payload = (
        build_error_chat_response_dict(
            message=message,
            error_code=error_code,
            conversation_id=conversation_id,
        )
    )

    headers: dict[
        str,
        str,
    ] = {}

    if request_id:

        headers[
            "X-Request-ID"
        ] = request_id

    if authenticate_header:

        headers[
            "WWW-Authenticate"
        ] = "Bearer"

    return JSONResponse(
        status_code=status_code,
        content=payload,
        headers=headers,
    )


# ============================================================
# FASTAPI VALIDATION ERROR
# ============================================================


@app.exception_handler(
    RequestValidationError
)
async def request_validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """
    Return the same public response structure when Pydantic/FastAPI
    rejects malformed request JSON.

    Raw validation details are intentionally not exposed.
    """

    request_id = (
        _normalize_request_id(
            request.headers.get(
                "X-Request-ID"
            )
        )
    )

    logger.info(
        "Request validation failed. request_id=%s",
        request_id,
    )

    return _error_response(
        status_code=422,

        message=(
            "The request body is invalid. "
            "Please check the message and request fields."
        ),

        error_code=(
            "REQUEST_VALIDATION_ERROR"
        ),

        request_id=request_id,
    )


# ============================================================
# UNHANDLED API ERROR
# ============================================================


@app.exception_handler(
    Exception
)
async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """
    Last-resort error boundary.

    We log only the error type and request ID here.

    Sensitive headers and access tokens are never logged.
    """

    request_id = (
        _normalize_request_id(
            request.headers.get(
                "X-Request-ID"
            )
        )
    )

    logger.exception(
        "Unhandled API error. "
        "request_id=%s error_type=%s",
        request_id,
        type(
            exc
        ).__name__,
    )

    return _error_response(
        status_code=500,

        message=(
            "The request could not be completed right now. "
            "Please try again."
        ),

        error_code="INTERNAL_API_ERROR",

        request_id=request_id,
    )


# ============================================================
# ROOT
# ============================================================


@app.get(
    "/",
    tags=[
        "system"
    ],
)
async def root() -> dict[
    str,
    Any,
]:
    """
    Basic API discovery endpoint.
    """

    return {
        "success":
            True,

        "service":
            APP_TITLE,

        "api_version":
            API_VERSION,

        "status":
            "running",

        "runtime":
            "api_only",

        "authentication":
            "supabase_bearer_token",

        "endpoints": {
            "health":
                "/health",

            "chat":
                "/chat",

            "docs":
                "/docs",

            "openapi":
                "/openapi.json",
        },
    }


# ============================================================
# HEALTH
# ============================================================


@app.get(
    "/health",
    tags=[
        "system"
    ],
)
async def health() -> dict[
    str,
    Any,
]:
    """
    Lightweight health endpoint.

    This intentionally does not make expensive model/database calls.

    Hugging Face or another hosting platform can use this endpoint to
    determine whether the API process itself is alive.
    """

    return {
        "success":
            True,

        "status":
            "healthy",

        "service":
            APP_TITLE,

        "api_version":
            API_VERSION,

        "runtime":
            "api_only",

        "authentication":
            "supabase",

        "chat_endpoint":
            "/chat",
    }


# ============================================================
# ENSURE PROFILE
# ============================================================


def _ensure_authenticated_profile() -> None:
    """
    Preserve the useful profile-initialization behavior from the
    previous application.

    Authentication itself has already been verified by api_context.

    Profile creation is best-effort because general/product chat should
    not be completely unavailable merely because profile synchronization
    temporarily fails.

    Private cart/order operations remain protected independently by
    database/users.py and Supabase RLS.
    """

    try:

        ensure_current_profile()

    except UserDatabaseError as exc:

        logger.warning(
            "Unable to synchronize authenticated profile. "
            "error_type=%s",
            type(
                exc
            ).__name__,
        )

    except Exception as exc:

        logger.warning(
            "Unexpected profile synchronization failure. "
            "error_type=%s",
            type(
                exc
            ).__name__,
        )


# ============================================================
# SYNCHRONOUS CHAT PIPELINE
# ============================================================


def _execute_chat_request(
    *,
    body: ChatRequest,
    authorization_header: str,
    request_id: str,
) -> tuple[
    ChatResponse,
    str,
]:
    """
    Execute ONE complete authenticated chat request.

    IMPORTANT
    ---------

    This entire function runs inside one worker thread.

    That keeps:

        api_context ContextVar
        brain ContextVar

    within the same execution context.

    Flow:

        Authorization header
            ↓
        verify Supabase access token
            ↓
        APIRequestContext
            ↓
        activate request context
            ↓
        ensure profile
            ↓
        process_message()
            ↓
        ResponsePayload
            ↓
        response_mapper
            ↓
        ChatResponse
    """

    # ========================================================
    # AUTHENTICATE + CREATE REQUEST CONTEXT
    # ========================================================

    context = (
        build_api_request_context_from_authorization(
            authorization_header=(
                authorization_header
            ),

            conversation_id=(
                body.conversation_id
            ),

            locale=(
                body.locale
            ),

            client_context=(
                body.client_context
            ),

            request_id=request_id,
        )
    )

    # ========================================================
    # ACTIVATE REQUEST CONTEXT
    # ========================================================

    with api_request_context(
        context
    ):

        # ----------------------------------------------------
        # Preserve authenticated profile behavior.
        # ----------------------------------------------------

        _ensure_authenticated_profile()

        # ----------------------------------------------------
        # CENTRAL CHATBOT BRAIN
        # ----------------------------------------------------

        result = (
            process_message(
                body.message
            )
        )

        # ----------------------------------------------------
        # Defensive response contract check.
        # ----------------------------------------------------

        if not (
            isinstance(
                result,
                BrainResponse,
            )
            or (
                hasattr(
                    result,
                    "text",
                )
                and hasattr(
                    result,
                    "success",
                )
            )
        ):

            logger.error(
                "brain.process_message returned an unexpected object. "
                "request_id=%s",
                request_id,
            )

            raise RuntimeError(
                "Invalid chatbot response."
            )

        # ----------------------------------------------------
        # PUBLIC API RESPONSE
        # ----------------------------------------------------

        response = (
            map_brain_response(
                result,

                conversation_id=(
                    context.conversation_id
                ),
            )
        )

        return (
            response,
            context.request_id,
        )


# ============================================================
# CHAT ENDPOINT
# ============================================================


@app.post(
    "/chat",

    response_model=ChatResponse,

    tags=[
        "chat"
    ],

    summary=(
        "Send a message to the grocery assistant"
    ),

    responses={
        400: {
            "description":
                "Invalid request context",
        },

        401: {
            "description":
                "Missing, invalid or expired Supabase session",
        },

        422: {
            "description":
                "Invalid request body",
        },

        500: {
            "description":
                "Internal API error",
        },

        503: {
            "description":
                "Authentication service unavailable",
        },
    },
)
async def chat(
    body: ChatRequest,

    response: Response,

    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),

    x_request_id: str | None = Header(
        default=None,
        alias="X-Request-ID",
    ),
) -> ChatResponse | JSONResponse:
    """
    Main chatbot API endpoint.

    Required header:

        Authorization: Bearer <SUPABASE_ACCESS_TOKEN>

    Example request:

        {
            "message": "show milk",
            "conversation_id": "my-chat-123",
            "client_context": {}
        }

    Example structured response:

        {
            "success": true,
            "text": "...",
            "response_type": "products",
            "conversation_id": "my-chat-123",
            "products": [
                {
                    "name": "Amul Taaza",
                    "image_url": "https://...",
                    "variants": [
                        {
                            "sku_id": "...",
                            "size": "500 ml",
                            "price": 29,
                            "stock": 4,
                            "in_stock": true
                        }
                    ]
                }
            ]
        }
    """

    request_id = (
        _normalize_request_id(
            x_request_id
        )
    )

    # ========================================================
    # AUTHORIZATION HEADER REQUIRED
    # ========================================================

    if not authorization:

        return _error_response(
            status_code=401,

            message=(
                "You must be signed in to use the shopping assistant."
            ),

            error_code=(
                "AUTHORIZATION_REQUIRED"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,

            authenticate_header=True,
        )

    # ========================================================
    # EXECUTE COMPLETE PIPELINE IN WORKER THREAD
    # ========================================================

    try:

        (
            result,
            verified_request_id,
        ) = await run_in_threadpool(
            _execute_chat_request,

            body=body,

            authorization_header=(
                authorization
            ),

            request_id=request_id,
        )

    # ========================================================
    # INVALID AUTH HEADER
    # ========================================================

    except APIAuthorizationHeaderError:

        return _error_response(
            status_code=401,

            message=(
                "Authorization must use a valid Bearer token."
            ),

            error_code=(
                "INVALID_AUTHORIZATION_HEADER"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,

            authenticate_header=True,
        )

    # ========================================================
    # EXPIRED / INVALID ACCESS TOKEN
    # ========================================================

    except APIAccessTokenError:

        return _error_response(
            status_code=401,

            message=(
                "Your Supabase login session is invalid or has expired. "
                "Please refresh your session and try again."
            ),

            error_code=(
                "INVALID_OR_EXPIRED_ACCESS_TOKEN"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,

            authenticate_header=True,
        )

    # ========================================================
    # USER COULD NOT BE VERIFIED
    # ========================================================

    except APIUserVerificationError:

        return _error_response(
            status_code=401,

            message=(
                "Your authenticated user could not be verified."
            ),

            error_code=(
                "USER_VERIFICATION_FAILED"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,

            authenticate_header=True,
        )

    # ========================================================
    # INVALID CLIENT CONTEXT / CONVERSATION ID
    # ========================================================

    except APIContextValidationError:

        return _error_response(
            status_code=400,

            message=(
                "The conversation context is invalid."
            ),

            error_code=(
                "INVALID_API_CONTEXT"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,
        )

    # ========================================================
    # AUTH SERVICE FAILURE
    # ========================================================

    except APIAuthenticationError:

        logger.warning(
            "Authentication service failure. "
            "request_id=%s",
            request_id,
        )

        return _error_response(
            status_code=503,

            message=(
                "Authentication is temporarily unavailable. "
                "Please try again."
            ),

            error_code=(
                "AUTHENTICATION_SERVICE_UNAVAILABLE"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,
        )

    # ========================================================
    # GENERIC REQUEST CONTEXT FAILURE
    # ========================================================

    except APIContextError:

        return _error_response(
            status_code=400,

            message=(
                "The request context could not be initialized."
            ),

            error_code=(
                "API_CONTEXT_ERROR"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,
        )

    # ========================================================
    # CHAT PIPELINE FAILURE
    # ========================================================

    except Exception as exc:

        logger.exception(
            "Chat request failed. "
            "request_id=%s error_type=%s",
            request_id,
            type(
                exc
            ).__name__,
        )

        return _error_response(
            status_code=500,

            message=(
                "I couldn't process that request right now. "
                "Please try again."
            ),

            error_code=(
                "CHAT_PROCESSING_ERROR"
            ),

            conversation_id=(
                body.conversation_id
            ),

            request_id=request_id,
        )

    # ========================================================
    # REQUEST ID RESPONSE HEADER
    # ========================================================

    response.headers[
        "X-Request-ID"
    ] = verified_request_id

    return result


# ============================================================
# LOCAL DEVELOPMENT ENTRY POINT
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "api:app",

        host="0.0.0.0",

        port=7860,

        reload=False,

        log_level="info",
    )
