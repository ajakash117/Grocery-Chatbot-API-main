"""
core/cohere_client.py

Central Cohere API client for the Grocery Chatbot.

Purpose
-------
Cohere acts as the CENTRAL BRAIN / ORCHESTRATION MODEL.

Its responsibility is to understand what the user wants and decide
which backend capability should be used.

Examples
--------

User:
    "Show me Amul milk"

Cohere:
    -> select product search tool


User:
    "Add the 1 litre one"

Cohere:
    -> understand conversation context
    -> select add-to-cart tool
    -> provide required arguments


User:
    "What can I cook with paneer?"

Cohere:
    -> select RAG / knowledge capability


IMPORTANT
---------
This module:

- creates the Cohere client
- sends messages to Cohere
- supports tool/function calling
- parses Cohere responses
- normalizes tool calls
- handles API failures
- handles retries
- validates inputs

This module does NOT:

- execute database tools
- access Supabase
- modify carts
- perform product searches
- perform RAG itself
- generate the final user-facing response

Those responsibilities belong elsewhere.

Architecture
------------

app.py
   |
   v
brain.py
   |
   v
cohere_client.py
   |
   v
Cohere
   |
   v
Tool decision
   |
   v
brain.py
   |
   v
Python tool execution
"""

from __future__ import annotations

import json
import logging
import random
import time

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

import cohere

from config import settings
from rules import get_cohere_rules


# ============================================================
# LOGGER
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

DEFAULT_MAX_RETRIES = 3

DEFAULT_RETRY_BASE_DELAY_SECONDS = 1.0

DEFAULT_RETRY_MAX_DELAY_SECONDS = 8.0

ALLOWED_MESSAGE_ROLES = {
    "system",
    "user",
    "assistant",
    "tool",
}


# ============================================================
# CUSTOM EXCEPTIONS
# ============================================================


class CohereClientError(RuntimeError):
    """
    Base exception for Cohere client failures.
    """

    pass


class CohereConfigurationError(CohereClientError):
    """
    Raised when Cohere configuration is invalid.
    """

    pass


class CohereRequestError(CohereClientError):
    """
    Raised when a Cohere request cannot be completed.
    """

    pass


class CohereResponseError(CohereClientError):
    """
    Raised when Cohere returns an unexpected or unusable response.
    """

    pass


class CohereToolCallError(CohereClientError):
    """
    Raised when a Cohere tool call cannot be parsed.
    """

    pass


# ============================================================
# NORMALIZED TOOL CALL
# ============================================================


@dataclass(slots=True)
class CohereToolCall:
    """
    Application-friendly representation of a Cohere tool call.

    We intentionally normalize Cohere SDK objects so the rest of our
    application does not depend directly on Cohere's internal classes.

    Example
    -------

    Cohere may return something conceptually like:

        ToolCallV2(
            id="search_products_abc123",
            type="function",
            function={
                "name": "search_products",
                "arguments": '{"query":"milk"}'
            }
        )

    We convert it into:

        CohereToolCall(
            id="search_products_abc123",
            name="search_products",
            arguments={"query": "milk"}
        )
    """

    id: str

    name: str

    arguments: dict[str, Any] = field(
        default_factory=dict
    )

    type: str = "function"

    raw_arguments: str | None = None


# ============================================================
# NORMALIZED COHERE RESPONSE
# ============================================================


@dataclass(slots=True)
class CohereBrainResponse:
    """
    Normalized response returned to brain.py.

    Properties
    ----------
    text:
        Any normal text returned by Cohere.

    tool_calls:
        Structured function calls requested by Cohere.

    tool_plan:
        Cohere's optional tool-use planning text.

        IMPORTANT:
        This is internal orchestration metadata.
        It should NOT automatically be displayed to the user.

    finish_reason:
        Cohere request completion reason.

    response_id:
        Cohere response identifier, useful for debugging.

    raw_response:
        Original SDK response.

        Keep this server-side only.
    """

    text: str = ""

    tool_calls: list[CohereToolCall] = field(
        default_factory=list
    )

    tool_plan: str | None = None

    finish_reason: str | None = None

    response_id: str | None = None

    raw_response: Any = field(
        default=None,
        repr=False,
    )

    # --------------------------------------------------------
    # Convenience properties
    # --------------------------------------------------------

    @property
    def has_tool_calls(self) -> bool:
        """
        Return True when Cohere requested one or more tools.
        """

        return bool(self.tool_calls)

    @property
    def has_text(self) -> bool:
        """
        Return True when Cohere returned meaningful text.
        """

        return bool(self.text.strip())

    @property
    def tool_names(self) -> list[str]:
        """
        Return names of all requested tools.
        """

        return [
            tool_call.name
            for tool_call in self.tool_calls
        ]

    def first_tool_call(
        self,
    ) -> CohereToolCall | None:
        """
        Return the first requested tool, if any.
        """

        if not self.tool_calls:
            return None

        return self.tool_calls[0]


# ============================================================
# MESSAGE TYPE ALIAS
# ============================================================

ChatMessage = dict[str, Any]


# ============================================================
# BASIC VALIDATION
# ============================================================


def _validate_configuration() -> None:
    """
    Validate Cohere-specific configuration before creating a client.
    """

    if not settings.cohere_api_key:
        raise CohereConfigurationError(
            "COHERE_API_KEY is missing."
        )

    if not settings.cohere_model:
        raise CohereConfigurationError(
            "COHERE_MODEL is missing."
        )


def _validate_user_message(
    message: str,
) -> str:
    """
    Validate and normalize a user message.
    """

    if not isinstance(message, str):
        raise TypeError(
            "User message must be a string."
        )

    message = message.strip()

    if not message:
        raise ValueError(
            "User message cannot be empty."
        )

    return message


# ============================================================
# CLIENT CREATION
# ============================================================


@lru_cache(maxsize=1)
def get_cohere_client() -> cohere.ClientV2:
    """
    Return one shared Cohere ClientV2 instance.

    The client is cached because repeatedly constructing SDK clients
    for every Streamlit rerun is unnecessary.

    Streamlit reruns Python application code frequently, therefore
    caching the client helps keep the architecture clean.

    Returns
    -------
    cohere.ClientV2
        Configured Cohere V2 client.
    """

    _validate_configuration()

    try:
        client = cohere.ClientV2(
            api_key=settings.cohere_api_key
        )

    except Exception as exc:
        logger.exception(
            "Failed to initialize Cohere client."
        )

        raise CohereConfigurationError(
            "Unable to initialize Cohere client."
        ) from exc

    return client


# ============================================================
# MESSAGE BUILDING
# ============================================================


def build_messages(
    *,
    user_message: str,
    chat_history: Sequence[Mapping[str, Any]] | None = None,
    include_system_rules: bool = True,
    additional_system_context: str | None = None,
) -> list[ChatMessage]:
    """
    Construct the message sequence sent to Cohere.

    Final order:

        system rules
        optional additional system context
        previous conversation
        current user message

    Example
    -------

        messages = build_messages(
            user_message="Show me milk",
            chat_history=[
                {
                    "role": "user",
                    "content": "I need groceries"
                },
                {
                    "role": "assistant",
                    "content": "What are you looking for?"
                }
            ]
        )
    """

    user_message = _validate_user_message(
        user_message
    )

    messages: list[ChatMessage] = []

    # --------------------------------------------------------
    # System rules
    # --------------------------------------------------------

    if include_system_rules:

        central_rules = get_cohere_rules().strip()

        if central_rules:
            messages.append(
                {
                    "role": "system",
                    "content": central_rules,
                }
            )

    # --------------------------------------------------------
    # Optional runtime context
    # --------------------------------------------------------

    if additional_system_context:

        context = additional_system_context.strip()

        if context:
            messages.append(
                {
                    "role": "system",
                    "content": context,
                }
            )

    # --------------------------------------------------------
    # Previous chat history
    # --------------------------------------------------------

    if chat_history:

        sanitized_history = sanitize_chat_history(
            chat_history
        )

        messages.extend(
            sanitized_history
        )

    # --------------------------------------------------------
    # Current user message
    # --------------------------------------------------------

    messages.append(
        {
            "role": "user",
            "content": user_message,
        }
    )

    return messages


# ============================================================
# CHAT HISTORY SANITIZATION
# ============================================================


def sanitize_chat_history(
    chat_history: Sequence[Mapping[str, Any]],
) -> list[ChatMessage]:
    """
    Validate and trim conversation history before sending it to Cohere.

    We intentionally do NOT blindly send whatever happens to be stored
    in Streamlit session_state.

    This protects against malformed messages and unnecessarily large
    history.

    MAX_CHAT_HISTORY comes from `.env`.

    Note
    ----
    MAX_CHAT_HISTORY represents conversational messages, not tokens.
    """

    if not chat_history:
        return []

    sanitized: list[ChatMessage] = []

    # Keep only the most recent configured messages.
    history_slice = list(chat_history)[
        -settings.max_chat_history:
    ]

    for message in history_slice:

        if not isinstance(message, Mapping):
            logger.warning(
                "Ignoring malformed chat history item: %r",
                message,
            )
            continue

        role = message.get("role")

        content = message.get("content")

        if role not in {
            "user",
            "assistant",
        }:
            # Tool/system messages should be managed explicitly
            # by brain.py rather than blindly copied from UI history.
            continue

        if not isinstance(content, str):
            continue

        content = content.strip()

        if not content:
            continue

        sanitized.append(
            {
                "role": role,
                "content": content,
            }
        )

    return sanitized


# ============================================================
# TOOL SCHEMA VALIDATION
# ============================================================


def validate_tools(
    tools: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """
    Validate tool definitions before sending them to Cohere.

    Expected high-level format:

        [
            {
                "type": "function",
                "function": {
                    "name": "search_products",
                    "description": "...",
                    "parameters": {
                        "type": "object",
                        "properties": {...},
                        "required": ["query"]
                    }
                }
            }
        ]

    Actual tools will be defined later in tools/* files.

    This validation catches obvious programming errors early.
    """

    if not tools:
        return []

    validated: list[dict[str, Any]] = []

    seen_names: set[str] = set()

    for index, tool in enumerate(tools):

        if not isinstance(tool, Mapping):
            raise ValueError(
                f"Tool at index {index} must be a mapping."
            )

        tool_dict = dict(tool)

        tool_type = tool_dict.get(
            "type",
            "function",
        )

        if tool_type != "function":
            raise ValueError(
                f"Tool at index {index} has unsupported "
                f"type {tool_type!r}."
            )

        function = tool_dict.get("function")

        if not isinstance(function, Mapping):
            raise ValueError(
                f"Tool at index {index} must contain "
                "a 'function' object."
            )

        function_dict = dict(function)

        name = function_dict.get("name")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"Tool at index {index} has no valid function name."
            )

        name = name.strip()

        if name in seen_names:
            raise ValueError(
                f"Duplicate tool name detected: {name!r}"
            )

        seen_names.add(name)

        description = function_dict.get(
            "description",
            "",
        )

        if description is not None and not isinstance(
            description,
            str,
        ):
            raise ValueError(
                f"Description for tool {name!r} must be a string."
            )

        parameters = function_dict.get(
            "parameters"
        )

        if parameters is not None:

            if not isinstance(
                parameters,
                Mapping,
            ):
                raise ValueError(
                    f"Parameters for tool {name!r} "
                    "must be a JSON-schema mapping."
                )

            parameters = dict(parameters)

            parameter_type = parameters.get(
                "type"
            )

            if (
                parameter_type is not None
                and parameter_type != "object"
            ):
                raise ValueError(
                    f"Tool {name!r} parameter schema "
                    "must have type='object'."
                )

        validated.append(
            {
                "type": "function",
                "function": {
                    **function_dict,
                    "name": name,
                },
            }
        )

    return validated


# ============================================================
# STRICT TOOL SUPPORT CHECK
# ============================================================


def tools_support_strict_mode(
    tools: Sequence[Mapping[str, Any]],
) -> bool:
    """
    Determine whether every supplied tool can safely use Cohere's
    strict_tools mode.

    Cohere strict tool mode has schema requirements.

    In particular, tools need required parameters.

    Some grocery tools may naturally have no parameters:

        view_cart()
        view_orders()
        get_shopping_list()

    Therefore we should NOT globally force strict_tools=True.

    Instead:

    - strict mode is enabled when schemas support it
    - otherwise normal tool calling remains available
    """

    if not tools:
        return False

    for tool in tools:

        function = tool.get("function")

        if not isinstance(function, Mapping):
            return False

        parameters = function.get("parameters")

        if not isinstance(parameters, Mapping):
            return False

        required = parameters.get("required")

        if not isinstance(required, list):
            return False

        if len(required) == 0:
            return False

    return True


# ============================================================
# RESPONSE TEXT EXTRACTION
# ============================================================


def _extract_response_text(
    response: Any,
) -> str:
    """
    Extract textual content from Cohere V2 response safely.

    Cohere V2 commonly exposes:

        response.message.content

    as a list of content blocks.

    We concatenate text blocks instead of blindly assuming [0].
    """

    try:
        message = getattr(
            response,
            "message",
            None,
        )

        if message is None:
            return ""

        content = getattr(
            message,
            "content",
            None,
        )

        if not content:
            return ""

        text_parts: list[str] = []

        # ----------------------------------------------------
        # Handle list-like V2 content blocks
        # ----------------------------------------------------

        if isinstance(
            content,
            Iterable,
        ) and not isinstance(
            content,
            (str, bytes, dict),
        ):

            for block in content:

                text = getattr(
                    block,
                    "text",
                    None,
                )

                if isinstance(text, str) and text:
                    text_parts.append(text)

                elif isinstance(block, Mapping):

                    block_text = block.get("text")

                    if isinstance(
                        block_text,
                        str,
                    ):
                        text_parts.append(
                            block_text
                        )

            return "".join(
                text_parts
            ).strip()

        # ----------------------------------------------------
        # Defensive fallback for string content
        # ----------------------------------------------------

        if isinstance(content, str):
            return content.strip()

    except Exception:
        logger.exception(
            "Failed while extracting Cohere response text."
        )

    return ""


# ============================================================
# TOOL ARGUMENT PARSER
# ============================================================


def _parse_tool_arguments(
    arguments: Any,
) -> tuple[dict[str, Any], str | None]:
    """
    Convert Cohere tool arguments into a Python dictionary.

    Cohere commonly returns function arguments as JSON text.

    Example:

        '{"query":"milk","limit":5}'

    becomes:

        {
            "query": "milk",
            "limit": 5
        }

    Returns
    -------
    tuple
        (parsed_arguments, raw_arguments)
    """

    # --------------------------------------------------------
    # Already a dictionary
    # --------------------------------------------------------

    if isinstance(arguments, Mapping):

        result = dict(arguments)

        try:
            raw = json.dumps(
                result,
                ensure_ascii=False,
            )
        except Exception:
            raw = None

        return result, raw

    # --------------------------------------------------------
    # Empty arguments
    # --------------------------------------------------------

    if arguments is None:
        return {}, None

    # --------------------------------------------------------
    # JSON string
    # --------------------------------------------------------

    if isinstance(arguments, str):

        raw = arguments.strip()

        if not raw:
            return {}, raw

        try:
            parsed = json.loads(raw)

        except json.JSONDecodeError as exc:

            raise CohereToolCallError(
                "Cohere returned invalid JSON "
                "for tool arguments."
            ) from exc

        if not isinstance(parsed, dict):
            raise CohereToolCallError(
                "Tool arguments must decode to "
                "a JSON object."
            )

        return parsed, raw

    raise CohereToolCallError(
        "Unsupported Cohere tool argument format: "
        f"{type(arguments).__name__}"
    )


# ============================================================
# TOOL CALL EXTRACTION
# ============================================================


def _extract_tool_calls(
    response: Any,
) -> list[CohereToolCall]:
    """
    Convert Cohere SDK tool-call objects into our internal model.
    """

    message = getattr(
        response,
        "message",
        None,
    )

    if message is None:
        return []

    raw_tool_calls = getattr(
        message,
        "tool_calls",
        None,
    )

    if not raw_tool_calls:
        return []

    normalized: list[CohereToolCall] = []

    for raw_call in raw_tool_calls:

        try:

            call_id = getattr(
                raw_call,
                "id",
                None,
            )

            call_type = getattr(
                raw_call,
                "type",
                "function",
            )

            function = getattr(
                raw_call,
                "function",
                None,
            )

            # -----------------------------------------------
            # Dict fallback
            # -----------------------------------------------

            if function is None and isinstance(
                raw_call,
                Mapping,
            ):
                call_id = raw_call.get("id")
                call_type = raw_call.get(
                    "type",
                    "function",
                )
                function = raw_call.get(
                    "function"
                )

            if function is None:
                raise CohereToolCallError(
                    "Tool call is missing function information."
                )

            # -----------------------------------------------
            # Extract function name / arguments
            # -----------------------------------------------

            if isinstance(
                function,
                Mapping,
            ):

                name = function.get("name")

                arguments = function.get(
                    "arguments"
                )

            else:

                name = getattr(
                    function,
                    "name",
                    None,
                )

                arguments = getattr(
                    function,
                    "arguments",
                    None,
                )

            if not isinstance(
                name,
                str,
            ) or not name.strip():

                raise CohereToolCallError(
                    "Cohere returned a tool call "
                    "without a valid function name."
                )

            parsed_arguments, raw_arguments = (
                _parse_tool_arguments(
                    arguments
                )
            )

            normalized.append(
                CohereToolCall(
                    id=str(
                        call_id
                        or f"{name}_{len(normalized)}"
                    ),
                    name=name.strip(),
                    arguments=parsed_arguments,
                    type=str(
                        call_type
                        or "function"
                    ),
                    raw_arguments=raw_arguments,
                )
            )

        except CohereToolCallError:
            raise

        except Exception as exc:
            raise CohereToolCallError(
                "Failed to normalize Cohere tool call."
            ) from exc

    return normalized


# ============================================================
# TOOL PLAN EXTRACTION
# ============================================================


def _extract_tool_plan(
    response: Any,
) -> str | None:
    """
    Extract Cohere's optional tool plan.

    This is INTERNAL orchestration information.

    Do not automatically expose it in Streamlit.
    """

    message = getattr(
        response,
        "message",
        None,
    )

    if message is None:
        return None

    tool_plan = getattr(
        message,
        "tool_plan",
        None,
    )

    if tool_plan is None:
        return None

    if isinstance(tool_plan, str):
        value = tool_plan.strip()

        return value or None

    return str(tool_plan)


# ============================================================
# NORMALIZE COMPLETE RESPONSE
# ============================================================


def normalize_response(
    response: Any,
) -> CohereBrainResponse:
    """
    Transform raw Cohere SDK response into an application response.
    """

    if response is None:
        raise CohereResponseError(
            "Cohere returned an empty response."
        )

    try:

        text = _extract_response_text(
            response
        )

        tool_calls = _extract_tool_calls(
            response
        )

        tool_plan = _extract_tool_plan(
            response
        )

        finish_reason = getattr(
            response,
            "finish_reason",
            None,
        )

        response_id = getattr(
            response,
            "id",
            None,
        )

        return CohereBrainResponse(
            text=text,
            tool_calls=tool_calls,
            tool_plan=tool_plan,
            finish_reason=(
                str(finish_reason)
                if finish_reason is not None
                else None
            ),
            response_id=(
                str(response_id)
                if response_id is not None
                else None
            ),
            raw_response=response,
        )

    except CohereToolCallError:
        raise

    except Exception as exc:

        logger.exception(
            "Failed to normalize Cohere response."
        )

        raise CohereResponseError(
            "Unable to process Cohere response."
        ) from exc


# ============================================================
# RETRY DELAY
# ============================================================


def _retry_delay(
    attempt: int,
    *,
    base_delay: float = DEFAULT_RETRY_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_RETRY_MAX_DELAY_SECONDS,
) -> float:
    """
    Exponential backoff with small random jitter.

    Example approximate delays:

        attempt 0 -> ~1 sec
        attempt 1 -> ~2 sec
        attempt 2 -> ~4 sec

    Jitter prevents many concurrent clients from retrying at exactly
    the same moment.
    """

    exponential = base_delay * (
        2 ** attempt
    )

    jitter = random.uniform(
        0.0,
        0.25,
    )

    return min(
        exponential + jitter,
        max_delay,
    )


# ============================================================
# MAIN CHAT FUNCTION
# ============================================================


def chat(
    *,
    user_message: str,
    chat_history: Sequence[Mapping[str, Any]] | None = None,
    tools: Sequence[Mapping[str, Any]] | None = None,
    additional_system_context: str | None = None,
    strict_tools: bool | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> CohereBrainResponse:
    """
    Send a user request to the Cohere central brain.

    Parameters
    ----------
    user_message:
        Current natural-language query.

    chat_history:
        Previous user/assistant conversation.

    tools:
        Available backend tool definitions.

    additional_system_context:
        Runtime-only context from brain.py.

        Example:

            "Authenticated user exists."

        Do NOT place secrets here.

    strict_tools:
        Controls Cohere strict tool schema enforcement.

        None:
            Automatically enable strict mode only if all supplied
            tools satisfy strict schema requirements.

        True:
            Request strict mode. If tool definitions cannot support
            it, safely fall back to False.

        False:
            Disable strict mode.

    max_retries:
        Maximum attempts for transient request failures.

    Returns
    -------
    CohereBrainResponse
        Normalized brain decision.
    """

    # --------------------------------------------------------
    # Validate arguments
    # --------------------------------------------------------

    user_message = _validate_user_message(
        user_message
    )

    if max_retries < 1:
        raise ValueError(
            "max_retries must be at least 1."
        )

    # --------------------------------------------------------
    # Build message sequence
    # --------------------------------------------------------

    messages = build_messages(
        user_message=user_message,
        chat_history=chat_history,
        include_system_rules=True,
        additional_system_context=additional_system_context,
    )

    # --------------------------------------------------------
    # Validate tool schemas
    # --------------------------------------------------------

    validated_tools = validate_tools(
        tools
    )

    # --------------------------------------------------------
    # Determine strict-tool behavior
    # --------------------------------------------------------

    supports_strict = (
        tools_support_strict_mode(
            validated_tools
        )
        if validated_tools
        else False
    )

    if strict_tools is None:

        use_strict_tools = (
            supports_strict
        )

    elif strict_tools is True:

        if supports_strict:

            use_strict_tools = True

        else:

            logger.warning(
                "strict_tools=True requested, but one or more "
                "tool schemas do not satisfy strict-mode "
                "requirements. Falling back to normal tool mode."
            )

            use_strict_tools = False

    else:

        use_strict_tools = False

    # --------------------------------------------------------
    # Client
    # --------------------------------------------------------

    client = get_cohere_client()

    # --------------------------------------------------------
    # Build request arguments
    # --------------------------------------------------------

    request_kwargs: dict[str, Any] = {
        "model": settings.cohere_model,
        "messages": messages,
    }

    if validated_tools:

        request_kwargs["tools"] = (
            validated_tools
        )

        request_kwargs["strict_tools"] = (
            use_strict_tools
        )

    # --------------------------------------------------------
    # Request with retries
    # --------------------------------------------------------

    last_exception: Exception | None = None

    for attempt in range(
        max_retries
    ):

        try:

            logger.debug(
                "Sending Cohere request. "
                "model=%s tools=%s attempt=%s",
                settings.cohere_model,
                len(validated_tools),
                attempt + 1,
            )

            response = client.chat(
                **request_kwargs
            )

            normalized = normalize_response(
                response
            )

            logger.debug(
                "Cohere response received. "
                "response_id=%s tools=%s finish_reason=%s",
                normalized.response_id,
                normalized.tool_names,
                normalized.finish_reason,
            )

            return normalized

        except (
            CohereResponseError,
            CohereToolCallError,
        ):
            # Response parsing problems should not normally be
            # repeatedly retried because the request itself succeeded.
            raise

        except Exception as exc:

            last_exception = exc

            logger.warning(
                "Cohere request failed on attempt %s/%s: %s",
                attempt + 1,
                max_retries,
                type(exc).__name__,
            )

            # Last attempt: stop immediately.
            if attempt >= max_retries - 1:
                break

            delay = _retry_delay(
                attempt
            )

            time.sleep(delay)

    # --------------------------------------------------------
    # All attempts failed
    # --------------------------------------------------------

    logger.error(
        "Cohere request failed after %s attempts.",
        max_retries,
    )

    raise CohereRequestError(
        "The central AI brain is temporarily unavailable."
    ) from last_exception


# ============================================================
# DIRECT MESSAGE-BASED CHAT
# ============================================================


def chat_with_messages(
    *,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    strict_tools: bool | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> CohereBrainResponse:
    """
    Advanced Cohere call where brain.py already has a complete message
    sequence.

    This will become useful for multi-step tool calling.

    Example future flow
    -------------------

    messages:

        system
        user
        assistant tool request
        tool result
        assistant

    This method intentionally does NOT automatically append central
    rules because the caller controls the complete message sequence.

    For ordinary user messages, use chat() instead.
    """

    if not messages:
        raise ValueError(
            "messages cannot be empty."
        )

    if max_retries < 1:
        raise ValueError(
            "max_retries must be at least 1."
        )

    normalized_messages: list[
        dict[str, Any]
    ] = []

    for index, message in enumerate(
        messages
    ):

        if not isinstance(
            message,
            Mapping,
        ):
            raise ValueError(
                f"Message {index} must be a mapping."
            )

        message_dict = dict(message)

        role = message_dict.get("role")

        if role not in ALLOWED_MESSAGE_ROLES:
            raise ValueError(
                f"Unsupported message role "
                f"{role!r} at index {index}."
            )

        normalized_messages.append(
            message_dict
        )

    validated_tools = validate_tools(
        tools
    )

    supports_strict = (
        tools_support_strict_mode(
            validated_tools
        )
        if validated_tools
        else False
    )

    if strict_tools is None:
        use_strict_tools = supports_strict

    elif strict_tools:
        use_strict_tools = supports_strict

    else:
        use_strict_tools = False

    request_kwargs: dict[str, Any] = {
        "model": settings.cohere_model,
        "messages": normalized_messages,
    }

    if validated_tools:

        request_kwargs["tools"] = (
            validated_tools
        )

        request_kwargs["strict_tools"] = (
            use_strict_tools
        )

    client = get_cohere_client()

    last_exception: Exception | None = None

    for attempt in range(
        max_retries
    ):

        try:

            response = client.chat(
                **request_kwargs
            )

            return normalize_response(
                response
            )

        except (
            CohereResponseError,
            CohereToolCallError,
        ):
            raise

        except Exception as exc:

            last_exception = exc

            logger.warning(
                "Cohere advanced chat failed "
                "on attempt %s/%s.",
                attempt + 1,
                max_retries,
            )

            if attempt >= max_retries - 1:
                break

            time.sleep(
                _retry_delay(
                    attempt
                )
            )

    raise CohereRequestError(
        "The central AI brain is temporarily unavailable."
    ) from last_exception


# ============================================================
# SIMPLE HEALTH CHECK
# ============================================================


def check_cohere_connection() -> bool:
    """
    Make a very small request to verify Cohere connectivity.

    Intended for:
    - startup diagnostics
    - developer testing

    Do NOT call this before every chatbot message because that would
    create an unnecessary additional API request.
    """

    client = get_cohere_client()

    try:

        response = client.chat(
            model=settings.cohere_model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Reply with exactly: OK"
                    ),
                }
            ],
        )

        text = _extract_response_text(
            response
        )

        return bool(text)

    except Exception:

        logger.exception(
            "Cohere connection check failed."
        )

        return False