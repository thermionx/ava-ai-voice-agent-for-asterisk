import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import axios from 'axios';

const outcomeLabels: Record<string, string> = {
    in_progress: 'In progress', completed: 'Completed',
    caller_hung_up_during_greeting: 'Caller hung up during greeting',
    caller_hung_up_during_conversation: 'Caller hung up during conversation',
    transferred_to_inside_phone: 'Transferred to inside phone',
    operator_zero_terminated: 'Operator Zero terminated', rejected: 'Rejected',
    failed: 'Failed', no_speech: 'No speech', unknown: 'Unknown',
};
const label = (value: string) => outcomeLabels[value] || value.replace(/_/g, ' ');
const eventTime = (value: string) => {
    const time = new Date(value);
    return `${time.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}.${String(time.getMilliseconds()).padStart(3, '0')}`;
};
const date = (value: string | null) => value ? new Date(value).toLocaleString() : 'Not recorded';
const duration = (value: number | null) => value == null ? 'Unknown duration' : value < 60 ? `${Number(value.toFixed(1))} seconds` : `${Math.floor(value / 60)}m ${Math.round(value % 60)}s`;
const offset = (value: number | null) => value == null ? 'Timing unavailable' : `${(value / 1000).toFixed(1)}s`;
const speakers: Record<string, string> = { caller: 'Caller', operator_zero: 'Operator Zero', system: 'System', unknown: 'Unknown speaker' };

interface Segment {
    id: string; speaker: string; text: string; start_offset_ms: number | null; end_offset_ms: number | null;
    confidence: number | null; is_final: boolean | number; timing_source: string | null; provider: string | null;
}
interface Call {
    call_id: string; caller_number: string | null; caller_name: string | null; called_number: string | null;
    started_at: string | null; answered_at: string | null; ended_at: string | null; duration_seconds: number | null;
    outcome: string; operator_zero_started: boolean; caller_spoke: boolean | null; transcript_available: boolean;
    recording_available: boolean; audit_status: string; disconnect_initiator: string;
    hangup_cause: number | null; hangup_cause_text: string | null;
    asterisk_unique_id: string; asterisk_linked_id: string | null; twilio_call_sid: string | null;
    transcript_preview?: { speaker: string; text: string; is_final: boolean | number } | null;
}
interface Detail extends Call {
    transcript_segments: Segment[];
    events: Array<{ timestamp: string; event_type: string; source: string; details: Record<string, unknown> }>;
    recordings: Array<{ track: string; status: string }>;
}
interface CallList { calls: Call[]; total: number; total_pages: number }

const button = 'rounded-md border border-border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50';

function RecordingPlayer({ callId, track }: { callId: string; track: string }) {
    const [url, setUrl] = useState<string | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(false);
    const objectUrl = useRef<string | null>(null);
    const controller = useRef<AbortController | null>(null);
    useEffect(() => () => {
        controller.current?.abort();
        if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    }, []);
    const load = async () => {
        controller.current?.abort();
        const request = new AbortController();
        controller.current = request;
        setLoading(true); setError(false);
        try {
            // Axios carries the existing bearer token. Never put tokens in URLs.
            const response = await axios.get(`/api/call-audit/calls/${encodeURIComponent(callId)}/recordings/${track}`, { responseType: 'blob', signal: request.signal });
            if (request.signal.aborted) return;
            if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
            objectUrl.current = URL.createObjectURL(response.data);
            setUrl(objectUrl.current);
        } catch {
            if (!request.signal.aborted) setError(true);
        } finally {
            if (!request.signal.aborted) setLoading(false);
        }
    };
    return <div className="space-y-2">
        {url ? <audio aria-label="Call recording" controls autoPlay src={url} className="w-full" /> :
            <button className={button} onClick={load} disabled={loading}>{loading ? 'Loading recording…' : 'Play recording'}</button>}
        {error && <p role="alert">Recording could not be loaded. It may no longer be available. Try again.</p>}
    </div>;
}

function CallDetail({ callId }: { callId: string }) {
    const [call, setCall] = useState<Detail | null>(null);
    const [error, setError] = useState('');
    const [revision, setRevision] = useState(0);
    const [track, setTrack] = useState('mixed');
    useEffect(() => {
        const controller = new AbortController();
        const fetchDetail = async () => {
            try {
                const response = await axios.get(`/api/call-audit/calls/${encodeURIComponent(callId)}`, { signal: controller.signal });
                if (!controller.signal.aborted) { setCall(response.data); setError(''); }
            } catch (error) {
                if (!controller.signal.aborted) setError(axios.isAxiosError(error) && error.response?.status === 404 ? 'Call not found.' : 'Call details are temporarily unavailable.');
            }
        };
        void fetchDetail();
        const timer = window.setInterval(() => { if (!document.hidden) void fetchDetail(); }, 15000);
        return () => { controller.abort(); window.clearInterval(timer); };
    }, [callId, revision]);
    const recordings = call?.recordings.filter(recording => recording.status === 'available') || [];
    const selectedTrack = recordings.some(recording => recording.track === track) ? track : recordings[0]?.track;
    return <section className="space-y-5" aria-label="Inbound call details">
        <div className="flex flex-wrap items-center justify-between gap-3">
            <Link className={button} to="/history?view=inbound">Back to inbound calls</Link>
            <button className={button} onClick={() => setRevision(value => value + 1)}>Refresh details</button>
        </div>
        <h1 className="text-2xl font-semibold">Call details</h1>
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {!call && !error && <p role="status">Loading call details…</p>}
        {call && <>
            <h2 className="text-lg font-medium">{label(call.outcome)}</h2>
            {call.audit_status === 'partial' && <p className="text-amber-600">Some call evidence is missing. Unknown values are shown explicitly.</p>}
            <dl className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 rounded-lg border border-border p-4">
                {Object.entries({ Caller: call.caller_number || 'Unknown', 'Caller ID name': call.caller_name || 'Not provided',
                    'Called number': call.called_number || 'Unknown', Started: date(call.started_at), Answered: date(call.answered_at),
                    Ended: call.ended_at ? date(call.ended_at) : 'Call end not yet recorded', Duration: duration(call.duration_seconds),
                    'Operator Zero session started': call.operator_zero_started ? 'Yes' : 'No',
                    'Caller spoke': call.caller_spoke == null ? 'Unknown' : call.caller_spoke ? 'Yes' : 'No speech detected',
                    'Disconnect initiator': label(call.disconnect_initiator),
                    'Asterisk hangup cause': call.hangup_cause == null ? 'Not recorded' : `${call.hangup_cause} — ${call.hangup_cause_text || 'Unknown cause'}`,
                    'Call ID / Asterisk UNIQUEID': call.asterisk_unique_id, 'Asterisk LINKEDID': call.asterisk_linked_id || 'Not provided',
                    'Twilio Call SID': call.twilio_call_sid || 'Not provided',
                }).map(([key, value]) => <div key={key}><dt className="text-sm text-muted-foreground">{key}</dt><dd className="break-words">{value}</dd></div>)}
            </dl>
            <section className="space-y-3" aria-label="Recording">
                <h2 className="text-lg font-semibold">Recording</h2>
                {selectedTrack ? <>
                    <label className="block">Audio track <select className="ml-2 rounded border border-border bg-background p-2" value={selectedTrack} onChange={event => setTrack(event.target.value)}>
                        {recordings.map(recording => <option key={recording.track} value={recording.track}>{{ mixed: 'Full call', caller: 'Caller only', sent: 'Audio sent to caller' }[recording.track] || recording.track}</option>)}
                    </select></label>
                    <RecordingPlayer key={`${callId}:${selectedTrack}`} callId={callId} track={selectedTrack} />
                </> : <p className="text-muted-foreground">No recording available yet. Recording may be disabled, processing, or unavailable.</p>}
            </section>
            <section className="space-y-3" aria-label="Conversation">
                <h2 className="text-lg font-semibold">Conversation</h2>
                <p className="text-sm text-muted-foreground">Times are approximate. Operator Zero text reflects generated speech; a hangup may cut playback short. Use the recording to verify what was heard.</p>
                {!call.transcript_segments.length && <p>No transcript available yet. The caller may not have spoken, or transcription may still be processing or unavailable.</p>}
                <ol className="space-y-3">
                    {call.transcript_segments.map(segment => <li key={segment.id} className={`rounded-lg border border-border p-3 ${segment.speaker === 'operator_zero' ? 'bg-primary/5' : 'bg-muted/30'}`}>
                        <div className="flex flex-wrap justify-between gap-2 text-sm"><strong>{speakers[segment.speaker] || 'Unknown speaker'}</strong><span>{offset(segment.start_offset_ms)} – {offset(segment.end_offset_ms)}</span></div>
                        <p className="whitespace-pre-wrap break-words mt-1">{segment.text}</p>
                        {!segment.is_final && <p className="text-xs text-amber-600">Partial transcript — may be incomplete or superseded by recovered speech.</p>}
                        {segment.confidence != null && <p className="text-xs text-muted-foreground">Confidence: {Math.round(segment.confidence * 100)}%</p>}
                    </li>)}
                    {call.disconnect_initiator === 'caller_hung_up' && <li className="text-sm text-muted-foreground">Caller: [hangup]</li>}
                </ol>
            </section>
            <section className="space-y-3" aria-label="Event timeline">
                <h2 className="text-lg font-semibold">Event timeline</h2>
                <ol className="space-y-2">
                    {call.events.map((event, index) => <li key={`${event.timestamp}:${index}`} className="rounded border border-border p-3 text-sm">
                        <div className="flex flex-wrap gap-2"><time dateTime={event.timestamp}>{eventTime(event.timestamp)}</time><strong>{label(event.event_type)}</strong></div>
                        {Object.keys(event.details).length > 0 && <details><summary className="cursor-pointer text-muted-foreground">Evidence</summary><pre className="whitespace-pre-wrap break-words text-xs mt-2">{JSON.stringify(event.details, null, 2)}</pre></details>}
                    </li>)}
                </ol>
            </section>
        </>}
    </section>;
}

export default function InboundCallHistory() {
    const [params] = useSearchParams();
    const callId = params.get('call_id');
    const [data, setData] = useState<CallList | null>(null);
    const [page, setPage] = useState(1);
    const [search, setSearch] = useState('');
    const [query, setQuery] = useState('');
    const [outcome, setOutcome] = useState('');
    const [error, setError] = useState('');
    const [revision, setRevision] = useState(0);
    const [loading, setLoading] = useState(true);
    const refresh = useCallback(() => setRevision(value => value + 1), []);
    useEffect(() => {
        if (callId) return;
        const controller = new AbortController();
        setLoading(true);
        axios.get('/api/call-audit/calls', { params: { page, page_size: 20, search: query, outcome }, signal: controller.signal })
            .then(response => { if (!controller.signal.aborted) { setData(response.data); setError(''); } })
            .catch(() => { if (!controller.signal.aborted) setError('Inbound call history is temporarily unavailable. Your calls can continue normally.'); })
            .finally(() => { if (!controller.signal.aborted) setLoading(false); });
        return () => controller.abort();
    }, [page, query, outcome, revision, callId]);
    useEffect(() => {
        const timer = window.setInterval(() => { if (!document.hidden) refresh(); }, 15000);
        return () => window.clearInterval(timer);
    }, [refresh]);
    if (callId) return <CallDetail key={callId} callId={callId} />;
    return <section className="space-y-5" aria-label="Inbound call history">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-2xl font-semibold">Recent inbound calls</h1><p className="text-sm text-muted-foreground">Every recorded inbound attempt, including early hangups. Newest first.</p></div><button className={button} onClick={refresh} disabled={loading}>Refresh calls</button></div>
        <form className="flex flex-wrap gap-3" onSubmit={event => { event.preventDefault(); setPage(1); setQuery(search); }}>
            <label className="flex-1">Search calls<input className="block w-full rounded border border-border bg-background p-2" value={search} onChange={event => setSearch(event.target.value)} placeholder="Number, caller name, or transcript" maxLength={200} /></label>
            <label>Outcome<select className="block rounded border border-border bg-background p-2" value={outcome} onChange={event => { setOutcome(event.target.value); setPage(1); }}><option value="">All outcomes</option>{Object.entries(outcomeLabels).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
            <button className={`${button} self-end`} type="submit">Search</button>
        </form>
        {error && <p role="alert" className="text-destructive">{error}</p>}
        {loading && !data && <p role="status">Loading inbound calls…</p>}
        {data && <>
            <p className="text-sm text-muted-foreground">{data.total} calls</p>
            {!data.calls.length && <p>No inbound calls match this view.</p>}
            <ol className="space-y-3">
                {data.calls.map(call => <li key={call.call_id}><Link to={`/history?view=inbound&call_id=${encodeURIComponent(call.call_id)}`} className="block rounded-lg border border-border p-4 hover:bg-muted/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary">
                    <div className="flex flex-wrap justify-between gap-2"><strong>{call.caller_name ? `${call.caller_name} · ` : ''}{call.caller_number || 'Unknown caller'}</strong><time>{date(call.started_at)}</time></div>
                    <p className="text-sm text-muted-foreground">Called {call.called_number || 'unknown number'} · {duration(call.duration_seconds)}</p>
                    <p className="mt-2 font-medium">{label(call.outcome)}</p>
                    {call.transcript_preview ? <p className="mt-2 line-clamp-2 text-sm">{speakers[call.transcript_preview.speaker] || 'Unknown speaker'}: “{call.transcript_preview.text}”{!call.transcript_preview.is_final && ' (partial)'}</p> : <p className="mt-2 text-sm text-muted-foreground">No transcript available yet</p>}
                </Link></li>)}
            </ol>
            <div className="flex items-center justify-between gap-3"><button className={button} disabled={loading || page <= 1} onClick={() => setPage(value => value - 1)}>Previous</button><span>Page {page} of {data.total_pages}</span><button className={button} disabled={loading || page >= data.total_pages} onClick={() => setPage(value => value + 1)}>Next</button></div>
        </>}
    </section>;
}
