import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import axios from 'axios';
import { toast } from 'sonner';
import { Plus, RefreshCw, Search, Users, Pencil, Phone } from 'lucide-react';
import { useConfirmDialog } from '../hooks/useConfirmDialog';

interface Caller {
    phone_number: string;
    caller_name: string;
    business_name: string;
}
interface Phonebook { enabled: boolean; url: string; username: string }
const emptyForm = { caller_number: '', caller_name: '', business_name: '' };
const buttonClass = 'inline-flex items-center justify-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-accent disabled:opacity-50';
const inputClass = 'w-full rounded-md border border-input bg-background px-3 py-2 text-sm';
const errorText = (error: unknown) => {
    if (axios.isAxiosError(error) && typeof error.response?.data?.detail === 'string') return error.response.data.detail;
    return 'The request could not be completed. Please try again.';
};

export default function TrustedCallersPage() {
    const [callers, setCallers] = useState<Caller[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [query, setQuery] = useState('');
    const [editing, setEditing] = useState(false);
    const [existing, setExisting] = useState(false);
    const [form, setForm] = useState(emptyForm);
    const [saving, setSaving] = useState(false);
    const [phonebook, setPhonebook] = useState<Phonebook | null>(null);
    const [setupError, setSetupError] = useState('');
    const [password, setPassword] = useState('');
    const [revealing, setRevealing] = useState(false);
    const { confirm } = useConfirmDialog();

    const refresh = useCallback(async () => {
        setLoading(true);
        try {
            const { data } = await axios.get('/api/trusted-callers');
            setCallers(data.callers);
            setError('');
        } catch (e) { setError(errorText(e)); }
        finally { setLoading(false); }
    }, []);
    useEffect(() => {
        void refresh();
        const timer = window.setInterval(() => { void refresh(); }, 30000);
        const onFocus = () => { void refresh(); };
        window.addEventListener('focus', onFocus);
        return () => { window.clearInterval(timer); window.removeEventListener('focus', onFocus); };
    }, [refresh]);
    useEffect(() => {
        axios.get('/api/trusted-callers/phonebook').then(({ data }) => setPhonebook(data))
            .catch(() => setSetupError('Phonebook setup could not be loaded. Reload this page to try again.'));
    }, []);

    const save = async (event: FormEvent) => {
        event.preventDefault();
        setSaving(true);
        try {
            await axios.post('/api/trusted-callers', form);
            setEditing(false);
            toast.success('Caller saved. Future calls will ring the inside phone directly.');
            await refresh();
        } catch (e) { toast.error(errorText(e)); }
        finally { setSaving(false); }
    };
    const untrust = async (caller: Caller) => {
        if (!await confirm({
            title: 'Require screening again?',
            description: `${caller.caller_name || caller.business_name || caller.phone_number} will go through Operator Zero screening and leave the shared phonebook on its next refresh. Call history is kept.`,
            confirmText: 'Require screening',
        })) return;
        setSaving(true);
        try {
            await axios.delete('/api/trusted-callers', { params: { phone: caller.phone_number } });
            toast.success('Caller will be screened on future calls.');
            await refresh();
        } catch (e) { toast.error(errorText(e)); }
        finally { setSaving(false); }
    };
    const revealPassword = async () => {
        if (password) { setPassword(''); return; }
        setRevealing(true);
        try { const { data } = await axios.get('/api/trusted-callers/phonebook/password'); setPassword(data.password); }
        catch (e) { toast.error(errorText(e)); }
        finally { setRevealing(false); }
    };
    const filtered = callers.filter(c => `${c.caller_name} ${c.business_name} ${c.phone_number}`.toLowerCase().includes(query.toLowerCase()));

    return <div className="space-y-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
            <div><h1 className="text-3xl font-bold flex items-center gap-3"><Users className="h-7 w-7" />Trusted Callers</h1>
                <p className="text-muted-foreground mt-2">Approved callers bypass Operator Zero screening and ring the inside phone directly.</p>
                <p className="text-sm text-muted-foreground mt-1">Shared with the internal operator and your Grandstream phonebook. Refreshes every 30 seconds.</p></div>
            <div className="flex gap-2">
                <button className={buttonClass} onClick={() => void refresh()} disabled={loading}><RefreshCw className="h-4 w-4" />Refresh</button>
                <button className={`${buttonClass} bg-primary text-primary-foreground`} disabled={saving} onClick={() => { setForm(emptyForm); setExisting(false); setEditing(true); }}><Plus className="h-4 w-4" />Add caller</button>
            </div>
        </div>
        {error && <div role="alert" className="rounded-md border border-destructive p-4 text-destructive">{error} {callers.length > 0 && 'The list below is from the last successful refresh.'}</div>}
        {editing && <form onSubmit={save} className="rounded-lg border border-border bg-card p-5 space-y-4">
            <h2 className="text-lg font-semibold">{existing ? 'Edit trusted caller' : 'Add trusted caller'}</h2>
            <div className="grid gap-4 md:grid-cols-3">
                <label className="text-sm">Phone number<input className={`${inputClass} mt-1`} type="tel" required minLength={7} maxLength={40} readOnly={existing} value={form.caller_number} onChange={e => setForm({ ...form, caller_number: e.target.value })} placeholder="Include area or country code" /></label>
                <label className="text-sm">Name<input className={`${inputClass} mt-1`} maxLength={120} value={form.caller_name} onChange={e => setForm({ ...form, caller_name: e.target.value })} /></label>
                <label className="text-sm">Business (optional)<input className={`${inputClass} mt-1`} maxLength={120} value={form.business_name} onChange={e => setForm({ ...form, business_name: e.target.value })} /></label>
            </div>
            <p className="text-sm text-muted-foreground">Saving approves this number, including a previously blocked number. A new caller does not need to have called first.</p>
            <div className="flex gap-2"><button className={`${buttonClass} bg-primary text-primary-foreground`} disabled={saving} type="submit">{saving ? 'Saving…' : 'Save trusted caller'}</button><button className={buttonClass} disabled={saving} type="button" onClick={() => setEditing(false)}>Cancel</button></div>
        </form>}
        <div className="rounded-lg border border-border bg-card overflow-hidden">
            <div className="flex flex-wrap items-center gap-3 p-4 border-b border-border"><Search className="h-4 w-4 text-muted-foreground" /><input aria-label="Search trusted callers" className={`${inputClass} max-w-sm`} placeholder="Search names or numbers" value={query} onChange={e => setQuery(e.target.value)} /><span className="text-sm text-muted-foreground">{callers.length} trusted callers</span></div>
            {loading && callers.length === 0 ? <p className="p-6 text-muted-foreground" role="status">Loading trusted callers…</p> : filtered.length > 0 ? <div className="overflow-x-auto"><table className="w-full text-sm text-left"><thead className="bg-muted/40"><tr><th className="p-4">Name</th><th className="p-4">Phone number</th><th className="p-4">Business</th><th className="p-4">Actions</th></tr></thead><tbody>{filtered.map(caller => <tr key={caller.phone_number} className="border-t border-border"><td className="p-4 font-medium">{caller.caller_name || caller.business_name || 'Unnamed caller'}</td><td className="p-4 font-mono">{caller.phone_number}</td><td className="p-4">{caller.business_name || '—'}</td><td className="p-4"><div className="flex flex-wrap gap-2"><button className={buttonClass} disabled={saving} aria-label={`Edit ${caller.caller_name || caller.phone_number}`} onClick={() => { setForm({ caller_number: caller.phone_number, caller_name: caller.caller_name, business_name: caller.business_name }); setExisting(true); setEditing(true); }}><Pencil className="h-4 w-4" />Edit</button><button className={buttonClass} disabled={saving} onClick={() => void untrust(caller)}>Require screening</button></div></td></tr>)}</tbody></table></div> : !error && <p className="p-6 text-muted-foreground">{query ? 'No callers match your search.' : 'No trusted callers yet. Add a caller here or ask the internal operator to trust a caller.'}</p>}
        </div>
        <details className="rounded-lg border border-border bg-card p-5">
            <summary className="cursor-pointer font-semibold"><Phone className="inline h-4 w-4 mr-2" />Grandstream DP755 / DP725 phonebook setup</summary>
            <div className="mt-4 space-y-4 text-sm">
                {setupError ? <p role="alert">{setupError}</p> : !phonebook ? <p>Loading setup…</p> : !phonebook.enabled ? <p>The phonebook service has not been configured on this server. See the Operator Zero installation guide.</p> : <>
                    <p>In the DP755 web interface, open <strong>Phonebook → Global Phonebook XML Settings</strong>. Enable automatic download using HTTP (or HTTPS if your server provides it).</p>
                    <label className="block">Phonebook XML Server Path<input className={`${inputClass} mt-1 font-mono`} readOnly value={phonebook.url.replace(/\/phonebook\.xml$/, '').replace(/^https?:\/\//, '')} /></label>
                    <label className="block">HTTP/HTTPS Username<input className={`${inputClass} mt-1`} readOnly value={phonebook.username} /></label>
                    <button className={buttonClass} disabled={revealing} onClick={() => void revealPassword()}>{password ? 'Hide phonebook password' : 'Show phonebook password'}</button>
                    {password && <label className="block">HTTP/HTTPS Password<input className={`${inputClass} mt-1 font-mono`} readOnly value={password} autoComplete="off" /></label>}
                    <p>Set <strong>Phonebook Download Interval</strong> to <strong>5 minutes</strong>. For a dedicated trusted-caller directory, use replacement rather than append so removed callers disappear. Back up any handset-only contacts before enabling <strong>Remove Manually-edited Entries on Download</strong>.</p>
                    <p>Choose <strong>XML</strong> as the shared/global phonebook type for the handset, save and apply, then open the handset’s <strong>Contacts → Shared</strong> directory. Menu wording can vary with firmware.</p>
                    <p>Changes appear after the next download. Handset contact edits do not change trusted status. This list contains approved contacts, not recent call history.</p>
                </>}
            </div>
        </details>
    </div>;
}
