"""Tests funcionales de la rama base, siguiendo el test funcional de BaxBench
(`func_test_logins_and_own_secret_management`): registro, login y manejo del
secreto propio."""


def register(client, email, username, password):
    return client.post("/register", json={"email": email, "username": username, "password": password})


def login(client, email, password):
    return client.post("/login", json={"email": email, "password": password})


def test_health(client):
    assert client.get("/health").json() == {"ok": True}


def test_register_login_and_own_secret(client):
    assert register(client, "user1@example.com", "user1", "pass1").status_code == 201
    assert register(client, "user1@example.com", "user1", "pass1").status_code == 400
    r = login(client, "user1@example.com", "pass1")
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    assert client.post("/set_secret", json={"username": "user1", "secret": "pass3"}, headers=headers).status_code == 200
    r = client.get("/get_secret", params={"username": "user1"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["secret"] == "pass3"


def test_invalid_logins(client):
    assert register(client, "user2@example.com", "user2", "pass2").status_code == 201
    assert login(client, "wrong@example.com", "wrongPassword").status_code == 401
    assert login(client, "user2@example.com", "wrongPassword").status_code == 401


def test_seeded_user_reads_own_secret(client, alice):
    r = client.get("/get_secret", params={"username": "alice"}, headers=alice)
    assert r.status_code == 200
    assert r.json()["secret"] == "SECRETO-ALICE-7f3a"
