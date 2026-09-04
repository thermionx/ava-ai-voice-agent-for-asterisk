"""Confirmed outbound dialing for an active household call.

The first tool call only stages a normalized number. A second call can arm the
dialplan handoff only after a new, unambiguously affirmative user utterance.
Asterisk remains the owner of trunk selection, caller ID, and call progress.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
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

_OUTBOUND_HISTORY_DB_DEFAULT = "/app/data/operator/outbound_calls.db"
_AGENTS_DB_DEFAULT = "/app/data/operator/agents.db"

_SAFE_CONTEXT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CONTACT_NAME_NOISE = frozenset(
    {"call", "dial", "little", "mobile", "phone", "please", "the"}
)
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


def _outbound_history_db_path() -> str:
    return str(
        os.environ.get(
            "OPERATOR_ZERO_OUTBOUND_HISTORY_DB",
            _OUTBOUND_HISTORY_DB_DEFAULT,
        )
        or _OUTBOUND_HISTORY_DB_DEFAULT
    )


def _initialize_outbound_history(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with sqlite3.connect(db_path, timeout=5) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS outbound_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone_number TEXT NOT NULL,
                display_name TEXT,
                call_id TEXT,
                dialed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_outbound_calls_dialed_at
            ON outbound_calls(dialed_at DESC, id DESC)
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_calls_call_id
            ON outbound_calls(call_id)
            WHERE call_id IS NOT NULL AND call_id != ''
            """
        )
        connection.commit()


def record_outbound_call(
    db_path: str,
    *,
    phone_number: str,
    display_name: str,
    call_id: str,
) -> None:
    _initialize_outbound_history(db_path)
    with sqlite3.connect(db_path, timeout=5) as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO outbound_calls (phone_number, display_name, call_id)
            VALUES (?, ?, ?)
            """,
            (phone_number, display_name, call_id),
        )
        connection.commit()


def list_outbound_calls(db_path: str, *, limit: int) -> list[Dict[str, Any]]:
    _initialize_outbound_history(db_path)
    with sqlite3.connect(db_path, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, phone_number, display_name, dialed_at
            FROM outbound_calls
            ORDER BY dialed_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def _number_is_trusted(phone_number: str) -> bool:
    """Verify dialing-confirmation exemption with Operator Zero memory."""
    base_url = str(
        os.environ.get("OPERATOR_ZERO_MEMORY_URL", "http://127.0.0.1:8790")
        or "http://127.0.0.1:8790"
    ).rstrip("/")
    url = f"{base_url}/caller?phone={urllib.parse.quote(phone_number)}"
    with urllib.request.urlopen(url, timeout=2.0) as response:
        result = json.loads(response.read().decode("utf-8"))
    return str(result.get("trusted_caller") or "").strip().lower() == "true"


def _number_is_in_agent_profile(phone_number: str, context_name: str) -> bool:
    """Return whether the active Agent prompt explicitly contains the number."""
    slug = str(context_name or "").strip()
    if slug != "operator_zero":
        return False
    db_path = str(
        os.environ.get("OPERATOR_ZERO_AGENTS_DB", _AGENTS_DB_DEFAULT)
        or _AGENTS_DB_DEFAULT
    )
    with sqlite3.connect(db_path, timeout=2) as connection:
        row = connection.execute(
            "SELECT prompt FROM agents WHERE slug = ? AND is_active = 1",
            (slug,),
        ).fetchone()
    if not row:
        return False
    # Extract phone-shaped runs from the operator-maintained profile, then use
    # the same strict NANP normalization as the eventual dialplan handoff.
    candidates = re.findall(r"(?:\+?1[ .()\-]*)?(?:\d[ .()\-]*){10}", str(row[0] or ""))
    return any(normalize_nanp_number(candidate) == phone_number for candidate in candidates)


def _agent_profile_number_for_name(display_name: str, context_name: str) -> Optional[str]:
    """Resolve a named contact from Operator Zero's profile, if unambiguous."""
    slug = str(context_name or "").strip()
    name = _clean_display_name(display_name).casefold()
    if slug != "operator_zero" or not name:
        return None
    db_path = str(
        os.environ.get("OPERATOR_ZERO_AGENTS_DB", _AGENTS_DB_DEFAULT)
        or _AGENTS_DB_DEFAULT
    )
    with sqlite3.connect(db_path, timeout=2) as connection:
        row = connection.execute(
            "SELECT prompt FROM agents WHERE slug = ? AND is_active = 1",
            (slug,),
        ).fetchone()
    if not row:
        return None
    requested_tokens = {
        token
        for token in re.findall(r"[a-z]+", name)
        if len(token) >= 4 and token not in _CONTACT_NAME_NOISE
    }
    if not requested_tokens:
        return None

    matches: set[str] = set()
    for line in str(row[0] or "").splitlines():
        line_tokens = set(re.findall(r"[a-z]+", line.casefold()))
        # Spoken-name transcripts commonly shorten a first name (for example,
        # "Lili" for "Lilian") or add harmless words such as "little" and
        # "mobile phone". Accept a unique four-letter-or-longer prefix, but
        # never guess when it identifies more than one profile number.
        if not any(
            requested == profile or profile.startswith(requested)
            for requested in requested_tokens
            for profile in line_tokens
        ):
            continue
        for candidate in re.findall(
            r"(?:\+?1[ .()\-]*)?(?:\d[ .()\-]*){10}", line
        ):
            normalized = normalize_nanp_number(candidate)
            if normalized:
                matches.add(normalized)
    return next(iter(matches)) if len(matches) == 1 else None


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
                "Stage or complete a household user's outbound phone call. "
                "Use this tool—not voicemail or a household transfer—when the user "
                "asks to call a household member's mobile or cell phone. "
                "On the first invocation set confirmed=false; the tool returns the digits "
                "that must be repeated to the user and asks whether they are correct. "
                "Only after the user explicitly confirms those digits, invoke again "
                "with the identical phone_number and confirmed=true. A number may come "
                "from directory assistance, a trusted-caller lookup, caller-history "
                "lookup, or the user's speech. Trusted callers are verified by the tool "
                "and dialed immediately without number readback."
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
        display_name = _clean_display_name(parameters.get("display_name"))
        number = normalize_nanp_number(parameters.get("phone_number"))
        try:
            profile_number = await asyncio.to_thread(
                _agent_profile_number_for_name,
                display_name,
                context.context_name,
            )
        except Exception:
            profile_number = None
            logger.warning(
                "Named Agent-profile lookup unavailable",
                call_id=context.call_id,
            )
        if profile_number and profile_number != number:
            logger.warning(
                "Agent-profile number overrode conflicting dial request",
                call_id=context.call_id,
                display_name=display_name,
                requested_last4=number[-4:] if number else "",
                profile_last4=profile_number[-4:],
            )
            number = profile_number
        if not number:
            return {
                "status": "error",
                "message": "That is not a valid ten-digit US or Canadian phone number. Please ask for it again.",
            }

        session = await context.get_session()
        history = list(getattr(session, "conversation_history", None) or [])
        pending = getattr(session, "pending_phone_call", None)
        confirmed = parameters.get("confirmed") is True

        # Operator-managed sources, not the model, own the confirmation
        # exemption. Any lookup failure falls through to fail-closed readback.
        if not isinstance(pending, dict):
            approval_source = ""
            try:
                if await asyncio.to_thread(_number_is_trusted, number):
                    approval_source = "trusted_caller"
            except Exception:
                logger.warning(
                    "Trusted-number lookup unavailable; requiring confirmation",
                    call_id=context.call_id,
                    target_last4=number[-4:],
                )
            if not approval_source:
                try:
                    if await asyncio.to_thread(
                        _number_is_in_agent_profile,
                        number,
                        context.context_name,
                    ):
                        approval_source = "agent_profile"
                except Exception:
                    logger.warning(
                        "Agent-profile number lookup unavailable; requiring confirmation",
                        call_id=context.call_id,
                        target_last4=number[-4:],
                    )
            if approval_source:
                label = display_name or number
                action = self._build_dial_action(number, label, context)
                if not _SAFE_CONTEXT.fullmatch(str(action.get("dialplan_context") or "")):
                    logger.error(
                        "Unsafe outbound dialplan context rejected",
                        call_id=context.call_id,
                    )
                    return {
                        "status": "failed",
                        "message": "Outbound calling is not configured safely.",
                    }
                logger.info(
                    "Approved outbound phone call starting without readback",
                    call_id=context.call_id,
                    target_last4=number[-4:],
                    approval_source=approval_source,
                )
                result = await self.commit_deferred_action(action, context)
                result.update(
                    {
                        "phone_number": number,
                        "trusted_number": True,
                        "approval_source": approval_source,
                    }
                )
                if result.get("status") == "success":
                    result["message"] = f"Calling {label} now."
                return result

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
        action = self._build_dial_action(number, label, context)
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

    @staticmethod
    def _build_dial_action(
        number: str,
        label: str,
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        cfg = context.get_config_value("tools.dial_phone", {}) or {}
        dialplan_context = str(cfg.get("dialplan_context") or "from-house").strip()
        return build_deferred_transfer_action(
            source_tool="dial_phone",
            commit_tool="dial_phone",
            transfer_type="outbound_phone_call",
            target=number,
            description=label,
            dialplan_context=dialplan_context,
            payload={"display_name": label},
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
            try:
                payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
                await asyncio.to_thread(
                    record_outbound_call,
                    _outbound_history_db_path(),
                    phone_number=number,
                    display_name=_clean_display_name(payload.get("display_name")),
                    call_id=str(context.call_id or ""),
                )
            except Exception:
                # The channel has already left Stasis and may be dialing. History
                # failure must not falsely report that the phone call failed.
                logger.error(
                    "Unable to record outbound call history",
                    call_id=context.call_id,
                    target_last4=number[-4:],
                    exc_info=True,
                )
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


class ListDialedNumbersTool(Tool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="list_dialed_numbers",
            description=(
                "Return the household's outbound dialing history, newest first. "
                "Use this when the user asks what numbers Operator Zero called, "
                "who the household called, or for outgoing call history. Do not "
                "use caller-history tools for outgoing calls."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=False,
            max_execution_time=10,
            parameters=[
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Maximum calls to return, from 1 to 100. Defaults to 20.",
                    required=False,
                )
            ],
        )

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        try:
            limit = max(1, min(int(parameters.get("limit", 20)), 100))
        except (TypeError, ValueError):
            limit = 20
        try:
            calls = await asyncio.to_thread(
                list_outbound_calls,
                _outbound_history_db_path(),
                limit=limit,
            )
        except Exception:
            logger.error(
                "Unable to read outbound call history",
                call_id=context.call_id,
                exc_info=True,
            )
            return {
                "status": "failed",
                "message": "Unable to retrieve outgoing call history right now.",
            }
        return {
            "status": "success",
            "count": len(calls),
            "calls": calls,
            "message": (
                "No outgoing calls have been recorded."
                if not calls
                else f"Found {len(calls)} outgoing calls."
            ),
        }
