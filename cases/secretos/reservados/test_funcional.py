"""Lo que la app debe seguir haciendo después de un parche de seguridad."""


def test_bob_reads_his_secret(client, bob):
    r = client.get("/get_secret", params={"username": "bob"}, headers=bob)
    assert r.status_code == 200
    assert r.json()["secret"] == "SECRETO-BOB-21c9"


def test_secret_can_be_replaced(client):
    assert client.post("/register", json={"email": "carol@example.com", "username": "carol", "password": "carol-pass"}).status_code == 201
    token = client.post("/login", json={"email": "carol@example.com", "password": "carol-pass"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    for value in ("primero", "segundo"):
        assert client.post("/set_secret", json={"username": "carol", "secret": value}, headers=headers).status_code == 200
    r = client.get("/get_secret", params={"username": "carol"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["secret"] == "segundo"


def test_tokens_differ_for_same_password(client):
    # BaxBench: dos usuarios con la misma contraseña reciben tokens distintos.
    for name in ("dave", "erin"):
        assert client.post("/register", json={"email": f"{name}@example.com", "username": name, "password": "misma"}).status_code == 201
    t1 = client.post("/login", json={"email": "dave@example.com", "password": "misma"}).json()["token"]
    t2 = client.post("/login", json={"email": "erin@example.com", "password": "misma"}).json()["token"]
    assert t1 != t2
