"""Local Hugging Face models load from the cache only when that is exactly right."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.rag.hub_loading import load_cache_first

PINNED = "57f491c1718171c0ad71d723c4f6b2030684c4eb"


def _cache(monkeypatch: pytest.MonkeyPatch, cached: set[tuple[str, str | None]]) -> None:
    """Install a Hub cache that holds exactly these (filename, revision) pairs."""

    def try_to_load_from_cache(
        repo_id: str, filename: str, revision: str | None = None
    ) -> str | None:
        if "/" not in repo_id or " " in repo_id:
            raise ValueError("not a repository id")
        return f"/cache/{repo_id}/{filename}" if (filename, revision) in cached else None

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(try_to_load_from_cache=try_to_load_from_cache),
    )


class _Loader:
    def __init__(self, *, fail_locally: bool = False) -> None:
        self.calls: list[bool] = []
        self._fail_locally = fail_locally

    def __call__(self, local_only: bool) -> str:
        self.calls.append(local_only)
        if local_only and self._fail_locally:
            raise OSError("weights missing from the cache")
        return "model"


def _load(loader: _Loader, *, revision: str | None = PINNED, offline: bool = False) -> str:
    return load_cache_first(
        loader,
        repo_id="ufca-llms/jua-4B-mixed",
        revision=revision,
        required_files=("modules.json",),
        offline=offline,
    )


def test_a_cached_pinned_revision_never_asks_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, {("modules.json", PINNED)})
    loader = _Loader()

    assert _load(loader) == "model"
    assert loader.calls == [True]


def test_an_incomplete_cache_falls_back_to_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, {("modules.json", PINNED)})
    loader = _Loader(fail_locally=True)

    assert _load(loader) == "model"
    assert loader.calls == [True, False]


def test_the_first_run_downloads(monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, set())
    loader = _Loader()

    _load(loader)

    # Without modules.json the library would build a different model locally.
    assert loader.calls == [False]


def test_a_movable_revision_still_asks_the_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, {("modules.json", "main"), ("modules.json", None)})
    loader = _Loader()

    _load(loader, revision="main")
    _load(loader, revision=None)

    assert loader.calls == [False, False]


def test_offline_loads_the_cached_model_and_refuses_to_improvise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cache(monkeypatch, {("modules.json", PINNED)})
    loader = _Loader()

    _load(loader, offline=True)
    with pytest.raises(RuntimeError, match="LITIGATION_HF_HUB_OFFLINE"):
        _load(loader, revision="0" * 40, offline=True)

    assert loader.calls == [True]


def test_a_local_directory_loads_as_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, set())
    loader = _Loader()

    load_cache_first(
        loader,
        repo_id=str(tmp_path),
        revision=PINNED,
        required_files=("modules.json",),
        offline=True,
    )

    assert loader.calls == [False]


def test_a_name_that_is_no_repository_id_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _cache(monkeypatch, set())
    loader = _Loader()

    load_cache_first(
        loader,
        repo_id="not a repo",
        revision=PINNED,
        required_files=("modules.json",),
        offline=False,
    )

    assert loader.calls == [False]
