"""Thin timing/recording wrapper around httpx.AsyncClient.

Per the plan's tool choice (section 8): httpx against a REAL socket
(`base_url=...`, no `transport=ASGITransport(...)`) -- this module never
imports the FastAPI app, only talks to it over HTTP, exactly like a real
browser/API client would.
"""
from __future__ import annotations

import inspect
import re
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

from .metrics import (
    ClassifiedOutcome,
    MetricsCollector,
    Outcome,
    RequestRecord,
    RootCause,
    classify_http,
    normalize_path,
)

_TXN_ID_HTML_RE = re.compile(r"TRANSACTION ID.{0,80}?([0-9a-fA-F-]{36})", re.S)


def extract_txn_id_from_html(text: str) -> Optional[str]:
    m = _TXN_ID_HTML_RE.search(text or "")
    return m.group(1) if m else None


def extract_txn_id_from_json(resp: httpx.Response) -> Optional[str]:
    try:
        data = resp.json()
    except Exception:
        return None
    return data.get("transaction_id") if isinstance(data, dict) else None


class TimedClient:
    """Wraps one httpx.AsyncClient (one virtual user's session -- cookie
    jar for web journeys, or a bearer token for API journeys) and records
    every call into a shared MetricsCollector."""

    def __init__(self, client: httpx.AsyncClient, collector: MetricsCollector, vu_id: str = ""):
        self.client = client
        self.collector = collector
        self.vu_id = vu_id

    async def call(
        self,
        method: str,
        path: str,
        *,
        success_statuses=(200, 201),
        root_cause_hint=None,
        claims_transaction: bool = False,
        transaction_id_extractor: Optional[Callable[[httpx.Response], Optional[str]]] = None,
        success_check: Optional[Callable[[httpx.Response], object]] = None,
        **kwargs,
    ) -> Optional[httpx.Response]:
        """Issue one HTTP call, record it, and return the response (or None
        if the transport itself failed -- the failure is still recorded).

        success_check: for the handful of routes in this app whose web POST
        handler swallows its ValueError and always redirects 303 either way
        (EOD close/reopen, duplicate review, notification resolve -- see
        journeys.py), the HTTP status code alone cannot tell "the business
        action happened" from "it was rejected but the route redirected
        anyway". When given, success_check(resp) (sync or async, returning
        truthy/falsy) is the sole arbiter of business_success vs.
        valid_rejection for any non-5xx response, overriding
        success_statuses entirely -- this is exactly the "independently
        confirmed to have produced the expected DB effect" requirement in
        plan section 6, implemented via a side-channel API read rather than
        a direct DB query (the harness never opens the app's DB while the
        server itself might be writing to it, except in the dedicated
        post-run integrity pass in integrity.py)."""
        start = time.time()
        normalized = normalize_path(path)
        try:
            resp = await self.client.request(method, path, **kwargs)
        except Exception as exc:  # httpx.TransportError and friends
            latency_ms = (time.time() - start) * 1000
            classified = classify_http(None, success_statuses=success_statuses, exc=exc)
            self.collector.append(RequestRecord(
                method=method, path=normalized, status_code=None, latency_ms=latency_ms,
                started_at=start, outcome=classified.bucket, root_cause=classified.root_cause,
                sample_detail=f"{type(exc).__name__}: {exc}"[:300],
            ))
            return None

        latency_ms = (time.time() - start) * 1000
        body_text = ""
        if resp.status_code >= 500:
            try:
                body_text = resp.text[:2000]
            except Exception:
                body_text = ""

        if success_check is not None and resp.status_code < 500:
            try:
                result = success_check(resp)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                result = False
            classified = (
                ClassifiedOutcome(Outcome.BUSINESS_SUCCESS, RootCause.NONE)
                if result
                else ClassifiedOutcome(Outcome.VALID_REJECTION, root_cause_hint or RootCause.OTHER)
            )
        else:
            classified = classify_http(
                resp.status_code, success_statuses=success_statuses,
                body_text=body_text, root_cause_hint=root_cause_hint,
            )
        sample_detail = ""
        if classified.bucket.value != "business_success":
            sample_detail = (body_text or "")[:300]

        self.collector.append(RequestRecord(
            method=method, path=normalized, status_code=resp.status_code, latency_ms=latency_ms,
            started_at=start, outcome=classified.bucket, root_cause=classified.root_cause,
            sample_detail=sample_detail,
        ))

        if claims_transaction and classified.bucket.value == "business_success":
            extractor = transaction_id_extractor or extract_txn_id_from_json
            txn_id = extractor(resp)
            if txn_id:
                self.collector.claimed_transaction_ids.append(txn_id)

        return resp


def new_web_client(base_url: str, timeout: float = 15.0) -> httpx.AsyncClient:
    """One cookie-jar session per virtual user, matching how a real
    browser holds one session across the wizard's steps."""
    return httpx.AsyncClient(base_url=base_url, follow_redirects=False, timeout=timeout)


def new_api_client(base_url: str, timeout: float = 15.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, follow_redirects=False, timeout=timeout)


def path_from_url(url: str) -> str:
    return urlparse(url).path
