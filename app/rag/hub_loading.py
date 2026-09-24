"""Load local Hugging Face models from the cache when that is exactly right.

A model pinned to a full commit hash names immutable files, so a cached copy
is what the Hub would serve and asking the Hub again only costs start-up time
(and, without a token, prints a rate-limit warning). A branch or tag can move,
so those still ask the Hub.

The cached copy must be complete before it is used. sentence-transformers
treats a missing ``modules.json`` as "not a sentence-transformers model" and
silently builds a plain mean-pooling encoder from the weights, which would
embed with a different model than the one configured. The caller names the
files that prove a complete download, and the local load is attempted only
when they are cached.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from app.core.logging import get_logger

logger = get_logger(__name__)

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")

ModelT = TypeVar("ModelT")


def load_cache_first(
    load: Callable[[bool], ModelT],
    *,
    repo_id: str,
    revision: str | None,
    required_files: Sequence[str],
    offline: bool,
) -> ModelT:
    """Call ``load(local_files_only)`` the cheapest way that loads the right model.

    - A local directory loads as is.
    - ``offline`` never contacts the Hub; it fails clearly when the cache lacks
      the model instead of letting the library improvise one.
    - A commit-pinned revision whose required files are cached loads locally,
      and falls back to the Hub only if the local load fails.
    - Anything else asks the Hub, which also fills the cache.
    """
    if Path(repo_id).is_dir():
        return load(False)
    cached = _cached(repo_id, revision, required_files)
    if offline:
        if not cached:
            raise RuntimeError(
                f"{repo_id}@{revision or 'default branch'} is not complete in the local "
                "Hugging Face cache; unset LITIGATION_HF_HUB_OFFLINE (or HF_HUB_OFFLINE) "
                "once to download it"
            )
        return load(True)
    if cached and revision is not None and _COMMIT_SHA.fullmatch(revision):
        try:
            return load(True)
        except OSError:
            logger.info("hugging_face_cache_incomplete", repo_id=repo_id, revision=revision)
    return load(False)


def _cached(repo_id: str, revision: str | None, required_files: Sequence[str]) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:  # pragma: no cover - installed with sentence-transformers
        return False
    try:
        return all(
            isinstance(try_to_load_from_cache(repo_id, filename, revision=revision), str)
            for filename in required_files
        )
    except ValueError:  # not a valid Hub repository id
        return False
