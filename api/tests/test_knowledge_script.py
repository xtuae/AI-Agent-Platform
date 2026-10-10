"""api.scripts.knowledge: Markdown → chunks, load replaces by source, tenant-scoped."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from api.db.models import KnowledgeChunk, Tenant
from api.db.models.knowledge import EMBEDDING_DIMS
from api.db.session import Database
from api.scripts.knowledge import CHUNK_WORDS, KnowledgeError, load, parse
from api.tests.conftest import TenantPair


class FlatEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIMS - 1) for _ in texts]


def test_parse_sections_split_long_and_detect_arabic() -> None:
    long_para = " ".join(["word"] * (CHUNK_WORDS - 10))
    text = (
        "intro line is ignored\n"
        "## Services\nWe build AI agents.\n\nThey answer on WhatsApp.\n"
        f"## Long topic\n{long_para}\n\n{long_para}\n"
        "## اللغات\nنتحدث العربية والإنجليزية.\n"
    )
    chunks = parse(text)
    assert [c.title for c in chunks] == ["Services", "Long topic", "Long topic", "اللغات"]
    assert chunks[0].content == "We build AI agents.\nThey answer on WhatsApp."
    assert [c.language for c in chunks] == ["en", "en", "en", "ar"]


def test_parse_without_headings_is_empty() -> None:
    assert parse("just text, no sections") == []


async def _slug(db: Database, tenants: TenantPair) -> str:
    async with db.platform_session() as s:
        slug = await s.scalar(select(Tenant.slug).where(Tenant.id == tenants.a))
    assert slug
    return slug


async def test_load_replaces_same_source_and_stays_in_tenant(
    db: Database, tenants: TenantPair, tmp_path: Path
) -> None:
    slug = await _slug(db, tenants)
    f = tmp_path / "kb.md"
    f.write_text("## One\nfirst\n## Two\nsecond\n", encoding="utf-8")
    assert await load(db, FlatEmbedder(), slug, f) == (0, 2)
    f.write_text("## One\nfirst, edited\n", encoding="utf-8")
    assert await load(db, FlatEmbedder(), slug, f) == (2, 1)

    async with db.tenant_session(tenants.a) as s:
        rows = (await s.scalars(select(KnowledgeChunk))).all()
    assert [(r.source, r.title, r.content) for r in rows] == [("kb.md", "One", "first, edited")]
    assert rows[0].embedding is not None
    async with db.tenant_session(tenants.b) as s:
        assert (await s.scalars(select(KnowledgeChunk))).all() == []


async def test_load_rejects_file_without_sections(
    db: Database, tenants: TenantPair, tmp_path: Path
) -> None:
    f = tmp_path / "empty.md"
    f.write_text("nothing here", encoding="utf-8")
    with pytest.raises(KnowledgeError):
        await load(db, FlatEmbedder(), await _slug(db, tenants), f)
