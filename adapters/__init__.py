"""Adapters for Market Brief Studio.

Every external dependency (OKX market data, LLM script writing) sits behind an
adapter with two modes, selected by the OKX_MODE env var:

  - "mock" (default): fully working, deterministic-per-date, no credentials.
  - "real": marked stubs that document the exact okx-trade-cli / Claude API
    calls to wire once credentials exist.
"""
