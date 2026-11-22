"""Initial schema baseline matching db/init.sql.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS model_releases (
            id SERIAL PRIMARY KEY,
            model_name VARCHAR(255) NOT NULL,
            provider VARCHAR(120) NOT NULL,
            release_date DATE NOT NULL,
            audited BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_model_releases_name_provider UNIQUE (model_name, provider)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_datasets (
            id SERIAL PRIMARY KEY,
            category VARCHAR(120) NOT NULL,
            prompt_text TEXT NOT NULL,
            source VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_eval_datasets_prompt UNIQUE (category, prompt_text)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_results (
            id SERIAL PRIMARY KEY,
            model_id INTEGER NOT NULL REFERENCES model_releases(id) ON DELETE CASCADE,
            dataset_id INTEGER NOT NULL REFERENCES eval_datasets(id) ON DELETE CASCADE,
            passed BOOLEAN NOT NULL,
            score NUMERIC(5, 4) NOT NULL CHECK (score >= 0 AND score <= 1),
            logs TEXT NOT NULL DEFAULT '',
            cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS blog_posts (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            slug VARCHAR(255) NOT NULL UNIQUE,
            summary TEXT NOT NULL,
            markdown_body TEXT NOT NULL,
            eval_id INTEGER REFERENCES eval_results(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
            published_at TIMESTAMPTZ
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_runs (
            id SERIAL PRIMARY KEY,
            trigger_source VARCHAR(64) NOT NULL DEFAULT 'scheduler',
            model_id INTEGER REFERENCES model_releases(id) ON DELETE SET NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'skipped')),
            budget_approved BOOLEAN NOT NULL DEFAULT FALSE,
            evaluations_count INTEGER NOT NULL DEFAULT 0,
            published_post_slug VARCHAR(255),
            error_summary TEXT NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at TIMESTAMPTZ,
            duration_ms INTEGER
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_releases_audited "
        "ON model_releases (audited, release_date DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_results_model_id "
        "ON eval_results (model_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_blog_posts_status_published "
        "ON blog_posts (status, published_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_runs_started_at "
        "ON audit_runs (started_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_runs_status "
        "ON audit_runs (status, started_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_audit_runs_status")
    op.execute("DROP INDEX IF EXISTS idx_audit_runs_started_at")
    op.execute("DROP INDEX IF EXISTS idx_blog_posts_status_published")
    op.execute("DROP INDEX IF EXISTS idx_eval_results_model_id")
    op.execute("DROP INDEX IF EXISTS idx_model_releases_audited")

    op.execute("DROP TABLE IF EXISTS audit_runs")
    op.execute("DROP TABLE IF EXISTS blog_posts")
    op.execute("DROP TABLE IF EXISTS eval_results")
    op.execute("DROP TABLE IF EXISTS eval_datasets")
    op.execute("DROP TABLE IF EXISTS model_releases")
