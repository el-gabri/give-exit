"""PostgreSQL adapter against a real database.

Runs only when ``GIVE_EXIT_TEST_POSTGRES_DSN`` names a database with the
``vector`` extension enabled; CI and the default suite skip it. Each test
writes to its own namespace and deletes it afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.retrieval import build_legal_queries_for_case
from app.rag.vector_store import InMemoryVectorStore, PostgresVectorStore
from app.schemas.rag import Chunk

DSN = os.environ.get("GIVE_EXIT_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="GIVE_EXIT_TEST_POSTGRES_DSN is not set")
psycopg = pytest.importorskip("psycopg") if DSN else None


@pytest.fixture
async def store() -> AsyncIterator[PostgresVectorStore]:
    namespace = f"test-{uuid.uuid4().hex[:12]}"
    store = PostgresVectorStore(dsn=DSN, index_name=namespace, pool_max_size=2)
    try:
        yield store
    finally:
        for doc_id in await store.list_document_ids():
            await store.delete_document(doc_id)
        store.close()


async def test_bm25_matches_the_in_process_ranking_exactly(store: PostgresVectorStore) -> None:
    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()
    await store.upsert(chunks, [[1.0, 0.0]] * len(chunks))
    reference = InMemoryVectorStore()
    await reference.upsert(chunks, [[1.0, 0.0]] * len(chunks))
    queries = build_legal_queries_for_case(
        complaint="A loja cobrou duas vezes a mesma compra no cartão.",
        desired_resolution="Quero a devolução em dobro do valor cobrado indevidamente.",
    )

    for query in queries:
        database = await store.lexical_query(query, corpus.document_id, 32)
        in_process = await reference.lexical_query(query, corpus.document_id, 32)

        assert [item.chunk.chunk_id for item in database] == [
            item.chunk.chunk_id for item in in_process
        ]
        assert [item.score for item in database] == pytest.approx(
            [item.score for item in in_process], rel=1e-12
        )


async def test_rows_written_before_token_storage_are_backfilled(
    store: PostgresVectorStore,
) -> None:
    chunk = Chunk(
        chunk_id="legacy:0001",
        doc_id="legacy-doc",
        text="Cobrança indevida lançada na fatura.",
        page_start=1,
        page_end=1,
    )
    await store.upsert([chunk], [[1.0, 0.0]])
    with psycopg.connect(DSN) as connection:  # type: ignore[union-attr]
        connection.execute(
            f"UPDATE {store.TABLE} SET lexical_tokens = NULL WHERE namespace = %s",
            (store.index_name,),
        )

    reopened = PostgresVectorStore(dsn=DSN, index_name=store.index_name, pool_max_size=1)
    try:
        results = await reopened.lexical_query("cobrança na fatura", "legacy-doc", 5)
    finally:
        reopened.close()

    assert [item.chunk.chunk_id for item in results] == ["legacy:0001"]


async def test_dense_search_and_deletion_share_the_pool(store: PostgresVectorStore) -> None:
    near = Chunk(chunk_id="d:1", doc_id="doc", text="perto", page_start=1, page_end=1)
    far = Chunk(chunk_id="d:2", doc_id="doc", text="longe", page_start=1, page_end=1)
    await store.upsert([near, far], [[1.0, 0.0], [0.0, 1.0]])

    results = await store.query([0.9, 0.1], "doc", 2)
    await store.delete_document("doc")

    assert [item.chunk.chunk_id for item in results] == ["d:1", "d:2"]
    assert await store.list_document_ids() == set()
