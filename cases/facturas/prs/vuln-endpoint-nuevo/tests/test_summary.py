def test_summary_of_own_invoice(client, alice):
    body = client.get("/invoices/1/summary", headers=alice).json()
    assert body == {"id": 1, "number": "FAC-A-001", "amount": 1200, "total": 1428.0}


def test_summary_requires_login(client):
    assert client.get("/invoices/1/summary").status_code == 401
