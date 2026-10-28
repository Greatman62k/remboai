"""Set-up checks: everything that has to be true for a real customer message to become a confirmed, paid booking.
Each check says what it found and, when it fails, what to do about it. Run with `python scripts/doctor.py`."""
from __future__ import annotations

import time as clock
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .llm import Extractor, _coerce, _json_from, sanitize
from .vision import StyleMatcher, make_png
from .mpesa import Daraja, MpesaError
from .whatsapp import explain


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False   # a problem that does not stop a demo


async def run_checks(s, http: httpx.AsyncClient, pool) -> list[Check]:
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, **k))  # noqa: E731

    missing = [n for n, v in (("LLM_API_KEY", s.llm_api_key),) if not v]
    add("configuration", not missing, "required values present" if not missing else "missing in .env: " + ", ".join(missing))

    salons = []
    try:
        salons = [dict(r) for r in await pool.fetch(
            "SELECT s.id::text, s.name, s.wa_phone_number_id, s.tg_bot_id, s.owner_wa_id, s.deposit_kes, sp.kind, sp.shortcode, sp.party_b, "
            "(SELECT count(*) FROM services v WHERE v.salon_id=s.id AND v.active) AS services, "
            "(SELECT count(*) FROM staff t WHERE t.salon_id=s.id AND t.active) AS staff "
            "FROM salons s LEFT JOIN salon_payment sp ON sp.salon_id=s.id WHERE s.active")]
        add("database", True, f"connected, {len(salons)} active salon(s)")
    except Exception as e:  # noqa: BLE001
        add("database", False, f"{e.__class__.__name__}: {e}")
    if not salons:
        add("salon", False, "no salon yet: run scripts/seed_salon.py")

    for sl in salons:
        label = f"salon '{sl['name']}'"
        add(label, sl["services"] > 0 and sl["staff"] > 0, f"{sl['staff']} stylist(s), {sl['services']} service(s)")
        if sl["deposit_kes"] > 0 and not sl["kind"]:
            add(label + " deposits", False, "deposit is set but no till/paybill: bookings will be confirmed with NO deposit (use --till)", warn=True)
        elif sl["kind"]:
            add(label + " deposits", True, f"KES {sl['deposit_kes']} to {sl['kind']} {sl['party_b']} (shortcode {sl['shortcode']})")
        add(label + " owner", bool(sl["owner_wa_id"]), "owner contact set" if sl["owner_wa_id"] else "no --owner: no booking alerts or owner commands", warn=True)
        if not sl["wa_phone_number_id"]:
            continue
        if not s.wa_access_token:
            add(label + " WhatsApp", False, "WA_ACCESS_TOKEN missing in .env")
            continue
        try:
            r = await http.get(f"https://graph.facebook.com/{s.graph_version}/{sl['wa_phone_number_id']}",
                               params={"fields": "display_phone_number,verified_name"}, headers={"Authorization": f"Bearer {s.wa_access_token}"})
            body = r.json()
            if r.status_code == 200:
                add(label + " WhatsApp", True, f"{body.get('display_phone_number')} ({body.get('verified_name')})")
            else:
                code = (body.get("error") or {}).get("code")
                add(label + " WhatsApp", False, f"Meta says {code}: {explain(code)}")
        except (httpx.HTTPError, ValueError) as e:
            add(label + " WhatsApp", False, f"could not reach Meta: {e.__class__.__name__}")

    if s.telegram_bot_token:
        try:
            r = await http.post(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe", timeout=10)
            me = r.json().get("result") or {}
            add("telegram", bool(me), f"bot @{me.get('username')} (id {me.get('id')})" if me else "Telegram refused the token (check TELEGRAM_BOT_TOKEN)")
            linked = any(sl.get("tg_bot_id") == f"tg:{me.get('id')}" for sl in salons) if me else False
            if me:
                add("telegram salon", linked, "a salon is linked to this bot" if linked else f"no salon uses this bot: seed with --telegram-bot-id {me.get('id')}")
        except (httpx.HTTPError, ValueError) as e:
            add("telegram", False, f"could not reach Telegram: {e.__class__.__name__}")
    else:
        add("telegram", False, "TELEGRAM_BOT_TOKEN not set: Telegram channel off", warn=True)

    ext = Extractor(http, s)
    today = datetime.now(ZoneInfo("Africa/Nairobi")).date()
    for base, key, model, _ in ext.endpoints():
        t0 = clock.perf_counter()
        try:
            r = await http.post(base.rstrip("/") + "/chat/completions", json=ext._body(model, "nataka fade kesho mchana", today, ["Fade"], "idle"),
                                headers={"Authorization": f"Bearer {key}"}, timeout=s.llm_timeout_s * 2)
            r.raise_for_status()
            got = sanitize(_coerce(_json_from(r.json()["choices"][0]["message"]["content"])), today)
            ms = int((clock.perf_counter() - t0) * 1000)
            good = got.intent == "book" and got.service == "Fade"
            add(f"model {model}", good, f"{ms} ms; read 'nataka fade kesho mchana' as intent={got.intent} service={got.service} window={got.window}"
                + ("" if good else " (unexpected: check the model name and prompt)"))
        except Exception as e:  # noqa: BLE001
            add(f"model {model}", False, f"{e.__class__.__name__}: {str(e)[:120]}")
    # Photo matching: show the model a plain red square. If it says red, images reach it in the format we send.
    matcher = StyleMatcher(http, s, ext.endpoints)
    if not matcher.enabled:
        add("photo matching", False, "switched off (VISION_ENABLED=false or no LLM_API_KEY)", warn=True)
    for base, key, model, _ in ext.endpoints() if matcher.enabled else []:
        try:
            body = matcher.body(model, 'Reply with ONE JSON object: {"color": <the main colour of the image, one word>}', make_png((220, 20, 20)), "image/png")
            r = await http.post(base.rstrip("/") + "/chat/completions", json=body, headers={"Authorization": f"Bearer {key}"}, timeout=s.llm_timeout_s * 2)
            r.raise_for_status()
            answer = str(_json_from(r.json()["choices"][0]["message"]["content"]).get("color", ""))
            add(f"photo matching {model}", "red" in answer.lower(), f"shown a red square, it said '{answer}'" + ("" if "red" in answer.lower() else " (image input may need a different format)"),
                warn=True)
        except Exception as e:  # noqa: BLE001
            add(f"photo matching {model}", False, f"{e.__class__.__name__}: {str(e)[:120]}", warn=True)
    if not s.llm_fallback_base_url:
        add("model failover", False, "no LLM_FALLBACK_* set: if Meta's API is down the keyword rules answer alone", warn=True)

    if s.asr_url:
        try:
            r = await http.get(s.asr_url.rsplit("/", 1)[0] + "/health", timeout=s.asr_timeout_s)
            add("voice notes", r.status_code == 200, f"speech service answered: {r.json().get('model') if r.status_code == 200 else r.status_code}")
        except (httpx.HTTPError, ValueError) as e:
            add("voice notes", False, f"speech service not reachable ({e.__class__.__name__}); a cold start can take 30+ seconds, try again")
    else:
        add("voice notes", False, "ASR_URL not set: voice notes get a 'please type' reply (deploy/modal_asr.py)", warn=True)

    dj = Daraja(http, s)
    if not dj.configured:
        add("M-Pesa", False, "not fully configured (consumer key/secret, passkey, callback secret, PUBLIC_BASE_URL)", warn=True)
    else:
        try:
            await dj._auth(force=True)
            add("M-Pesa", True, f"Daraja {s.mpesa_env} token OK; callback {s.public_base_url.rstrip('/')}/mpesa/callback/<secret>")
        except MpesaError as e:
            add("M-Pesa", False, str(e))
        if s.mpesa_env != "production":
            add("M-Pesa mode", True, "SANDBOX: prompts work but no real money moves; production needs Safaricom Go Live", warn=True)
        try:
            r = await http.get(s.public_base_url.rstrip("/") + "/health", timeout=5)
            add("public URL", r.status_code == 200, f"{s.public_base_url}/health answered {r.status_code}")
        except httpx.HTTPError as e:
            add("public URL", False, f"{s.public_base_url} is not reachable from here: {e.__class__.__name__}")
    return out
