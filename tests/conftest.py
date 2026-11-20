"""Isola a suíte do .env de produção (postgres/JUÁ) antes de importar a app."""

from __future__ import annotations

import os

# app.api.main cria Settings() no import; forçamos stack offline de teste.
os.environ["LITIGATION_VECTOR_STORE"] = "memory"
os.environ["LITIGATION_LLM_PROVIDER"] = "mock"
os.environ.pop("LITIGATION_POSTGRES_DSN", None)
os.environ.pop("LITIGATION_DOCKER_POSTGRES_DSN", None)
