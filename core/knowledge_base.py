"""
XClaw Knowledge Base — file ingestion, chunking, and retrieval.

Navigator can upload documents (PDF, TXT, CSV, Markdown, code) which are
chunked, stored in SQLite, and searchable by keyword or simple tf-idf scoring.

The KB is exposed as a tool to the AgentLoop so the LLM can query it directly:
  search_knowledge(query) → relevant chunks
  ingest_text(content, source) → stored

Storage: SQLite `chunks` table (added to Memory DB in memory.py).
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.memory import Memory

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 400       # words per chunk
_CHUNK_OVERLAP = 80     # words overlap between chunks
_MAX_RESULTS = 6
_MIN_SCORE = 0.01


class KnowledgeBase:
    """
    Chunked document store with BM25-style keyword retrieval.

    Args:
        memory: Shared Memory instance (chunks stored in its SQLite DB).
        kb_dir: Directory for storing raw uploaded files.
    """

    def __init__(self, memory: "Memory", kb_dir: str | Path = "memory/kb") -> None:
        self._memory = memory
        self._kb_dir = Path(kb_dir)
        self._kb_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        conn = self._memory._conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS kb_chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id      TEXT    NOT NULL,
                source      TEXT    NOT NULL,
                chunk_idx   INTEGER NOT NULL,
                content     TEXT    NOT NULL,
                word_count  INTEGER NOT NULL,
                tags        TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_kb_source ON kb_chunks(source);
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def ingest_text(self, content: str, source: str, tags: list[str] | None = None) -> str:
        """
        Chunk and store raw text.

        Returns a summary of what was ingested.
        """
        doc_id = hashlib.sha256(f"{source}:{content[:200]}".encode()).hexdigest()[:16]
        tags_str = ",".join(tags or [])
        chunks = self._chunk(content)

        now = self._memory._now()
        with self._memory._conn() as conn:
            # Remove old chunks for this doc
            conn.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
            conn.executemany(
                "INSERT INTO kb_chunks (doc_id, source, chunk_idx, content, word_count, tags, created_at) VALUES (?,?,?,?,?,?,?)",
                [(doc_id, source, i, chunk, len(chunk.split()), tags_str, now) for i, chunk in enumerate(chunks)],
            )

        logger.info("[kb] ingested %d chunks from %s (doc_id=%s)", len(chunks), source, doc_id)
        return f"Ingested '{source}' → {len(chunks)} chunks stored."

    def ingest_file(self, path: str | Path, tags: list[str] | None = None) -> str:
        """Read and ingest a local file. Supports .txt, .md, .py, .csv, .pdf (basic)."""
        path = Path(path)
        if not path.exists():
            return f"File not found: {path}"

        suffix = path.suffix.lower()
        try:
            if suffix == ".pdf":
                content = self._read_pdf(path)
            else:
                content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Could not read {path}: {exc}"

        # Copy file to KB directory
        dest = self._kb_dir / path.name
        dest.write_bytes(path.read_bytes())

        return self.ingest_text(content, source=path.name, tags=tags)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def search(self, query: str, limit: int = _MAX_RESULTS) -> list[str]:
        """
        Return the most relevant chunks for `query` using TF-IDF scoring.
        Returns a list of text chunks sorted by relevance.
        """
        query_terms = self._tokenise(query)
        if not query_terms:
            return []

        with self._memory._conn() as conn:
            rows = conn.execute("SELECT content, source, chunk_idx FROM kb_chunks").fetchall()

        if not rows:
            return []

        corpus = [(r["content"], r["source"], r["chunk_idx"]) for r in rows]
        N = len(corpus)

        # Document frequency
        df: Counter = Counter()
        tokenised = []
        for content, *_ in corpus:
            terms = set(self._tokenise(content))
            tokenised.append(terms)
            for t in terms:
                df[t] += 1

        # Score each chunk
        scores: list[tuple[float, str]] = []
        for i, (content, source, chunk_idx) in enumerate(corpus):
            score = 0.0
            term_counts = Counter(self._tokenise(content))
            total_terms = sum(term_counts.values()) or 1
            for qt in query_terms:
                tf = term_counts.get(qt, 0) / total_terms
                idf = math.log((N + 1) / (df.get(qt, 0) + 1))
                score += tf * idf
            if score > _MIN_SCORE:
                scores.append((score, f"[{source}]\n{content}"))

        scores.sort(key=lambda x: -x[0])
        return [text for _, text in scores[:limit]]

    def search_formatted(self, query: str, limit: int = _MAX_RESULTS) -> str:
        """Return search results as a single formatted string."""
        results = self.search(query, limit)
        if not results:
            return "No relevant knowledge found."
        return "\n\n---\n\n".join(results)

    def list_sources(self) -> list[dict]:
        """List all ingested documents."""
        with self._memory._conn() as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) as chunks, MAX(created_at) as last_ingested FROM kb_chunks GROUP BY source ORDER BY last_ingested DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_source(self, source: str) -> str:
        with self._memory._conn() as conn:
            cur = conn.execute("DELETE FROM kb_chunks WHERE source=?", (source,))
        return f"Deleted {cur.rowcount} chunks for '{source}'."

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenise(text: str) -> list[str]:
        """Lowercase word tokenisation, removes stop words and short tokens."""
        _STOP = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at",
                 "to", "of", "and", "or", "for", "it", "this", "that", "with", "be"}
        words = re.findall(r"[a-z][a-z0-9]*", text.lower())
        return [w for w in words if w not in _STOP and len(w) > 2]

    @staticmethod
    def _chunk(text: str, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
        """Split text into overlapping word-based chunks."""
        words = text.split()
        if not words:
            return []
        step = max(1, size - overlap)
        chunks = []
        for i in range(0, len(words), step):
            chunk = " ".join(words[i: i + size])
            if chunk.strip():
                chunks.append(chunk)
        return chunks

    @staticmethod
    def _read_pdf(path: Path) -> str:
        """Best-effort PDF text extraction (requires pypdf if available)."""
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            return path.read_bytes().decode("utf-8", errors="replace")
