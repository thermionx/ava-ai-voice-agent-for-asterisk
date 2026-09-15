"""Shared incoming-call facts, screening decisions, and announcement text.

Audio item ordering remains the provider's responsibility. These facts are
persisted with the handoff so announcements and directory learning agree.
"""
from dataclasses import asdict, dataclass
import re
from typing import Any, Dict, List, Mapping


def value_was_spoken(value: str, history: List[Dict[str, Any]], *, name_aliases: Any = None) -> bool:
    def normalize(raw: Any) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).split())

    # Aliases are household configuration, never model/tool arguments.
    # Invalid or ambiguous mappings fall back to exact transcript matching.
    replacements = {}
    valid = isinstance(name_aliases, dict)
    if valid:
        for canonical, variants in name_aliases.items():
            if not isinstance(canonical, str) or not normalize(canonical) or not isinstance(variants, list):
                valid = False
                break
            target = normalize(canonical)
            for variant in [canonical, *variants]:
                if not isinstance(variant, str) or not normalize(variant):
                    valid = False
                    break
                alias = normalize(variant)
                if alias in replacements and replacements[alias] != target:
                    valid = False
                    break
                replacements[alias] = target
            if not valid:
                break
    pattern = None
    if valid and replacements:
        pattern = re.compile(r"\b(?:" + "|".join(
            re.escape(alias) for alias in sorted(replacements, key=len, reverse=True)
        ) + r")\b")

    def evidence_text(raw: Any) -> str:
        text = normalize(raw)
        # One pass prevents alias replacements from cascading.
        return pattern.sub(lambda match: replacements[match.group(0)], text) if pattern else text

    needle = evidence_text(value)
    if not needle:
        return False
    caller_text = " ".join(
        evidence_text(message.get("content", ""))
        for message in history
        if isinstance(message, dict) and message.get("role") == "user"
    )
    return f" {needle} " in f" {caller_text} "


@dataclass(frozen=True)
class ScreeningDecision:
    action: str
    message: str = ""


@dataclass(frozen=True)
class ScreeningFacts:
    caller_name: str = ""
    recipient: str = ""
    company: str = ""
    reason: str = ""

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> "ScreeningFacts":
        return cls(**{key: str(metadata.get(key) or "").strip()
                      for key in ("caller_name", "recipient", "company", "reason")})

    @classmethod
    def from_action(cls, action: Mapping[str, Any]) -> "ScreeningFacts | None":
        metadata = action.get("screening_facts")
        return cls.from_metadata(metadata) if isinstance(metadata, dict) else None

    def to_metadata(self) -> dict[str, str]:
        return asdict(self)

    @property
    def directory_business(self) -> str:
        """Prefer the stated purpose; use the business when no purpose was supplied."""
        return self.reason or self.company

    @property
    def announcement(self) -> str:
        identity = self.caller_name or self.company or "The caller"
        if self.caller_name and self.company:
            identity = f"{self.caller_name} from {self.company}"
        text = f"{identity} is on the line."
        if self.reason:
            text += f" Reason for calling: {self.reason}"
        return text

    def next_action(self, history: List[Dict[str, Any]], name_aliases: Any = None) -> ScreeningDecision:
        """Decide from caller-supported facts; broad purposes need no judging."""
        def spoken(value, *, name=False):
            return value_was_spoken(value, history, name_aliases=name_aliases if name else None)

        if self.caller_name and not spoken(self.caller_name, name=True):
            return ScreeningDecision("ASK_CALLER", "The caller's identity was not confirmed in the conversation.")
        generic = {"household", "household member", "member of the household", "family",
                   "family member", "head of household", "homeowner", "someone there", "anyone there"}
        recipient = " ".join(self.recipient.lower().replace("_", " ").replace("-", " ").split())
        if recipient and recipient not in generic:
            if not self.caller_name:
                return ScreeningDecision("ASK_CALLER", "The caller must identify themselves before a household transfer.")
            if not spoken(self.recipient, name=True):
                return ScreeningDecision("ASK_RECIPIENT", "The named recipient was not stated by the caller.")
            has_reason = bool(self.reason) and spoken(self.reason) and not is_reason_refusal(self.reason)
            has_business = bool(self.company) and spoken(self.company) and not is_reason_refusal(self.company)
            if not (has_reason or has_business):
                return ScreeningDecision(
                    "ASK_REASON",
                    "A caller-stated business name or reason is required before transferring. "
                    "Ask what they are calling about only if neither is known; deliveries, appointments, and relationships suffice. "
                    "If they provide neither, offer voicemail instead of transferring.",
                )
            return ScreeningDecision("TRANSFER")

        # Preserve the separate no-recipient emergency/official-service exception.
        official_terms = {"police", "sheriff", "fire department", "ambulance", "ems",
                          "hospital", "doctor", "medical", "court", "government", "emergency services"}
        company = " ".join(self.company.lower().replace("_", " ").replace("-", " ").split())
        if (any(term in company for term in official_terms) and self.reason
                and spoken(self.company) and spoken(self.reason) and not is_reason_refusal(self.reason)):
            return ScreeningDecision("TRANSFER")
        return ScreeningDecision("ASK_RECIPIENT", "A specific named recipient or a confirmed emergency/official reason is required.")


def is_reason_refusal(reason: str) -> bool:
    """Reject explicit refusals, without requiring a formal or detailed purpose."""
    normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", reason.lower()).split())
    return bool(re.fullmatch(
        r"(?:no|none|no reason|nothing|unknown|not provided|not given|declined|refused|"
        r"(?:i )?(?:would |d )?rather not(?: say| tell you| give a reason)?(?: why| more)?|"
        r"i (?:don t|do not|won t|will not) (?:want to )?(?:say|tell you|give a reason)|"
        r"none of your business)"
        r"(?: please)?(?: (?:please )?(?:just )?(?:connect me|put me through))?",
        normalized,
    ))
