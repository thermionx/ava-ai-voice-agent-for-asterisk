"""Port-to-port contract replaces the former analog hangup/ringback contract."""
import pytest
from src.tools.telephony.dial_inside import DialInsideTool, LINES, TARGETS


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target', [(s,t) for s in LINES for t in TARGETS if s != t])
async def test_every_port_stays_connected_and_waits_for_announcement(tool_context, mock_ari_client, sample_call_session, source, target):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = source
    mock_ari_client.set_channel_var.return_value = True
    tool = DialInsideTool()
    result = await tool.execute({'target': target}, tool_context)
    assert result['status'] == 'success'
    assert 'stay on the line' in result['message']
    assert not result.get('waiting_for_hangup')
    assert 'hang up' not in result['message']
    assert all(c.args[1] != 'CHANNEL(hangup_handler_push)' for c in mock_ari_client.set_channel_var.await_args_list)
    mock_ari_client.continue_in_dialplan.assert_not_awaited()
    mock_ari_client.hangup_channel.assert_not_awaited()
    if source in {'102', '103'}:
        mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_RING_TARGET', '')
    result = await tool.commit_deferred_action(sample_call_session.pending_deferred_transfer, tool_context)
    assert result['status'] == 'success'
    mock_ari_client.continue_in_dialplan.assert_awaited_once_with(tool_context.caller_channel_id, context='operator-zero-local-call', extension=target, priority=1)
    mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_EXCLUDE', source)


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target,ctx', [(s,s,'operator_zero') for s in LINES] + [('100','9252165848','operator_zero'),('999','all','operator_zero'),('102','all','operator_zero_incoming'),('102','join','operator_zero_incoming')])
async def test_invalid_or_external_request_is_rejected(tool_context, mock_ari_client, source, target, ctx):
    tool_context.caller_number = source
    tool_context.context_name = ctx
    tool = DialInsideTool()
    assert (await tool.execute({'target': target}, tool_context))['status'] == 'error'
    assert (await tool.commit_deferred_action({'target': target}, tool_context))['status'] == 'error'
    mock_ari_client.set_channel_var.assert_not_awaited()
    mock_ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_replaces_pending_call_and_cancel_clears_it(tool_context, mock_ari_client, sample_call_session):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = True
    tool = DialInsideTool()
    await tool.execute({'target': 'all'}, tool_context)
    assert sample_call_session.pending_deferred_transfer['target'] == 'all'
    await tool.execute({'target': '101'}, tool_context)
    assert sample_call_session.pending_deferred_transfer['target'] == '101'
    result = await tool.execute({'target': 'cancel'}, tool_context)
    assert result['status'] == 'success'
    assert sample_call_session.pending_deferred_transfer is None
    mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_RING_TARGET', '')


@pytest.mark.asyncio
async def test_cancel_preserves_other_tools_pending_action(tool_context, mock_ari_client, sample_call_session):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = True
    pending = {'source_tool': 'dial_phone', 'target': 'external'}
    sample_call_session.pending_deferred_transfer = pending
    assert (await DialInsideTool().execute({'target': 'cancel'}, tool_context))['status'] == 'success'
    assert sample_call_session.pending_deferred_transfer == pending


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['all', '101', 'join'])
async def test_missing_dialplan_does_not_arm_call(tool_context, mock_ari_client, sample_call_session, target):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.dialplan_target_exists.return_value = False
    assert (await DialInsideTool().execute({'target': target}, tool_context))['status'] == 'error'
    assert sample_call_session.pending_deferred_transfer is None
    mock_ari_client.set_channel_var.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['all', '101', 'join', 'cancel'])
async def test_failed_legacy_ringback_clear_does_not_arm_call(tool_context, mock_ari_client, sample_call_session, target):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = False
    assert (await DialInsideTool().execute({'target': target}, tool_context))['status'] == 'error'
    assert sample_call_session.pending_deferred_transfer is None
    mock_ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_commit_variable_does_not_handoff(tool_context, mock_ari_client):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = False
    assert (await DialInsideTool().commit_deferred_action({'target': 'all'}, tool_context))['status'] == 'error'
    mock_ari_client.continue_in_dialplan.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_handoff_resets_transfer_state(tool_context, mock_ari_client, sample_call_session):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = True
    mock_ari_client.continue_in_dialplan.side_effect = None
    mock_ari_client.continue_in_dialplan.return_value = False
    assert (await DialInsideTool().commit_deferred_action({'target': 'all'}, tool_context))['status'] == 'error'
    assert sample_call_session.transfer_active is False
    assert sample_call_session.transfer_target is None
