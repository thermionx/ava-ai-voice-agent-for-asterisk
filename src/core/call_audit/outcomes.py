"""Evidence-only classification; never imported by call-routing decisions."""


def classify(call, events):
    """Unknown speech/termination stays unknown; a SIP cause is not a speaker."""
    if not call.get("ended_at"):
        return "in_progress", "unknown_disconnect"
    end = call["ended_at"]
    evidence = []
    for event in events:
        details = event["details"]
        evidence_at = details.get("requested_at", event["timestamp"]) if event["event_type"] == "hangup_accepted" else event["timestamp"]
        if evidence_at > end:
            continue
        if event["event_type"] == "channel_ended":
            initiator = details.get("disconnect_initiator")
            if initiator in {"caller_hung_up", "asterisk_hung_up", "remote_sip_failure", "network_failure"}:
                evidence.append((event["timestamp"], initiator))
        elif event["event_type"] == "hangup_accepted":
            evidence.append((evidence_at, "operator_zero_hung_up"))
    # Earliest positive termination evidence wins. In a same-time race prefer
    # the observed channel cause over a control request acknowledgement.
    evidence.sort(key=lambda item: (item[0], item[1] == "operator_zero_hung_up"))
    initiator = evidence[0][1] if evidence else "unknown_disconnect"
    kinds = {event["event_type"] for event in events}
    if "transfer_completed" in kinds:
        return "transferred_to_inside_phone", initiator
    if "call_rejected" in kinds:
        return "rejected", initiator
    if initiator == "operator_zero_hung_up":
        return "operator_zero_terminated", initiator
    if "call_failed" in kinds or initiator in {"remote_sip_failure", "network_failure"}:
        return "failed", initiator
    if initiator == "caller_hung_up":
        greeting_start = [e["timestamp"] for e in events if e["event_type"] == "operator_zero_greeting_started"]
        greeting_end = [e["timestamp"] for e in events if e["event_type"] == "operator_zero_greeting_finished"]
        if greeting_start and min(greeting_start) <= end and not any(t <= end for t in greeting_end):
            return "caller_hung_up_during_greeting", initiator
        if "call_completed" in kinds:
            return "completed", initiator
        if call.get("caller_spoke") is False:
            return "no_speech", initiator
        if greeting_end or call.get("caller_spoke") is True:
            return "caller_hung_up_during_conversation", initiator
    if call.get("caller_spoke") is False:
        return "no_speech", initiator
    if "call_completed" in kinds:
        return "completed", initiator
    return "unknown", initiator
