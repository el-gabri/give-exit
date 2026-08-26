"""Embedding client port + adapters.

The mock adapter is not random: it hashes tokens into a fixed number of
buckets (bag-of-words projection), so texts sharing vocabulary really are
closer in cosine space. Retrieval tests exercise true ranking behavior
offline instead of asserting against arbitrary vectors.
"""

import asyncio
import hashlib
import math
import time
from typing import Any, Protocol, runtime_checkable

from google import genai
from google.genai import types
from openai import AsyncOpenAI

from app.core.logging import get_logger
from app.llm.base import TokenUsage
from app.llm.pricing import estimate_cost_usd

logger = get_logger(__name__)


@runtime_checkable
class EmbeddingClient(Protocol):
    """Purpose-aware embedding port used by new adapters."""

    @property
    def model_name(self) -> str: ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


@runtime_checkable
class BatchedQueryEmbeddingClient(Protocol):
    """Optional optimization for embedding several retrieval queries at once."""

    async def embed_queries(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class LegacyEmbeddingClient(Protocol):
    """Original embedding contract retained for third-party/test adapters."""

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


EmbeddingBackend = EmbeddingClient | LegacyEmbeddingClient


async def embed_document_texts(client: EmbeddingBackend, texts: list[str]) -> list[list[float]]:
    """Use the document-specific method, falling back to the v1 contract."""
    if isinstance(client, EmbeddingClient):
        return await client.embed_documents(texts)
    return await client.embed(texts)


async def embed_query_texts(client: EmbeddingBackend, texts: list[str]) -> list[list[float]]:
    """Embed retrieval queries without silently applying document semantics."""
    if isinstance(client, BatchedQueryEmbeddingClient):
        return await client.embed_queries(texts)
    if isinstance(client, EmbeddingClient):
        return list(await asyncio.gather(*(client.embed_query(text) for text in texts)))
    return await client.embed(texts)


class OpenAIEmbeddingClient:
    """EmbeddingClient backed by the OpenAI embeddings API."""

    document_format_version = "plain-document-v1"
    query_format_version = "instruction-prefix-v2"
    normalization = "l2"

    def __init__(
        self,
        api_key: str,
        model: str,
        batch_size: int = 128,
        query_instruction: str | None = None,
        model_revision: str | None = None,
    ) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model
        self._batch_size = batch_size
        self._query_instruction = query_instruction
        self._model_revision = model_revision

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Compatibility alias: unqualified text is treated as document text."""
        return await self._embed_batch(texts, purpose="document")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prepared = [_with_instruction(text, self._query_instruction) for text in texts]
        return await self._embed_batch(prepared, purpose="query")

    async def _embed_batch(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            t0 = time.perf_counter()
            response = await self._client.embeddings.create(model=self._model, input=batch)
            usage = TokenUsage(prompt_tokens=getattr(response.usage, "prompt_tokens", 0) or 0)
            logger.info(
                "embeddings_created",
                model=self._model,
                purpose=purpose,
                texts=len(batch),
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                tokens=usage.prompt_tokens,
                cost_usd=estimate_cost_usd(self._model, usage),
            )
            vectors.extend(_l2_normalize(item.embedding) for item in response.data)
        return vectors


class GeminiEmbeddingClient:
    """Purpose-aware embeddings through the native Google GenAI SDK.

    Gemini Embedding 2 aggregates a plain list of strings into one vector.
    Each input is therefore wrapped in its own ``Content`` object so the
    output cardinality always matches the number of chunks or queries.
    """

    FRAME_VERSION = "retrieval-prefix-v1"
    document_format_version = "gemini-retrieval-document-v1"
    query_format_version = "gemini-retrieval-query-v1"
    normalization = "l2"

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-2",
        *,
        dimensions: int = 768,
        batch_size: int = 8,
        query_instruction: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._client = client or genai.Client(api_key=api_key)
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._query_instruction = query_instruction
        self._is_embedding_2 = model.removeprefix("models/") == "gemini-embedding-2"
        self._model_revision = f"dimensions={dimensions};frame={self.FRAME_VERSION}"

    @property
    def model_name(self) -> str:
        return self._model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Compatibility alias: unqualified text is treated as a document."""
        return await self.embed_documents(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prepared = [self._prepare_document(text) for text in texts]
        return await self._embed_batches(prepared, purpose="document")

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prepared = [self._prepare_query(text) for text in texts]
        return await self._embed_batches(prepared, purpose="query")

    def _prepare_document(self, text: str) -> str:
        if self._is_embedding_2:
            return f"title: none | text: {text}"
        return text

    def _prepare_query(self, text: str) -> str:
        instructed = _with_instruction(text, self._query_instruction)
        if self._is_embedding_2:
            return f"task: search result | query: {instructed}"
        return instructed

    async def _embed_batches(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            # The SDK accepts this documented list-of-Content shape, but its
            # generated union annotation is not covariant under strict MyPy.
            contents: Any = [
                types.Content(parts=[types.Part.from_text(text=text)]) for text in batch
            ]
            config = types.EmbedContentConfig(output_dimensionality=self._dimensions)
            if not self._is_embedding_2:
                config.task_type = (
                    "RETRIEVAL_DOCUMENT" if purpose == "document" else "RETRIEVAL_QUERY"
                )

            t0 = time.perf_counter()
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=contents,
                config=config,
            )
            embeddings = list(getattr(response, "embeddings", None) or [])
            if len(embeddings) != len(batch):
                raise ValueError(
                    "Gemini embedding response cardinality mismatch: "
                    f"expected {len(batch)}, received {len(embeddings)}"
                )
            for embedding in embeddings:
                values = getattr(embedding, "values", None)
                if not values:
                    raise ValueError("Gemini returned an embedding without values")
                vector = [float(value) for value in values]
                norm = math.sqrt(sum(value * value for value in vector)) or 1.0
                vectors.append([value / norm for value in vector])
            logger.info(
                "embeddings_created",
                provider="gemini",
                model=self._model,
                dimensions=self._dimensions,
                purpose=purpose,
                texts=len(batch),
                latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            )
        return vectors


class MockEmbeddingClient:
    """Deterministic bag-of-words embeddings for tests/offline mode."""

    document_format_version = "plain-document-v1"
    query_format_version = "instruction-prefix-v2"
    normalization = "l2"

    def __init__(self, dimensions: int = 128, query_instruction: str | None = None) -> None:
        self._dimensions = dimensions
        self._query_instruction = query_instruction
        self._model_revision = "mock-hashed-bow-v1"

    @property
    def model_name(self) -> str:
        return f"mock-hashed-bow-v1:{self._dimensions}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prepared = [_with_instruction(text, self._query_instruction) for text in texts]
        return await self.embed(prepared)

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        for token in text.lower().split():
            digest = hashlib.md5(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimensions
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


class SentenceTransformerEmbeddingClient:
    """Local multilingual embeddings through the optional ST dependency.

    The model name is entirely configuration-driven, so the same adapter works
    with ``ufca-llms/jua-4B-mixed``, ``BAAI/bge-m3`` and compatible models.
    The dependency and model weights are loaded lazily on the first embedding
    call. This keeps API startup and persisted-index readiness checks fast;
    pre-indexing still fails explicitly if the optional dependency is absent.
    """

    document_format_version = "plain-document-v1"
    query_format_version = "instruction-prefix-v2"
    normalization = "l2"

    def __init__(
        self,
        model: str,
        *,
        query_instruction: str | None = None,
        device: str | None = None,
        batch_size: int = 8,
        model_revision: str | None = None,
        show_progress_bar: bool = False,
    ) -> None:
        self._model_name = model
        self._query_instruction = query_instruction
        self._device = device
        self._batch_size = batch_size
        self._model_revision = model_revision
        self._show_progress_bar = show_progress_bar
        self._encode_lock = asyncio.Lock()
        self._model: Any | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Compatibility alias: unqualified text is treated as document text."""
        return await self.embed_documents(texts)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        async with self._encode_lock:
            return await asyncio.to_thread(self._encode, texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_queries([text]))[0]

    async def embed_queries(self, texts: list[str]) -> list[list[float]]:
        prepared = [_with_instruction(text, self._query_instruction) for text in texts]
        async with self._encode_lock:
            return await asyncio.to_thread(self._encode, prepared)

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            try:
                import sentence_transformers
            except ImportError as exc:  # pragma: no cover - environment-specific
                raise RuntimeError(
                    "sentence-transformers is required for local embeddings; "
                    "install the 'local-embeddings' optional dependency"
                ) from exc
            logger.info(
                "local_embedding_model_loading",
                model=self._model_name,
                device=self._device or "auto",
            )
            started = time.perf_counter()
            self._model = sentence_transformers.SentenceTransformer(
                self._model_name,
                device=self._device,
                revision=self._model_revision,
            )
            logger.info(
                "local_embedding_model_loaded",
                model=self._model_name,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        encoded = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=self._show_progress_bar,
            batch_size=self._batch_size,
        )
        return [[float(value) for value in row] for row in encoded]


def _with_instruction(text: str, instruction: str | None) -> str:
    if not instruction or not instruction.strip():
        return text
    prefix = instruction.strip()
    separator = "\n" if prefix.casefold().endswith("query:") else " "
    return f"{prefix}{separator}{text.lstrip()}"


def validate_embedding_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
    expected_dimension: int | None = None,
    require_l2_normalized: bool = True,
    norm_tolerance: float = 0.05,
) -> int:
    """Validate the stable vector contract and return its single dimension."""

    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors for {expected_count} texts"
        )
    if not vectors:
        if expected_count == 0:
            return expected_dimension or 0
        raise ValueError("embedding provider returned no vectors")
    dimensions = {len(vector) for vector in vectors}
    if 0 in dimensions or len(dimensions) != 1:
        raise ValueError("embedding vectors must have one non-zero dimension")
    [dimension] = dimensions
    if expected_dimension is not None and dimension != expected_dimension:
        raise ValueError(
            f"embedding dimension mismatch: expected {expected_dimension}, received {dimension}"
        )
    for vector in vectors:
        if not all(math.isfinite(float(value)) for value in vector):
            raise ValueError("embedding vectors must contain only finite values")
        if require_l2_normalized:
            norm = math.sqrt(sum(float(value) * float(value) for value in vector))
            if abs(norm - 1.0) > norm_tolerance:
                raise ValueError(
                    f"embedding vector is not L2-normalized: norm={norm:.6f}"
                )
    return dimension


def _l2_normalize(values: list[float]) -> list[float]:
    vector = [float(value) for value in values]
    if not vector or not all(math.isfinite(value) for value in vector):
        raise ValueError("embedding vectors must be non-empty and finite")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        raise ValueError("embedding vectors must have non-zero norm")
    return [value / norm for value in vector]
