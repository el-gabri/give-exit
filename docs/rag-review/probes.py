"""Read-only retrieval review probes; writes only the requested JSON evidence file."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.consumer.legal_corpus import get_default_legal_corpus
from app.consumer.legal_policy import provision_is_eligible, strongly_supported_chunk_ids
from app.consumer.retrieval import build_legal_queries, build_legal_queries_for_case
from app.consumer.schemas import ConsumerCaseFacts
from app.consumer.service import ConsumerCaseService, _merge_results
import app.consumer.service as service_module
from app.core.logging import configure_logging
from app.evaluation.consumer_golden import load_consumer_legal_dataset
from app.rag.chunking import SectionAwareChunker
from app.rag.embeddings import MockEmbeddingClient
from app.rag.pipeline import RagPipeline, reciprocal_rank_fusion
from app.rag.resilience import QueryEmbeddingGuard
from app.rag.vector_store import InMemoryVectorStore
from app.rag.vector_store import portuguese_lexical_tokens
from app.schemas.trace import RetrievalTrace
from app.schemas.document import DocumentPage, ParsedDocument
from app.schemas.rag import RetrievedChunk


class BrokenEmbedder(MockEmbeddingClient):
    async def embed_queries(self, texts):
        raise RuntimeError("synthetic dependency outage")


async def main():
    configure_logging(level="ERROR")
    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()
    service = object.__new__(ConsumerCaseService)
    service._legal_corpus = corpus
    dataset = load_consumer_legal_dataset(Path("eval_data/consumer_legal_retrieval"))
    output = {"chunk_count": len(chunks), "cases": [], "proofs": {}}
    def corroborated_hybrid(traces):
        supported = {}
        for trace in traces:
            if trace.error or trace.degraded_mode or trace.score_type != "rrf_score":
                continue
            ceiling = max(trace.dense_weight, trace.lexical_weight) / (trace.rrf_constant + 1)
            for item in trace.results:
                if item.score > ceiling + 1e-12:
                    supported.setdefault(item.chunk_id, set()).add(trace.query_sha256)
        return frozenset(key for key, hashes in supported.items() if len(hashes) >= 2)
    store = InMemoryVectorStore()
    normal = RagPipeline(MockEmbeddingClient(), store)
    await normal.index_chunks(chunks)
    degraded = RagPipeline(BrokenEmbedder(), store)
    for case in dataset.cases:
        facts = ConsumerCaseFacts(
            complaint_summary=case.complaint,
            desired_resolution=case.desired_resolution,
        )
        row = {"case": case.case_id, "hard_negatives": case.hard_negatives}
        for label, pipeline in [("hybrid", normal), ("degraded", degraded)]:
            results, traces = await pipeline.retrieve_many_with_traces(
                build_legal_queries(facts), doc_id=chunks[0].doc_id,
                agent="consumer_legal_authorities", k=8,
            )
            grounds = service._legal_grounds(results, facts, traces)
            if label == "hybrid":
                service_module.strongly_supported_chunk_ids = corroborated_hybrid
                try:
                    row["corroborated_hybrid"] = [
                        g.authority.model_dump(mode="json")
                        for g in service._legal_grounds(results, facts, traces)
                    ]
                finally:
                    service_module.strongly_supported_chunk_ids = strongly_supported_chunk_ids
            row[label] = [g.authority.model_dump(mode="json") for g in grounds]
            row[label + "_ranks"] = [
                [{"id": i.source_unit_id or i.source_provision_id,
                  "rank": i.rank, "score": i.score,
                  "supported": i.chunk_id in strongly_supported_chunk_ids(traces)}
                 for i in t.results] for t in traces
            ]
        output["cases"].append(row)

    proof = output["proofs"]
    first_case = dataset.cases[0]
    first_facts = ConsumerCaseFacts(complaint_summary=first_case.complaint,
                                   desired_resolution=first_case.desired_resolution)
    first_query = build_legal_queries(first_facts)[0]
    first_vector = await MockEmbeddingClient().embed_query(first_query)
    dense = await store.query(first_vector, chunks[0].doc_id, 32)
    lexical = await store.lexical_query(first_query, chunks[0].doc_id, 32)
    proof["art5_channels"] = {
        channel: [{"rank": rank, "score": item.score, "chunk_id": item.chunk.chunk_id,
                   "shared_tokens": sorted(set(portuguese_lexical_tokens(first_query)) &
                                           set(portuguese_lexical_tokens(item.chunk.text)))}
                  for rank, item in enumerate(items, 1)
                  if item.chunk.metadata.get("provision_id") == "br-cdc-art-5"]
        for channel, items in [("dense", dense), ("lexical", lexical)]
    }
    proof["evaluation_design"] = {
        "cases": len(dataset.cases),
        "no_ground_cases": sum(c.no_applicable_ground for c in dataset.cases),
        "requested_k": 10, "production_k": 8,
        "candidate_k": normal.retrieval_configuration(requested_k=10)["candidate_k"],
        "production_candidate_k": normal.retrieval_configuration(requested_k=8)["candidate_k"],
        "query_formatter_in_trace": "embedding_query_formatter_version" in RetrievalTrace.model_fields,
    }
    proof["rrf"] = {
        "maximum": 2 / 61, "single_ceiling": 1 / 61,
        "both_channels_rank32": 2 / 92, "minimum_ratio": (2 / 92) / (2 / 61),
        "rrf_floor_excludes_supported_default": False,
    }
    facts = ConsumerCaseFacts(
        complaint_summary="A loja cobrou duas vezes a compra.",
        desired_resolution="Quero a devolução do valor pago em duplicidade.")
    targets = [c for c in chunks if c.metadata.get("unit_id") in
               {"br-cdc-art-5-inciso-i", "br-cdc-art-104-a-paragrafo-5"}]
    # Exercise the true pipeline and RRF against a controlled channel fixture.
    class FixedStore(InMemoryVectorStore):
        async def query(self, vector, doc_id, k):
            return [RetrievedChunk(chunk=c, score=0.000001) for c in targets]

        async def lexical_query(self, query, doc_id, k):
            return [RetrievedChunk(chunk=c, score=0.000001) for c in targets]

    fixture = RagPipeline(MockEmbeddingClient(), FixedStore())
    results, traces = await fixture.retrieve_many_with_traces(
        build_legal_queries(facts), doc_id=chunks[0].doc_id,
        agent="consumer_legal_authorities", k=8,
    )
    proof["low_raw_scores_still_cited"] = [
        {"unit_id": g.authority.unit_id, "score": g.authority.retrieval_score}
        for g in service._legal_grounds(results, facts, traces)
    ]
    proof["target_eligibility"] = {
        c.metadata["unit_id"]: provision_is_eligible(corpus.provision_for_chunk(c))
        for c in targets
    }
    proof["degraded_natural_queries"] = []
    for complaint in [
        "A assistência técnica da loja não me atende e não resolve o defeito do aparelho.",
        "Paguei assistência para consertar a geladeira, mas não resolveram.",
        "Comprei um produto e a loja se recusa a me dar assistência gratuita.",
    ]:
        custom_facts = ConsumerCaseFacts(complaint_summary=complaint,
                                         desired_resolution="Quero a solução do problema.")
        custom_results, custom_traces = await degraded.retrieve_many_with_traces(
            build_legal_queries(custom_facts), doc_id=chunks[0].doc_id,
            agent="consumer_legal_authorities", k=8)
        proof["degraded_natural_queries"].append({
            "complaint": complaint,
            "queries": [t.query for t in custom_traces],
            "grounds": [g.authority.unit_id or g.authority.provision_id
                        for g in service._legal_grounds(custom_results, custom_facts, custom_traces)],
        })
    if targets:
        duplicate_results, duplicate_traces = await degraded.retrieve_many_with_traces(
            ["consumidor", "consumidor"], doc_id=chunks[0].doc_id,
            agent="consumer_legal_authorities", k=8,
        )
        proof["identical_query_fallback"] = {
            "hashes_equal": duplicate_traces[0].query_sha256 == duplicate_traces[1].query_sha256,
            "supported_count": len(strongly_supported_chunk_ids(duplicate_traces)),
        }

    def document(text):
        return ParsedDocument(filename="receipt.pdf", language="pt",
                              extraction_method="native_text",
                              pages=[DocumentPage(number=1, text=text)])

    receipt = "CASO ABCDEF12 EVIDENCIA ABCDEF34 PAGINA 1\n\nPAGAMENTO APROVADO\n\nTOTAL R$ 149,90"
    proof["uppercase_receipt"] = {
        "input": receipt,
        "chunks": [c.model_dump() for c in SectionAwareChunker().chunk(document(receipt))],
    }
    queries = build_legal_queries_for_case(
        complaint=("Histórico de atendimento. " * 120),
        desired_resolution="Quero a devolução dos valores pagos.",
    )
    proof["long_queries"] = {
        "lengths": list(map(len, queries)), "q3": queries[2],
        "resolution_retained": ["devolução" in q for q in queries],
    }
    # Equal-scale max fusion is legal arithmetic, but discards query agreement.
    a, b = chunks[:2]
    sets = [[RetrievedChunk(chunk=a, score=2/61), RetrievedChunk(chunk=b, score=2/62)],
            [RetrievedChunk(chunk=b, score=2/62)],
            [RetrievedChunk(chunk=b, score=2/62)]]
    proof["merge"] = {
        "max_order": [i.chunk.chunk_id for i in _merge_results(sets)],
        "cross_query_rrf_order": [i.chunk.chunk_id for i in reciprocal_rank_fusion(sets, k=2)],
    }
    # Cancellation, unlike the timeout path, must also retain the slot.
    started, release = asyncio.Event(), asyncio.Event()
    class BlockingEmbedder(MockEmbeddingClient):
        def __init__(self):
            super().__init__()
            self.active = self.peak = 0

        async def embed_queries(self, texts):
            self.active += 1
            self.peak = max(self.peak, self.active)
            started.set()
            await release.wait()
            self.active -= 1
            return await super().embed_queries(texts)

    embedder = BlockingEmbedder()
    guard = QueryEmbeddingGuard(
        embedder, timeout_seconds=10, max_concurrency=1, queue_timeout_seconds=1,
        circuit_breaker_failures=2, circuit_breaker_reset_seconds=60,
        cache_ttl_seconds=300, cache_max_entries=16, expected_dimension=128,
    )
    first = asyncio.create_task(guard.embed(["first"]))
    await started.wait()
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    second = asyncio.create_task(guard.embed(["second"]))
    await asyncio.sleep(0.02)
    proof["cancellation"] = {"active_before_release": embedder.active, "peak": embedder.peak}
    release.set()
    await second
    await asyncio.sleep(0)
    proof["legal_chunks"] = {
        "max_length": max(len(c.text) for c in chunks),
        "over_target": sum(len(c.text) > 1200 for c in chunks),
        "split_unit_count": len({c.metadata.get("unit_id") for c in chunks
                                 if ":part-02" in c.chunk_id and c.metadata.get("unit_id")}),
        "article_chunks": sum(c.metadata.get("chunk_level") == "article" for c in chunks),
    }
    proof["summary"] = {}
    for label in ("hybrid", "degraded", "corroborated_hybrid"):
        cited = [g for row in output["cases"] for g in row[label]]
        hard = [(row["case"], g["unit_id"] or g["provision_id"])
                for row in output["cases"] for g in row[label]
                if g["provision_id"] in row["hard_negatives"] or
                g["unit_id"] in row["hard_negatives"]]
        exact_recalls = []
        for row, case in zip(output["cases"], dataset.cases):
            if case.no_applicable_ground:
                continue
            exact_hits = sum(any(
                (g["unit_id"] == j.unit_id if j.unit_id else g["provision_id"] == j.article_id)
                for g in row[label]) for j in case.relevant)
            exact_recalls.append(exact_hits / len(case.relevant))
        proof["summary"][label] = {"grounds": len(cited), "labeled_hard_negatives": hard,
                                   "notice_exact_recall": sum(exact_recalls) / len(exact_recalls),
                                   "articles": dict(Counter(g["provision_id"] for g in cited))}
    Path("docs/rag-review/probes.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(proof, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
