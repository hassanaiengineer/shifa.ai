#
# Lightweight in-memory RAG over the voice assistant's knowledge base.
#
# Chunks the KB by section, embeds each chunk once at startup using Google's
# Gemini embeddings, and answers queries by cosine similarity. No external
# vector DB needed — perfect for a small, curated knowledge base.
#

import os
import re
from pathlib import Path
from typing import List

import numpy as np
from google import genai
from loguru import logger

EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "models/gemini-embedding-001")
KNOWLEDGE_BASE_PATH = Path(__file__).parent / "knowledge_base.md"
TOP_K = int(os.getenv("RAG_TOP_K", "3"))


def _chunk_markdown(text: str) -> List[str]:
    """Split a Markdown doc into chunks, one per `##` section."""
    parts = re.split(r"\n(?=##\s)", text)
    chunks = [p.strip() for p in parts if p.strip() and not p.strip().startswith("# ")]
    head = text.split("\n##", 1)[0].strip()
    if head and head not in chunks:
        chunks.insert(0, head)
    return chunks


class KnowledgeBase:
    """Embeds a Markdown KB once and serves semantic search over it."""

    def __init__(self, api_key: str):
        self._client = genai.Client(api_key=api_key)
        raw = KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8")
        self._chunks = _chunk_markdown(raw)
        self._matrix = self._embed(self._chunks)
        logger.info(f"Voice RAG: embedded {len(self._chunks)} knowledge chunks.")

    def _embed(self, texts: List[str]) -> np.ndarray:
        result = self._client.models.embed_content(model=EMBEDDING_MODEL, contents=texts)
        vectors = np.array([e.values for e in result.embeddings], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.clip(norms, 1e-8, None)

    def retrieve(self, query: str, top_k: int = TOP_K) -> str:
        """Return the most relevant KB chunks for a query, as a single string."""
        q = self._embed([query])[0]
        scores = self._matrix @ q
        top_idx = np.argsort(scores)[::-1][:top_k]
        selected = [self._chunks[i] for i in top_idx]
        logger.info(f"Voice RAG query '{query}' -> top score {scores[top_idx[0]]:.3f}")
        return "\n\n---\n\n".join(selected)
