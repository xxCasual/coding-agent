from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ProtocolMode = Literal["native_tools", "legacy_json"]


@dataclass(frozen=True)
class ModelProfile:
    id: str
    base_url: str
    model: str
    api_key_env: str
    protocol_mode: ProtocolMode = "native_tools"
    timeout_seconds: float = 45.0
    max_retries: int = 1
    context_window_tokens: int | None = None
    price_input_per_1m: float | None = None
    price_output_per_1m: float | None = None
    price_as_of: str | None = None
    verified: bool = False


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_MODEL")
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")
    review_store_path: str = Field(default="reviews.sqlite3", alias="REVIEW_STORE_PATH")
    repo_cache_dir: str = Field(default=".cache/repos", alias="REPO_CACHE_DIR")
    review_agent_demo_fixture: str | None = Field(default=None, alias="REVIEW_AGENT_DEMO_FIXTURE")
    review_agent_llm_timeout_seconds: float = Field(
        default=45.0, alias="REVIEW_AGENT_LLM_TIMEOUT_SECONDS"
    )
    review_agent_llm_max_retries: int = Field(default=1, alias="REVIEW_AGENT_LLM_MAX_RETRIES")
    review_agent_model_protocol: ProtocolMode = Field(
        default="native_tools", alias="REVIEW_AGENT_MODEL_PROTOCOL"
    )
    review_agent_alt_base_url: str | None = Field(
        default=None, alias="REVIEW_AGENT_ALT_BASE_URL"
    )
    review_agent_alt_model: str | None = Field(default=None, alias="REVIEW_AGENT_ALT_MODEL")
    review_agent_alt_api_key_env: str = Field(
        default="DEEPSEEK_API_KEY", alias="REVIEW_AGENT_ALT_API_KEY_ENV"
    )
    review_agent_price_input_per_1m: float | None = Field(
        default=None, alias="REVIEW_AGENT_PRICE_INPUT_PER_1M"
    )
    review_agent_price_output_per_1m: float | None = Field(
        default=None, alias="REVIEW_AGENT_PRICE_OUTPUT_PER_1M"
    )
    review_agent_price_as_of: str | None = Field(
        default=None, alias="REVIEW_AGENT_PRICE_AS_OF"
    )
    review_agent_budget_path: str | None = Field(
        default=None, alias="REVIEW_AGENT_BUDGET_PATH",
        description="Existing shared CNY ledger; enables bounded official Flash calls, no SDK retries.",
    )
    review_agent_log_level: str = Field(default="INFO", alias="REVIEW_AGENT_LOG_LEVEL")
    review_agent_memory_enabled: bool = Field(
        default=True, alias="REVIEW_AGENT_MEMORY_ENABLED"
    )
    review_agent_memory_dir: str = Field(default=".memory", alias="REVIEW_AGENT_MEMORY_DIR")
    review_agent_memory_max_loaded: int = Field(
        default=5, alias="REVIEW_AGENT_MEMORY_MAX_LOADED"
    )
    review_agent_memory_compact_file_threshold: int = Field(
        default=10, alias="REVIEW_AGENT_MEMORY_COMPACT_FILE_THRESHOLD"
    )
    review_agent_agent_max_steps: int = Field(default=24, alias="REVIEW_AGENT_AGENT_MAX_STEPS")
    review_agent_reviewer_max_steps: int = Field(
        default=6, alias="REVIEW_AGENT_REVIEWER_MAX_STEPS"
    )
    review_agent_max_verification_repairs: int = Field(
        default=2, alias="REVIEW_AGENT_MAX_VERIFICATION_REPAIRS"
    )
    review_agent_context_window_tokens: int | None = Field(
        default=None, alias="REVIEW_AGENT_CONTEXT_WINDOW_TOKENS"
    )
    review_agent_context_reserve_output_tokens: int = Field(
        default=4096, alias="REVIEW_AGENT_CONTEXT_RESERVE_OUTPUT_TOKENS"
    )
    review_agent_command_timeout_seconds: int = Field(
        default=30, alias="REVIEW_AGENT_COMMAND_TIMEOUT_SECONDS"
    )
    review_agent_approval_mode: str = Field(default="confirm", alias="REVIEW_AGENT_APPROVAL_MODE")
    review_agent_data_root: str = Field(
        default="~/.review-agent", alias="REVIEW_AGENT_DATA_ROOT"
    )
    review_agent_executor_backend: Literal["host", "docker"] = Field(
        default="docker",
        alias="REVIEW_AGENT_EXECUTOR_BACKEND",
        description="Project commands default to docker. Set host for explicit local-dev / pytest.",
    )
    review_agent_task_image: str = Field(
        default="python:3.12-slim", alias="REVIEW_AGENT_TASK_IMAGE"
    )
    review_agent_command_preview_chars: int = Field(
        default=6000, alias="REVIEW_AGENT_COMMAND_PREVIEW_CHARS"
    )
    review_agent_docker_network: str = Field(
        default="none", alias="REVIEW_AGENT_DOCKER_NETWORK"
    )
    review_agent_skills_enabled: bool = Field(
        default=True,
        alias="REVIEW_AGENT_SKILLS_ENABLED",
        description="When false, skip SkillLoader, skill tools, and skill hint injection.",
    )
    review_agent_context_optimization: bool = Field(
        default=True,
        alias="REVIEW_AGENT_CONTEXT_OPTIMIZATION",
        description="When false, skip history summaries and changed-file snippet injection.",
    )
    review_agent_skills_extra_roots: str = Field(
        default="",
        alias="REVIEW_AGENT_SKILLS_EXTRA_ROOTS",
        description="Colon-separated absolute skill roots (optional); never scans the whole disk.",
    )
    review_agent_mcp_servers: str = Field(
        default="",
        alias="REVIEW_AGENT_MCP_SERVERS",
        description="JSON array of stdio MCP servers: server_id, command argv, env_allowlist, timeout_seconds.",
    )
    review_agent_mcp_timeout_seconds: float = Field(
        default=30.0,
        alias="REVIEW_AGENT_MCP_TIMEOUT_SECONDS",
    )
    review_agent_database_url: str = Field(
        default="",
        alias="REVIEW_AGENT_DATABASE_URL",
        description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@localhost:5432/review_agent",
    )
    review_agent_celery_broker: str = Field(
        default="",
        alias="REVIEW_AGENT_CELERY_BROKER",
        description="Celery broker URL, e.g. redis://localhost:6379/0. Empty disables publish.",
    )
    review_agent_lease_seconds: int = Field(
        default=60,
        alias="REVIEW_AGENT_LEASE_SECONDS",
    )
    review_agent_api_url: str = Field(
        default="http://127.0.0.1:8000",
        alias="REVIEW_AGENT_API_URL",
        description="Base URL for CLI/Web TaskService clients.",
    )

    def model_profiles(self) -> dict[str, ModelProfile]:
        price_in = self.review_agent_price_input_per_1m
        price_out = self.review_agent_price_output_per_1m
        price_as_of = self.review_agent_price_as_of
        context_window = self.review_agent_context_window_tokens
        deepseek = ModelProfile(
            id="deepseek",
            base_url=self.deepseek_base_url,
            model=self.deepseek_model,
            api_key_env="DEEPSEEK_API_KEY",
            protocol_mode=self.review_agent_model_protocol,
            timeout_seconds=self.review_agent_llm_timeout_seconds,
            max_retries=self.review_agent_llm_max_retries,
            context_window_tokens=context_window,
            price_input_per_1m=price_in,
            price_output_per_1m=price_out,
            price_as_of=price_as_of,
            verified=False,
        )
        profiles = {"deepseek": deepseek}
        if self.review_agent_alt_model or self.review_agent_alt_base_url:
            profiles["alt"] = ModelProfile(
                id="alt",
                base_url=self.review_agent_alt_base_url or self.deepseek_base_url,
                model=self.review_agent_alt_model or self.deepseek_model,
                api_key_env=self.review_agent_alt_api_key_env,
                protocol_mode=self.review_agent_model_protocol,
                timeout_seconds=self.review_agent_llm_timeout_seconds,
                max_retries=self.review_agent_llm_max_retries,
                context_window_tokens=context_window,
                price_input_per_1m=price_in,
                price_output_per_1m=price_out,
                price_as_of=price_as_of,
                verified=False,
            )
        return profiles

    def get_model_profile(self, profile_id: str | None = None) -> ModelProfile:
        profiles = self.model_profiles()
        key = profile_id or "deepseek"
        if key not in profiles:
            raise KeyError(f"Unknown model profile: {key}")
        return profiles[key]

    def api_key_for_profile(self, profile: ModelProfile) -> str | None:
        if profile.api_key_env == "DEEPSEEK_API_KEY":
            return self.deepseek_api_key
        import os

        return os.environ.get(profile.api_key_env)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
