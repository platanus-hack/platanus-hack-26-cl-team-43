"""Tests funcionales reservados: el dueño sigue accediendo a sus datos por todas las rutas.

Detectan un parche que niega todo o que solo deja pasar a un usuario.
"""


def test_owner_reads_invoice(client, bob):
    r = client.get("/invoices/3", headers=bob)
    assert r.status_code == 200
    assert r.json()["number"] == "FAC-B-001"


def test_owner_reads_pdf(client, bob):
    r = client.get("/invoices/3/pdf", headers=bob)
    assert r.status_code == 200
    assert "FAC-B-001" in r.text


def test_owner_reads_total(client, bob):
    r = client.get("/invoices/3/total", headers=bob)
    assert r.status_code == 200
    assert r.json()["total"] == 535.5


def test_owner_reads_v2(client, bob):
    r = client.get("/v2/invoices/3", headers=bob)
    assert r.status_code == 200
    assert r.json()["data"]["number"] == "FAC-B-001"


def test_owner_exports_own_invoice(client, bob):
    r = client.get("/invoices/export", params={"ids": "3"}, headers=bob)
    assert r.status_code == 200
    assert [i["number"] for i in r.json()] == ["FAC-B-001"]


def test_owner_reads_own_attachment(client, bob):
    r = client.get("/attachments", params={"name": "orden.txt"}, headers=bob)
    assert r.status_code == 200
    assert "FAC-B-001" in r.text
