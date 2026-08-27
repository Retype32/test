"""Admin-only CRUD for customers/locations (create, rename IDs, edit names,
delete-with-dependents guard) at the API layer."""


def _headers(token: str, catalog: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if catalog:
        headers["X-Catalog"] = catalog
    return headers


async def test_non_admin_cannot_create_customer(api_client, tokens):
    r = await api_client.post(
        "/api/v1/customers/",
        json={"customer_id": "SNEAKY1", "customer_name": "Sneaky Co"},
        headers=_headers(tokens["supervisor1"], "vms"),
    )
    assert r.status_code == 403


async def test_admin_can_create_customer(api_client, tokens):
    r = await api_client.post(
        "/api/v1/customers/",
        json={"customer_id": "APITEST1", "customer_name": "API Test Customer"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert r.status_code == 201
    assert r.json()["customer_id"] == "APITEST1"

    listed = await api_client.get("/api/v1/customers/", headers=_headers(tokens["admin"], "vms"))
    assert any(c["customer_id"] == "APITEST1" for c in listed.json())


async def test_create_customer_duplicate_id_rejected(api_client, tokens):
    r = await api_client.post(
        "/api/v1/customers/",
        json={"customer_id": "C001", "customer_name": "Duplicate"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert r.status_code == 400


async def test_admin_can_rename_customer_id_and_keep_locations(api_client, tokens):
    create = await api_client.post(
        "/api/v1/customers/",
        json={"customer_id": "RENAMEME1", "customer_name": "Rename Target"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert create.status_code == 201

    loc = await api_client.post(
        "/api/v1/customers/RENAMEME1/locations",
        json={"location_id": "RENAMEME1-L1", "location_name": "Site One"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert loc.status_code == 201

    renamed = await api_client.patch(
        "/api/v1/customers/RENAMEME1",
        json={"customer_id": "RENAMED1", "customer_name": "Rename Target"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert renamed.status_code == 200
    assert renamed.json()["customer_id"] == "RENAMED1"

    old_gone = await api_client.get(
        "/api/v1/customers/RENAMEME1", headers=_headers(tokens["admin"], "vms")
    )
    assert old_gone.status_code == 404

    new_customer = await api_client.get(
        "/api/v1/customers/RENAMED1", headers=_headers(tokens["admin"], "vms")
    )
    assert new_customer.status_code == 200
    assert any(l["location_id"] == "RENAMEME1-L1" for l in new_customer.json()["locations"])


async def test_delete_customer_with_transactions_blocked(api_client, tokens):
    r = await api_client.delete(
        "/api/v1/customers/C001", headers=_headers(tokens["admin"], "vms")
    )
    assert r.status_code == 400


async def test_admin_can_create_rename_and_delete_location(api_client, tokens):
    create_customer = await api_client.post(
        "/api/v1/customers/",
        json={"customer_id": "LOCTEST1", "customer_name": "Location Test Customer"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert create_customer.status_code == 201

    create_loc = await api_client.post(
        "/api/v1/customers/LOCTEST1/locations",
        json={"location_id": "LOCTEST1-L1", "location_name": "Original Name"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert create_loc.status_code == 201

    rename_loc = await api_client.patch(
        "/api/v1/customers/LOCTEST1/locations/LOCTEST1-L1",
        json={"location_id": "LOCTEST1-L2", "location_name": "New Name"},
        headers=_headers(tokens["admin"], "vms"),
    )
    assert rename_loc.status_code == 200
    assert rename_loc.json()["location_id"] == "LOCTEST1-L2"
    assert rename_loc.json()["location_name"] == "New Name"

    delete_loc = await api_client.delete(
        "/api/v1/customers/LOCTEST1/locations/LOCTEST1-L2", headers=_headers(tokens["admin"], "vms")
    )
    assert delete_loc.status_code == 204

    missing = await api_client.get(
        "/api/v1/customers/LOCTEST1/locations/LOCTEST1-L2", headers=_headers(tokens["admin"], "vms")
    )
    assert missing.status_code == 404
