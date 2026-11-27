"""Regressão: .env local com postgres não pode quebrar a coleta dos testes."""

from __future__ import annotations

import os

import pytest


def test_litigation_vector_store_forced_to_memory_for_tests() -> None:
    assert os.environ.get("LITIGATION_VECTOR_STORE") == "memory"
    assert os.environ.get("LITIGATION_LLM_PROVIDER") == "mock"


def test_create_app_import_succeeds_with_local_env_present() -> None:
    # Import tardio: conftest já isolou o ambiente.
    from app.api.main import create_app
    from app.core.config import LLMProvider, Settings, VectorStoreBackend

    settings = Settings(
        llm_provider=LLMProvider.MOCK,
        vector_store=VectorStoreBackend.MEMORY,
        _env_file=None,
    )
    app = create_app(settings)
    assert app is not None
