"""Internal voicemail retrieval handoff.

This tool does not implement voicemail itself. It defers the live call handoff
until caller-facing AI audio has completed, then returns the channel to an
Asterisk dialplan context where VoiceMailMain owns the interaction.
"""

from typing import Any, Dict

import structlog

from ..base import Tool, ToolDefinition, ToolParameter, ToolCategory
from ..context import ToolExecutionContext
from .deferred_transfer import (
    build_deferred_transfer_action,
    build_deferred_transfer_result,
    store_pending_deferred_transfer,
)

logger = structlog.get_logger(__name__)


class CheckVoicemailTool(Tool):

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="check_voicemail",
            description=(
                "Open the household voicemail message interface so the user can "
                "listen to, save, or delete existing voicemail messages. "
                "Use this when the household user asks to check, play, hear, "
                "retrieve, or listen to voicemail or messages."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=15,
            parameters=[],
        )

    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:

        action = build_deferred_transfer_action(
            source_tool="check_voicemail",
            commit_tool="check_voicemail",
            transfer_type="voicemail_main",
            target="s",
            description="voicemail",
            dialplan_context="operator-zero-voicemail-main",
        )

        await store_pending_deferred_transfer(context, action)

        logger.info(
            "Voicemail retrieval handoff armed",
            call_id=context.call_id,
            action_id=action.get("id"),
        )

        # Operator Zero speaks this before the deferred handoff commits.
        return build_deferred_transfer_result(
            action=action,
            message="Certainly.",
        )

    async def commit_deferred_action(
        self,
        action: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:

        dialplan_context = str(
            action.get("dialplan_context") or "operator-zero-voicemail-main"
        ).strip()
        target = str(action.get("target") or "s").strip()

        await context.update_session(
            transfer_active=True,
            transfer_target="Voicemail message retrieval",
        )

        try:
            logger.info(
                "Continuing internal call into voicemail retrieval",
                call_id=context.call_id,
                channel_id=context.caller_channel_id,
                context=dialplan_context,
                extension=target,
            )

            continued = await context.ari_client.continue_in_dialplan(
                context.caller_channel_id,
                context=dialplan_context,
                extension=target,
                priority=1,
            )

            if continued is False:
                raise RuntimeError("Asterisk rejected voicemail retrieval handoff")

            return {
                "status": "success",
                "message": "Voicemail interface opened",
            }

        except Exception as exc:
            await context.update_session(
                transfer_active=False,
                transfer_target=None,
            )

            logger.error(
                "Voicemail retrieval handoff failed",
                call_id=context.call_id,
                error=str(exc),
                exc_info=True,
            )

            return {
                "status": "failed",
                "message": "Unable to open voicemail",
            }
