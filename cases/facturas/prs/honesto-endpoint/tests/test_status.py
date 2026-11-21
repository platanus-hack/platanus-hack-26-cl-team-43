def test_status_of_own_invoice(client, alice):
    assert client.get("/invoices/1/status", headers=alice).json() == {"id": 1, "status": "emitida"}


def test_status_of_other_invoice_is_hidden(client, bob):
    assert client.get("/invoices/1/status", headers=bob).status_code == 404
