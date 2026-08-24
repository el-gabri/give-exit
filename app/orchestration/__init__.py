"""LangGraph orchestration of the analysis pipeline."""

from typing import TYPE_CHECKING

from app.orchestration.state import AnalysisState

if TYPE_CHECKING:
    from app.orchestration.graph import build_analysis_graph

__all__ = ["AnalysisState", "build_analysis_graph"]


def __getattr__(name: str) -> object:
    """Load the graph factory only when callers request the package export.

    State is imported by individual graph nodes, so eagerly importing the
    assembled graph here creates an import cycle while those nodes initialize.
    """
    if name != "build_analysis_graph":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from app.orchestration.graph import build_analysis_graph

    globals()[name] = build_analysis_graph
    return build_analysis_graph
