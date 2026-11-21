"""Tests funcionales de la rama base: lo que la app debe seguir haciendo."""


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_requires_auth(client):
    assert client.get("/invoices").status_code == 401


def test_lists_only_own_invoices(client, alice, bob):
    assert [i["number"] for i in client.get("/invoices", headers=alice).json()] == ["FAC-A-001", "FAC-A-002"]
    assert [i["number"] for i in client.get("/invoices", headers=bob).json()] == ["FAC-B-001"]


def test_reads_own_invoice(client, alice):
    r = client.get("/invoices/1", headers=alice)
    assert r.status_code == 200
    assert r.json()["number"] == "FAC-A-001"


def test_missing_invoice_is_404(client, alice):
    r = client.get("/invoices/999", headers=alice)
    assert r.status_code == 404
    assert r.json()["detail"] == "no existe"


def test_pdf_of_own_invoice(client, bob):
    r = client.get("/invoices/3/pdf", headers=bob)
    assert r.status_code == 200
    assert "FAC-B-001" in r.text


def test_v2_own_invoice(client, alice):
    assert client.get("/v2/invoices/2", headers=alice).json()["data"]["number"] == "FAC-A-002"


def test_export_own_invoices(client, alice):
    r = client.get("/invoices/export", params={"ids": "1,2"}, headers=alice)
    assert [i["number"] for i in r.json()] == ["FAC-A-001", "FAC-A-002"]


def test_own_attachment(client, alice):
    r = client.get("/attachments", params={"name": "contrato.txt"}, headers=alice)
    assert r.status_code == 200
    assert "FAC-A-001" in r.text


def test_total_includes_tax(client, alice):
    assert client.get("/invoices/1/total", headers=alice).json()["total"] == 1428.0


def test_bob_own_attachment(client, bob):
    r = client.get("/attachments", params={"name": "orden.txt"}, headers=bob)
    assert r.status_code == 200
    assert "FAC-B-001" in r.text


def test_every_route_requires_auth(client):
    for path in ["/invoices/1", "/invoices/1/total", "/invoices/1/pdf", "/v2/invoices/1",
                 "/invoices/export?ids=1", "/attachments?name=contrato.txt"]:
        assert client.get(path).status_code == 401, path
