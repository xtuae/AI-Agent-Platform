"""Load a tenant's knowledge base (what the agent may quote in support answers) — HMH Labz staff.

    python -m api.scripts.knowledge load  --slug hmhlabz --file /k/hmhlabz.md  # replaces
    python -m api.scripts.knowledge list  --slug hmhlabz
    python -m api.scripts.knowledge clear --slug hmhlabz [--source hmhlabz.md]

The file is Markdown: every `## Heading` starts one topic, its text below is the answer. A topic
longer than ~CHUNK_WORDS is split on paragraph boundaries, each piece keeping the heading as its
title. `load` replaces every chunk previously loaded from the same file name (the `source`), so
re-running after an edit is safe. Text before the first heading is ignored.

Chunks are embedded with EMBEDDING_MODEL (fastembed, in-process) and stored under the tenant's
RLS context; retrieval takes the 4 nearest per turn (api.agents.context.retrieve_knowledge).
Only put confirmed facts here: the agent treats them as true.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from sqlalchemy import func, select

from api.config import get_settings
from api.db.models import KnowledgeChunk, Tenant
from api.db.session import Database
from api.llm.embeddings import Embedder, FastEmbedder

CHUNK_WORDS: Final = 120
_ARABIC = re.compile(r"[؀-ۿ]")


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class Chunk:
    title: str
    content: str
    language: str


def parse(text: str) -> list[Chunk]:
    """`## Heading` sections → chunks of at most ~CHUNK_WORDS words, split between paragraphs."""
    chunks: list[Chunk] = []
    for section in re.split(r"(?m)^##\s+", text)[1:]:
        heading, _, body = section.partition("\n")
        title = heading.strip().strip("#").strip()
        paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]
        piece: list[str] = []
        for p in paragraphs:
            if piece and len(" ".join([*piece, p]).split()) > CHUNK_WORDS:
                chunks.append(_chunk(title, piece))
                piece = []
            piece.append(p)
        if piece:
            chunks.append(_chunk(title, piece))
    return chunks


def _chunk(title: str, paragraphs: list[str]) -> Chunk:
    content = "\n".join(paragraphs)
    return Chunk(title, content, "ar" if _ARABIC.search(title + content) else "en")


async def _tenant_id(db: Database, slug: str) -> uuid.UUID:
    async with db.platform_session() as s:
        tid = await s.scalar(select(Tenant.id).where(Tenant.slug == slug))
    if tid is None:
        raise KnowledgeError(f"no tenant with slug {slug!r}")
    return tid


async def _delete(db: Database, tid: uuid.UUID, source: str | None) -> int:
    async with db.tenant_session(tid) as s:
        q = select(KnowledgeChunk)
        if source is not None:
            q = q.where(KnowledgeChunk.source == source)
        rows = (await s.scalars(q)).all()
        for row in rows:
            await s.delete(row)
    return len(rows)


async def load(db: Database, embedder: Embedder, slug: str, path: Path) -> tuple[int, int]:
    """Replace the chunks from `path.name` with the file's current content → (removed, added)."""
    chunks = parse(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    if not chunks:
        raise KnowledgeError(f"{path.name}: no '## Heading' sections found")
    vectors = await embedder.embed([f"{c.title}\n{c.content}" for c in chunks])
    tid = await _tenant_id(db, slug)
    removed = await _delete(db, tid, path.name)
    async with db.tenant_session(tid) as s:
        for c, v in zip(chunks, vectors, strict=True):
            s.add(
                KnowledgeChunk(
                    source=path.name,
                    title=c.title,
                    content=c.content,
                    language=c.language,
                    embedding=v,
                )
            )
    return removed, len(chunks)


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    db = Database(settings)
    try:
        if args.cmd == "load":
            embedder = FastEmbedder(settings.embedding_model, settings.embedding_cache_dir)
            removed, added = await load(db, embedder, args.slug, Path(args.file))
            print(f"{args.slug}: {Path(args.file).name}: removed {removed}, loaded {added} chunks")
        elif args.cmd == "clear":
            n = await _delete(db, await _tenant_id(db, args.slug), args.source)
            print(f"{args.slug}: removed {n} chunks")
        else:  # list
            tid = await _tenant_id(db, args.slug)
            async with db.tenant_session(tid) as s:
                rows = (
                    await s.execute(
                        select(
                            KnowledgeChunk.source,
                            KnowledgeChunk.title,
                            func.length(KnowledgeChunk.content),
                        ).order_by(KnowledgeChunk.source, KnowledgeChunk.title)
                    )
                ).all()
            for source, title, length in rows:
                print(f"  {source or '-':<24} {length:>5} chars  {title}")
            print(f"{args.slug}: {len(rows)} chunks")
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("load", "list", "clear"):
        p = sub.add_parser(name)
        p.add_argument("--slug", required=True)
        if name == "load":
            p.add_argument("--file", required=True)
        if name == "clear":
            p.add_argument("--source", help="only chunks loaded from this file name")
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args))
    except (KnowledgeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
