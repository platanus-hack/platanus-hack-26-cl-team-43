"""Lo que la app debe seguir haciendo después de un parche de seguridad."""

import pytest


@pytest.mark.parametrize("path,expected", [
    ("/invoices/3", "FAC-B-001"),
    ("/invoices/3/pdf", "FAC-B-001"),
    ("/v2/invoices/3", "FAC-B-001"),
    ("/invoices/export?ids=3", "FAC-B-001"),
    ("/invoices/3/total", "535.5"),
])
def test_bob_reaches_his_invoice_by_every_route(client, bob, path, expected):
    r = client.get(path, headers=bob)
    assert r.status_code == 200
    assert expected in r.text


def test_export_keeps_requested_order(client, alice):
    r = client.get("/invoices/export", params={"ids": "2,1"}, headers=alice)
    assert [i["number"] for i in r.json()] == ["FAC-A-002", "FAC-A-001"]


def test_attachment_in_subfolder(client, alice):
    r = client.get("/attachments", params={"name": "notas/reunion.txt"}, headers=alice)
    assert r.status_code == 200
    assert "Notas de la reunión" in r.text


def test_missing_attachment_is_404(client, bob):
    assert client.get("/attachments", params={"name": "no-existe.txt"}, headers=bob).status_code == 404


def test_second_invoice_pdf(client, alice):
    assert "FAC-A-002" in client.get("/invoices/2/pdf", headers=alice).text
