"""Application configuration.

Single source of truth for all runtime settings, loaded from environment
variables (prefix ``LITIGATION_``) or a local ``.env`` file. Every component
receives a ``Settings`` instance via dependency injection instead of reading
``os.environ`` directly, which keeps configuration testable and explicit.
"""

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    """Supported LLM backends. Adding a provider = new enum value + new client."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    MOCK = "mock"


class VectorStoreBackend(str, Enum):
    """Supported vector store backends (see ADR 0003)."""

    CHROMA = "chroma"
    MEMORY = "memory"


class EmbeddingProvider(str, Enum):
    """Embedding backends independent from the conversational LLM provider."""

    AUTO = "auto"
    OPENAI = "openai"
    GEMINI = "gemini"
    SENTENCE_TRANSFORMERS = "sentence_transformers"
    MOCK = "mock"


class RetrievalMode(str, Enum):
    """Candidate-generation strategies supported by the RAG pipeline."""

    DENSE = "dense"
    HYBRID = "hybrid"


class RerankerProvider(str, Enum):
    """Optional second-stage retrieval rankers."""

    NONE = "none"
    SENTENCE_TRANSFORMERS = "sentence_transformers"


class PromptInjectionScanMode(str, Enum):
    """How the pre-analysis document security gate reviews suspicious text."""

    RULES = "rules"
    BALANCED = "balanced"
    STRICT = "strict"


class DeploymentMode(str, Enum):
    """Runtime posture used to enforce deployment-only safety invariants."""

    LOCAL = "local"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime configuration for the whole application."""

    model_config = SettingsConfigDict(
        env_prefix="LITIGATION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM ---
    # A fresh clone starts in an explicitly offline demonstration mode. Real
    # providers never fall back to mock when their configured key is missing.
    llm_provider: LLMProvider = LLMProvider.MOCK
    openai_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    gemini_api_key: str | None = Field(default=None, repr=False)
    # Optional provider-independent override. When omitted, the factory picks
    # a valid default for OpenAI, Anthropic or Gemini.
    llm_model: str | None = None
    llm_max_output_tokens: int = Field(default=8192, ge=1)
    # Total attempts per LLM call for transient provider errors (429/5xx,
    # timeouts, dropped connections). 1 disables retrying.
    llm_retry_max_attempts: int = Field(default=3, ge=1)
    embedding_provider: EmbeddingProvider = EmbeddingProvider.AUTO
    # Optional override. AUTO resolves a provider-specific default instead of
    # accidentally sending another vendor's model name.
    embedding_model: str | None = None
    embedding_model_revision: str | None = None
    embedding_query_instruction: str | None = None
    embedding_device: str | None = None
    embedding_batch_size: int = Field(default=8, ge=1)
    embedding_show_progress_bar: bool = False
    gemini_embedding_dimensions: int = Field(default=768, ge=128, le=3072)

    # --- Storage ---
    data_dir: Path = Path("./data")
    vector_store: VectorStoreBackend = VectorStoreBackend.CHROMA
    max_document_pages: int = Field(default=250, ge=1)

    # --- RAG ---
    chunk_target_chars: int = 1200
    chunk_overlap_chars: int = 150
    retrieval_k: int = Field(default=6, ge=1)
    retrieval_trace_include_previews: bool = False
    # Consumer queries need semantic paraphrase matching and exact statutory,
    # protocol, date and monetary terms, so hybrid is the product default.
    retrieval_mode: RetrievalMode = RetrievalMode.HYBRID
    retrieval_candidate_multiplier: int = Field(default=4, ge=1)
    retrieval_rrf_constant: int = Field(default=60, ge=1)
    retrieval_dense_weight: float = Field(default=1.0, gt=0)
    retrieval_lexical_weight: float = Field(default=1.0, gt=0)
    rag_corpus_version: str = Field(default="consumer-documents-v1", min_length=1)
    reranker_provider: RerankerProvider = RerankerProvider.NONE
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_model_revision: str | None = None
    reranker_device: str | None = None
    reranker_batch_size: int = Field(default=8, ge=1)

    # --- Document security ---
    # Every mode applies deterministic bilingual rules to every page. Balanced
    # reviews suspicious candidates; strict semantically reviews all page text.
    prompt_injection_scan_mode: PromptInjectionScanMode = PromptInjectionScanMode.BALANCED
    prompt_injection_strict_max_chars: int = Field(default=500_000, ge=1)
    prompt_injection_strict_max_batches: int = Field(default=64, ge=1)

    # --- API ---
    deployment_mode: DeploymentMode = DeploymentMode.LOCAL
    # Comma-separated list of browser origins allowed by CORS. The default
    # covers the local Streamlit frontend; production deployments override it.
    cors_allow_origins: str = "http://localhost:8501"
    # When set, every route except /health requires the X-API-Key header.
    # The local demo stays open; any network-reachable deployment must set it.
    api_auth_key: str | None = Field(default=None, repr=False)
    # Per-client ceiling for the expensive upload endpoints (OCR + scan + LLM).
    # 0 disables the limiter.
    upload_rate_limit_per_minute: int = Field(default=20, ge=0)
    # --- Logging ---
    log_level: str = "INFO"
    # Optional high-entropy HMAC key for privacy-safe telemetry references.
    # Configure this through a secret manager when references must correlate
    # across processes. If omitted, correlation is process-local by design.
    telemetry_pseudonym_key: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _production_requires_authentication(self) -> "Settings":
        if (
            self.deployment_mode is DeploymentMode.PRODUCTION
            and not (self.api_auth_key or "").strip()
        ):
            raise ValueError(
                "LITIGATION_API_AUTH_KEY is required when "
                "LITIGATION_DEPLOYMENT_MODE=production"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    @property
    def chroma_dir(self) -> Path:
        return self.data_dir / "chroma"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor for entry points (API, CLI, UI).

    Components should still receive ``Settings`` as a constructor argument;
    this accessor exists only at composition roots.
    """
    return Settings()
