import asyncio
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status
from psycopg.errors import UniqueViolation

from app.agents.graph import run_audit_cycle
from app.config import get_settings
from app.db import get_db_connection
from app.schemas import BlogPostCreate, BlogPostResponse, TriggerDemoResponse

router = APIRouter()


def verify_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    expected = get_settings().internal_api_token
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing internal API token.",
        )


@router.get("/health")
def health_check() -> dict[str, str]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    return {"status": "ok"}


@router.get("/api/posts", response_model=list[BlogPostResponse])
def list_published_posts() -> list[dict]:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, slug, summary, markdown_body, eval_id, status, published_at
                FROM blog_posts
                WHERE status = 'published'
                ORDER BY published_at DESC NULLS LAST, id DESC
                """
            )
            return list(cur.fetchall())


@router.get("/api/posts/{slug}", response_model=BlogPostResponse)
def get_published_post(slug: str) -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, title, slug, summary, markdown_body, eval_id, status, published_at
                FROM blog_posts
                WHERE slug = %s AND status = 'published'
                """,
                (slug,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found.")
    return dict(row)


@router.post(
    "/api/posts",
    response_model=BlogPostResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_internal_token)],
)
def create_blog_post(payload: BlogPostCreate) -> dict:
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO blog_posts (title, slug, summary, markdown_body, eval_id, status, published_at)
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s = 'published' THEN NOW() ELSE NULL END
                    )
                    RETURNING id, title, slug, summary, markdown_body, eval_id, status, published_at
                    """,
                    (
                        payload.title,
                        payload.slug,
                        payload.summary,
                        payload.markdown_body,
                        payload.eval_id,
                        payload.status,
                        payload.status,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
    except UniqueViolation:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A post with slug '{payload.slug}' already exists.",
        )
    return dict(row)


@router.post("/api/trigger-demo", response_model=TriggerDemoResponse)
async def trigger_demo_cycle() -> TriggerDemoResponse:
    """Manually trigger one full LangGraph audit cycle for live demos."""
    result = await asyncio.to_thread(run_audit_cycle, "demo_api")
    return TriggerDemoResponse(
        status="completed",
        trigger_source=result.get("trigger_source", "demo_api"),
        target_model=result.get("target_model"),
        budget_approved=bool(result.get("budget_approved", False)),
        budget_remaining_usd=float(result.get("budget_remaining_usd", 0.0)),
        budget_reason=result.get("budget_reason", ""),
        evaluations_run=len(result.get("eval_results") or []),
        published_post_slug=result.get("published_post_slug"),
        errors=list(result.get("errors") or []),
    )
