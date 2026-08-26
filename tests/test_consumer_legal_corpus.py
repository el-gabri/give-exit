"""Tests for the versioned consumer-law reference corpus."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.consumer.cdc_snapshot import (
    CDC_SOURCE_URL,
    DEFAULT_MANIFEST_PATH,
    load_manifest,
    load_official_cdc,
)
from app.consumer.legal_corpus import (
    CONSUMER_LAW_CORPUS_RELEASE_ID,
    LegalCorpus,
    get_default_legal_corpus,
)
from app.consumer.schemas import (
    LegalAuthorityCitation,
    LegalContentKind,
    LegalSource,
    LegalTextUnit,
    LegalUnitKind,
    ProvisionStatus,
)
from app.consumer.update_cdc_snapshot import refresh_snapshot
from app.rag.chunking import SectionAwareChunker
from app.schemas.rag import Chunk, RetrievedChunk


def test_snapshot_is_offline_versioned_and_integrity_checked() -> None:
    manifest = load_manifest()
    snapshot_path = DEFAULT_MANIFEST_PATH.parent / manifest.snapshot_file

    assert manifest.release_id == "br-cdc-official-2026-08-04-v1"
    assert manifest.schema_version == 2
    assert manifest.source_url == CDC_SOURCE_URL
    assert manifest.retrieved_on.isoformat() == "2026-08-04"
    assert manifest.encoding == "windows-1252"
    assert manifest.parser_version == "cdc-html-parser-v2"
    assert manifest.acquisition_method == "download_https"
    assert manifest.final_url == CDC_SOURCE_URL
    assert manifest.refresh_tool_version == "cdc-snapshot-refresh-v2"
    assert manifest.review_status == "engineering_validated"
    assert snapshot_path.is_file()
    assert snapshot_path.stat().st_size == 169_132
    assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == (
        "bbfa64a79067ad3edd6b4dfff46cf905c85a44b2d5d8b2ac058a6a8855f13ef8"
    )


def test_official_snapshot_covers_complete_compiled_cdc() -> None:
    manifest, articles = load_official_cdc()
    ids = {article.provision_id for article in articles}

    assert manifest.law_id == "br-cdc"
    assert len(articles) == 130
    assert {article.number for article in articles} == set(range(1, 120))
    assert {
        "br-cdc-art-42-a",
        *(f"br-cdc-art-54-{suffix}" for suffix in "abcdefg"),
        *(f"br-cdc-art-104-{suffix}" for suffix in "abc"),
    }.issubset(ids)
    assert "repactuação de dívidas" in next(
        article.official_text for article in articles if article.provision_id == "br-cdc-art-104-a"
    )


def test_default_corpus_combines_full_cdc_with_reviewed_constitution() -> None:
    corpus = get_default_legal_corpus()
    cdc = [item for item in corpus.provisions if item.source is LegalSource.CONSUMER_DEFENSE_CODE]
    constitution = [
        item for item in corpus.provisions if item.source is LegalSource.FEDERAL_CONSTITUTION
    ]

    assert corpus.release_id == CONSUMER_LAW_CORPUS_RELEASE_ID
    assert len(corpus.provisions) == 137
    assert len(cdc) == 130
    assert len(constitution) == 7
    assert len({item.provision_id for item in corpus.provisions}) == 137
    assert {item.verified_on.isoformat() for item in corpus.provisions} == {"2026-08-04"}
    assert all(
        item.official_url.startswith("https://www.planalto.gov.br/") for item in corpus.provisions
    )
    assert all(item.official_text and item.official_text_sha256 for item in cdc)
    assert all(item.official_text and item.official_text_sha256 for item in constitution)
    assert all(item.source_snapshot_sha256 for item in cdc)
    for item in corpus.provisions:
        assert item.content_sha256 == hashlib.sha256(item.summary.encode()).hexdigest()


def test_legal_hierarchy_and_unit_ids_are_stable() -> None:
    corpus = get_default_legal_corpus()
    article_42 = corpus.get("br-cdc-art-42")
    article_54_a = corpus.get("br-cdc-art-54-a")
    article_104_a = corpus.get("br-cdc-art-104-a")

    paragraph = next(unit for unit in article_42.units if unit.unit_id.endswith("paragrafo-unico"))
    assert paragraph.kind is LegalUnitKind.PARAGRAPH
    assert paragraph.paragraph == "unico"
    assert paragraph.status is ProvisionStatus.ACTIVE
    assert article_42.section == "SEÇÃO V Da Cobrança de Dívidas"
    assert article_54_a.chapter and "CAPÍTULO VI-A" in article_54_a.chapter
    assert article_104_a.chapter == "CAPÍTULO V DA CONCILIAÇÃO NO SUPERENDIVIDAMENTO"
    assert corpus.get("br-cdc-art-3-p2").provision_id == "br-cdc-art-3"


def test_parser_preserves_penalties_and_quoted_amendments_as_normative_units() -> None:
    corpus = get_default_legal_corpus()
    article_63 = corpus.get("br-cdc-art-63")
    article_110 = corpus.get("br-cdc-art-110")
    article_113 = corpus.get("br-cdc-art-113")

    penalties = [unit for unit in article_63.units if unit.kind is LegalUnitKind.PENALTY]
    assert [unit.unit_id for unit in penalties] == [
        "br-cdc-art-63-pena-001",
        "br-cdc-art-63-pena-002",
    ]
    assert penalties[1].paragraph == "2"
    amendment = article_110.units[1]
    assert amendment.kind is LegalUnitKind.QUOTED_AMENDMENT
    assert amendment.inciso == "iv"
    assert amendment.unit_id == "br-cdc-art-110-alteracao-citada-001"
    quoted_paragraph = article_113.units[1]
    assert quoted_paragraph.kind is LegalUnitKind.QUOTED_AMENDMENT
    assert quoted_paragraph.paragraph == "4"
    paragraph_five = next(unit for unit in article_113.units if unit.paragraph == "5")
    assert paragraph_five.kind is LegalUnitKind.PARAGRAPH
    assert not any(
        unit.kind is LegalUnitKind.NOTE
        for provision in corpus.provisions
        if provision.source is LegalSource.CONSUMER_DEFENSE_CODE
        for unit in provision.units
    )


def test_vetoed_articles_and_units_are_auditable_but_not_active() -> None:
    corpus = get_default_legal_corpus()
    vetoed_article_ids = {
        item.provision_id for item in corpus.provisions if item.status is ProvisionStatus.VETOED
    }

    assert vetoed_article_ids == {
        "br-cdc-art-11",
        "br-cdc-art-15",
        "br-cdc-art-16",
        "br-cdc-art-45",
        "br-cdc-art-54-e",
        "br-cdc-art-62",
        "br-cdc-art-85",
        "br-cdc-art-86",
        "br-cdc-art-89",
        "br-cdc-art-96",
        "br-cdc-art-108",
        "br-cdc-art-109",
    }
    assert len(corpus.active_provisions) == 125
    article_51_veto = corpus.get("br-cdc-art-51").units[5]
    assert article_51_veto.unit_id == "br-cdc-art-51-inciso-v"
    assert article_51_veto.status is ProvisionStatus.VETOED


def test_parsed_document_preserves_article_page_mapping() -> None:
    corpus = get_default_legal_corpus()
    document = corpus.as_parsed_document()

    assert document.page_count == len(corpus.provisions)
    assert document.language == "pt"
    assert "texto oficial compilado" in document.warnings[0].lower()
    for page, provision in zip(document.pages, corpus.provisions, strict=True):
        assert corpus.provision_for_page(page.number) == provision
        assert provision.provision_id.upper() in page.text
        content_hash = provision.official_text_sha256 or provision.content_sha256
        assert content_hash in page.text


def test_legal_aware_chunks_never_cross_articles_and_expose_metadata() -> None:
    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks(target_chars=1_200)

    assert chunks
    assert all(chunk.page_start == chunk.page_end for chunk in chunks)
    assert all(len(chunk.text) <= 1_200 for chunk in chunks)
    assert not any(":br-cdc-art-51-inciso-v:" in chunk.chunk_id for chunk in chunks)
    paragraph_chunk = next(
        chunk for chunk in chunks if ":br-cdc-art-42-paragrafo-unico:" in chunk.chunk_id
    )
    metadata = corpus.metadata_for_chunk(paragraph_chunk)

    assert corpus.provision_for_chunk(paragraph_chunk).provision_id == "br-cdc-art-42"
    assert corpus.unit_for_chunk(paragraph_chunk).unit_id == ("br-cdc-art-42-paragrafo-unico")
    assert metadata == {
        "law_id": "br-cdc",
        "provision_id": "br-cdc-art-42",
        "article": "art. 42",
        "article_key": "42",
        "title": "TÍTULO I Dos Direitos do Consumidor",
        "chapter": "CAPÍTULO V Das Práticas Comerciais",
        "section": "SEÇÃO V Da Cobrança de Dívidas",
        "unit_id": "br-cdc-art-42-paragrafo-unico",
        "unit_kind": "paragraph",
        "paragraph": "unico",
        "inciso": None,
        "alinea": None,
        "status": "active",
        "content_kind": "official",
        "chunking_version": "legal-hierarchy-v2:target=1200",
        "chunk_level": "unit",
        "lead_in_unit_ids": None,
        "official_url": CDC_SOURCE_URL,
        "corpus_release_id": CONSUMER_LAW_CORPUS_RELEASE_ID,
        "verified_on": "2026-08-04",
        "content_sha256": corpus.unit_for_chunk(paragraph_chunk).content_sha256,
        "source_snapshot_sha256": load_manifest().snapshot_sha256,
        "page": paragraph_chunk.page_start,
    }


def test_generic_chunker_also_cannot_cross_legal_provisions() -> None:
    corpus = get_default_legal_corpus()
    document = corpus.as_parsed_document()
    chunks = SectionAwareChunker(target_chars=1_200, overlap_chars=100).chunk(document)

    assert chunks
    assert all(chunk.page_start == chunk.page_end for chunk in chunks)
    assert all(len(corpus.provisions_for_chunk(chunk)) == 1 for chunk in chunks)


def test_retrieved_chunk_maps_to_legal_authority_not_evidence() -> None:
    corpus = get_default_legal_corpus()
    article_42_chunk = next(
        chunk for chunk in corpus.as_chunks() if ":br-cdc-art-42-paragrafo-unico:" in chunk.chunk_id
    )
    retrieved = RetrievedChunk(chunk=article_42_chunk, score=0.87)

    citation = corpus.authority_for_chunk(retrieved, retrieval_rank=1)

    assert isinstance(citation, LegalAuthorityCitation)
    assert citation.provision_id == "br-cdc-art-42"
    assert citation.unit_id == "br-cdc-art-42-paragrafo-unico"
    assert citation.unit_label == "parágrafo único"
    assert citation.official_excerpt and "repetição do indébito" in citation.official_excerpt
    assert (
        citation.official_excerpt_sha256 == corpus.unit_for_chunk(article_42_chunk).content_sha256
    )
    assert citation.official_text and "repetição do indébito" in citation.official_text
    assert citation.chunk_id == article_42_chunk.chunk_id
    assert citation.retrieval_rank == 1
    assert citation.retrieval_score == 0.87
    assert citation.source_snapshot_sha256 == load_manifest().snapshot_sha256
    assert citation.content_kind is LegalContentKind.OFFICIAL


def test_authority_uses_unit_status_and_official_constitution_content() -> None:
    corpus = get_default_legal_corpus()
    article_51 = corpus.get("br-cdc-art-51")
    vetoed_unit = next(unit for unit in article_51.units if unit.unit_id.endswith("inciso-v"))

    vetoed_citation = LegalAuthorityCitation.from_provision(article_51, unit=vetoed_unit)
    constitution_citation = LegalAuthorityCitation.from_provision(corpus.get("br-cf-art-5-xxxii"))

    assert vetoed_citation.status is ProvisionStatus.VETOED
    assert vetoed_citation.content_kind is LegalContentKind.OFFICIAL
    assert constitution_citation.content_kind is LegalContentKind.OFFICIAL
    assert constitution_citation.official_text == (
        "XXXII - o Estado promoverá, na forma da lei, a defesa do consumidor;"
    )


def test_chunk_from_another_document_cannot_be_mapped_as_law() -> None:
    corpus = get_default_legal_corpus()
    foreign = Chunk(
        chunk_id="foreign:0000",
        doc_id="foreign",
        text="not law",
        page_start=1,
        page_end=1,
    )

    with pytest.raises(ValueError, match="does not belong"):
        corpus.provision_for_chunk(foreign)


def test_corpus_rejects_duplicate_ids_and_has_stable_manifest_hash() -> None:
    corpus = get_default_legal_corpus()

    with pytest.raises(ValueError, match="duplicate"):
        LegalCorpus([corpus.provisions[0], corpus.provisions[0]])
    assert len(corpus.corpus_sha256) == 64
    assert corpus.corpus_sha256 == get_default_legal_corpus().corpus_sha256


def test_legal_artifacts_are_frozen_and_nested_collections_are_immutable() -> None:
    provision = get_default_legal_corpus().get("br-cdc-art-42")
    unit = provision.units[0]

    with pytest.raises(ValidationError, match="frozen"):
        provision.summary = "adulterado"  # type: ignore[misc]
    with pytest.raises(ValidationError, match="frozen"):
        unit.text = "adulterado"  # type: ignore[misc]
    assert isinstance(provision.tags, tuple)
    assert isinstance(provision.units, tuple)


def test_corpus_revalidates_copied_models_and_hash_covers_canonical_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = get_default_legal_corpus()
    provision = corpus.get("br-cf-art-5-xxxii")
    baseline = LegalCorpus([provision])
    changed_tags = provision.model_copy(update={"tags": (*provision.tags, "nova-tag")})
    changed_hierarchy = provision.model_copy(update={"section": "Seção de auditoria"})

    assert LegalCorpus([changed_tags]).corpus_sha256 != baseline.corpus_sha256
    assert LegalCorpus([changed_hierarchy]).corpus_sha256 != baseline.corpus_sha256

    tampered = provision.model_copy(update={"summary": "resumo adulterado"})
    with pytest.raises(ValidationError, match="does not match summary"):
        LegalCorpus([tampered])

    import app.consumer.legal_corpus as legal_corpus_module

    monkeypatch.setattr(legal_corpus_module, "CDC_PARSER_VERSION", "cdc-html-parser-audit-test")
    assert LegalCorpus(corpus.provisions).corpus_sha256 != corpus.corpus_sha256


def test_legal_unit_and_citation_reject_tampered_content_hashes() -> None:
    corpus = get_default_legal_corpus()
    provision = corpus.get("br-cdc-art-42")
    unit = provision.units[0]
    citation = LegalAuthorityCitation.from_provision(provision, unit=unit)

    tampered_unit = unit.model_copy(update={"text": "texto adulterado"})
    with pytest.raises(ValidationError, match="does not match legal unit text"):
        LegalTextUnit.model_validate(tampered_unit.model_dump())

    tampered_citation = citation.model_copy(update={"official_excerpt": "trecho adulterado"})
    with pytest.raises(ValidationError, match="does not match official_excerpt"):
        LegalAuthorityCitation.model_validate(tampered_citation.model_dump())


def test_tampered_snapshot_is_rejected(tmp_path: Path) -> None:
    manifest = load_manifest()
    source_path = DEFAULT_MANIFEST_PATH.parent / manifest.snapshot_file
    copied_manifest = tmp_path / "manifest.json"
    copied_snapshot = tmp_path / manifest.snapshot_file
    copied_manifest.write_bytes(DEFAULT_MANIFEST_PATH.read_bytes())
    copied_snapshot.write_bytes(source_path.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="integrity check failed"):
        load_official_cdc(copied_manifest)


def test_local_snapshot_refresh_requires_provenance_and_explicit_promotion(
    tmp_path: Path,
) -> None:
    source = DEFAULT_MANIFEST_PATH.parent / load_manifest().snapshot_file
    with pytest.raises(ValueError, match="acquisition-note"):
        refresh_snapshot(
            tmp_path,
            date(2026, 8, 4),
            source_file=source,
        )

    _, manifest_path = refresh_snapshot(
        tmp_path,
        date(2026, 8, 4),
        source_file=source,
        acquisition_note="Downloaded from the official URL and reviewed as a local copy.",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["acquisition_method"] == "local_file"
    assert payload["review_status"] == "pending_review"
    with pytest.raises(ValueError, match="not been promoted"):
        load_manifest(manifest_path)


def test_subdivisions_are_indexed_with_the_caput_they_depend_on() -> None:
    """An inciso is meaningless without the rule it qualifies.

    ``I - impossibilitem, exonerem ou atenuem a responsabilidade...`` never says
    "cláusula", "abusiva" or "nula"; those words live in the caput. Indexing the
    subdivision alone hid the article's own vocabulary from retrieval.
    """

    corpus = get_default_legal_corpus()
    chunk = next(
        item
        for item in corpus.as_chunks()
        if ":br-cdc-art-51-inciso-i:" in item.chunk_id
    )

    assert "São nulas de pleno direito" in chunk.text
    assert "I - impossibilitem" in chunk.text
    assert chunk.metadata["lead_in_unit_ids"] == "br-cdc-art-51-caput"
    assert chunk.metadata["chunk_level"] == "unit"


def test_lead_in_never_widens_the_cited_excerpt() -> None:
    """The caput is retrieval context only; the citation stays the unit."""

    corpus = get_default_legal_corpus()
    chunk = next(
        item
        for item in corpus.as_chunks()
        if ":br-cdc-art-51-inciso-i:" in item.chunk_id
    )

    citation = corpus.authority_for_chunk(RetrievedChunk(chunk=chunk, score=1.0))
    unit = corpus.get("br-cdc-art-51").units[1]

    assert citation.unit_id == "br-cdc-art-51-inciso-i"
    assert citation.official_excerpt == unit.text
    assert "São nulas de pleno direito" not in (citation.official_excerpt or "")


def test_nested_subdivision_carries_its_whole_ancestor_chain() -> None:
    corpus = get_default_legal_corpus()
    chunk = next(
        item
        for item in corpus.as_chunks()
        if ":br-cdc-art-12-paragrafo-1-inciso-i:" in item.chunk_id
    )

    assert chunk.metadata["lead_in_unit_ids"] == (
        "br-cdc-art-12-caput,br-cdc-art-12-paragrafo-1"
    )
    assert "O produto é defeituoso quando" in chunk.text


def test_heavily_subdivided_articles_also_get_an_article_level_chunk() -> None:
    """A query pitched at the article needs a chunk that states the whole rule."""

    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()
    article_chunks = [
        item for item in chunks if ":br-cdc-art-51:article:" in item.chunk_id
    ]

    assert article_chunks
    assert all(item.metadata["chunk_level"] == "article" for item in article_chunks)
    assert corpus.unit_for_chunk(article_chunks[0]) is None
    citation = corpus.authority_for_chunk(RetrievedChunk(chunk=article_chunks[0], score=1.0))
    assert citation.provision_id == "br-cdc-art-51"
    assert citation.unit_id is None


def test_article_level_chunks_are_only_built_for_subdivided_articles() -> None:
    corpus = get_default_legal_corpus()
    levels = {
        chunk.metadata["provision_id"]
        for chunk in corpus.as_chunks()
        if chunk.metadata["chunk_level"] == "article"
    }

    assert "br-cdc-art-51" in levels
    # art. 42 has a caput and one paragraph - its units already state the rule.
    assert "br-cdc-art-42" not in levels


def test_amendment_only_articles_are_audited_but_never_retrievable() -> None:
    """CDC arts. 110-117 amend Lei 7.347/85; their payload is another law.

    Retrieving one would print "CDC, art. 114" above the text of art. 15 of a
    different statute, so they are excluded from the index while remaining in
    the corpus for audit and hashing.
    """

    corpus = get_default_legal_corpus()
    amendment_ids = {f"br-cdc-art-{number}" for number in range(110, 118)}
    chunked = {chunk.metadata["provision_id"] for chunk in corpus.as_chunks()}
    retrievable = {provision.provision_id for provision in corpus.retrievable_provisions()}
    audited = {provision.provision_id for provision in corpus.provisions}

    assert not (chunked & amendment_ids)
    assert not (retrievable & amendment_ids)
    assert amendment_ids <= audited


def test_constant_provenance_fields_left_the_embedded_text_for_metadata() -> None:
    """Dropped because every chunk shared them; still recorded and cited."""

    corpus = get_default_legal_corpus()
    chunks = corpus.as_chunks()

    assert not any(CDC_SOURCE_URL in chunk.text for chunk in chunks)
    assert not any("Status:" in chunk.text for chunk in chunks)
    assert all(chunk.metadata["official_url"] for chunk in chunks)
    assert all(chunk.metadata["status"] for chunk in chunks)
    # The hierarchy breadcrumb stays: it is a topic label, not boilerplate.
    assert any("SEÇÃO II Das Cláusulas Abusivas" in chunk.text for chunk in chunks)
