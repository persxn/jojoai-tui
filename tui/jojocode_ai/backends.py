"""Which model server the local agent talks to.

Two protocols, one interface (version, list_models, has_model, capabilities,
context_window, chat). `api` is "ollama", "openai", or "auto" — auto means
Ollama unless the host URL ends in /v1, which is how every OpenAI-compatible
server (LM Studio, llama.cpp's llama-server, vLLM, Jan, LocalAI, and Ollama's
own compatibility endpoint) is addressed.
"""

from __future__ import annotations

from .ollama_client import OllamaClient
from .openai_client import OpenAIClient


def pick_api(api: str | None, host: str) -> str:
    api = (api or "auto").strip().lower()
    if api in ("openai", "oai", "v1"):
        return "openai"
    if api == "ollama":
        return "ollama"
    return "openai" if host.rstrip("/").endswith("/v1") else "ollama"


def make_client(cfg):
    if pick_api(getattr(cfg, "api", "auto"), cfg.host) == "openai":
        return OpenAIClient(cfg.host, cfg.model, cfg.num_ctx, cfg.request_timeout)
    return OllamaClient(cfg.host, cfg.model, cfg.num_ctx, cfg.request_timeout)
