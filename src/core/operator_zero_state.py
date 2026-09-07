"""Explicit state transitions for Operator Zero's predial handoff.

The engine still owns ARI side effects.  This module owns only the legal,
deterministic state changes, while preserving the legacy action keys consumed
by existing code and deployments.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class OperatorZeroPhase(str, Enum):
    DIALING = "dialing"
    ANSWERED = "answered"
    READY = "ready"
    ANNOUNCING = "announcing"
    ANNOUNCED = "announced"
    BRIDGED = "bridged"
    VOICEMAIL = "voicemail"
    FAILED = "failed"


@dataclass(frozen=True)
class OperatorZeroTransferState:
    action: dict[str, Any]

    @classmethod
    def start(cls, action: Mapping[str, Any]) -> "OperatorZeroTransferState":
        updated = dict(action)
        updated.update(
            operator_zero_phase=OperatorZeroPhase.DIALING.value,
            answered=False,
            ready_to_bridge=False,
            private_announcement_started=False,
            private_announcement_played=False,
            bridged=False,
        )
        return cls(updated)

    @classmethod
    def load(cls, action: Mapping[str, Any]) -> "OperatorZeroTransferState":
        updated = dict(action)
        if not updated.get("operator_zero_phase"):
            if updated.get("bridged"):
                phase = OperatorZeroPhase.BRIDGED
            elif updated.get("private_announcement_played"):
                phase = OperatorZeroPhase.ANNOUNCED
            elif updated.get("private_announcement_started"):
                phase = OperatorZeroPhase.ANNOUNCING
            elif updated.get("ready_to_bridge"):
                phase = OperatorZeroPhase.READY
            elif updated.get("answered"):
                phase = OperatorZeroPhase.ANSWERED
            else:
                phase = OperatorZeroPhase.DIALING
            updated["operator_zero_phase"] = phase.value
        return cls(updated)

    @property
    def phase(self) -> OperatorZeroPhase:
        return OperatorZeroPhase(self.action["operator_zero_phase"])

    @property
    def announcement_complete(self) -> bool:
        return self.phase in {OperatorZeroPhase.ANNOUNCED, OperatorZeroPhase.BRIDGED}

    @property
    def trust_eligible(self) -> bool:
        return self.phase is OperatorZeroPhase.BRIDGED and bool(
            self.action.get("private_announcement_played")
        )

    def destination_answered(self, channel_id: str) -> "OperatorZeroTransferState":
        return self._change(
            (
                OperatorZeroPhase.ANSWERED
                if self.phase is OperatorZeroPhase.DIALING
                else self.phase
            ),
            answered=True,
            predial_channel_id=channel_id,
        )

    def caller_audio_drained(self) -> "OperatorZeroTransferState":
        phase = (
            OperatorZeroPhase.READY
            if self.phase in {OperatorZeroPhase.DIALING, OperatorZeroPhase.ANSWERED}
            else self.phase
        )
        return self._change(phase, ready_to_bridge=True)

    def announcement_started(self) -> "OperatorZeroTransferState":
        if self.action.get("private_announcement_started"):
            raise ValueError("private announcement already started")
        return self._change(
            OperatorZeroPhase.ANNOUNCING,
            private_announcement_started=True,
            private_announcement_played=False,
        )

    def announcement_finished(self, completed: bool) -> "OperatorZeroTransferState":
        return self._change(
            OperatorZeroPhase.ANNOUNCED if completed else OperatorZeroPhase.FAILED,
            private_announcement_played=bool(completed),
        )

    def bridge_finished(self, completed: bool) -> "OperatorZeroTransferState":
        if completed and not self.announcement_complete:
            raise ValueError("cannot bridge before private announcement completes")
        return self._change(
            OperatorZeroPhase.BRIDGED if completed else OperatorZeroPhase.FAILED,
            bridged=bool(completed),
        )

    def voicemail_handoff(self) -> "OperatorZeroTransferState":
        return self._change(OperatorZeroPhase.VOICEMAIL)

    def failed(self) -> "OperatorZeroTransferState":
        return self._change(OperatorZeroPhase.FAILED)

    def _change(self, phase: OperatorZeroPhase, **values: Any) -> "OperatorZeroTransferState":
        updated = dict(self.action)
        updated.update(values)
        updated["operator_zero_phase"] = phase.value
        return OperatorZeroTransferState(updated)
