"""Offline generation of lay-language aliases (ADR 0022).

One LLM call per covered article sends the citation label, the hierarchy and
each in-force unit with its lead-in, and asks for two to four sentences per
unit in a lay consumer's words. The generator reads the corpus only: it never
sees the golden set, so the holdout measures aliases written without it.

The run is resumable. The alias file is rewritten after every article; a rerun
skips units whose entry matches the current statute text and prompt version,
and never replaces a reviewed or rejected entry unless forced.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, Field

from app.consumer.aliases import (
    ALIAS_LAW_IDS,
    ALIASES_PATH,
    AliasEntry,
    AliasSet,
    alias_problem,
    load_alias_set,
    normalize_alias,
    write_alias_set,
)
from app.consumer.legal_corpus import LegalCorpus, default_legal_provisions
from app.consumer.statutes import HIERARCHY_LEVELS
from app.core.config import Settings
from app.core.hashing import sha256_hex
from app.llm.base import LLMClient
from app.llm.factory import create_llm_client

ALIAS_PROMPT_VERSION = "consumer-lay-aliases:v2"
# v2 (ADR 0022): v1 aliases described outcomes many articles share ("money
# back after a defect"), so a service complaint reached the product-defect
# article. Each alias now has to carry what distinguishes its unit.
SYSTEM_PROMPT = (
    "Você escreve paráfrases leigas para um índice de busca de direitos do consumidor no "
    "Brasil. Para cada unidade de um dispositivo legal, indicada pela chave entre colchetes, "
    "escreva de 2 a 4 frases curtas, em primeira pessoa, como um consumidor comum descreveria "
    "uma situação concreta coberta por aquela unidade.\n"
    "Regras:\n"
    "- Cada frase traz a condição que distingue esta unidade das demais: se é produto ou "
    "serviço, qual prática, qual dado pessoal ou qual situação específica o texto descreve. "
    "Uma frase que serviria igualmente para outra unidade está errada.\n"
    "- Não use desfechos que muitos dispositivos compartilham, como querer o dinheiro de "
    "volta, ser indenizado ou ter os direitos de consumidor respeitados, a não ser junto da "
    "condição específica da unidade.\n"
    "- Se a unidade não descreve uma situação concreta de consumo (por exemplo, define "
    "termos, organiza órgãos públicos, fixa princípios gerais ou apenas remete a outra "
    "norma), responda com lista vazia para ela.\n"
    "- Não cite artigos, incisos, parágrafos, alíneas, leis, códigos ou siglas como CDC ou "
    "LGPD.\n"
    "- Prefira palavras do dia a dia a termos jurídicos.\n"
    "- No máximo 25 palavras por frase.\n"
    "- Não atribua à unidade direitos que o texto não dá.\n"
    "- Responda com uma entrada por unidade, usando exatamente a chave entre colchetes."
)
USER_TEMPLATE = "Dispositivo: {citation}\nHierarquia: {hierarchy}\n\nUnidades:\n\n{blocks}"
PROMPT_SHA256 = sha256_hex(f"{SYSTEM_PROMPT}\n{USER_TEMPLATE}")
MAX_ALIASES = 4
# Reasoning tokens count against the output budget on the Responses path.
ALIAS_MAX_OUTPUT_TOKENS = 8_000


class _UnitAliases(BaseModel):
    unit_key: str
    aliases: list[str] = Field(default_factory=list)


class _ArticleAliases(BaseModel):
    units: list[_UnitAliases] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AliasRequest:
    """One article's request: the prompt and the source block of each unit."""

    provision_id: str
    user: str
    sources: Mapping[str, str]


@dataclass(slots=True)
class GenerationReport:
    written: list[str] = field(default_factory=list)
    skipped: int = 0
    dropped: int = 0
    missing: list[str] = field(default_factory=list)
    short: list[str] = field(default_factory=list)


def alias_requests(corpus: LegalCorpus, *, articles: Sequence[str] = ()) -> list[AliasRequest]:
    """The covered, citable articles in corpus order, or only ``articles``."""

    wanted = set(articles)
    requests: list[AliasRequest] = []
    for provision in corpus.retrievable_provisions():
        if provision.law_id not in ALIAS_LAW_IDS:
            continue
        if wanted and provision.provision_id not in wanted:
            continue
        sources = {
            provision.provision_id if unit is None else unit.unit_id: corpus.alias_source_block(
                provision, unit
            )
            for unit in corpus.aliasable_units(provision)
        }
        if not sources:  # every unit vetoed or revoked: nothing to alias
            continue
        levels = (getattr(provision, name) for name in HIERARCHY_LEVELS)
        hierarchy = " > ".join(str(level) for level in levels if level)
        user = USER_TEMPLATE.format(
            citation=provision.citation_label,
            hierarchy=hierarchy or "-",
            blocks="\n\n".join(sources.values()),
        )
        requests.append(AliasRequest(provision.provision_id, user, MappingProxyType(sources)))
    unknown = sorted(wanted - {request.provision_id for request in requests})
    if unknown:
        raise ValueError("not a covered, citable article: " + ", ".join(unknown))
    return requests


async def generate_aliases(
    corpus: LegalCorpus,
    alias_set: AliasSet,
    client: LLMClient,
    *,
    model: str,
    today: date,
    save: Callable[[AliasSet], None],
    articles: Sequence[str] = (),
    force: bool = False,
    reasoning_effort: str | None = None,
) -> tuple[AliasSet, GenerationReport]:
    """Generate the missing or stale entries, saving after each article.

    A reasoning model (gpt-5.6-terra) rejects temperature 0, so with
    ``reasoning_effort`` the client uses its Responses path instead.
    """

    entries = {entry.unit_key: entry for entry in alias_set.entries}
    order = {key: index for index, key in enumerate(_corpus_order(corpus))}
    report = GenerationReport()
    current = alias_set
    for request in alias_requests(corpus, articles=articles):
        pending = [
            key
            for key, source in request.sources.items()
            if _needs_generation(entries.get(key), source, force=force)
        ]
        report.skipped += len(request.sources) - len(pending)
        if not pending:
            continue
        result = await client.parse(
            system=SYSTEM_PROMPT,
            user=request.user,
            schema=_ArticleAliases,
            temperature=0.0,
            prompt_version=ALIAS_PROMPT_VERSION,
            reasoning_effort=reasoning_effort,
            max_output_tokens=ALIAS_MAX_OUTPUT_TOKENS,
        )
        _replace(entries, pending, _new_entries(request, pending, result.data, report))
        current = AliasSet(
            prompt_version=ALIAS_PROMPT_VERSION,
            prompt_sha256=PROMPT_SHA256,
            model=model,
            generated_on=today,
            entries=tuple(
                sorted(entries.values(), key=lambda entry: order.get(entry.unit_key, len(order)))
            ),
        )
        save(current)
    return current, report


def render_report(report: GenerationReport) -> str:
    lines = [
        f"written: {len(report.written)}",
        f"skipped (current or protected): {report.skipped}",
        f"dropped invalid aliases: {report.dropped}",
    ]
    if report.missing:
        lines.append("no answer: " + ", ".join(report.missing))
    if report.short:
        lines.append("fewer than two valid aliases: " + ", ".join(report.short))
    return "\n".join(lines)


def _needs_generation(entry: AliasEntry | None, source: str, *, force: bool) -> bool:
    if entry is None or force:
        return True
    if entry.status != "generated":
        return False
    return entry.prompt_version != ALIAS_PROMPT_VERSION or entry.source_sha256 != sha256_hex(
        source
    )


def _replace(
    entries: dict[str, AliasEntry], pending: list[str], new: dict[str, AliasEntry]
) -> None:
    """Install the new entries; drop a pending generated entry that got none.

    A stale entry must not outlive the run that was meant to replace it, or an
    older prompt's aliases would sit under the current manifest. Entries a
    person reviewed or rejected are kept when a forced run brings nothing.
    """
    for key in pending:
        if key in new:
            entries[key] = new[key]
        elif key in entries and entries[key].status == "generated":
            del entries[key]


def _new_entries(
    request: AliasRequest,
    pending: list[str],
    answer: _ArticleAliases,
    report: GenerationReport,
) -> dict[str, AliasEntry]:
    answers: dict[str, list[str]] = {}
    for unit in answer.units:
        # Units nobody asked about are ignored, and the first answer wins.
        if unit.unit_key in pending and unit.unit_key not in answers:
            answers[unit.unit_key] = unit.aliases
    entries: dict[str, AliasEntry] = {}
    for key in pending:
        if key not in answers:
            report.missing.append(key)
            continue
        aliases = _valid_aliases(answers[key], report)
        if len(aliases) < 2:
            report.short.append(key)
            continue
        entries[key] = AliasEntry(
            unit_key=key,
            provision_id=request.provision_id,
            source_sha256=sha256_hex(request.sources[key]),
            aliases=tuple(aliases[:MAX_ALIASES]),
            prompt_version=ALIAS_PROMPT_VERSION,
        )
        report.written.append(key)
    return entries


def _valid_aliases(raw: list[str], report: GenerationReport) -> list[str]:
    kept: list[str] = []
    for text in (normalize_alias(item) for item in raw):
        if alias_problem(text) or text.casefold() in {alias.casefold() for alias in kept}:
            report.dropped += 1
            continue
        kept.append(text)
    return kept


def _corpus_order(corpus: LegalCorpus) -> list[str]:
    return [key for request in alias_requests(corpus) for key in request.sources]


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate lay-language aliases for the citable CDC, LGPD and CF units."
    )
    parser.add_argument("--model", help="LLM model (default: LITIGATION_LLM_MODEL)")
    parser.add_argument(
        "--article", action="append", default=[], metavar="PROVISION_ID", help="repeatable"
    )
    parser.add_argument(
        "--force", action="store_true", help="also regenerate reviewed and rejected entries"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the requests; call no model, write nothing"
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        help="for reasoning models that reject temperature 0, such as gpt-5.6-terra",
    )
    parser.add_argument("--output", type=Path, default=ALIASES_PATH)
    args = parser.parse_args(argv)
    corpus = LegalCorpus(default_legal_provisions())
    try:
        requests = alias_requests(corpus, articles=args.article)
    except ValueError as exc:
        parser.error(str(exc))
    if args.dry_run:
        for request in requests:
            print(f"--- {request.provision_id} ({len(request.sources)} units)\n{request.user}\n")
        return 0
    alias_set = (
        load_alias_set(args.output)
        if args.output.exists()
        else AliasSet(prompt_version=ALIAS_PROMPT_VERSION)
    )
    settings = Settings(llm_model=args.model) if args.model else Settings()
    model = settings.llm_model or settings.llm_provider.value
    try:
        _, report = await generate_aliases(
            corpus,
            alias_set,
            create_llm_client(settings),
            model=model,
            today=date.today(),
            save=lambda current: write_alias_set(current, args.output),
            articles=args.article,
            force=args.force,
            reasoning_effort=args.reasoning_effort,
        )
    except Exception as exc:  # the file already holds every finished article
        print(
            f"generate_aliases: stopped ({type(exc).__name__}: {exc}); rerun to resume",
            file=sys.stderr,
        )
        return 1
    print(render_report(report))
    return 0
