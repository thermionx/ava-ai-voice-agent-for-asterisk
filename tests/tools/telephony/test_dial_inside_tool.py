import pytest
from unittest.mock import AsyncMock
from src.tools.telephony.dial_inside import DialInsideTool


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['100', '101'])
@pytest.mark.parametrize('target', ['all', '102', '103'])
async def test_sip_dial_waits_for_announcement_then_stays_local(tool_context, mock_ari_client, sample_call_session, source, target):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = source
    mock_ari_client.set_channel_var.return_value = True
    tool = DialInsideTool()
    result = await tool.execute({'target': target}, tool_context)
    assert 'stay on the line' in result['message']
    mock_ari_client.continue_in_dialplan.assert_not_awaited()
    result = await tool.commit_deferred_action(sample_call_session.pending_deferred_transfer, tool_context)
    assert result['status'] == 'success'
    assert mock_ari_client.continue_in_dialplan.call_args.kwargs['context'] == 'operator-zero-local-call'
    mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_EXCLUDE', source)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['102', '103'])
async def test_analog_arms_hangup_without_dial_or_forced_hangup(tool_context, mock_ari_client, source):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = source
    mock_ari_client.set_channel_var.return_value = True
    result = await DialInsideTool().execute({'target': 'all'}, tool_context)
    assert result['waiting_for_hangup'] is True
    assert 'hang up' in result['message']
    mock_ari_client.set_channel_var.assert_any_await(tool_context.caller_channel_id, 'CHANNEL(hangup_handler_push)', 'operator-zero-local-ringback,s,1')
    mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_RING_TARGET', 'all')
    mock_ari_client.continue_in_dialplan.assert_not_awaited()
    mock_ari_client.hangup_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_analog_retry_reuses_handler_and_cancel_clears_target(tool_context, mock_ari_client):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = True
    mock_ari_client.send_command = AsyncMock(return_value={'value': '1'})
    tool = DialInsideTool()
    await tool.execute({'target': '101'}, tool_context)
    assert all(c.args[1] != 'CHANNEL(hangup_handler_push)' for c in mock_ari_client.set_channel_var.await_args_list)
    result = await tool.execute({'target': 'cancel'}, tool_context)
    assert result['status'] == 'success'
    mock_ari_client.set_channel_var.assert_awaited_with(tool_context.caller_channel_id, 'OZ_LOCAL_RING_TARGET', '')


@pytest.mark.asyncio
@pytest.mark.parametrize('source,target,ctx', [('100','100','operator_zero'),('100','9252165848','operator_zero'),('999','all','operator_zero'),('102','all','operator_zero_incoming')])
async def test_invalid_or_external_request_is_rejected(tool_context, mock_ari_client, source, target, ctx):
    tool_context.caller_number = source
    tool_context.context_name = ctx
    result = await DialInsideTool().execute({'target': target}, tool_context)
    assert result['status'] == 'error'
    mock_ari_client.set_channel_var.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_handler_install_never_arms_ring(tool_context, mock_ari_client):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.set_channel_var.return_value = False
    result = await DialInsideTool().execute({'target':'all'}, tool_context)
    assert result['status'] == 'error'
    assert all(c.args[1] != 'OZ_LOCAL_RING_TARGET' for c in mock_ari_client.set_channel_var.await_args_list)


@pytest.mark.asyncio
async def test_missing_dialplan_does_not_arm_callback(tool_context, mock_ari_client):
    tool_context.context_name = 'operator_zero'
    tool_context.caller_number = '102'
    mock_ari_client.dialplan_target_exists.return_value = False
    result = await DialInsideTool().execute({'target':'all'}, tool_context)
    assert result['status'] == 'error'
    mock_ari_client.set_channel_var.assert_not_awaited()
