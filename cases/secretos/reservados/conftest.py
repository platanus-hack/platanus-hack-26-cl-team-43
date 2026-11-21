"""Tests funcionales reservados: el parchador no los ve. Corren solo en el evaluador."""

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=os.environ["BASE_URL"], timeout=10) as c:
        yield c


@pytest.fixture
def bob():
    return {"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJib2IiLCJleHAiOjQxMDI0NDQ4MDB9.zbSIOau-7A4gn3p-tzQPvtfKkEgbFvsFMeT3_t6U6U8"}
