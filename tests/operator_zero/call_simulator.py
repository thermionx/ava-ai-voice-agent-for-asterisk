"""Deterministic call-control simulator for Operator Zero contract tests.

This intentionally lives in tests.  It describes externally observable call
events without pretending that an AI prompt is a telephony state machine.
Production integrations are exercised separately by focused engine/tool tests.
"""

from dataclasses import dataclass, field
from enum import Enum, auto


class State(Enum):
    NEW = auto()
    SCREENING = auto()
    READY = auto()
    ANNOUNCING = auto()
    CONNECTED = auto()
    CLOSING = auto()
    ENDED = auto()


@dataclass
class CallerMemory:
    callers: dict[str, dict] = field(default_factory=dict)
    recent_number: str = ""

    def seen(self, number: str, name: str = "") -> None:
        self.recent_number = number
        self.callers.setdefault(number, {"name": name, "trusted": False, "blocked": False})

    def recent_caller(self) -> dict:
        return dict(self.callers.get(self.recent_number, {}), phone_number=self.recent_number)

    def manage_caller(self, number: str, action: str) -> None:
        caller = self.callers[number]
        if action == "trust":
            caller.update(trusted=True, blocked=False)
        elif action == "untrust":
            caller.update(trusted=False, blocked=False)
        elif action == "block":
            caller.update(trusted=False, blocked=True)
        else:
            raise ValueError(action)


@dataclass
class OperatorZeroCall:
    external: bool = True
    state: State = State.NEW
    caller_name: str = ""
    recipient: str = ""
    purpose: str = ""
    spoken: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    resources: set[str] = field(default_factory=set)
    goodbye_count: int = 0

    def start(self) -> None:
        self.resources.update({"caller_channel", "ai_media", "mixing_bridge"})
        self.state = State.SCREENING
        if self.external:
            self.spoken.append("Operator Zero. How may I help you?")

    def identify(self, name: str) -> None:
        self.caller_name = name.strip()
        self._update_readiness()

    def request_recipient(self, recipient: str, *, specific: bool = True) -> None:
        self.recipient = recipient.strip() if specific else ""
        self._update_readiness()

    def give_official_purpose(self, purpose: str) -> None:
        self.purpose = purpose.strip()
        self._update_readiness()

    def _update_readiness(self) -> None:
        if (self.caller_name and self.recipient) or self.purpose:
            self.state = State.READY

    def transfer(self, announcement: str) -> None:
        if self.state is not State.READY:
            raise RuntimeError("screening incomplete")
        self.spoken.append("One moment, please.")
        self.state = State.ANNOUNCING
        self.effects.append(f"announce:{announcement}")
        self.effects.append("blind_transfer:inside_phone")
        self.resources.add("inside_channel")
        self.state = State.CONNECTED

    def voicemail(self) -> None:
        self.effects.append("continue:operator-zero-voicemail")
        self.resources.discard("ai_media")
        self.resources.discard("mixing_bridge")

    def audio_path(self) -> tuple[bool, bool]:
        active = self.state in {State.SCREENING, State.READY, State.CONNECTED}
        return active and "caller_channel" in self.resources, active and bool(
            {"ai_media", "inside_channel"} & self.resources
        )

    def close(self) -> None:
        if self.state is State.ENDED:
            return
        self.goodbye_count += 1
        self.spoken.append("Goodbye.")
        if self.goodbye_count == 1:
            self.state = State.CLOSING
        else:
            self.hangup("second_goodbye")

    def reopen(self) -> None:
        if self.state is State.CLOSING:
            self.state = State.CONNECTED

    def fail(self, source: str) -> None:
        self.effects.append(f"failure:{source}")
        self.hangup(source)

    def timeout(self) -> None:
        self.hangup("outside_timeout")

    def hangup(self, reason: str = "caller_hangup") -> None:
        self.effects.append(f"hangup:{reason}")
        self.resources.clear()
        self.state = State.ENDED
