"""RAG retrieval over knowledge_chunks: nearest first, top 4, tenant-isolated, never fatal."""

from __future__ import annotations

import math

from api.agents.context import retrieve_knowledge
from api.db.models import KnowledgeChunk
from api.db.models.knowledge import EMBEDDING_DIMS
from api.db.session import Database
from api.tests.conftest import TenantPair

TOPICS = ["coupon", "delivery", "bottle", "payment", "hours", "area"]


def vec(topic: str, noise: float = 0.0) -> list[float]:
    v = [0.0] * EMBEDDING_DIMS
    v[TOPICS.index(topic)] = 1.0
    v[-1] = noise
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


class TopicEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [vec(next(t for t in TOPICS if t in text.lower())) for text in texts]


class BrokenEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("onnx failed")


async def test_nearest_chunks_first_limited_to_four_and_tenant_scoped(
    db: Database, tenants: TenantPair
) -> None:
    async with db.tenant_session(tenants.a) as s:
        for i, topic in enumerate(TOPICS):
            s.add(
                KnowledgeChunk(
                    title=f"A-{topic}",
                    content=f"about {topic}",
                    embedding=vec(topic, noise=i * 0.01),
                )
            )
        s.add(KnowledgeChunk(title="A-noembed", content="not embedded yet"))
    async with db.tenant_session(tenants.b) as s:
        s.add(
            KnowledgeChunk(
                title="B-coupon", content="tenant B coupon secret", embedding=vec("coupon")
            )
        )

    async with db.tenant_session(tenants.a) as s:
        chunks = await retrieve_knowledge(s, TopicEmbedder(), "what about my coupon")
    assert len(chunks) == 4
    assert chunks[0].startswith("A-coupon")
    assert not any("tenant B" in c for c in chunks)


async def test_embedding_failure_degrades_to_no_knowledge(
    db: Database, tenants: TenantPair
) -> None:
    async with db.tenant_session(tenants.a) as s:
        assert await retrieve_knowledge(s, BrokenEmbedder(), "coupon") == []
        assert await retrieve_knowledge(s, None, "coupon") == []
