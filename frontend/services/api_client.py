import httpx
from typing import Optional, Any
from dataclasses import dataclass


BASE_URL = "http://127.0.0.1:8000/api/v1"


@dataclass
class ApiResponse:
    ok: bool
    data: Any = None
    error: str = ""
    status_code: int = 0


class ApiClient:
    def __init__(self):
        self._token: Optional[str] = None
        self._catalog: Optional[str] = None
        self._timeout = httpx.Timeout(10.0)

    @property
    def token(self) -> Optional[str]:
        return self._token

    def set_token(self, token: str):
        self._token = token

    def clear_token(self):
        self._token = None

    @property
    def catalog(self) -> Optional[str]:
        return self._catalog

    def set_catalog(self, code: str):
        self._catalog = code

    def clear_catalog(self):
        self._catalog = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if self._catalog:
            h["X-Catalog"] = self._catalog
        return h

    def _request(self, method: str, path: str, **kwargs) -> ApiResponse:
        url = f"{BASE_URL}{path}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.request(method, url, headers=self._headers(), **kwargs)
            if resp.status_code < 400:
                return ApiResponse(ok=True, data=resp.json(), status_code=resp.status_code)
            else:
                try:
                    detail = resp.json().get("detail", resp.text)
                except Exception:
                    detail = resp.text
                return ApiResponse(ok=False, error=str(detail), status_code=resp.status_code)
        except httpx.ConnectError:
            return ApiResponse(ok=False, error="Cannot connect to server. Ensure the backend is running.", status_code=0)
        except httpx.TimeoutException:
            return ApiResponse(ok=False, error="Request timed out.", status_code=0)
        except Exception as e:
            return ApiResponse(ok=False, error=str(e), status_code=0)

    def login(self, username: str, password: str) -> ApiResponse:
        return self._request("POST", "/auth/login", json={"username": username, "password": password})

    def list_catalogs(self) -> ApiResponse:
        return self._request("GET", "/catalogs/")

    def validate_customer(self, customer_id: str) -> ApiResponse:
        return self._request("GET", f"/customers/{customer_id}")

    def create_transaction(self, payload: dict) -> ApiResponse:
        return self._request("POST", "/transactions/", json=payload)

    def list_transactions(self, params: dict = None) -> ApiResponse:
        return self._request("GET", "/transactions/", params=params)

    def get_transaction(self, transaction_id: str) -> ApiResponse:
        return self._request("GET", f"/transactions/{transaction_id}")

    def list_users(self) -> ApiResponse:
        return self._request("GET", "/auth/users")

    def create_user(self, payload: dict) -> ApiResponse:
        return self._request("POST", "/auth/users", json=payload)

    def set_user_active(self, user_id: str, is_active: bool) -> ApiResponse:
        return self._request("PATCH", f"/auth/users/{user_id}/active", json={"is_active": is_active})

    def transfer_transaction(self, transaction_id: str, new_business_date: str, reason: str) -> ApiResponse:
        return self._request(
            "POST", f"/transactions/{transaction_id}/transfer",
            json={"new_business_date": new_business_date, "reason": reason},
        )

    def close_eod(self, business_date: str) -> ApiResponse:
        return self._request("POST", "/eod/close", json={"business_date": business_date})

    def reopen_eod(self, business_date: str, reason: str) -> ApiResponse:
        return self._request("POST", "/eod/reopen", json={"business_date": business_date, "reason": reason})

    def eod_status(self, business_date: str) -> ApiResponse:
        return self._request("GET", "/eod/status", params={"business_date": business_date})

    def list_eod_closures(self, date_from: str = None, date_to: str = None) -> ApiResponse:
        params = {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._request("GET", "/eod/", params=params)

    def list_notifications(self, status: str = None, event_type: str = None) -> ApiResponse:
        params = {}
        if status:
            params["status"] = status
        if event_type:
            params["event_type"] = event_type
        return self._request("GET", "/notifications/", params=params)

    def unread_notification_count(self) -> ApiResponse:
        return self._request("GET", "/notifications/unread-count")

    def get_notification(self, notification_id: str) -> ApiResponse:
        return self._request("GET", f"/notifications/{notification_id}")

    def resolve_notification(self, notification_id: str, status: str, notes: str = None) -> ApiResponse:
        return self._request(
            "POST", f"/notifications/{notification_id}/resolve",
            json={"status": status, "notes": notes},
        )

    def list_duplicates(self, status: str = None) -> ApiResponse:
        params = {"status": status} if status else None
        return self._request("GET", "/duplicates/", params=params)

    def get_duplicate(self, flag_id: str) -> ApiResponse:
        return self._request("GET", f"/duplicates/{flag_id}")

    def review_duplicate(self, flag_id: str, status: str, notes: str = None) -> ApiResponse:
        return self._request(
            "POST", f"/duplicates/{flag_id}/review",
            json={"status": status, "notes": notes},
        )

    def processor_stats_for_day(self, business_date: str) -> ApiResponse:
        return self._request("GET", "/stats/processors", params={"business_date": business_date})

    def processor_stats_summary(self, date_from: str, date_to: str) -> ApiResponse:
        return self._request(
            "GET", "/stats/processors/summary",
            params={"date_from": date_from, "date_to": date_to},
        )


api = ApiClient()
