"""Move tenant_settings off LLM model ids Google no longer serves.

On 2026-10-10 the Gemini API returned 404 for gemini-2.5-flash-lite and gemini-2.5-flash ("no
longer available to new users") and for gemini-3-flash (never a real id; only
gemini-3-flash-preview exists), so every turn on the old defaults failed in the classifier.

* Column defaults: llm_model_chat → gemini-3.5-flash, llm_model_classify → gemini-3.5-flash-lite.
* Rows still on a retired id move to the new one; a tenant deliberately on another model keeps it.

Production was hot-fixed by hand with the same values, so there this is a no-op. tenant_settings
is a platform table (no RLS), so a plain UPDATE sees every tenant.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHAT, CLASSIFY = "gemini-3.5-flash", "gemini-3.5-flash-lite"
OLD_CHAT, OLD_CLASSIFY = "gemini-3-flash", "gemini-2.5-flash-lite"
# Two statements, executed separately (asyncpg rejects several commands in one execute).
MOVE_CHAT = """
UPDATE tenant_settings SET llm_model_chat = 'gemini-3.5-flash'
  WHERE llm_model_chat IN ('gemini-3-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite')
"""
MOVE_CLASSIFY = """
UPDATE tenant_settings SET llm_model_classify = 'gemini-3.5-flash-lite'
  WHERE llm_model_classify IN ('gemini-3-flash', 'gemini-2.5-flash', 'gemini-2.5-flash-lite')
"""


def upgrade() -> None:
    op.execute(f"ALTER TABLE tenant_settings ALTER COLUMN llm_model_chat SET DEFAULT '{CHAT}'")
    op.execute(
        f"ALTER TABLE tenant_settings ALTER COLUMN llm_model_classify SET DEFAULT '{CLASSIFY}'"
    )
    op.execute(MOVE_CHAT)
    op.execute(MOVE_CLASSIFY)


def downgrade() -> None:
    # Defaults only: the old ids 404, so rows are not moved back onto them.
    op.execute(f"ALTER TABLE tenant_settings ALTER COLUMN llm_model_chat SET DEFAULT '{OLD_CHAT}'")
    op.execute(
        f"ALTER TABLE tenant_settings ALTER COLUMN llm_model_classify SET DEFAULT '{OLD_CLASSIFY}'"
    )
