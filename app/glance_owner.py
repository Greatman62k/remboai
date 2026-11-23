"""Telegram owner commands for pairing and revoking Glance devices."""
from __future__ import annotations

import re
import unicodedata
from uuid import UUID

from . import glance_sessions


PAIR_COMMAND = re.compile(r"^\s*/?glance\s+pair\s*$", re.I)
DEVICES_COMMAND = re.compile(r"^\s*/?glance\s+devices\s*$", re.I)
PAIR_CALLBACK = re.compile(r"^glance:pair:([0-9a-fA-F-]{36})$")
REVOKE_CALLBACK = re.compile(r"^glance:revoke:([0-9a-fA-F-]{36})$")


def is_command(text: str) -> bool:
    return bool(PAIR_COMMAND.fullmatch(text or "") or DEVICES_COMMAND.fullmatch(text or ""))


def _clean_label(value: str | None, limit: int = 32) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Cn"})
    return " ".join(text.split())[:limit] or "Salon"


def _bot_id(ctx, salon: dict) -> str | None:
    sender = getattr(ctx.wa, "tg", None)
    bot_key = getattr(sender, "bot_key", None)
    configured = salon.get("tg_bot_id")
    if not bot_key or not configured or configured != bot_key:
        return None
    return bot_key


async def _send(ctx, sender: str, body: str, buttons: list[tuple[str, str]] | None = None) -> bool:
    # Intentionally bypass outbound conversational tracing when returning a pairing code.
    # Pairing codes must never be written to conversation_trace_replies or outbound logs.
    if not sender.startswith("tg:"):
        return False
    if buttons:
        return await ctx.wa.send_buttons(None, "", sender, body, buttons)
    return await ctx.wa.send_text(None, "", sender, body)


async def _send_pairing_code(ctx, salon: dict, sender: str, bot_id: str) -> bool:
    try:
        issued = await glance_sessions.issue_pairing_code(ctx.pool, ctx.settings, sender, salon["id"], bot_id)
    except glance_sessions.GlanceAuthError as exc:
        await _send(ctx, sender, exc.message)
        return True
    salon_name = _clean_label(issued["salon_name"])
    body = (f"RemboAI Glance pairing for {salon_name}\n\n"
            f"Code: {issued['code']}\nExpires in 5 minutes. Enter it in the Glance phone app.")
    # Do not log the body or route this through the conversational trace helpers.
    await _send(ctx, sender, body)
    return True


async def handle_command(ctx, salon: dict, sender: str, text: str) -> bool:
    if not is_command(text):
        return False
    if not sender.startswith("tg:") or (salon.get("owner_wa_id") or "") != sender:
        return False
    bot_id = _bot_id(ctx, salon)
    if bot_id is None:
        await _send(ctx, sender, "Use the authenticated RemboAI Telegram owner chat to manage Glance.")
        return True
    if PAIR_COMMAND.fullmatch(text or ""):
        salons = await glance_sessions.eligible_owner_salons(ctx.pool, sender, bot_id)
        if not salons:
            await _send(ctx, sender, "No eligible salon is available for Glance pairing.")
        elif len(salons) == 1:
            await _send_pairing_code(ctx, salons[0], sender, bot_id)
        else:
            buttons = [(f"glance:pair:{row['id']}", _clean_label(row["name"], 32)) for row in salons[:90]]
            await _send(ctx, sender, "Choose the salon to pair with Glance:", buttons)
        return True
    sessions = await glance_sessions.list_owner_sessions(ctx.pool, sender, bot_id)
    if not sessions:
        await _send(ctx, sender, "No active Glance devices are paired.")
        return True
    buttons = []
    lines = ["Active Glance devices:"]
    for row in sessions[:90]:
        device = _clean_label(row["device_label"], 24)
        salon_label = _clean_label(row["salon_name"], 24)
        created = row["created_at"].strftime("%d %b %H:%M UTC")
        lines.append(f"{device} · {salon_label} · paired {created}")
        buttons.append((f"glance:revoke:{row['session_id']}", f"Revoke {device}"[:32]))
    await _send(ctx, sender, "\n".join(lines), buttons)
    return True


async def handle_tap(ctx, salon: dict, sender: str, callback_data: str) -> bool:
    if not sender.startswith("tg:") or (salon.get("owner_wa_id") or "") != sender:
        return False
    bot_id = _bot_id(ctx, salon)
    if bot_id is None:
        return False
    if match := PAIR_CALLBACK.fullmatch(callback_data or ""):
        try:
            selected = str(UUID(match[1]))
        except ValueError:
            await _send(ctx, sender, "That salon selection is unavailable. Run GLANCE PAIR again.")
            return True
        await _send_pairing_code(ctx, {"id": selected}, sender, bot_id)
        return True
    if match := REVOKE_CALLBACK.fullmatch(callback_data or ""):
        result = await glance_sessions.revoke_owner_session(ctx.pool, sender, bot_id, match[1])
        if result is None:
            await _send(ctx, sender, "That device is unavailable or is no longer yours to revoke.")
        else:
            await _send(ctx, sender, f"Revoked { _clean_label(result['device_label'], 24) } for { _clean_label(result['salon_name'], 24) }.")
        return True
    return False
