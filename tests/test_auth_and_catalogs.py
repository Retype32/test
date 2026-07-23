"""Auth (login, user management, self-lockout guard) and catalog listing."""


def _headers(token: str, catalog: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if catalog:
        headers["X-Catalog"] = catalog
    return headers


async def test_login_success_returns_token_and_user(api_client):
    r = await api_client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["username"] == "admin"
    assert body["user"]["role"] == "administrator"


async def test_login_wrong_password_rejected(api_client):
    r = await api_client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


async def test_login_unknown_user_rejected(api_client):
    r = await api_client.post("/api/v1/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401


async def test_request_without_bearer_token_rejected(api_client):
    r = await api_client.get("/api/v1/catalogs/", headers={"X-Catalog": "vms"})
    assert r.status_code in (401, 403)  # HTTPBearer returns 403 when the header is simply absent


async def test_list_catalogs_returns_all_four(api_client, tokens):
    r = await api_client.get("/api/v1/catalogs/", headers=_headers(tokens["admin"]))
    assert r.status_code == 200
    codes = {c["code"] for c in r.json()}
    assert codes == {"vms", "dayshift", "complete", "esnf"}


async def test_missing_catalog_header_rejected_on_catalog_scoped_route(api_client, tokens):
    r = await api_client.get("/api/v1/customers/", headers=_headers(tokens["admin"]))
    assert r.status_code == 400


async def test_unknown_catalog_header_rejected(api_client, tokens):
    r = await api_client.get("/api/v1/customers/", headers=_headers(tokens["admin"], "not-a-real-catalog"))
    assert r.status_code == 400


async def test_customers_seeded_in_every_catalog(api_client, tokens):
    for catalog in ("vms", "dayshift", "complete", "esnf"):
        r = await api_client.get("/api/v1/customers/", headers=_headers(tokens["admin"], catalog))
        assert r.status_code == 200
        ids = {c["customer_id"] for c in r.json()}
        assert "C001" in ids, f"catalog {catalog} missing seeded customer C001"


async def test_non_admin_cannot_create_user(api_client, tokens):
    r = await api_client.post(
        "/api/v1/auth/users",
        json={"username": "sneaky", "password": "sneaky123", "role": "cashier"},
        headers=_headers(tokens["supervisor1"]),
    )
    assert r.status_code == 403


async def test_admin_can_create_and_list_users(api_client, tokens):
    r = await api_client.post(
        "/api/v1/auth/users",
        json={"username": "testuser_auth", "password": "testpass123", "role": "cashier"},
        headers=_headers(tokens["admin"]),
    )
    assert r.status_code == 201
    assert r.json()["username"] == "testuser_auth"

    r2 = await api_client.get("/api/v1/auth/users", headers=_headers(tokens["admin"]))
    assert r2.status_code == 200
    assert any(u["username"] == "testuser_auth" for u in r2.json())


async def test_admin_cannot_deactivate_own_account(api_client, tokens):
    r = await api_client.get("/api/v1/auth/users", headers=_headers(tokens["admin"]))
    admin_id = next(u["id"] for u in r.json() if u["username"] == "admin")

    r2 = await api_client.patch(
        f"/api/v1/auth/users/{admin_id}/active",
        json={"is_active": False},
        headers=_headers(tokens["admin"]),
    )
    assert r2.status_code == 400
    assert "own account" in r2.json()["detail"]


async def test_admin_can_deactivate_another_user(api_client, tokens):
    create = await api_client.post(
        "/api/v1/auth/users",
        json={"username": "testuser_deactivate", "password": "testpass123", "role": "cashier"},
        headers=_headers(tokens["admin"]),
    )
    user_id = create.json()["id"]

    r = await api_client.patch(
        f"/api/v1/auth/users/{user_id}/active",
        json={"is_active": False},
        headers=_headers(tokens["admin"]),
    )
    assert r.status_code == 200
    assert r.json()["is_active"] is False
