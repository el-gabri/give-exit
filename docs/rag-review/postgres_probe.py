"""Read-only PostgreSQL probes; no embedding inference or index mutations."""

from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.retrieval import build_legal_queries
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService
from app.core.config import Settings
from app.core.logging import configure_logging
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.rag.factory import configured_embedding_identity, vector_store_index_name
from app.rag.pipeline import RagPipeline
from app.rag.vector_store import PostgresVectorStore
from probes import BrokenEmbedder


async def main():
    configure_logging(level="ERROR")
    settings = Settings()
    corpus = get_default_legal_corpus()
    namespace = vector_store_index_name(
        settings, corpus_version=f"{corpus.release_id}-{corpus.corpus_sha256[:12]}",
        embedding_model=configured_embedding_identity(settings))
    connection_times = []
    class ReadOnlyStore(PostgresVectorStore):
        def _connect(self):
            started = time.perf_counter()
            result = psycopg.connect(self._dsn, connect_timeout=5,
                                    options="-c default_transaction_read_only=on")
            connection_times.append((time.perf_counter() - started) * 1000)
            return result

    store = ReadOnlyStore(dsn=settings.postgres_dsn, index_name=namespace)
    # The configured evaluator has already opened this namespace. Suppress DDL
    # and enforce read-only at the connection level for this measurement.
    store._schema_ready = True
    doc_id = corpus.as_parsed_document().doc_id
    entries = await store.export_document(doc_id)
    dataset = load_consumer_legal_dataset(Path("eval_data/consumer_legal_retrieval"))
    pipeline = RagPipeline(BrokenEmbedder(), store)
    service = object.__new__(ConsumerCaseService)
    service._legal_corpus = corpus
    output = {"entries": len(entries), "cases": []}
    for case in dataset.cases:
        facts = ConsumerCaseFacts(issue_category=case.intake_category,
                                  complaint_summary=case.complaint,
                                  desired_resolution=case.desired_resolution)
        results, traces = await pipeline.retrieve_many_with_traces(
            build_legal_queries(facts), doc_id=doc_id, agent="consumer_legal_authorities", k=8)
        grounds = service._legal_grounds(results, facts, traces)
        output["cases"].append({"case": case.case_id,
                                "grounds": [g.authority.model_dump(mode="json") for g in grounds],
                                "traces": [t.model_dump(mode="json") for t in traces]})
    dense_ms, lexical_ms = [], []
    query = build_legal_queries(ConsumerCaseFacts(
        issue_category="unauthorized_charge", complaint_summary="Paguei cobrança em duplicidade.",
        desired_resolution="Quero restituição."))[0]
    for _ in range(10):
        started = time.perf_counter()
        await store.query(entries[0][1], doc_id, 32)
        dense_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        await store.lexical_query(query, doc_id, 32)
        lexical_ms.append((time.perf_counter() - started) * 1000)
    output["timings_ms"] = {label: {"n": len(values), "median": statistics.median(values),
                                      "max": max(values)}
                             for label, values in [("connection", connection_times),
                                                    ("dense", dense_ms), ("lexical", lexical_ms)]}
    output["summary"] = {
        "grounds": sum(len(row["grounds"]) for row in output["cases"]),
        "score_types": sorted({t["score_type"] for row in output["cases"] for t in row["traces"]}),
        "art5_or_104a": [(row["case"], g["unit_id"] or g["provision_id"])
                         for row in output["cases"] for g in row["grounds"]
                         if g["provision_id"] in {"br-cdc-art-5", "br-cdc-art-104-a"}],
    }
    Path("docs/rag-review/postgres.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: output[k] for k in ["entries", "timings_ms", "summary"]}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
