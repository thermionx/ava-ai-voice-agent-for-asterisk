"""Normalize ARI observations without retaining raw headers or event payloads."""

from .events import observation

ARI_EVENTS = ("ChannelCreated", "ChannelDialplan", "ChannelStateChange", "ChannelVarset",
              "StasisStart", "StasisEnd", "ChannelDestroyed", "ChannelTalkingStarted")
INBOUND_CONTEXTS = {"from-twilio", "from-twilio-number"}
VARIABLES = {
    "OZ_AUDIT_CALLED_NUMBER": "called_number",
    "OZ_AUDIT_LINKEDID": "asterisk_linked_id",
    "OZ_AUDIT_TWILIO_SID": "twilio_call_sid",
    "OZ_AUDIT_SIP_CALL_ID": "sip_call_id",
}


def normalize(event, known):
    channel = event.get("channel") or {}
    channel_id = channel.get("id")
    name = channel.get("name", "")
    dialplan = channel.get("dialplan") or {}
    kind = event.get("type")
    result = []
    # A Twilio-named channel can also be OUTBOUND. Require inbound context,
    # then remember the original ID across dialplan handoffs.
    if (channel_id not in known and name.startswith("PJSIP/twilio-")
            and dialplan.get("context") in INBOUND_CONTEXTS):
        known[channel_id] = name
        caller = channel.get("caller") or {}
        result.append(observation(channel_id, "inbound_call_received",
                                  at=channel.get("creationtime") or event.get("timestamp"), source="ari",
                                  details={"caller_number": caller.get("number"), "caller_name": caller.get("name"),
                                           "called_number": dialplan.get("exten")}))
    if channel_id not in known:
        return result
    at = event.get("timestamp")
    if channel.get("state") == "Up" and kind in {"ChannelCreated", "ChannelStateChange", "StasisStart"}:
        result.append(observation(channel_id, "asterisk_answered", at=at, source="ari"))
    if kind == "ChannelVarset" and str(event.get("variable", "")).lstrip("_") in VARIABLES:
        key = VARIABLES[str(event["variable"]).lstrip("_")]
        result.append(observation(channel_id, "channel_metadata", at=at, source="ari",
                                  details={key: event.get("value"), "original_metadata": True}))
    elif kind == "ChannelDestroyed":
        # Cause 16 is normal clearing, NOT proof that the caller hung up.
        result.append(observation(channel_id, "channel_ended", at=at, source="ari",
                                  details={"hangup_cause": event.get("cause"),
                                           "hangup_cause_text": event.get("cause_txt")}))
    elif kind == "StasisEnd":
        result.append(observation(channel_id, "ai_session_ended", at=at, source="ari"))
    elif kind == "ChannelTalkingStarted":
        result.append(observation(channel_id, "caller_speech_started", at=at, source="ari"))
    return result
