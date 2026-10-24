"""
Shared Ollama client settings for all agents.
Model, context and limits come from .env so every agent stays in sync.
- Thinking is off by default: agents need JSON only, and qwen3's thinking can
  loop for thousands of tokens, holding Ollama while every other agent waits.
- num_predict caps each reply so no single request can run unbounded.
- The timeout is generous because agents run concurrently and Ollama queues
  their requests.
"""

import os

from langchain_ollama import ChatOllama, OllamaEmbeddings


def chat_llm() -> ChatOllama:
    return ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        temperature=0.1,
        reasoning=os.getenv("OLLAMA_THINK", "false").lower() == "true",
        num_ctx=int(os.getenv("OLLAMA_NUM_CTX", "8192")),
        num_predict=int(os.getenv("OLLAMA_NUM_PREDICT", "3072")),
        timeout=int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "600")),
    )


def embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"))
