"""Set-up checks: everything that has to be true for a real customer message to become a confirmed, paid booking.
Each check says what it found and, when it fails, what to do about it. Run with `python scripts/doctor.py`."""
from __future__ import annotations

import time as clock
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .ai.llm import Extractor, _coerce, _json_from, sanitize
from .ai.voice import Transcriber
from .ai.vision import StyleMatcher, make_png
from .mpesa import Daraja, MpesaError
from .channels.whatsapp import explain
from .platform import configured_ids


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    warn: bool = False


def _platform_identity_overlaps(settings, salons: list[dict]) -> tuple[set[str], int]:
    ids = configured_ids(settings)
    overlap = sum(1 for salon in salons if salon.get("owner_wa_id") in ids)
    return ids, overlap


async def run_checks(s, http: httpx.AsyncClient, pool) -> list[Check]:
    out: list[Check] = []
    add = lambda *a, **k: out.append(Check(*a, **k))  # noqa: E731

    missing = [n for n, v in (("LLM_API_KEY", s.llm_api_key), ("DATABASE_URL", getattr(s, "database_url", ""))) if not v]
    add("configuration", not missing, "required values present" if not missing else "missing in .env: " + ", ".join(missing))

    salons = []
    try:
        salons = [dict(r) for r in await pool.fetch(
            "SELECT s.id::text, s.name, s.wa_phone_number_id, s.tg_bot_id, s.owner_wa_id, s.deposit_kes, sp.kind, sp.shortcode, sp.party_b, sp.manual, "
            "(SELECT count(*) FROM services v WHERE v.salon_id=s.id AND v.active) AS services, "
            "(SELECT count(*) FROM staff t WHERE t.salon_id=s.id AND t.active) AS staff "
            "FROM salons s LEFT JOIN salon_payment sp ON sp.salon_id=s.id WHERE s.active")]
        add("database", True, f"connected, {len(salons)} active salon(s)")
    except Exception as e:  # noqa: BLE001
        add("database", False, f"{e.__class__.__name__}: {e}")
    try:
        owner_customer_overlap = int(await pool.fetchval(
            "SELECT count(*) FROM salons s JOIN clients c ON c.salon_id=s.id AND c.wa_id=s.owner_wa_id WHERE s.active") or 0)
        platform_ids, platform_salon_overlap = _platform_identity_overlaps(s, salons)
        platform_client_overlap = int(await pool.fetchval(
            "SELECT count(*) FROM clients WHERE wa_id=ANY($1::text[])", list(platform_ids)) or 0) if platform_ids else 0
        collisions = owner_customer_overlap + platform_salon_overlap + platform_client_overlap
        if collisions:
            add("identity separation", False,
                f"{owner_customer_overlap} salon owner/customer overlap(s); platform owner matches {platform_salon_overlap} salon owner(s) and {platform_client_overlap} customer record(s). "
                "Platform messages fail closed; use /owner <command> for an explicitly owned salon and a separate account for customer testing.", warn=True)
        elif platform_ids:
            add("identity separation", True, "platform administrators, salon owners, and customers use separate IDs")
    except Exception as e:  # noqa: BLE001
        add("identity separation", False, f"could not check role overlap: {e.__class__.__name__}", warn=True)
    if not salons:
        add("salon", True, "no salon yet: message the bot REGISTER to set up the first business (or run scripts/seed_salon.py)")

    for sl in salons:
        label = f"salon '{sl['name']}'"
        add(label, sl["services"] > 0 and sl["staff"] > 0, f"{sl['staff']} stylist(s), {sl['services']} service(s)")
        if sl["deposit_kes"] > 0 and not sl["kind"]:
            add(label + " deposits", False, "deposit is set but no till/paybill: bookings will be confirmed with NO deposit (use --till)", warn=True)
        elif sl["kind"]:
            manual = "manual till, owner confirms each deposit" if sl["manual"] else f"Daraja STK (shortcode {sl['shortcode']})"
            add(label + " deposits", True, f"KES {sl['deposit_kes']} to {sl['kind']} {sl['party_b']} ({manual})")
        try:
            approved = await pool.fetchval("SELECT count(*) FROM reminder_templates WHERE salon_id=$1::uuid AND approved=true", sl["id"])
            if not approved:
                add(label + " reminders", False, "no approved reminder templates: no 24h/2h/thanks reminders go out until rows are added (see docs/TEMPLATES.md)", warn=True)
        except Exception as e:  # noqa: BLE001
            add(label + " reminders", False, f"could not read reminder_templates: {e.__class__.__name__}", warn=True)
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
                add("telegram salon", True, "a salon is linked to this bot" if linked else "no salon uses this bot yet: chat registration will link the first one",
                    warn=not linked)
        except (httpx.HTTPError, ValueError) as e:
            add("telegram", False, f"could not reach Telegram: {e.__class__.__name__}")
    else:
        add("telegram", False, "TELEGRAM_BOT_TOKEN not set: Telegram channel off", warn=True)

    ext = Extractor(http, s)
    today = datetime.now(ZoneInfo("Africa/Nairobi")).date()
    for _backend, base, key, model, _ in ext.endpoints():
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
    if getattr(s, "agent_mode", "off") == "on":
        from .agent.probe import probe_backends
        probes = await probe_backends(http, s, ext.endpoints())
        for r in probes:
            other_works = any(x.ok and x.backend == r.backend and x.model == r.model for x in probes)
            add(f"agent {r.model} ({r.protocol})", r.ok, f"{r.ms} ms; {r.detail}" + ("" if r.ok or not other_works else " (the other wire format works, which is enough)"),
                warn=not r.ok and other_works)
    else:
        add("agent mode", False, "AGENT_MODE is off: the keyword/extraction flow answers customers. Set AGENT_MODE=on so the model drives the conversation", warn=True)
    matcher = StyleMatcher(http, s, ext.endpoints)
    if not matcher.enabled:
        add("photo matching", False, "switched off (VISION_ENABLED=false or no LLM_API_KEY)", warn=True)
    for _backend, base, key, model, _ in ext.endpoints() if matcher.enabled else []:
        try:
            from .agent.policy import load_policy
            color_prompt = load_policy(getattr(s, "agent_policy_path", None))["diagnostics"]["vision_color_test"]
            body = matcher.body(model, color_prompt, make_png((220, 20, 20), 256), "image/png")
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
            r = await http.get(s.asr_url.rsplit("/", 1)[0] + "/health", headers=Transcriber(http, s).auth_headers, timeout=s.asr_timeout_s)
            add("speech service health", r.status_code == 200,
                f"health endpoint answered: {r.json().get('model') if r.status_code == 200 else r.status_code}; this does not test audio transcription")
        except (httpx.HTTPError, ValueError) as e:
            add("speech service health", False, f"speech service not reachable ({e.__class__.__name__}); a cold start can take 30+ seconds, try again")
    else:
        add("speech service health", False, "ASR_URL not set: voice notes get a 'please type' reply (deploy/modal_asr.py)", warn=True)

    try:
        from .ai.budget import Budget
        used = await Budget(pool, s).today()
        add("daily limits", True, f"text {used.get('text', 0)}/{s.llm_daily_limit or 'no limit'}, photos {used.get('vision', 0)}/{s.vision_daily_limit or 'no limit'}, "
                                  f"voice {used.get('voice', 0)}/{s.voice_daily_limit or 'no limit'} today; {s.per_sender_daily_limit or 'no'} messages per person per day")
    except Exception as e:  # noqa: BLE001
        add("daily limits", False, f"{e.__class__.__name__}: {str(e)[:100]}", warn=True)

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
        except (httpx.HTTPError, ValueError) as e:
            add("public URL", False, f"{s.public_base_url!r} is not reachable from here ({e.__class__.__name__}): it must look like https://your-domain")
    return out
