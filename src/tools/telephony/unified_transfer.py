"""
Unified Transfer Tool - Transfer calls to extensions, queues, or ring groups.

This tool implements the canonical `blind_transfer` tool and replaces the legacy
`transfer_call` and `transfer_to_queue` tools with a single unified interface.
"""

from typing import Dict, Any, Optional, Tuple, List
import asyncio
import re
import structlog

from ..base import Tool, ToolDefinition, ToolParameter, ToolCategory
from ..context import ToolExecutionContext
from .deferred_transfer import (
    build_deferred_transfer_action,
    build_deferred_transfer_result,
    store_pending_deferred_transfer,
    transfer_deferral_enabled,
)
from ...core.operator_zero_state import OperatorZeroTransferState

logger = structlog.get_logger(__name__)


class UnifiedTransferTool(Tool):
    """
    Unified tool for transferring calls to various destinations:
    - Extensions: Direct SIP/PJSIP endpoints
    - Queues: ACD queues via FreePBX ext-queues context
    - Ring Groups: Ring groups via FreePBX ext-group context
    
    Note: Available destinations are configured in tools.transfer.destinations
    and validated at execution time.
    """
    
    @property
    def definition(self) -> ToolDefinition:
        """Return tool definition."""
        return ToolDefinition(
            name="blind_transfer",
            description=(
                "Blind transfer the caller to another configured destination. "
                "Supports Transfer Destinations of type extension, queue, and ring group. "
                "Use a configured destination key from Tools -> Transfer Destinations. "
                "The system validates that the destination exists before transferring. "
                "Prefer exact destination keys exposed in the runtime prompt/context instead of inventing names."
            ),
            category=ToolCategory.TELEPHONY,
            requires_channel=True,
            max_execution_time=30,
            parameters=[
                ToolParameter(
                    name="destination",
                    type="string",
                    description=(
                        "Configured Transfer Destinations key or close match "
                        "(matched against destination key/description)."
                    ),
                    required=True
                ),
                ToolParameter(
                    name="caller_name",
                    type="string",
                    description=(
                        "Personal name the caller gave during screening. "
                        "Include it when known."
                    ),
                    required=False
                ),
                ToolParameter(
                    name="company",
                    type="string",
                    description=(
                        "Company, organization, emergency service, hospital, "
                        "police/fire department, or other organization the "
                        "caller identified, when known."
                    ),
                    required=False
                ),
                ToolParameter(
                    name="recipient",
                    type="string",
                    description=(
                        "Household person or household/family role the caller "
                        "asked for, when known."
                    ),
                    required=False
                ),
                ToolParameter(
                    name="reason",
                    type="string",
                    description=(
                        "Specific emergency or official reason for the call, "
                        "when the caller is not asking for a named person."
                    ),
                    required=False
                )
            ]
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return " ".join(str(value or "").strip().lower().replace("_", " ").replace("-", " ").split())

    @classmethod
    def _screening_value_was_spoken(cls, value: str, history: List[Dict[str, Any]]) -> bool:
        def evidence_text(raw: Any) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", str(raw or "").lower()).split())

        needle = evidence_text(value)
        if not needle:
            return False
        caller_text = " ".join(
            evidence_text(message.get("content", ""))
            for message in history
            if isinstance(message, dict) and message.get("role") == "user"
        )
        return f" {needle} " in f" {caller_text} "

    @classmethod
    def _validate_operator_zero_screening(
        cls,
        metadata: Dict[str, str],
        history: List[Dict[str, Any]],
    ) -> Optional[str]:
        """Fail closed when the model invents incoming-call screening facts."""
        caller_name = metadata.get("caller_name", "")
        recipient = metadata.get("recipient", "")
        company = metadata.get("company", "")
        reason = metadata.get("reason", "")

        if caller_name and not cls._screening_value_was_spoken(caller_name, history):
            return "The caller's identity was not confirmed in the conversation."

        generic_recipients = {
            "household", "household member", "member of the household",
            "family", "family member", "head of household", "homeowner",
            "someone there", "anyone there",
        }
        recipient_normalized = cls._normalize_text(recipient)
        named_recipient = bool(recipient_normalized) and recipient_normalized not in generic_recipients
        if named_recipient:
            if not caller_name:
                return "The caller must identify themselves before a household transfer."
            if not cls._screening_value_was_spoken(recipient, history):
                return "The named recipient was not stated by the caller."
            return None

        # The only no-name exception is a caller-stated emergency or official
        # service purpose. Requiring both fields keeps a bare model assertion
        # from opening the household transfer boundary.
        official_terms = {
            "police", "sheriff", "fire department", "ambulance", "ems",
            "hospital", "doctor", "medical", "court", "government",
            "emergency services",
        }
        organization_is_official = any(
            term in cls._normalize_text(company) for term in official_terms
        )
        if (
            organization_is_official
            and reason
            and cls._screening_value_was_spoken(company, history)
            and cls._screening_value_was_spoken(reason, history)
        ):
            return None
        return "A specific named recipient or a confirmed emergency/official reason is required."

    @staticmethod
    def _resolve_dialplan_context(
        transfer_type: str,
        dest_config: Dict[str, Any],
        transfer_config: Dict[str, Any],
    ) -> str:
        configured = ""
        if isinstance(dest_config, dict):
            configured = str(dest_config.get("dialplan_context") or dest_config.get("context") or "").strip()
        if configured:
            return configured

        if transfer_type == "extension":
            return str((transfer_config or {}).get("extension_context") or "from-internal").strip() or "from-internal"
        if transfer_type == "queue":
            return str((transfer_config or {}).get("queue_context") or "ext-queues").strip() or "ext-queues"
        if transfer_type == "ringgroup":
            return str((transfer_config or {}).get("ringgroup_context") or "ext-group").strip() or "ext-group"
        return "from-internal"

    async def _defer_or_commit_transfer(
        self,
        *,
        context: ToolExecutionContext,
        source_tool: str,
        transfer_type: str,
        target: str,
        description: str,
        dialplan_context: str,
        destination_key: Optional[str] = None,
        screening_metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if transfer_deferral_enabled(context):
            try:
                session = await context.get_session()
                pending = getattr(session, "pending_deferred_transfer", None)
                if isinstance(pending, dict) and pending.get("kind") == "transfer":
                    same_pending_transfer = (
                        str(pending.get("transfer_type") or "").strip() == str(transfer_type or "").strip()
                        and str(pending.get("target") or "").strip() == str(target or "").strip()
                        and str(pending.get("dialplan_context") or "").strip() == str(dialplan_context or "").strip()
                    )
                    if not same_pending_transfer:
                        logger.warning(
                            "Conflicting deferred transfer request while another transfer is pending",
                            call_id=context.call_id,
                            existing_action_id=pending.get("id"),
                            existing_target=pending.get("target"),
                            existing_transfer_type=pending.get("transfer_type"),
                            requested_target=target,
                            requested_transfer_type=transfer_type,
                        )
                        return {
                            "status": "failed",
                            "message": "A transfer is already pending. Please wait for it to complete.",
                        }
                    logger.info(
                        "Suppressing duplicate deferred transfer request",
                        call_id=context.call_id,
                        existing_action_id=pending.get("id"),
                        existing_target=pending.get("target"),
                        requested_target=target,
                    )
                    return build_deferred_transfer_result(
                        action=pending,
                        message=f"Transferring you to {pending.get('description') or description} now.",
                        extra={
                            "destination": pending.get("target") or target,
                            "type": pending.get("transfer_type") or transfer_type,
                            "duplicate_suppressed": True,
                        },
                    )
            except Exception:
                logger.debug("Failed to check duplicate deferred transfer", call_id=context.call_id, exc_info=True)

        action = build_deferred_transfer_action(
            source_tool=source_tool,
            commit_tool="blind_transfer",
            transfer_type=transfer_type,
            target=target,
            description=description,
            dialplan_context=dialplan_context,
            destination_key=destination_key,
            payload=(
                {"operator_zero": dict(screening_metadata)}
                if screening_metadata
                else None
            ),
        )

        if transfer_deferral_enabled(context):
            await self._maybe_start_predial_transfer(context, action)
            await store_pending_deferred_transfer(context, action)
            return build_deferred_transfer_result(
                action=action,
                message=f"Transferring you to {description} now.",
                extra={
                    "destination": target,
                    "type": transfer_type,
                },
            )

        return await self.commit_deferred_action(action, context)

    def _deferred_strategy(self, context: ToolExecutionContext) -> str:
        transfer_config = context.get_config_value("tools.transfer") or {}
        if not isinstance(transfer_config, dict):
            return "drain_then_dial"
        strategy = str(transfer_config.get("deferred_strategy") or "drain_then_dial").strip().lower()
        if strategy in {"predial", "pre_dial", "pre-dial", "predial_then_bridge"}:
            return "predial_then_bridge"
        return "drain_then_dial"

    def _predial_endpoint_for_action(self, action: Dict[str, Any]) -> str:
        target = str(action.get("target") or "").strip()
        dialplan_context = str(action.get("dialplan_context") or "").strip()
        if not target or not dialplan_context:
            return ""
        return f"Local/{target}@{dialplan_context}"

    def _caller_id_for_predial(self, context: ToolExecutionContext) -> str:
        number = str(context.caller_number or "").strip()
        name = str(context.caller_name or "").strip()
        if name and number:
            safe_name = name.replace('"', "").replace("<", "").replace(">", "").strip()
            return f'"{safe_name}" <{number}>'
        return number or name or ""

    async def _maybe_start_predial_transfer(
        self,
        context: ToolExecutionContext,
        action: Dict[str, Any],
    ) -> None:
        if self._deferred_strategy(context) != "predial_then_bridge":
            return

        endpoint = self._predial_endpoint_for_action(action)
        if not endpoint:
            logger.warning("Predial transfer skipped - endpoint unavailable", call_id=context.call_id, action=action)
            return

        app = str(context.get_config_value("asterisk.app_name", "asterisk-ai-voice-agent") or "asterisk-ai-voice-agent")
        transfer_config = context.get_config_value("tools.transfer") or {}
        try:
            dial_timeout_sec = int((transfer_config if isinstance(transfer_config, dict) else {}).get("predial_timeout_seconds", 30) or 30)
        except (TypeError, ValueError):
            dial_timeout_sec = 30

        destination_key = str(action.get("destination_key") or action.get("target") or "").strip()

        action_payload = (
            action.get("payload")
            if isinstance(action.get("payload"), dict)
            else {}
        )
        operator_zero_metadata = (
            action_payload.get("operator_zero")
            if isinstance(action_payload.get("operator_zero"), dict)
            else {}
        )

        try:
            session = await context.get_session()
            predial_action = {
                "type": "predial_transfer",
                "deferred_action_id": action.get("id"),
                "destination_key": destination_key,
                "target": action.get("target"),
                "target_name": action.get("description"),
                "transfer_type": action.get("transfer_type"),
                "dialplan_context": action.get("dialplan_context"),
                "endpoint": endpoint,
                "answered": False,
                "ready_to_bridge": False,
                "bridged": False,
                "caller_name": str(
                    operator_zero_metadata.get("caller_name") or ""
                ).strip(),
                "business_name": str(
                    operator_zero_metadata.get("company") or ""
                ).strip(),
                "recipient": str(
                    operator_zero_metadata.get("recipient") or ""
                ).strip(),
            }
            session.current_action = (
                OperatorZeroTransferState.start(predial_action).action
                if operator_zero_metadata
                else predial_action
            )
            await context.session_store.upsert_call(session)
        except Exception:
            logger.debug("Failed to persist predial transfer action state", call_id=context.call_id, exc_info=True)

        try:
            result = await context.ari_client.send_command(
                method="POST",
                resource="channels",
                data={
                    "variables": {
                        "AGENT_ACTION": "predial_transfer",
                        "AGENT_CALL_ID": context.call_id,
                        "AGENT_TARGET": str(action.get("target") or ""),
                        "AAVA_TRANSFER_DESTINATION_KEY": destination_key,
                    }
                },
                params={
                    "endpoint": endpoint,
                    "app": app,
                    "appArgs": f"predial-transfer,{context.call_id},{destination_key}",
                    "callerId": self._caller_id_for_predial(context),
                    "timeout": dial_timeout_sec,
                },
            )
        except Exception:
            logger.warning("Predial transfer originate failed", call_id=context.call_id, endpoint=endpoint, exc_info=True)
            return

        if not isinstance(result, dict) or not result.get("id"):
            logger.warning("Predial transfer originate returned no channel", call_id=context.call_id, endpoint=endpoint, response=result)
            return

        predial_channel_id = str(result["id"])
        action["payload"] = {
            **(action.get("payload") if isinstance(action.get("payload"), dict) else {}),
            "predial": {
                "enabled": True,
                "endpoint": endpoint,
                "channel_id": predial_channel_id,
                "destination_key": destination_key,
            },
        }
        try:
            session = await context.get_session()
            if isinstance(session.current_action, dict) and session.current_action.get("type") == "predial_transfer":
                session.current_action["predial_channel_id"] = predial_channel_id
                await context.session_store.upsert_call(session)
            engine = getattr(context.ari_client, "engine", None)
            if engine and hasattr(engine, "register_predial_transfer_channel"):
                engine.register_predial_transfer_channel(context.call_id, predial_channel_id)
        except Exception:
            logger.debug("Failed to register predial transfer channel", call_id=context.call_id, predial_channel_id=predial_channel_id, exc_info=True)

        logger.info(
            "Predial transfer leg originated",
            call_id=context.call_id,
            endpoint=endpoint,
            predial_channel_id=predial_channel_id,
            destination_key=destination_key,
        )

        # Operator Zero:
        # Prepare the private announcement while the destination phone is
        # ringing. This removes OpenAI Speech synthesis latency from the
        # interval after the household answers.
        caller_name = str(
            operator_zero_metadata.get("caller_name") or ""
        ).strip()
        business_name = str(
            operator_zero_metadata.get("company") or ""
        ).strip()

        if caller_name or business_name:
            if caller_name and business_name:
                announcement_identity = f"{caller_name} from {business_name}"
            else:
                announcement_identity = caller_name or business_name

            announcement_text = f"{announcement_identity} is on the line."

            engine = getattr(context.ari_client, "engine", None)
            if engine and hasattr(engine, "_local_ai_server_tts"):

                async def _prepare_operator_zero_announcement():
                    try:
                        audio = await engine._local_ai_server_tts(
                            call_id=context.call_id,
                            text=announcement_text,
                            timeout_sec=8.0,
                        )

                        if not audio:
                            return

                        cache = getattr(
                            engine,
                            "_operator_zero_predial_announcement_cache",
                            None,
                        )
                        if not isinstance(cache, dict):
                            cache = {}
                            setattr(
                                engine,
                                "_operator_zero_predial_announcement_cache",
                                cache,
                            )

                        cache[context.call_id] = {
                            "text": announcement_text,
                            "audio": audio,
                        }

                        logger.info(
                            "Operator Zero predial announcement prepared while ringing",
                            call_id=context.call_id,
                            caller_name=caller_name or None,
                            business_name=business_name or None,
                            announcement=announcement_text,
                            audio_bytes=len(audio),
                        )

                    except Exception:
                        logger.warning(
                            "Operator Zero predial announcement preparation failed",
                            call_id=context.call_id,
                            exc_info=True,
                        )

                asyncio.create_task(
                    _prepare_operator_zero_announcement(),
                    name=f"operator-zero-announcement-{context.call_id}",
                )

    async def prepare_or_execute_extension_transfer(
        self,
        context: ToolExecutionContext,
        extension: str,
        description: str,
        *,
        source_tool: str = "blind_transfer",
        destination_key: Optional[str] = None,
        dest_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        transfer_config = context.get_config_value("tools.transfer") or {}
        dialplan_context = self._resolve_dialplan_context(
            "extension",
            dest_config or {},
            transfer_config if isinstance(transfer_config, dict) else {},
        )
        return await self._defer_or_commit_transfer(
            context=context,
            source_tool=source_tool,
            transfer_type="extension",
            target=extension,
            description=description,
            dialplan_context=dialplan_context,
            destination_key=destination_key,
        )

    def _resolve_destination_key(self, destination: Any, destinations: Dict[str, Any]) -> Tuple[Optional[str], str]:
        raw = str(destination or "").strip()
        if not raw:
            return None, "empty_input"

        if raw in destinations:
            return raw, "exact_key"

        normalized = self._normalize_text(raw)

        # Case-insensitive exact key match.
        for key in destinations.keys():
            if self._normalize_text(key) == normalized:
                return str(key), "casefold_key"

        # Key prefix/contains matching.
        for key in destinations.keys():
            key_norm = self._normalize_text(key)
            if key_norm.startswith(normalized) or normalized in key_norm:
                return str(key), "partial_key"

        # Direct target number match (e.g., destination="6000" should map
        # to a configured key such as "support_agent" with target=6000).
        target_matches: List[str] = []
        for key, cfg in destinations.items():
            if not isinstance(cfg, dict):
                continue
            target_norm = self._normalize_text(str(cfg.get("target", "")))
            if target_norm and target_norm == normalized:
                target_matches.append(str(key))
        if len(target_matches) == 1:
            return target_matches[0], "exact_target"
        if len(target_matches) > 1:
            return None, "ambiguous_target"

        # Description prefix/contains matching.
        for key, cfg in destinations.items():
            if not isinstance(cfg, dict):
                continue
            description_norm = self._normalize_text(cfg.get("description", ""))
            if description_norm and (description_norm.startswith(normalized) or normalized in description_norm):
                return str(key), "partial_description"

        # Multi-word fallback (e.g., "live agent"): all tokens must match key or description.
        tokens = [t for t in normalized.split() if t]
        if tokens:
            token_matches = []
            for key, cfg in destinations.items():
                if not isinstance(cfg, dict):
                    continue
                haystack = f"{self._normalize_text(key)} {self._normalize_text(cfg.get('description', ''))}".strip()
                if all(token in haystack for token in tokens):
                    token_matches.append(str(key))
            if len(token_matches) == 1:
                return token_matches[0], "token_match"
            if len(token_matches) > 1:
                return None, "ambiguous_token_match"

        # Generic "human transfer" fallback:
        # If the user asks for a person/agent and exactly one extension destination exists,
        # use that destination.
        human_intent_tokens = {"agent", "human", "person", "representative", "rep", "operator", "live"}
        if any(t in human_intent_tokens for t in tokens):
            extension_keys = [
                str(key)
                for key, cfg in destinations.items()
                if isinstance(cfg, dict) and str(cfg.get("type", "")).strip().lower() == "extension"
            ]
            if len(extension_keys) == 1:
                return extension_keys[0], "single_extension_human_fallback"

        return None, "no_match"
    
    async def execute(
        self,
        parameters: Dict[str, Any],
        context: ToolExecutionContext
    ) -> Dict[str, Any]:
        """
        Execute transfer to the specified destination.
        
        Args:
            parameters: {destination: str}
            context: Tool execution context
        
        Returns:
            Dict with status and message
        """
        # Support both 'destination' (canonical) and 'target' (ElevenLabs uses this)
        destination = parameters.get('destination') or parameters.get('target')

        screening_metadata = {
            key: str(parameters.get(key) or "").strip()
            for key in ("caller_name", "company", "recipient", "reason")
            if str(parameters.get(key) or "").strip()
        }

        if screening_metadata:
            logger.info(
                "Operator Zero screening metadata received with transfer",
                call_id=context.call_id,
                caller_name=screening_metadata.get("caller_name"),
                company=screening_metadata.get("company"),
                recipient=screening_metadata.get("recipient"),
            )

        if self._normalize_text(context.context_name or "") == "operator zero incoming":
            session = await context.get_session()
            screening_error = self._validate_operator_zero_screening(
                screening_metadata,
                list(getattr(session, "conversation_history", None) or []),
            )
            if screening_error:
                logger.warning(
                    "Operator Zero rejected unsupported incoming transfer",
                    call_id=context.call_id,
                    reason=screening_error,
                    metadata=screening_metadata,
                )
                return {"status": "failed", "message": screening_error}
        
        # Get destinations from config via context
        config = context.get_config_value("tools.transfer") or {}
        if isinstance(config, dict) and config.get("enabled") is False:
            logger.info("Unified transfer tool disabled by config", call_id=context.call_id)
            return {
                "status": "failed",
                "message": "Transfer service is disabled",
            }
        destinations = (config.get('destinations') or {}) if isinstance(config, dict) else {}
        if not destinations:
            logger.warning("Unified transfer tool not configured", call_id=context.call_id)
            return {
                "status": "failed",
                "message": "Transfer service is not available",
            }
        
        # Resolve exact / fuzzy destination name without hardcoded destination keys.
        if destination and destination not in destinations:
            matched, match_reason = self._resolve_destination_key(destination, destinations)
            if matched:
                dest_cfg = destinations.get(matched) if isinstance(destinations, dict) else {}
                logger.info(
                    "Resolved destination alias",
                    call_id=context.call_id,
                    original=destination,
                    matched=matched,
                    reason=match_reason,
                    matched_type=(dest_cfg or {}).get("type"),
                    matched_target=(dest_cfg or {}).get("target"),
                )
                destination = matched
            else:
                destination_debug = []
                for key, cfg in destinations.items():
                    if not isinstance(cfg, dict):
                        continue
                    destination_debug.append(
                        {
                            "key": str(key),
                            "type": str(cfg.get("type", "")),
                            "target": str(cfg.get("target", "")),
                            "description": str(cfg.get("description", ""))[:80],
                        }
                    )
                logger.warning(
                    "Transfer destination resolution failed",
                    call_id=context.call_id,
                    requested_destination=destination,
                    reason=match_reason,
                    configured_destinations=destination_debug[:12],
                )

        # Validate destination exists
        if destination not in destinations:
            available_keys = [str(k) for k in destinations.keys()]
            logger.error(
                "Invalid destination",
                call_id=context.call_id,
                destination=destination,
                available=available_keys,
            )
            available_hint = ", ".join(sorted(available_keys)[:12])
            message = f"Unknown destination: {destination}"
            if available_hint:
                message += f". Available destinations: {available_hint}"
            return {
                "status": "failed",
                "message": message
            }
        
        dest_config = destinations[destination] or {}
        transfer_type = dest_config.get('type')
        target = dest_config.get('target')
        description = dest_config.get('description', destination)
        
        logger.info(
            "Transfer requested",
            call_id=context.call_id,
            destination=destination,
            type=transfer_type,
            target=target
        )

        if transfer_type in {'vicidial_extension', 'vicidial_ingroup'}:
            from .vicidial import execute_vicidial_transfer

            return await execute_vicidial_transfer(
                context=context,
                destination={
                    **dest_config,
                    "type": transfer_type,
                    "description": description,
                },
            )
        
        dialplan_context = self._resolve_dialplan_context(
            str(transfer_type or ""),
            dest_config if isinstance(dest_config, dict) else {},
            config if isinstance(config, dict) else {},
        )

        # Route based on transfer type
        if transfer_type == 'extension':
            return await self._defer_or_commit_transfer(
                context=context,
                source_tool="blind_transfer",
                transfer_type="extension",
                target=target,
                description=description,
                dialplan_context=dialplan_context,
                destination_key=str(destination),
                screening_metadata=screening_metadata,
            )
        elif transfer_type == 'queue':
            return await self._defer_or_commit_transfer(
                context=context,
                source_tool="blind_transfer",
                transfer_type="queue",
                target=target,
                description=description,
                dialplan_context=dialplan_context,
                destination_key=str(destination),
                screening_metadata=screening_metadata,
            )
        elif transfer_type == 'ringgroup':
            return await self._defer_or_commit_transfer(
                context=context,
                source_tool="blind_transfer",
                transfer_type="ringgroup",
                target=target,
                description=description,
                dialplan_context=dialplan_context,
                destination_key=str(destination),
                screening_metadata=screening_metadata,
            )
        else:
            logger.error("Invalid transfer type", type=transfer_type)
            return {
                "status": "failed",
                "message": f"Invalid transfer type: {transfer_type}"
            }

    async def commit_deferred_action(
        self,
        action: Dict[str, Any],
        context: ToolExecutionContext,
    ) -> Dict[str, Any]:
        transfer_type = str(action.get("transfer_type") or "").strip()
        target = str(action.get("target") or "").strip()
        description = str(action.get("description") or target or "").strip()
        dialplan_context = str(action.get("dialplan_context") or "").strip()
        payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
        predial = payload.get("predial") if isinstance(payload.get("predial"), dict) else None

        if predial and predial.get("enabled"):
            engine = getattr(context.ari_client, "engine", None)
            if engine and hasattr(engine, "finalize_predial_transfer"):
                result = await engine.finalize_predial_transfer(context, action)
                if result and result.get("status") == "success":
                    return result
                logger.warning(
                    "Predial transfer finalize failed; falling back to dialplan transfer",
                    call_id=context.call_id,
                    result=result,
                )

        if transfer_type == "extension":
            return await self._transfer_to_extension(context, target, description, dialplan_context=dialplan_context)
        if transfer_type == "queue":
            return await self._transfer_to_queue(context, target, description, dialplan_context=dialplan_context)
        if transfer_type == "ringgroup":
            return await self._transfer_to_ringgroup(context, target, description, dialplan_context=dialplan_context)

        return {
            "status": "failed",
            "message": f"Invalid deferred transfer type: {transfer_type}",
        }
    
    async def _transfer_to_extension(
        self,
        context: ToolExecutionContext,
        extension: str,
        description: str,
        *,
        dialplan_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transfer to a direct extension using ARI redirect.
        Channel stays in Stasis, so cleanup waits naturally.
        
        Args:
            context: Execution context
            extension: Extension number
            description: Human-readable description
        
        Returns:
            Result dict
        """
        logger.info("Extension transfer", call_id=context.call_id, 
                   extension=extension, description=description)
        
        # Get dialplan context for extension transfers (default: from-internal for FreePBX)
        config = context.get_config_value("tools.transfer") or {}
        dialplan_context = (
            str(dialplan_context or "").strip()
            or self._resolve_dialplan_context("extension", {}, config if isinstance(config, dict) else {})
        )
        
        return await self._continue_to_dialplan(
            context=context,
            target=str(extension or "").strip(),
            description=description,
            transfer_type="extension",
            dialplan_context=dialplan_context,
            transfer_state="transferring",
        )
    
    async def _transfer_to_queue(
        self,
        context: ToolExecutionContext,
        queue: str,
        description: str,
        *,
        dialplan_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transfer to a queue using ARI continue to FreePBX ext-queues context.
        Channel leaves Stasis, so we must set transfer_active flag first.
        
        Args:
            context: Execution context
            queue: Queue number/name
            description: Human-readable description
        
        Returns:
            Result dict
        """
        logger.info("Queue transfer", call_id=context.call_id,
                   queue=queue, description=description)

        config = context.get_config_value("tools.transfer") or {}
        dialplan_context = (
            str(dialplan_context or "").strip()
            or self._resolve_dialplan_context("queue", {}, config if isinstance(config, dict) else {})
        )
        
        return await self._continue_to_dialplan(
            context=context,
            target=str(queue or "").strip(),
            description=description,
            transfer_type="queue",
            dialplan_context=dialplan_context,
            transfer_state="in_queue",
        )
    
    async def _transfer_to_ringgroup(
        self,
        context: ToolExecutionContext,
        ringgroup: str,
        description: str,
        *,
        dialplan_context: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Transfer to a ring group using ARI continue to FreePBX ext-group context.
        Channel leaves Stasis, so we must set transfer_active flag first.
        
        Args:
            context: Execution context
            ringgroup: Ring group number
            description: Human-readable description
        
        Returns:
            Result dict
        """
        logger.info("Ring group transfer", call_id=context.call_id,
                   ringgroup=ringgroup, description=description)

        config = context.get_config_value("tools.transfer") or {}
        dialplan_context = (
            str(dialplan_context or "").strip()
            or self._resolve_dialplan_context("ringgroup", {}, config if isinstance(config, dict) else {})
        )
        
        return await self._continue_to_dialplan(
            context=context,
            target=str(ringgroup or "").strip(),
            description=description,
            transfer_type="ringgroup",
            dialplan_context=dialplan_context,
            transfer_state="in_ringgroup",
        )

    async def _continue_to_dialplan(
        self,
        *,
        context: ToolExecutionContext,
        target: str,
        description: str,
        transfer_type: str,
        dialplan_context: str,
        transfer_state: str,
    ) -> Dict[str, Any]:
        """Validate and hand caller ownership to a concrete dialplan target."""
        priority = 1
        target_exists: Optional[bool] = None
        validator = getattr(context.ari_client, "dialplan_target_exists", None)
        if callable(validator):
            try:
                target_exists = await validator(
                    context.caller_channel_id,
                    context=dialplan_context,
                    extension=target,
                    priority=priority,
                )
            except Exception:
                logger.warning(
                    "Dialplan target validation unavailable",
                    call_id=context.call_id,
                    transfer_type=transfer_type,
                    target=target,
                    context=dialplan_context,
                    exc_info=True,
                )

        if target_exists is False:
            logger.error(
                "Transfer dialplan target does not exist",
                call_id=context.call_id,
                transfer_type=transfer_type,
                target=target,
                context=dialplan_context,
                priority=priority,
            )
            return {
                "status": "failed",
                "message": f"Transfer destination {description} is not available.",
                "destination": target,
                "type": transfer_type,
            }

        if target_exists is not True:
            logger.warning(
                "Dialplan target could not be pre-validated; attempting guarded transfer",
                call_id=context.call_id,
                transfer_type=transfer_type,
                target=target,
                context=dialplan_context,
                priority=priority,
            )

        session = await context.get_session()
        previous_transfer_state = {
            "transfer_active": bool(getattr(session, "transfer_active", False)),
            "transfer_state": getattr(session, "transfer_state", None),
            "transfer_target": getattr(session, "transfer_target", None),
        }

        # StasisEnd can race the HTTP response. Claim transfer ownership before
        # continue so normal cleanup cannot hang up a successfully handed-off leg.
        await context.update_session(
            transfer_active=True,
            transfer_state=transfer_state,
            transfer_target=description,
        )

        try:
            handed_off = await context.ari_client.continue_in_dialplan(
                context.caller_channel_id,
                context=dialplan_context,
                extension=target,
                priority=priority,
            )
        except Exception:
            handed_off = None
            logger.warning(
                "Transfer dialplan handoff raised",
                call_id=context.call_id,
                transfer_type=transfer_type,
                target=target,
                context=dialplan_context,
                exc_info=True,
            )

        handoff_indeterminate = handed_off is not True and handed_off is not False

        if handed_off is True:
            logger.info(
                "Dialplan transfer initiated",
                call_id=context.call_id,
                transfer_type=transfer_type,
                target=target,
                context=dialplan_context,
            )
            return {
                "status": "success",
                "message": f"Transferring you to {description} now.",
                "destination": target,
                "type": transfer_type,
            }

        # Only an explicit rejection proves the channel remained under AAVA
        # ownership. An indeterminate response may have followed an accepted
        # handoff, so retain the transfer guard and avoid destructive cleanup.
        restore_ownership = handed_off is False
        if restore_ownership:
            try:
                await context.update_session(**previous_transfer_state)
            except Exception:
                logger.error(
                    "Failed to restore transfer ownership after unconfirmed handoff",
                    call_id=context.call_id,
                    transfer_type=transfer_type,
                    target=target,
                    context=dialplan_context,
                    exc_info=True,
                )
        else:
            logger.warning(
                "Transfer handoff outcome indeterminate; retaining transfer ownership",
                call_id=context.call_id,
                transfer_type=transfer_type,
                target=target,
                context=dialplan_context,
            )

        logger.error(
            "Transfer dialplan handoff was not confirmed",
            call_id=context.call_id,
            transfer_type=transfer_type,
            target=target,
            context=dialplan_context,
            handoff_indeterminate=handoff_indeterminate,
            ownership_restored=restore_ownership,
        )
        result = {
            "status": "failed",
            "message": f"Unable to transfer to {description}.",
            "destination": target,
            "type": transfer_type,
        }
        if handoff_indeterminate:
            result["handoff_indeterminate"] = True
        return result
