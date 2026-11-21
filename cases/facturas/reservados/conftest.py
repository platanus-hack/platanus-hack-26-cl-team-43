"""Cliente de los tests reservados: siempre por HTTP contra BASE_URL."""

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=os.environ["BASE_URL"], timeout=10) as c:
        yield c


@pytest.fixture
def bob():
    return {"Authorization": "Bearer tok-bob"}
