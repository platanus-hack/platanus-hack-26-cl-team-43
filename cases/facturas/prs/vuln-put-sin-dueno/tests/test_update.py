def test_update_own_invoice(client, alice):
    r = client.put("/invoices/2", json={"number": "FAC-A-002", "amount": 800}, headers=alice)
    assert r.status_code == 200 and r.json()["amount"] == 800
