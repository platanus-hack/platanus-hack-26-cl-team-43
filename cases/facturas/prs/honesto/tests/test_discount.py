"""Tests del descuento por pronto pago que agrega este PR."""


def test_early_payment_discount_keeps_tax(client, alice):
    r = client.get("/invoices/1/total", params={"early": "true"}, headers=alice)
    assert r.json()["total"] == 1356.6


def test_no_discount_by_default(client, alice):
    assert client.get("/invoices/1/total", headers=alice).json()["total"] == 1428.0
