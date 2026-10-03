"""Lay-language alias entries: rules, the set's identity and file I/O."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.consumer.aliases import (
    AliasEntry,
    AliasSet,
    alias_problem,
    load_alias_set,
    normalize_alias,
    write_alias_set,
)

SHA = "a" * 64
FIRST = "A loja só me vendeu o celular se eu levasse a capinha junto."
SECOND = "O banco disse que só liberava o empréstimo se eu fizesse um seguro."


def _entry(**overrides: Any) -> AliasEntry:
    payload: dict[str, Any] = {
        "unit_key": "br-cdc-art-39-inciso-i",
        "provision_id": "br-cdc-art-39",
        "source_sha256": SHA,
        "aliases": [FIRST, SECOND],
    }
    payload.update(overrides)
    return AliasEntry.model_validate(payload)


def test_aliases_are_normalised_and_default_to_generated() -> None:
    entry = _entry(aliases=[f"  {FIRST.replace(' ', '   ')} ", SECOND])

    assert entry.aliases == (FIRST, SECOND)
    assert entry.status == "generated"
    assert normalize_alias("  a\n b\t c ") == "a b c"


@pytest.mark.parametrize(
    "text",
    [
        "Isso fere o art. 39 do que vale aqui.",
        "Segundo a LEI, a loja não podia fazer isso.",
        "Está escrito no parágrafo único da regra.",
        "O CDC proíbe que a loja faça isso comigo.",
        "A lgpd diz que eles não podiam guardar isso.",
        "Conforme o § 2º, a cobrança não podia vir.",
        "O Código diz que eu tenho esse direito sim.",
        "Os incisos garantem que eu receba de volta.",
    ],
)
def test_an_alias_that_cites_the_law_is_refused_naming_the_unit(text: str) -> None:
    assert alias_problem(text) is not None
    with pytest.raises(ValidationError, match="br-cdc-art-39-inciso-i: alias"):
        _entry(aliases=[text, SECOND])


@pytest.mark.parametrize(
    "text",
    [
        "Comprei leite estragado no mercado ontem à noite.",
        "A arte da embalagem era diferente da foto do site.",
        "Desisti da compra em sete dias e não devolveram nada.",
    ],
)
def test_everyday_words_that_look_like_legal_terms_pass(text: str) -> None:
    assert alias_problem(text) is None


@pytest.mark.parametrize(
    ("aliases", "message"),
    [
        (["curto", SECOND], "shorter than 12"),
        (["x" * 221, SECOND], "longer than 220"),
        ([FIRST, FIRST.upper()], "distinct"),
        ([FIRST], "at least 2"),
        ([FIRST, SECOND, "Terceira frase leiga bem comum.", "Quarta frase leiga bem comum.",
          "Quinta frase leiga bem comum."], "at most 4"),
    ],
)
def test_alias_bounds(aliases: list[str], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _entry(aliases=aliases)



def test_a_single_string_in_place_of_the_list_is_refused() -> None:
    # A hand edit that replaces the list with one sentence must not load.
    with pytest.raises(ValidationError, match="valid tuple"):
        _entry(aliases=FIRST)

def test_an_entry_must_belong_to_its_provision() -> None:
    with pytest.raises(ValidationError, match="br-cdc-art-40-caput: not a unit of br-cdc-art-39"):
        _entry(unit_key="br-cdc-art-40-caput")
    assert _entry(unit_key="br-cf-art-5-xxxii", provision_id="br-cf-art-5-xxxii").aliases


def test_the_set_rejects_duplicate_units_and_hashes_only_indexed_entries() -> None:
    with pytest.raises(ValidationError, match="duplicate alias entries: br-cdc-art-39-inciso-i"):
        AliasSet(prompt_version="p", entries=(_entry(), _entry()))
    rejected = _entry(unit_key="br-cdc-art-39-inciso-ii", status="rejected")
    alias_set = AliasSet(
        prompt_version="p", model="m", generated_on=date(2026, 10, 3), entries=(_entry(), rejected)
    )

    identity = alias_set.identity()

    assert [entry.unit_key for entry in alias_set.indexed] == ["br-cdc-art-39-inciso-i"]
    assert [item["unit_key"] for item in identity["entries"]] == ["br-cdc-art-39-inciso-i"]
    assert identity["generated_on"] == "2026-10-03"
    reviewed = AliasSet(
        prompt_version="p",
        model="m",
        generated_on=date(2026, 10, 3),
        entries=(_entry(status="reviewed"), rejected),
    )
    assert reviewed.identity() != identity


def test_the_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    alias_set = AliasSet(prompt_version="p", prompt_sha256="b" * 64, entries=(_entry(),))

    write_alias_set(alias_set, path)

    assert load_alias_set(path) == alias_set
    assert path.read_text(encoding="utf-8").endswith("\n")
    assert "empréstimo" in path.read_text(encoding="utf-8")


def test_a_hand_edited_file_with_bom_and_crlf_loads(tmp_path: Path) -> None:
    path = tmp_path / "aliases.json"
    write_alias_set(AliasSet(prompt_version="p", entries=(_entry(),)), path)
    text = path.read_text(encoding="utf-8").replace("\n", "\r\n")
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    assert load_alias_set(path).entries[0].aliases == (FIRST, SECOND)
