"""Asterisk 22 advanced CEL JSON normalization, independent of AI sessions."""

import json
from .events import observation

CAUSE_TEXT = {
    16: "Normal clearing", 17: "User busy", 18: "No user responding",
    19: "No answer", 21: "Call rejected", 34: "No circuit/channel available",
    38: "Network out of order", 41: "Temporary failure", 42: "Switching congestion",
    47: "Resource unavailable", 58: "Bearer capability unavailable",
    88: "Incompatible destination", 102: "Recovery on timer expiry",
    111: "Protocol error", 127: "Interworking, unspecified",
}


def normalize(raw, known_call=False):
    row = {key.lower(): value for key, value in raw.items()}
    name = str(row.get("channame") or "")
    # Require positive inbound routing evidence. Known original channels remain
    # eligible after transfer changes the context; outbound Twilio legs do not.
    inbound = row.get("context") in {"from-twilio", "from-twilio-number"} or row.get("eventtype") == "OZ_INBOUND"
    if not name.startswith("PJSIP/twilio-") or not (inbound or known_call):
        return []
    call_id = str(row.get("uniqueid") or "")
    at = row.get("eventtime")
    # Our template explicitly emits an epoch double, independent of host TZ.
    at = float(at)
    kind = row.get("eventtype", "")
    extra = row.get("eventextra") or {}
    if isinstance(extra, str):
        extra = json.loads(extra) if extra.startswith("{") else {"value": extra}
    # Asterisk 22 wraps CELGenUserEvent's payload in {"extra": ...}.
    # Retain support for older/plain journal representations as well.
    if "extra" in extra and "value" not in extra:
        extra["value"] = extra["extra"]
    metadata = {"caller_number": row.get("num"), "caller_name": row.get("name"),
                "asterisk_linked_id": row.get("linkedid")}
    events = []
    if kind in {"CHAN_START", "OZ_INBOUND"}:
        metadata.update(called_number=extra.get("value") if kind == "OZ_INBOUND" else row.get("exten"), original_metadata=True)
        events.append(observation(call_id, "inbound_call_received", at=at, source="cel", details=metadata))
    elif kind == "OZ_TWILIO_SID":
        events.append(observation(call_id, "channel_metadata", at=at, source="cel",
                                  details={"twilio_call_sid": extra.get("value"), "original_metadata": True}))
    elif kind == "OZ_SIP_CALL_ID":
        events.append(observation(call_id, "channel_metadata", at=at, source="cel",
                                  details={"sip_call_id": extra.get("value"), "original_metadata": True}))
    elif kind == "ANSWER":
        events.append(observation(call_id, "asterisk_answered", at=at, source="cel"))
    elif kind in {"OZ_RECORDING_STARTED", "OZ_RECORDING_STOPPED", "OZ_RECORDING_FAILED"}:
        events.append(observation(call_id, {"OZ_RECORDING_STARTED": "recording_started",
                                            "OZ_RECORDING_STOPPED": "recording_stopped",
                                            "OZ_RECORDING_FAILED": "recording_failed"}[kind],
                                  at=at, source="cel", details={"recording_key": extra.get("value"),
                                                              "evidence": "dialplan_application_returned"}))
    elif kind in {"HANGUP", "CHAN_END"}:
        source = str(extra.get("hangupsource") or "")
        cause = extra.get("hangupcause")
        cause = int(cause) if cause is not None else None
        initiator = "unknown_disconnect"
        if source == name:
            initiator = "caller_hung_up"
        elif source.startswith("dialplan/"):
            initiator = "asterisk_hung_up"
        # An empty source or normal-clearing code cannot establish who ended it.
        events.append(observation(call_id, "channel_ended", at=at, source="cel", details={
            "hangup_cause": cause, "hangup_source": source,
            "hangup_cause_text": CAUSE_TEXT.get(cause, f"Asterisk cause {cause}") if cause is not None else None,
            "disconnect_initiator": initiator, "dial_status": extra.get("dialstatus"),
        }))
    elif kind == "BRIDGE_ENTER":
        peers = str(row.get("peer") or "").split(",")
        if any(p.strip().startswith("PJSIP/100-") for p in peers):
            events.append(observation(call_id, "transfer_completed", at=at, source="cel",
                                      details={"destination": "inside_phone", "evidence": "bridge_enter"}))
    return events
