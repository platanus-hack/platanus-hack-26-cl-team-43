"""Cliente de los tests.

Con BASE_URL los tests hablan por HTTP con la app, sin importarla (así corre el
evaluador de Cachai). Sin BASE_URL importan la app en el mismo proceso,
como un CI normal.

Los tokens son JWT de prueba firmados con el APP_SECRET de prueba para los
usuarios sembrados (`alice` y `bob`).
"""

import os

import pytest

ALICE = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbGljZSIsImV4cCI6NDEwMjQ0NDgwMH0.5iQuAAj1kHneMaZmSRL6eVUIVaOdSJFPYYPK4oJqm_Y"}
BOB = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJib2IiLCJleHAiOjQxMDI0NDQ4MDB9.zbSIOau-7A4gn3p-tzQPvtfKkEgbFvsFMeT3_t6U6U8"}


@pytest.fixture(scope="session")
def client():
    base_url = os.environ.get("BASE_URL")
    if base_url:
        import httpx

        with httpx.Client(base_url=base_url, timeout=10) as c:
            yield c
    else:
        from fastapi.testclient import TestClient

        from secret_storage.main import app

        with TestClient(app) as c:
            yield c


@pytest.fixture
def alice():
    return dict(ALICE)


@pytest.fixture
def bob():
    return dict(BOB)
