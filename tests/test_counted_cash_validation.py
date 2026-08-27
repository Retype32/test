"""
A transaction's stated total must match the cash it says it counted.

total_value, each denomination line's value, and the counts all arrive as
independent client-supplied numbers. The web wizard recomputes the total
server-side before it reaches the service, but the JSON API did not -- so
a direct caller could book any cash total it liked against a bag while
listing denominations that summed to something completely different.

Real bug this covers: POSTing 2 x EUR50 with total_value=999999 was
accepted and stored as EUR999999.
"""
import uuid
from decimal import Decimal

CATALOG = "complete"


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Catalog": CATALOG}


def _bag() -> str:
    return f"VAL-{uuid.uuid4().hex[:8]}"


async def _post(api_client, tokens, **overrides):
    body = {
        "customer_id": "C001", "location_id": "L001", "bag_number": _bag(),
        "total_value": "100.00", "expected_total": "100.00",
        "denominations": [{"denomination": "€50", "count": 2, "value": "100.00"}],
    }
    body.update(overrides)
    return await api_client.post("/api/v1/transactions/", json=body,
                                 headers=_headers(tokens["cashier1"]))


async def test_a_consistent_transaction_is_accepted(api_client, tokens):
    r = await _post(api_client, tokens)
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["total_value"])) == Decimal("100.00")


async def test_total_contradicting_the_denominations_is_rejected(api_client, tokens):
    r = await _post(api_client, tokens, total_value="999999.00",
                    expected_total="999999.00")
    assert r.status_code == 400, (
        "a total that disagrees with the counted notes must be refused, not stored")
    assert "add up to" in r.text


async def test_a_denomination_line_that_does_not_multiply_out_is_rejected(api_client, tokens):
    r = await _post(api_client, tokens, denominations=[
        {"denomination": "€50", "count": 2, "value": "77777.00"}])
    assert r.status_code == 400
    assert "claims" in r.text


async def test_an_unknown_denomination_is_rejected(api_client, tokens):
    r = await _post(api_client, tokens, denominations=[
        {"denomination": "€1000000", "count": 1, "value": "100.00"}])
    assert r.status_code == 400
    assert "Unknown denomination" in r.text


async def test_coins_are_valued_at_one_euro_each(api_client, tokens):
    """Coins is a bulk euro amount, not a piece count (face value 1)."""
    r = await _post(api_client, tokens, total_value="107.00", expected_total="107.00",
                    denominations=[
                        {"denomination": "€50", "count": 2, "value": "100.00"},
                        {"denomination": "Coins", "count": 7, "value": "7.00"}])
    assert r.status_code == 201, r.text
    assert Decimal(str(r.json()["total_value"])) == Decimal("107.00")


async def test_a_correction_is_held_to_the_same_standard(api_client, web_client, tokens):
    """The one workflow meant to fix a wrong figure must not introduce one."""
    created = await _post(api_client, tokens)
    assert created.status_code == 201
    txn_id = created.json()["transaction_id"]

    await web_client.post("/web/login", data={"username": "admin", "password": "admin"})
    await web_client.post("/web/catalog/select", data={"code": CATALOG})

    form = {"reason": "recount", "expected_total": "100.00",
            "customer_id": "C001", "location_id": "L001",
            "bag_number": created.json()["bag_number"], "wallet_id": ""}
    for label in ("€500", "€200", "€100", "€50", "€20", "€10", "€5", "Coins"):
        form[f"count_{label}"] = "0"
    form["count_€50"] = "3"

    resp = await web_client.post(f"/web/transactions/{txn_id}/correct",
                                 data=form, follow_redirects=True)
    assert resp.status_code == 200
    # The correction recomputes its own total from the counts, so it stays
    # self-consistent by construction — assert it actually landed at 3 x 50.
    detail = await api_client.get(f"/api/v1/transactions/", headers=_headers(tokens["admin"]))
    corrected = [t for t in detail.json()
                 if t["bag_number"] == created.json()["bag_number"]]
    assert len(corrected) == 1
    assert Decimal(str(corrected[0]["total_value"])) == Decimal("150.00")
