"""Tests funcionales reservados: el parchador no los ve. Corren solo en el evaluador."""

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=os.environ["BASE_URL"], timeout=10) as c:
        yield c


@pytest.fixture
def alice():
    return {"Authorization": "Bearer tok-alice"}


@pytest.fixture
def bob():
    return {"Authorization": "Bearer tok-bob"}
