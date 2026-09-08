// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import axios from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import InboundCallHistory from './InboundCallHistory';
import CallHistoryPage from '../../pages/CallHistoryPage';

vi.mock('axios');
vi.mock('../../hooks/useConfirmDialog', () => ({ useConfirmDialog: () => ({ confirm: vi.fn() }) }));

const call = {
    call_id: '1700000000.123', caller_number: '+12025550123', caller_name: 'Example Caller', called_number: '+12025550124',
    started_at: '2023-11-14T22:13:20Z', answered_at: '2023-11-14T22:13:20.100Z', ended_at: '2023-11-14T22:13:22Z',
    duration_seconds: 2, outcome: 'caller_hung_up_during_greeting', operator_zero_started: true, caller_spoke: true,
    transcript_available: true, recording_available: true, audit_status: 'observed', disconnect_initiator: 'caller_hung_up',
    hangup_cause: 16, hangup_cause_text: 'Normal clearing', asterisk_unique_id: '1700000000.123', asterisk_linked_id: '1700000000.123', twilio_call_sid: null,
    transcript_preview: { speaker: 'caller', text: 'Hello? Is Alex', is_final: false },
    transcript_segments: [
        { id: '1', speaker: 'operator_zero', text: 'Operator Zero. How may I help?', start_offset_ms: 100, end_offset_ms: 1600, confidence: null, is_final: true },
        { id: '2', speaker: 'caller', text: 'Hello? Is Alex', start_offset_ms: 800, end_offset_ms: 1900, confidence: null, is_final: false },
    ],
    recordings: [{ track: 'mixed', status: 'available' }, { track: 'caller', status: 'available' }],
    events: [{ timestamp: '2023-11-14T22:13:22Z', event_type: 'channel_ended', source: 'cel', details: { disconnect_initiator: 'caller_hung_up' } }],
};
const listing = { calls: [call], total: 21, total_pages: 2 };

function mount(path = '/history?view=inbound', wrapper = false) {
    return render(<MemoryRouter initialEntries={[path]}>{wrapper ? <CallHistoryPage /> : <InboundCallHistory />}</MemoryRouter>);
}

beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:recording'), revokeObjectURL: vi.fn() }));
    vi.mocked(axios.get).mockImplementation(async url => {
        if (url === '/api/call-audit/status') return { data: { enabled: true, available: true } };
        if (url === '/api/call-audit/calls') return { data: listing };
        if (url === `/api/call-audit/calls/${call.call_id}`) return { data: call };
        if (String(url).includes('/recordings/')) return { data: new Blob(['RIFF'], { type: 'audio/wav' }) };
        // Existing AI history remains mounted only until audit availability resolves.
        if (url === '/api/calls') return { data: { calls: [], total: 0, total_pages: 1 } };
        if (url === '/api/agents') return { data: [] };
        return { data: null };
    });
});

describe('Inbound call history', () => {
    it('shows the original called number, short duration, outcome and caller preview', async () => {
        mount();
        const row = await screen.findByRole('link', { name: /Example Caller/ });
        expect(row).toHaveTextContent('+12025550124');
        expect(row).toHaveTextContent('2 seconds');
        expect(row).toHaveTextContent('Caller hung up during greeting');
        expect(row).toHaveTextContent('Hello? Is Alex');
        expect(row).toHaveTextContent('(partial)');
    });
    it('opens shareable call details and keeps speakers, evidence and partial status separate', async () => {
        mount();
        fireEvent.click(await screen.findByRole('link', { name: /Example Caller/ }));
        const detail = await screen.findByRole('region', { name: 'Inbound call details' });
        expect(await within(detail).findByText('Operator Zero. How may I help?')).toBeInTheDocument();
        expect(within(detail).getByText('Hello? Is Alex')).toBeInTheDocument();
        expect(within(detail).getByText(/Partial transcript/)).toBeInTheDocument();
        expect(within(detail).getByText('Caller: [hangup]')).toBeInTheDocument();
        expect(within(detail).getByText('16 — Normal clearing')).toBeInTheDocument();
        expect(within(detail).getByRole('region', { name: 'Event timeline' })).toHaveTextContent('channel ended');
        fireEvent.click(within(detail).getByRole('link', { name: 'Back to inbound calls' }));
        expect(await screen.findByRole('heading', { name: 'Recent inbound calls' })).toBeInTheDocument();
    });
    it('loads a deep-linked call without requiring the list', async () => {
        mount(`/history?view=inbound&call_id=${call.call_id}`);
        expect(await screen.findByText('Hello? Is Alex')).toBeInTheDocument();
        expect(vi.mocked(axios.get).mock.calls.some(([url]) => url === '/api/call-audit/calls')).toBe(false);
    });
    it('searches transcript text and filters outcomes, resetting pagination', async () => {
        mount();
        await screen.findByRole('link', { name: /Example Caller/ });
        fireEvent.click(screen.getByRole('button', { name: 'Next' }));
        await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/call-audit/calls', expect.objectContaining({ params: expect.objectContaining({ page: 2 }) })));
        fireEvent.change(screen.getByRole('textbox', { name: 'Search calls' }), { target: { value: 'Alex' } });
        fireEvent.click(screen.getByRole('button', { name: 'Search' }));
        await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/call-audit/calls', expect.objectContaining({ params: expect.objectContaining({ page: 1, search: 'Alex' }) })));
        fireEvent.change(screen.getByRole('combobox', { name: 'Outcome' }), { target: { value: 'no_speech' } });
        await waitFor(() => expect(axios.get).toHaveBeenCalledWith('/api/call-audit/calls', expect.objectContaining({ params: expect.objectContaining({ outcome: 'no_speech', page: 1 }) })));
    });
    it('loads audio with an authenticated Axios request and releases it on navigation', async () => {
        mount(`/history?view=inbound&call_id=${call.call_id}`);
        fireEvent.click(await screen.findByRole('button', { name: 'Play recording' }));
        expect(await screen.findByLabelText('Call recording')).toHaveAttribute('src', 'blob:recording');
        expect(axios.get).toHaveBeenCalledWith(`/api/call-audit/calls/${call.call_id}/recordings/mixed`, expect.objectContaining({ responseType: 'blob', signal: expect.any(AbortSignal) }));
        fireEvent.change(screen.getByRole('combobox', { name: 'Audio track' }), { target: { value: 'caller' } });
        expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:recording');
        expect(screen.queryByLabelText('Call recording')).not.toBeInTheDocument();
    });
    it('shows an explicit unavailable state and permits retry', async () => {
        vi.mocked(axios.get).mockRejectedValueOnce(new Error('Unavailable'));
        mount();
        expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable');
        fireEvent.click(screen.getByRole('button', { name: 'Refresh calls' }));
        expect(await screen.findByRole('link', { name: /Example Caller/ })).toBeInTheDocument();
    });
    it('does not invent speech or zero duration for incomplete evidence', async () => {
        vi.mocked(axios.get).mockResolvedValue({ data: { ...call, caller_spoke: null, duration_seconds: null, transcript_segments: [], recordings: [], disconnect_initiator: 'unknown_disconnect' } });
        mount(`/history?view=inbound&call_id=${call.call_id}`);
        expect(await screen.findByText('Unknown duration')).toBeInTheDocument();
        expect(screen.queryByText('Caller: [hangup]')).not.toBeInTheDocument();
        expect(screen.getByText(/No transcript available yet/)).toBeInTheDocument();
    });
    it('defaults the existing history page to inbound calls when audit is enabled', async () => {
        mount('/history', true);
        expect(await screen.findByRole('heading', { name: 'Recent inbound calls' })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: 'Inbound calls' })).toHaveAttribute('aria-pressed', 'true');
        fireEvent.click(screen.getByRole('button', { name: 'AI sessions' }));
        await waitFor(() => expect(screen.queryByRole('heading', { name: 'Recent inbound calls' })).not.toBeInTheDocument());
    });
});
