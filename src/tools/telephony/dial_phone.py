"""Confirmed outbound dialing for an active household call.

The first tool call only stages a normalized number. A second call can arm the
dialplan handoff only after a new, unambiguously affirmative user utterance.
Asterisk remains the owner of trunk selection, caller ID, and call progress.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, Optional

import structlog

from ..base import Tool, ToolCategory, ToolDefinition, ToolParameter
from ..context import ToolExecutionContext
from .deferred_transfer import (
    build_deferred_transfer_action,
    build_deferred_transfer_result,
    store_pending_deferred_transfer,
)

logger = structlog.get_logger(__name__)

_SAFE_CONTEXT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_NEGATIVE_CONFIRMATION = re.compile(
    r"\b(no|nope|not|wrong|incorrect|change|cancel|don't|do not|stop)\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_CONFIRMATION = re.compile(
    r"\b(yes|yeah|yep|correct|right|okay|ok|confirm|confirmed|go ahead|dial it|call (?:it|them))\b",
    re.IGNORECASE,
)


def normalize_nanp_number(value: Any) -> Optional[str]:
    """Return a dialplan-safe 10/11-digit NANP number, or ``None``."""
    raw = str(value or "").strip()
    if not raw or re.search(r"[A-Za-z]", raw):
        return None
    digits = re.sub(r"\D", "", raw)
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(national) != 10:
        return None
    # NANP area codes and exchanges cannot begin with 0 or 1. This also keeps
    # star codes, extensions, and malformed service numbers out of the trunk.
    if national[0] not in "23456789" or national[3] not in "23456789":
        return None
    return digits


def spoken_digits(number: str) -> str:
    return " ".join(number)


def _clean_display_name(value: Any) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return re.sub(r"\s+", " ", cleaned).strip()[:120]


def _new_user_messages(history: Iterable[Any], start_index: int) -> list[str]:
    messages: list[str] = []
    for item in list(history or [])[max(0, start_index):]:
        if not isinstance(item, dict) or str(item.get("role") or "").lower() != "user":
            continue
        content = str(item.get("content") or item.get("text") or "").strip()
        if content:
            messages.append(content)
    return messages


def _is_unambiguous_confirmation(messages: Iterable[str]) -> bool:
    text = " ".join(str(message or "") for message in messages).strip()
    return bool(
        text
        and not _NEGATIVE_CONFIRMATION.search(text)
        and _AFFIRMATIVE_CONFIRMATION.search(text)
    )


class DialPhoneTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="dial_phone",
            description=(
                "Stage or complete a household user's outbound phone call. On the "
                "first invocation set confirmed=false; the tool returns the digits "
                "that must be repeated to the user and asks whether they are correct. "
                "Only after the user explicitly confirms those digits, invoke again "
                "with the identical phone_number and confirmed=true. A number may come "
                "from a trusted-caller lookup, caller-history lookup, or the user's speech."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=15,
            parameters=[
                ToolParameter(
                    name="phone_number",
                    type="string",
                    description="The exact US/Canada phone number to stage or dial.",
                    required=True,
                ),
                ToolParameter(
                    name="confirmed",
                    type="boolean",
                    description=(
                        "False while reading the digits back; true only after a later "
                        "user utterance explicitly confirms that same number."
                    ),
                    required=True,
                ),
                ToolParameter(
                    name="display_name",
                    type="string",
                    description="Optional trusted or previous caller name for the readback.",
                    required=False,
                ),
            ],
        )

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        number = normalize_nanp_number(parameters.get("phone_number"))
        if not number:
            return {
                "status": "error",
                "message": "That is not a valid ten-digit US or Canadian phone number. Please ask for it again.",
            }

        session = await context.get_session()
        history = list(getattr(session, "conversation_history", None) or [])
        pending = getattr(session, "pending_phone_call", None)
        confirmed = parameters.get("confirmed") is True
        display_name = _clean_display_name(parameters.get("display_name"))

        if not confirmed:
            session.pending_phone_call = {
                "phone_number": number,
                "display_name": display_name,
                "history_index": len(history),
                "created_at": time.time(),
            }
            await context.session_store.upsert_call(session)
            return {
                "status": "confirmation_required",
                "phone_number": number,
                "spoken_digits": spoken_digits(number),
                "message": f"Should I dial {spoken_digits(number)}?",
            }

        if not isinstance(pending, dict) or pending.get("phone_number") != number:
            # Never dial from a first invocation marked confirmed, nor after the
            # model changes the number. Stage it and require a fresh user turn.
            session.pending_phone_call = {
                "phone_number": number,
                "display_name": display_name,
                "history_index": len(history),
                "created_at": time.time(),
            }
            await context.session_store.upsert_call(session)
            return {
                "status": "confirmation_required",
                "phone_number": number,
                "spoken_digits": spoken_digits(number),
                "message": f"Should I dial {spoken_digits(number)}?",
            }

        cfg = context.get_config_value("tools.dial_phone", {}) or {}
        try:
            confirmation_timeout = max(
                10.0,
                min(float(cfg.get("confirmation_timeout_seconds", 120)), 600.0),
            )
        except (TypeError, ValueError):
            confirmation_timeout = 120.0
        try:
            confirmation_minimum = max(
                0.0,
                min(float(cfg.get("confirmation_minimum_seconds", 0.5)), 5.0),
            )
        except (TypeError, ValueError):
            confirmation_minimum = 0.5
        try:
            created_at = float(pending.get("created_at") or 0)
            history_index = int(pending.get("history_index") or 0)
        except (TypeError, ValueError):
            created_at = 0
            history_index = len(history)
        if time.time() - created_at > confirmation_timeout:
            session.pending_phone_call = None
            await context.session_store.upsert_call(session)
            return {"status": "error", "message": "That confirmation expired. Please tell me the number again."}

        new_messages = _new_user_messages(history, history_index)
        # OpenAI Realtime can emit the function call before its transcript event
        # is delivered to us. The second invocation's explicit confirmed=true is
        # therefore authoritative once the identical target has already been
        # staged for long enough to rule out an immediate duplicate tool call.
        # Transcript evidence remains useful for providers that persist it first.
        if (
            time.time() - created_at < confirmation_minimum
            and not _is_unambiguous_confirmation(new_messages)
        ):
            return {
                "status": "confirmation_required",
                "phone_number": number,
                "spoken_digits": spoken_digits(number),
                "message": f"Should I dial {spoken_digits(number)}?",
            }

        dialplan_context = str(cfg.get("dialplan_context") or "from-house").strip()
        if not _SAFE_CONTEXT.fullmatch(dialplan_context):
            logger.error("Unsafe outbound dialplan context rejected", call_id=context.call_id)
            return {"status": "failed", "message": "Outbound calling is not configured safely."}

        label = str(pending.get("display_name") or display_name or number).strip()[:120]
        action = build_deferred_transfer_action(
            source_tool="dial_phone",
            commit_tool="dial_phone",
            transfer_type="outbound_phone_call",
            target=number,
            description=label,
            dialplan_context=dialplan_context,
        )
        session.pending_phone_call = None
        await context.session_store.upsert_call(session)
        await store_pending_deferred_transfer(context, action)
        logger.info(
            "Confirmed outbound phone call armed",
            call_id=context.call_id,
            target_last4=number[-4:],
        )
        return build_deferred_transfer_result(
            action=action,
            message=f"Calling {label} now.",
            extra={"phone_number": number},
        )

    async def commit_deferred_action(
        self,
        action: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        number = normalize_nanp_number(action.get("target"))
        dialplan_context = str(action.get("dialplan_context") or "").strip()
        if not number or not _SAFE_CONTEXT.fullmatch(dialplan_context):
            return {"status": "failed", "message": "Outbound call target is invalid."}

        await context.update_session(transfer_active=True, transfer_target=number)
        try:
            continued = await context.ari_client.continue_in_dialplan(
                context.caller_channel_id,
                context=dialplan_context,
                extension=number,
                priority=1,
            )
            if continued is not True:
                raise RuntimeError("Asterisk rejected the outbound dialing handoff")
            return {
                "status": "success",
                "message": "Outbound call started",
                "phone_number": number,
            }
        except Exception as exc:
            await context.update_session(transfer_active=False, transfer_target=None)
            logger.error(
                "Outbound dialing handoff failed",
                call_id=context.call_id,
                error=str(exc),
                exc_info=True,
            )
            return {"status": "failed", "message": "Unable to place that call right now."}
