"""RAG knowledge chunks. Embeddings: fastembed bge-small-en-v1.5, 384 dims."""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from api.db.base import Base, TenantScoped, uuid_pk

EMBEDDING_DIMS = 384


class KnowledgeChunk(TenantScoped, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        # HNSW rather than the spec's ivfflat — see the migration for the reason.
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    source: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMS))
    language: Mapped[str | None] = mapped_column(Text)
