"""Purpose-specific embedding framing regressions."""

from app.rag.embeddings import _with_instruction


def test_query_instruction_is_separated_from_query_text() -> None:
    framed = _with_instruction(
        "negativação indevida",
        "Instruct: Recupere dispositivos legais brasileiros. Query:",
    )

    assert framed == (
        "Instruct: Recupere dispositivos legais brasileiros. "
        "Query: negativação indevida"
    )


def test_missing_query_instruction_leaves_text_unchanged() -> None:
    assert _with_instruction("cobrança não reconhecida", None) == (
        "cobrança não reconhecida"
    )
