"""Passive snapshots of recognized/generated text; never sends provider commands.

Snapshots enter the existing nonblocking journal before conversational processing.
Thus disconnect cleanup cannot erase an already received partial utterance.
Recording recovery handles speech for which the provider emitted no text at all.
"""

import logging
import os
import uuid

from .events import timestamp
from .publisher import publish

LOG = logging.getLogger("call_audit.realtime")


class RealtimeTranscriptObserver:
    def __init__(self, owner, call_id):
        self.owner, self.call_id = owner, call_id
        self.namespace = uuid.uuid4().hex  # Reconnects cannot collide on unscoped items.
        self.items = {}
        self.speech = {}
        self.response = None
        self.greeting_started = False

    def observe(self, event):
        kind = event.get("type", "")
        at = timestamp()
        item = event.get("item_id")
        if kind == "response.created":
            self.response = (event.get("response") or {}).get("id")
        if kind in {"response.audio.delta", "response.output_audio.delta"} and not self.greeting_started:
            self.greeting_started = True
            publish(self.owner, self.call_id, "operator_zero_greeting_started", evidence="first_provider_audio")
        if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}:
            bounds = self.speech.setdefault(item, {})
            bounds["start_at" if kind.endswith("started") else "end_at"] = at
            publish(self.owner, self.call_id,
                    "caller_speech_started" if kind.endswith("started") else "caller_speech_finished",
                    provider_item_id=item, evidence="provider_vad")
        caller = kind.startswith("conversation.item.input_audio_transcription.")
        assistant = kind.startswith(("response.audio_transcript.", "response.output_audio_transcript."))
        if not (caller or assistant):
            return
        if kind.endswith("failed"):
            publish(self.owner, self.call_id, "transcription_failed",
                    provider="openai_realtime", provider_item_id=item)
            return
        final = kind.endswith(("completed", "done"))
        key = f"{self.namespace}:{'caller' if caller else 'operator_zero'}:{item or event.get('response_id') or self.response or 'unscoped'}:{event.get('content_index', 0)}"
        state = self.items.setdefault(key, {"text": "", "start_at": at, "revision": 0, "final": False})
        if state["final"]:
            return
        delta = event.get("delta", "")
        if isinstance(delta, dict):
            delta = delta.get("text", "")
        text = event.get("transcript") if final else event.get("text", delta)
        if isinstance(text, str):
            state["text"] = text if final and text else state["text"] + (text if not final else "")
        state["revision"] += 1
        state["final"] = final
        if not state["text"].strip():
            return
        # Bound memory/envelopes; preserve earlier text and expose truncation.
        if len(state["text"]) > 6000:
            state["text"] = state["text"][:6000]
            publish(self.owner, self.call_id, "audit_gap", reason="transcript_item_too_large")
        bounds = self.speech.get(item, {}) if caller else {}
        publish(self.owner, self.call_id, "transcript_segment", segment_id=key,
                speaker="caller" if caller else "operator_zero", text=state["text"],
                start_at=bounds.get("start_at", state["start_at"]), end_at=bounds.get("end_at", at),
                is_final=final, revision=state["revision"], provider="openai_realtime",
                provider_item_id=item, timing_source="observed_speech_bounds" if "start_at" in bounds and "end_at" in bounds else "observed_provider_event",
                delivery="recognized" if caller else "generated_not_confirmed_heard")
        # Typical calls contain tens of items. Avoid unbounded growth on long sessions.
        if len(self.items) > 2048:
            self.items.pop(next(iter(self.items)))
        if len(self.speech) > 2048:
            self.speech.pop(next(iter(self.speech)))


def observe(provider, event):
    """Audit failure must not escape into the provider receive loop."""
    try:
        if "transcript" in str(event.get("type", "")) and os.getenv("CALL_TRANSCRIPTION_ENABLED", "false").lower() not in {"true", "1", "yes"}:
            return
        owner = getattr(getattr(provider, "on_event", None), "__self__", None)
        if getattr(owner, "call_audit", None) is None or not provider._call_id:
            return
        observer = getattr(provider, "_audit_transcript_observer", None)
        if observer is None or observer.call_id != provider._call_id:
            observer = provider._audit_transcript_observer = RealtimeTranscriptObserver(owner, provider._call_id)
        observer.observe(event)
    except Exception:
        LOG.warning("Transcript observation failed; conversation continues")
