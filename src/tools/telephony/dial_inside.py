"""Local port-to-port household calls; every caller stays connected."""
from typing import Any, Dict

from ..base import Tool, ToolCategory, ToolDefinition, ToolParameter
from .deferred_transfer import build_deferred_transfer_action, build_deferred_transfer_result, store_pending_deferred_transfer

LINES = {"100": "HS1", "101": "HS2", "102": "Analog Port 1", "103": "Analog Port 2"}
TARGETS = {"all": "the inside phones", "join": "the inside conversation", **LINES}
CONTEXT = "operator-zero-local-call"


class DialInsideTool(Tool):
    @property
    def definition(self):
        return ToolDefinition(
            name="dial_inside", category=ToolCategory.TELEPHONY, requires_channel=True,
            description=("Call house phones locally. Use all for 'dial inside line' or 'ring the house phones'; "
                         "100 for HS1, 101 for HS2, 102 for Analog Port 1, 103 for Analog Port 2. "
                         "Use all when the destination is vague. Use join to join an existing inside conversation. "
                         "Invoke BEFORE speaking. Speak the returned instructions exactly. All callers stay connected. "
                         "Use cancel to cancel a pending inside call. "
                         "Never use dial_phone or the outside house telephone number for this request."),
            parameters=[ToolParameter(name="target", type="string", required=True,
                                      enum=[*TARGETS, "cancel"], description="Local line or all house phones.")],
        )

    async def execute(self, parameters: Dict[str, Any], context):
        source = str(context.caller_number or "")
        target = str(parameters.get("target") or "")
        if context.context_name != "operator_zero" or source not in LINES or target not in {*TARGETS, "cancel"}:
            return {"status": "error", "message": "Local calling requires a recognized inside line and target."}
        ari = context.ari_client
        channel = context.caller_channel_id
        if target == "cancel":
            if await ari.set_channel_var(channel, "OZ_LOCAL_RING_TARGET", "") is not True:
                return {"status": "error", "message": "I could not confirm cancellation of the ring request."}
            session = await context.get_session()
            pending = getattr(session, "pending_deferred_transfer", None)
            if isinstance(pending, dict) and pending.get("source_tool") == "dial_inside":
                session.pending_deferred_transfer = None
                await context.session_store.upsert_call(session)
            return {"status": "success", "message": "The inside ringing request is cancelled."}
        if await ari.dialplan_target_exists(channel, context=CONTEXT, extension=target, priority=1) is not True:
            return {"status": "error", "message": "Inside calling is not installed in Asterisk."}
        if target == source:
            return {"status": "error", "message": f"You are already using {LINES[source]}. Please choose another line."}
        if source in {"102", "103"}:
            # A previous all/same-port request must not fire when this call ends.
            if await ari.set_channel_var(channel, "OZ_LOCAL_RING_TARGET", "") is not True:
                return {"status": "error", "message": "Unable to cancel the previous inside ringing request."}
        action = build_deferred_transfer_action(
            source_tool="dial_inside", commit_tool="dial_inside", transfer_type="inside_call",
            target=target, description=TARGETS[target], dialplan_context=CONTEXT,
        )
        await store_pending_deferred_transfer(context, action)
        verb = "Joining" if target == "join" else "Calling"
        return build_deferred_transfer_result(action=action, message=f"{verb} {TARGETS[target]} now. Please stay on the line.")

    async def commit_deferred_action(self, action, context):
        target = str(action.get("target") or "")
        source = str(context.caller_number or "")
        if (context.context_name != "operator_zero" or source not in LINES or target not in TARGETS or source == target):
            return {"status": "error", "message": "Invalid inside call target."}
        if await context.ari_client.set_channel_var(context.caller_channel_id, "OZ_LOCAL_EXCLUDE", source) is not True:
            return {"status": "error", "message": "Unable to prepare inside calling."}
        await context.update_session(transfer_active=True, transfer_target=TARGETS[target])
        continued = await context.ari_client.continue_in_dialplan(
            context.caller_channel_id, context=CONTEXT, extension=target, priority=1,
        )
        if continued is not True:
            await context.update_session(transfer_active=False, transfer_target=None)
            return {"status": "error", "message": "Unable to confirm the inside call handoff."}
        return {"status": "success", "message": "Inside call started."}
