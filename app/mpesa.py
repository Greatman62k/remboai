"""Safaricom Daraja (M-Pesa Express / STK push). Talks to Daraja and nothing else: no database, no WhatsApp."""
from __future__ import annotations

import base64
import logging
import re
import time as clock
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("rembo.mpesa")
NAIROBI = ZoneInfo("Africa/Nairobi")
HOSTS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
TXN_TYPE = {"till": "CustomerBuyGoodsOnline", "paybill": "CustomerPayBillOnline"}

# Result codes worth explaining to a customer in their own words.
CODE_CANCELLED, CODE_INSUFFICIENT, CODE_WRONG_PIN, CODE_TIMEOUT = 1032, 1, 2001, 1037

_MSISDN = re.compile(r"^(?:\+?254|0)?([71]\d{8})$")


def normalize_msisdn(text: str) -> str | None:
    """'0712 345 678', '+254712345678', '712345678' -> '254712345678'. None if the text is not just a Kenyan mobile number."""
    s = re.sub(r"[\s\-().]", "", text or "")
    m = _MSISDN.match(s)
    return "254" + m.group(1) if m else None


class MpesaError(Exception):
    """Daraja refused or could not be reached."""


@dataclass(frozen=True, slots=True)
class Accepted:
    checkout_request_id: str
    merchant_request_id: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """The final answer for one STK prompt, from the callback or from a status query."""
    checkout_request_id: str
    code: int
    desc: str
    receipt: str | None = None
    amount: int | None = None
    phone: str | None = None

    @property
    def ok(self) -> bool:
        return self.code == 0


def parse_callback(payload: dict) -> Outcome | None:
    """Body.stkCallback -> Outcome. None if the body is not an STK callback."""
    try:
        cb = payload["Body"]["stkCallback"]
        code = int(cb["ResultCode"])
        cid = str(cb["CheckoutRequestID"])
    except (KeyError, TypeError, ValueError):
        return None
    items = {}
    for it in (cb.get("CallbackMetadata") or {}).get("Item", []) or []:
        if isinstance(it, dict) and "Name" in it:
            items[it["Name"]] = it.get("Value")
    try:
        amount = int(float(items["Amount"])) if items.get("Amount") is not None else None
    except (TypeError, ValueError):
        amount = None
    phone = str(items["PhoneNumber"]) if items.get("PhoneNumber") is not None else None
    receipt = str(items["MpesaReceiptNumber"]) if items.get("MpesaReceiptNumber") else None
    return Outcome(cid, code, str(cb.get("ResultDesc", "")), receipt, amount, phone)


class Daraja:
    def __init__(self, http: httpx.AsyncClient, settings):
        self.http, self.s = http, settings
        self._token: tuple[str, float] | None = None

    @property
    def configured(self) -> bool:
        s = self.s
        return all((s.mpesa_consumer_key, s.mpesa_consumer_secret, s.mpesa_passkey, s.public_base_url, s.mpesa_callback_secret))

    @property
    def base(self) -> str:
        return HOSTS.get(self.s.mpesa_env, HOSTS["sandbox"])

    @property
    def callback_url(self) -> str:
        return f"{self.s.public_base_url.rstrip('/')}/mpesa/callback/{self.s.mpesa_callback_secret}"

    async def _auth(self, force: bool = False) -> str:
        if self._token and not force and self._token[1] > clock.monotonic():
            return self._token[0]
        try:
            r = await self.http.get(f"{self.base}/oauth/v1/generate", params={"grant_type": "client_credentials"},
                                    auth=(self.s.mpesa_consumer_key, self.s.mpesa_consumer_secret))
            r.raise_for_status()
            body = r.json()
            self._token = (body["access_token"], clock.monotonic() + max(60, int(body.get("expires_in", 3599)) - 60))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise MpesaError(f"could not get a Daraja token: {e}") from e
        return self._token[0]

    async def _post(self, path: str, body: dict) -> httpx.Response:
        for attempt in (0, 1):
            token = await self._auth(force=attempt == 1)
            try:
                r = await self.http.post(f"{self.base}{path}", json=body, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                raise MpesaError(f"Daraja unreachable: {e}") from e
            if r.status_code != 401:
                return r
        return r

    def _credentials(self, shortcode: str) -> tuple[str, str]:
        ts = datetime.now(NAIROBI).strftime("%Y%m%d%H%M%S")
        return base64.b64encode(f"{shortcode}{self.s.mpesa_passkey}{ts}".encode()).decode(), ts

    async def stk_push(self, acct: dict, *, amount: int, phone: str, reference: str, description: str) -> Accepted:
        """Ask the customer's phone to approve a payment. `acct` is a salon_payment row."""
        password, ts = self._credentials(acct["shortcode"])
        r = await self._post("/mpesa/stkpush/v1/processrequest", {
            "BusinessShortCode": acct["shortcode"], "Password": password, "Timestamp": ts,
            "TransactionType": TXN_TYPE[acct["kind"]], "Amount": amount, "PartyA": phone, "PartyB": acct["party_b"],
            "PhoneNumber": phone, "CallBackURL": self.callback_url,
            "AccountReference": reference[:12], "TransactionDesc": description[:13]})
        try:
            body = r.json()
        except ValueError:
            raise MpesaError(f"Daraja answered {r.status_code} with no JSON")
        if r.status_code != 200 or str(body.get("ResponseCode")) != "0":
            raise MpesaError(f"Daraja refused the request: {body.get('errorCode') or body.get('ResponseCode')} "
                             f"{body.get('errorMessage') or body.get('ResponseDescription')}")
        return Accepted(body["CheckoutRequestID"], body.get("MerchantRequestID", ""))

    async def stk_query(self, acct: dict, checkout_request_id: str) -> Outcome | None:
        """Ask Daraja how a prompt ended. None while it is still being processed (or Daraja is unwell): try again later.
        A status query carries no receipt number; the callback is the only place that has it."""
        password, ts = self._credentials(acct["shortcode"])
        try:
            r = await self._post("/mpesa/stkpushquery/v1/query", {
                "BusinessShortCode": acct["shortcode"], "Password": password, "Timestamp": ts, "CheckoutRequestID": checkout_request_id})
            body = r.json()
        except (MpesaError, ValueError):
            return None
        if r.status_code != 200 or "ResultCode" not in body:
            return None
        try:
            return Outcome(checkout_request_id, int(body["ResultCode"]), str(body.get("ResultDesc", "")))
        except (TypeError, ValueError):
            return None
