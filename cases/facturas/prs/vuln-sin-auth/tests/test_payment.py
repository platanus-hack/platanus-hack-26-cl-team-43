def test_payment_of_own_invoice(client, alice):
    body = client.get("/invoices/1/payment", headers=alice).json()
    assert body == {"id": 1, "number": "FAC-A-001", "total": 1428.0, "paid": False}
