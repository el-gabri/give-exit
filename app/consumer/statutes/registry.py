"""The official statutes the Consumer legal corpus can be built from."""

from __future__ import annotations

from app.consumer.schemas import LegalSource
from app.consumer.statutes.spec import StatuteSpec

CDC = StatuteSpec(
    law_id="br-cdc",
    source=LegalSource.CONSUMER_DEFENSE_CODE,
    source_name="Código de Defesa do Consumidor (Lei nº 8.078/1990)",
    citation_prefix="CDC",
    source_url="https://www.planalto.gov.br/ccivil_03/leis/l8078compilado.htm",
    directory="cdc",
    snapshot_file="l8078compilado.html",
    encoding="windows-1252",
    last_article=119,
    expected_article_count=130,
    required_articles=frozenset(
        {"42-a", *(f"54-{suffix}" for suffix in "abcdefg"), *(f"104-{suffix}" for suffix in "abc")}
    ),
)

STATUTES: tuple[StatuteSpec, ...] = (CDC,)
