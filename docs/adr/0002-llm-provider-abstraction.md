# ADR 0002: Own LLM port instead of LiteLLM/proxy

Status: accepted · Date: 2026-07-23

## Context

The product must support OpenAI, Anthropic and Gemini without coupling its
security and drafting services to any vendor SDK. Options: LiteLLM (uniform API over 100+ providers),
LangChain chat models, or our own thin protocol.

## Decision

Define our own `LLMClient` protocol (`app/llm/base.py`) with exactly the two
operations the product needs (`complete`, `parse`) and rich call metadata.
OpenAI, Anthropic Claude, Google Gemini and Mock have native adapters; a factory
selects the provider from config. The same abstraction preserves Pydantic
structured outputs and usage metadata across vendors. Embeddings remain an
independent port because not every LLM vendor offers them.

## Consequences

- (+) The interface matches OUR domain (structured outputs + observability
  are mandatory, not optional extras).
- (+) Low lock-in: each provider is one adapter implementing two methods.
- (+) The mock adapter makes tests/CI/demos free and deterministic.
- (-) We maintain one small native adapter per provider instead of delegating
  compatibility to LiteLLM.
  Acceptable: we expect 2-3 providers, not 100.
