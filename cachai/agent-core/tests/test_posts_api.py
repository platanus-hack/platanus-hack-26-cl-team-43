"""Tests for the internal and public blog posts API endpoints."""

from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from psycopg.errors import UniqueViolation
import pytest

from app.api import routes
from app.config import Settings
from app import main

VALID_TOKEN = "test-internal-api-token"

VALID_POST_PAYLOAD = {
    "title": "Prompt Injection Audit Report",
    "slug": "prompt-injection-audit-report",
    "summary": "Detailed red-team evaluation of prompt injection resilience.",
    "markdown_body": "# Findings\n\nThe target model resisted direct override attempts.",
    "eval_id": 42,
    "status": "published",
}

SAMPLE_PUBLISHED_ROW = {
    "id": 1,
    "title": VALID_POST_PAYLOAD["title"],
    "slug": VALID_POST_PAYLOAD["slug"],
    "summary": VALID_POST_PAYLOAD["summary"],
    "markdown_body": VALID_POST_PAYLOAD["markdown_body"],
    "eval_id": VALID_POST_PAYLOAD["eval_id"],
    "status": "published",
    "published_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
}


@pytest.fixture
def client(monkeypatch):
    """Return a TestClient with DB pool and scheduler lifespan hooks disabled."""
    monkeypatch.setattr(main, "init_db_pool", lambda: None)
    monkeypatch.setattr(main, "close_db_pool", lambda: None)
    monkeypatch.setattr(main, "start_scheduler", lambda: None)
    monkeypatch.setattr(main, "stop_scheduler", lambda: None)
    monkeypatch.setattr(
        routes,
        "get_settings",
        lambda: Settings(internal_api_token=VALID_TOKEN),
    )

    with TestClient(main.app) as test_client:
        yield test_client


def test_create_post_rejects_missing_internal_token(client):
    response = client.post("/api/posts", json=VALID_POST_PAYLOAD)

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing internal API token."


def test_create_post_rejects_invalid_internal_token(client):
    response = client.post(
        "/api/posts",
        json=VALID_POST_PAYLOAD,
        headers={"X-Internal-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid or missing internal API token."


@pytest.mark.parametrize(
    "invalid_slug",
    [
        "Uppercase-Slug",
        "slug with spaces",
        "slug_with_underscores",
        "slug!invalid",
        "-leading-hyphen",
        "trailing-hyphen-",
        "double--hyphen",
    ],
)
def test_create_post_rejects_invalid_slug(client, invalid_slug):
    payload = {**VALID_POST_PAYLOAD, "slug": invalid_slug}

    response = client.post(
        "/api/posts",
        json=payload,
        headers={"X-Internal-Token": VALID_TOKEN},
    )

    assert response.status_code == 422


def test_create_post_rejects_extra_unknown_fields(client):
    payload = {**VALID_POST_PAYLOAD, "unexpected_field": "not-allowed"}

    response = client.post(
        "/api/posts",
        json=payload,
        headers={"X-Internal-Token": VALID_TOKEN},
    )

    assert response.status_code == 422


def test_create_post_succeeds_with_valid_token_and_payload(
    client, monkeypatch, make_fake_db
):
    _, _, fake_get_db_connection = make_fake_db(
        fetchone_results=[SAMPLE_PUBLISHED_ROW]
    )
    monkeypatch.setattr(routes, "get_db_connection", fake_get_db_connection)

    response = client.post(
        "/api/posts",
        json=VALID_POST_PAYLOAD,
        headers={"X-Internal-Token": VALID_TOKEN},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["id"] == 1
    assert data["title"] == VALID_POST_PAYLOAD["title"]
    assert data["slug"] == VALID_POST_PAYLOAD["slug"]
    assert data["summary"] == VALID_POST_PAYLOAD["summary"]
    assert data["markdown_body"] == VALID_POST_PAYLOAD["markdown_body"]
    assert data["eval_id"] == 42
    assert data["status"] == "published"
    assert data["published_at"] == "2026-05-01T12:00:00Z"


def test_create_post_returns_409_on_duplicate_slug(client, monkeypatch):
    class DuplicateSlugCursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params=None):
            raise UniqueViolation("duplicate key value violates unique constraint")

    class DuplicateSlugConnection:
        def cursor(self):
            return DuplicateSlugCursor()

        def commit(self):
            pass

    @contextmanager
    def fake_get_db_connection():
        yield DuplicateSlugConnection()

    monkeypatch.setattr(routes, "get_db_connection", fake_get_db_connection)

    response = client.post(
        "/api/posts",
        json=VALID_POST_PAYLOAD,
        headers={"X-Internal-Token": VALID_TOKEN},
    )

    assert response.status_code == 409
    assert VALID_POST_PAYLOAD["slug"] in response.json()["detail"]


def test_list_published_posts_returns_200(client, monkeypatch, make_fake_db):
    second_row = {
        **SAMPLE_PUBLISHED_ROW,
        "id": 2,
        "title": "Jailbreak Resilience Benchmark",
        "slug": "jailbreak-resilience-benchmark",
        "published_at": datetime(2026, 5, 2, 15, 30, 0, tzinfo=timezone.utc),
    }
    _, _, fake_get_db_connection = make_fake_db(
        fetchall_results=[[second_row, SAMPLE_PUBLISHED_ROW]]
    )
    monkeypatch.setattr(routes, "get_db_connection", fake_get_db_connection)

    response = client.get("/api/posts")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert data[0]["slug"] == "jailbreak-resilience-benchmark"
    assert data[1]["slug"] == VALID_POST_PAYLOAD["slug"]


def test_get_published_post_returns_200(client, monkeypatch, make_fake_db):
    _, _, fake_get_db_connection = make_fake_db(
        fetchone_results=[SAMPLE_PUBLISHED_ROW]
    )
    monkeypatch.setattr(routes, "get_db_connection", fake_get_db_connection)

    response = client.get(f"/api/posts/{VALID_POST_PAYLOAD['slug']}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == 1
    assert data["slug"] == VALID_POST_PAYLOAD["slug"]
    assert data["status"] == "published"


def test_get_published_post_returns_404_when_missing_or_unpublished(
    client, monkeypatch, make_fake_db
):
    _, _, fake_get_db_connection = make_fake_db(fetchone_results=[None])
    monkeypatch.setattr(routes, "get_db_connection", fake_get_db_connection)

    response = client.get("/api/posts/nonexistent-or-draft-post")

    assert response.status_code == 404
    assert response.json()["detail"] == "Post not found."
