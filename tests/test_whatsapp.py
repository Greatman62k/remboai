import hashlib
import hmac
from types import SimpleNamespace

from app.channels import whatsapp as wa
from app.channels.outbound import trace_thread


def sig(body: bytes, secret: str = "s3cret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_accepts_only_the_exact_bytes_and_secret():
    body = b'{"a": 1}'
    assert wa.verify_signature(body, sig(body), "s3cret")
    assert not wa.verify_signature(body + b" ", sig(body), "s3cret")
    assert not wa.verify_signature(body, sig(body, "other"), "s3cret")
    assert not wa.verify_signature(body, None, "s3cret")
    assert not wa.verify_signature(body, "sha1=abc", "s3cret")


def payload(*messages, **value):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "PID"}, "contacts": [{"wa_id": "254700000001", "profile": {"name": "Wanjiku"}}],
                                              "messages": list(messages), **value}}]}]}


def test_parses_text_and_button_replies_and_skips_malformed():
    p = payload(
        {"from": "254700000001", "id": "m1", "type": "text", "text": {"body": " nataka fade "}},
        {"from": "254700000001", "id": "m2", "type": "interactive", "interactive": {"button_reply": {"id": "slot:1", "title": "Sat 12:00"}}},
        {"from": "254700000001", "type": "text", "text": {"body": "no id"}},
        {"from": "254700000001", "id": "m3", "type": "audio", "audio": {"id": "X"}},
    )
    got = wa.parse_messages(p)
    assert [(m.id, m.kind, m.text, m.reply_id) for m in got] == [
        ("m1", "text", "nataka fade", None), ("m2", "interactive", "Sat 12:00", "slot:1"), ("m3", "audio", "", None)]
    assert got[0].name == "Wanjiku" and got[0].phone_number_id == "PID"
    assert wa.parse_messages({}) == [] and wa.parse_messages({"entry": [{"changes": [{}]}]}) == []


def test_failed_delivery_reports_are_found_and_explained():
    p = {"entry": [{"changes": [{"value": {"statuses": [
        {"status": "delivered", "recipient_id": "1"},
        {"status": "failed", "recipient_id": "254700000001", "errors": [{"code": 131030, "title": "not allowed"}]}]}}]}]}
    f = wa.parse_failures(p)
    assert len(f) == 1 and f[0].code == 131030 and "allowed list" in wa.explain(f[0].code)
    assert "expired" in wa.explain(190) and wa.explain(None) and wa.explain("x")


def test_numbers_are_masked_for_logs():
    assert wa.mask("254712345678") == "...5678" and wa.mask("") == ""


def test_trace_thread_is_stable_opaque_and_does_not_collide_on_sender_suffix():
    ctx = SimpleNamespace(settings=SimpleNamespace(wa_app_secret="trace-test-secret"))
    first = trace_thread(ctx, "254712340001")
    second = trace_thread(ctx, "254799990001")
    assert first == trace_thread(ctx, "254712340001")
    assert first != second
    assert "254712340001" not in first and first.startswith("whatsapp:")
    assert trace_thread(ctx, "tg:1001").startswith("telegram:")
