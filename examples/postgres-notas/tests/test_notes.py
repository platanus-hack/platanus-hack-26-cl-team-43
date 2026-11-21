def test_own_note(client, ana):
    assert client.get("/notes/1", headers=ana).json()["body"].startswith("Nota privada de Ana: ")


def test_other_users_note_is_hidden(client, beto):
    assert client.get("/notes/1", headers=beto).status_code == 404


def test_list_only_own(client, beto):
    assert [n["id"] for n in client.get("/notes", headers=beto).json()] == [2]


def test_requires_login(client):
    assert client.get("/notes").status_code == 401
