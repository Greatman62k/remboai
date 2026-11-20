import httpx
import pytest

from app.channels.telegram import Telegram
from app.channels.whatsapp import WhatsApp, parse_receipts


@pytest.mark.asyncio
async def test_whatsapp_structured_result_keeps_provider_id_and_does_not_claim_delivery():
    async def handler(request):
        return httpx.Response(200, json={"messages": [{"id": "wamid.accepted", "message_status": "accepted"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WhatsApp(http, "v99").send_text_result("PID", "test-token", "254700000001", "Approved")
    assert result.accepted is True
    assert result.provider_message_id == "wamid.accepted"
    assert result.ambiguous is False and result.retryable is False


@pytest.mark.asyncio
async def test_whatsapp_http_success_without_message_identity_is_ambiguous():
    async def handler(request):
        return httpx.Response(200, json={"messages": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WhatsApp(http, "v99").send_text_result("PID", "test-token", "254700000001", "Approved")
    assert result.accepted is False and result.ambiguous is True
    assert result.provider_message_id is None and result.retryable is False


@pytest.mark.asyncio
async def test_whatsapp_explicit_rate_limit_is_known_retryable_rejection():
    async def handler(request):
        return httpx.Response(429, json={"error": {"code": 130429}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await WhatsApp(http, "v99").send_text_result("PID", "test-token", "254700000001", "Approved")
    assert result.accepted is False and result.retryable is True and result.ambiguous is False
    assert result.error_category == "rate_limited"


@pytest.mark.asyncio
async def test_telegram_success_uses_message_id_and_read_timeout_stays_ambiguous():
    async def accepted(request):
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 731}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(accepted)) as http:
        result = await Telegram(http, "123:test-token").send_text_result("42", "Approved")
    assert result.accepted is True and result.provider_message_id == "731"

    async def timed_out(request):
        raise httpx.ReadTimeout("response timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timed_out)) as http:
        result = await Telegram(http, "123:test-token").send_text_result("42", "Approved")
    assert result.accepted is False and result.ambiguous is True and result.retryable is False


def test_whatsapp_receipt_parser_preserves_message_identity_number_and_failure_code():
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "PID"},
        "statuses": [
            {"id": "wamid-1", "status": "delivered", "timestamp": "1790000000"},
            {"id": "wamid-2", "status": "failed", "timestamp": "1790000001",
             "errors": [{"code": 131030, "title": "recipient not allowed"}]},
            {"status": "unknown", "recipient_id": "2547"},
        ],
    }}]}]}
    got = parse_receipts(payload)
    assert [(r.provider_message_id, r.phone_number_id, r.status, r.error_code) for r in got] == [
        ("wamid-1", "PID", "delivered", None), ("wamid-2", "PID", "failed", "131030")]
