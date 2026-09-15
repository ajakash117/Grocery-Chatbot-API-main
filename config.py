"""
config.py

Central configuration module for the Grocery Chatbot.

Responsibilities
----------------
1. Load environment variables from the project's `.env` file.
2. Convert environment variables into the correct Python types.
3. Validate required configuration.
4. Validate numeric ranges such as ports, temperatures, and RAG thresholds.
5. Expose one immutable settings object for the entire application.
6. Prevent API secrets from accidentally appearing in logs/debug output.

This module MUST NOT:
---------------------
- Connect to Supabase
- Connect to Groq
- Connect to Cohere
- Run Streamlit
- Perform authentication
- Perform database queries
- Contain chatbot/business logic

Every other module should import configuration from here instead of
calling os.getenv() independently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# ============================================================
# PROJECT PATHS
# ============================================================

# config.py is located in the project root:
#
# grocery_chatbot/
# ├── config.py
# ├── .env
# ├── app.py
# └── ...
#
PROJECT_ROOT = Path(__file__).resolve().parent

ENV_FILE = PROJECT_ROOT / ".env"


# ============================================================
# LOAD .ENV
# ============================================================

# override=False means:
#
# If an environment variable is already supplied by the deployment
# platform, Docker, CI/CD, Hugging Face, etc., that value wins over
# the local `.env` file.
#
# This is important for production deployment.
load_dotenv(
    dotenv_path=ENV_FILE,
    override=False,
)


# ============================================================
# CONFIGURATION EXCEPTION
# ============================================================


class ConfigurationError(RuntimeError):
    """
    Raised when application configuration is missing or invalid.

    Using our own exception makes startup failures much easier to
    understand than allowing a random ValueError later in the app.
    """

    pass


# ============================================================
# INTERNAL ENVIRONMENT HELPERS
# ============================================================


def _get_string(
    name: str,
    default: str | None = None,
    *,
    required: bool = False,
) -> str | None:
    """
    Read a string environment variable.

    Whitespace surrounding the value is removed.

    Parameters
    ----------
    name:
        Environment variable name.

    default:
        Value returned if the variable is absent.

    required:
        If True, an exception is raised when the variable does not exist
        or contains only whitespace.
    """

    value = os.getenv(name)

    if value is None:
        value = default

    if value is not None:
        value = value.strip()

    if required and not value:
        raise ConfigurationError(
            f"Required environment variable '{name}' is missing or empty."
        )

    return value


def _get_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """
    Read and validate an integer environment variable.
    """

    raw_value = _get_string(name)

    if raw_value is None or raw_value == "":
        value = default
    else:
        try:
            value = int(raw_value)
        except ValueError as exc:
            raise ConfigurationError(
                f"Environment variable '{name}' must be an integer. "
                f"Received: {raw_value!r}"
            ) from exc

    if minimum is not None and value < minimum:
        raise ConfigurationError(
            f"Environment variable '{name}' must be >= {minimum}. "
            f"Received: {value}"
        )

    if maximum is not None and value > maximum:
        raise ConfigurationError(
            f"Environment variable '{name}' must be <= {maximum}. "
            f"Received: {value}"
        )

    return value


def _get_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """
    Read and validate a floating-point environment variable.
    """

    raw_value = _get_string(name)

    if raw_value is None or raw_value == "":
        value = default
    else:
        try:
            value = float(raw_value)
        except ValueError as exc:
            raise ConfigurationError(
                f"Environment variable '{name}' must be numeric. "
                f"Received: {raw_value!r}"
            ) from exc

    if minimum is not None and value < minimum:
        raise ConfigurationError(
            f"Environment variable '{name}' must be >= {minimum}. "
            f"Received: {value}"
        )

    if maximum is not None and value > maximum:
        raise ConfigurationError(
            f"Environment variable '{name}' must be <= {maximum}. "
            f"Received: {value}"
        )

    return value


# ============================================================
# SETTINGS MODEL
# ============================================================


@dataclass(frozen=True, slots=True)
class Settings:
    """
    Immutable application configuration.

    `frozen=True`
        Prevents code from accidentally changing configuration at runtime.

    `slots=True`
        Avoids arbitrary attributes being attached to the settings object.
    """

    # --------------------------------------------------------
    # Application
    # --------------------------------------------------------

    app_name: str
    app_env: str

    app_host: str
    app_port: int

    streamlit_server_address: str
    streamlit_server_port: int

    # --------------------------------------------------------
    # Groq
    # --------------------------------------------------------

    groq_api_key: str
    groq_model: str
    groq_temperature: float

    # --------------------------------------------------------
    # Supabase
    # --------------------------------------------------------

    supabase_url: str

    # Used for user-authenticated operations.
    supabase_publishable_key: str

    # Server-only privileged key.
    #
    # NEVER expose this key through Streamlit UI,
    # browser JavaScript, chatbot responses, logs, etc.
    supabase_secret_key: str

    # --------------------------------------------------------
    # Cohere
    # --------------------------------------------------------

    cohere_api_key: str
    cohere_model: str

    # --------------------------------------------------------
    # RAG
    # --------------------------------------------------------

    embedding_model: str
    rag_match_count: int
    rag_similarity_threshold: float

    # --------------------------------------------------------
    # Chat
    # --------------------------------------------------------

    max_chat_history: int

    # ========================================================
    # CONVENIENCE PROPERTIES
    # ========================================================

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_testing(self) -> bool:
        return self.app_env == "test"

    # ========================================================
    # SAFE DEBUG INFORMATION
    # ========================================================

    def safe_dict(self) -> dict[str, Any]:
        """
        Return configuration information that is safe to print.

        API keys are intentionally masked.

        Example
        -------

        print(settings.safe_dict())

        This is safe.

        Avoid:

        print(settings.__dict__)

        or printing API keys directly.
        """

        return {
            "app_name": self.app_name,
            "app_env": self.app_env,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "streamlit_server_address": self.streamlit_server_address,
            "streamlit_server_port": self.streamlit_server_port,

            "groq_api_key": _mask_secret(self.groq_api_key),
            "groq_model": self.groq_model,
            "groq_temperature": self.groq_temperature,

            "supabase_url": self.supabase_url,
            "supabase_publishable_key": _mask_secret(
                self.supabase_publishable_key
            ),
            "supabase_secret_key": _mask_secret(
                self.supabase_secret_key
            ),

            "cohere_api_key": _mask_secret(self.cohere_api_key),
            "cohere_model": self.cohere_model,

            "embedding_model": self.embedding_model,
            "rag_match_count": self.rag_match_count,
            "rag_similarity_threshold": self.rag_similarity_threshold,

            "max_chat_history": self.max_chat_history,
        }


# ============================================================
# SECRET MASKING
# ============================================================


def _mask_secret(value: str | None) -> str:
    """
    Mask a secret before displaying it.

    Example:

        gsk_abcdefgh123456

    becomes:

        gsk_...3456
    """

    if not value:
        return "<missing>"

    if len(value) <= 8:
        return "********"

    return f"{value[:4]}...{value[-4:]}"


# ============================================================
# SETTINGS FACTORY
# ============================================================


def _build_settings() -> Settings:
    """
    Build and validate the Settings object.

    This function is intentionally separate from `get_settings()`
    so creation and caching remain clean.
    """

    # --------------------------------------------------------
    # Application
    # --------------------------------------------------------

    app_name = _get_string(
        "APP_NAME",
        "Voice Command Shopping Assistant",
    )

    app_env = _get_string(
        "APP_ENV",
        "development",
    )

    if app_env:
        app_env = app_env.lower()

    allowed_environments = {
        "development",
        "test",
        "staging",
        "production",
    }

    if app_env not in allowed_environments:
        raise ConfigurationError(
            "APP_ENV must be one of: "
            "development, test, staging, production. "
            f"Received: {app_env!r}"
        )

    app_host = _get_string(
        "APP_HOST",
        "0.0.0.0",
    )

    app_port = _get_int(
        "APP_PORT",
        5173,
        minimum=1,
        maximum=65535,
    )

    streamlit_server_address = _get_string(
        "STREAMLIT_SERVER_ADDRESS",
        "0.0.0.0",
    )

    streamlit_server_port = _get_int(
        "STREAMLIT_SERVER_PORT",
        5173,
        minimum=1,
        maximum=65535,
    )

    # --------------------------------------------------------
    # Groq
    # --------------------------------------------------------

    groq_api_key = _get_string(
        "GROQ_API_KEY",
        required=True,
    )

    groq_model = _get_string(
        "GROQ_MODEL",
        "openai/gpt-oss-120b",
    )

    groq_temperature = _get_float(
        "GROQ_TEMPERATURE",
        0.0,
        minimum=0.0,
        maximum=2.0,
    )

    # --------------------------------------------------------
    # Supabase
    # --------------------------------------------------------

    supabase_url = _get_string(
        "SUPABASE_URL",
        required=True,
    )

    supabase_publishable_key = _get_string(
        "SUPABASE_PUBLISHABLE_KEY",
        required=True,
    )

    supabase_secret_key = _get_string(
        "SUPABASE_SECRET_KEY",
        required=True,
    )

    # Basic URL validation.
    #
    # We don't over-validate the hostname because Supabase setup/domain
    # formats can evolve. We only reject obviously wrong values.
    if not (
        supabase_url.startswith("https://")
        or (
            app_env == "development"
            and supabase_url.startswith("http://")
        )
    ):
        raise ConfigurationError(
            "SUPABASE_URL must start with 'https://'. "
            "An 'http://' URL is allowed only during development."
        )

    # --------------------------------------------------------
    # Cohere
    # --------------------------------------------------------

    cohere_api_key = _get_string(
        "COHERE_API_KEY",
        required=True,
    )

    cohere_model = _get_string(
        "COHERE_MODEL",
        "command-a-plus-05-2026",
    )

    # --------------------------------------------------------
    # RAG
    # --------------------------------------------------------

    embedding_model = _get_string(
        "EMBEDDING_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )

    rag_match_count = _get_int(
        "RAG_MATCH_COUNT",
        5,
        minimum=1,
        maximum=100,
    )

    rag_similarity_threshold = _get_float(
        "RAG_SIMILARITY_THRESHOLD",
        0.35,
        minimum=0.0,
        maximum=1.0,
    )

    # --------------------------------------------------------
    # Chat
    # --------------------------------------------------------

    max_chat_history = _get_int(
        "MAX_CHAT_HISTORY",
        10,
        minimum=1,
        maximum=100,
    )

    # --------------------------------------------------------
    # Construct immutable Settings
    # --------------------------------------------------------

    return Settings(
        app_name=app_name or "Voice Command Shopping Assistant",
        app_env=app_env,
        app_host=app_host or "0.0.0.0",
        app_port=app_port,

        streamlit_server_address=(
            streamlit_server_address or "0.0.0.0"
        ),
        streamlit_server_port=streamlit_server_port,

        groq_api_key=groq_api_key,
        groq_model=groq_model or "openai/gpt-oss-120b",
        groq_temperature=groq_temperature,

        supabase_url=supabase_url,
        supabase_publishable_key=supabase_publishable_key,
        supabase_secret_key=supabase_secret_key,

        cohere_api_key=cohere_api_key,
        cohere_model=cohere_model or "command-a-plus-05-2026",

        embedding_model=(
            embedding_model
            or "sentence-transformers/all-MiniLM-L6-v2"
        ),
        rag_match_count=rag_match_count,
        rag_similarity_threshold=rag_similarity_threshold,

        max_chat_history=max_chat_history,
    )


# ============================================================
# CACHED SETTINGS
# ============================================================


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the single shared Settings instance.

    The configuration is constructed only once per Python process.

    Example
    -------

    from config import get_settings

    settings = get_settings()

    print(settings.groq_model)
    """

    return _build_settings()


# ============================================================
# DEFAULT SHARED INSTANCE
# ============================================================

# This allows either style:
#
#     from config import settings
#
# or:
#
#     from config import get_settings
#     settings = get_settings()
#
# Both refer to the same immutable configuration.
settings = get_settings()