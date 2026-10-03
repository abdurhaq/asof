"""Central configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: load .env when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# Groq retired these models on 16 Aug 2026 (free/developer tiers); map old .env values to the replacements.
RETIRED_GROQ_MODELS = {
    "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    "llama-3.1-8b-instant": "openai/gpt-oss-20b",
}


def _model(name: str) -> str:
    return RETIRED_GROQ_MODELS.get(name, name)


@dataclass
class Settings:
    # --- paths ---
    data_dir: Path = field(default_factory=lambda: Path(_env("ASOF_DATA_DIR", str(PROJECT_ROOT / "data"))))

    # --- LLM (Groq) ---
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    answer_model: str = field(default_factory=lambda: _model(_env("GROQ_ANSWER_MODEL", "openai/gpt-oss-120b")))
    fast_model: str = field(default_factory=lambda: _model(_env("GROQ_FAST_MODEL", "openai/gpt-oss-20b")))

    # --- Graph DB (Neo4j). Leave NEO4J_URI empty to use the in-memory NetworkX store. ---
    neo4j_uri: str = field(default_factory=lambda: _env("NEO4J_URI"))
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USERNAME", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD"))
    # empty = the instance's default database (newer AuraDB instances name it after the instance id)
    neo4j_database: str = field(default_factory=lambda: _env("NEO4J_DATABASE", ""))

    # --- Retrieval ---
    embedding_model: str = field(default_factory=lambda: _env("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5"))
    reranker_model: str = field(default_factory=lambda: _env("RERANKER_MODEL", "cross-encoder/ms-marco-MiniLM-L-6-v2"))
    use_reranker: bool = field(default_factory=lambda: _env("USE_RERANKER", "true").lower() == "true")
    top_k: int = field(default_factory=lambda: int(_env("TOP_K", "6")))
    candidate_k: int = field(default_factory=lambda: int(_env("CANDIDATE_K", "40")))
    # prompt context budget in characters (~4 chars/token); 14000 keeps requests under the
    # 8,000 tokens-per-minute limit of Groq's free tier for gpt-oss-120b
    context_chars: int = field(default_factory=lambda: int(_env("CONTEXT_CHARS", "14000")))

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def use_neo4j(self) -> bool:
        return bool(self.neo4j_uri)


def get_settings() -> Settings:
    return Settings()
