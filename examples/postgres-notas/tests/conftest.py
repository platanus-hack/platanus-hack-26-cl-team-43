import os

import httpx
import pytest


@pytest.fixture(scope="session")
def client():
    # Cachai pasa BASE_URL al evaluador; en local, `docker compose up` y la misma URL.
    with httpx.Client(base_url=os.environ.get("BASE_URL", "http://localhost:8000"), timeout=10) as c:
        yield c


@pytest.fixture
def ana():
    return {"Authorization": "Bearer tok-ana"}


@pytest.fixture
def beto():
    return {"Authorization": "Bearer tok-beto"}
