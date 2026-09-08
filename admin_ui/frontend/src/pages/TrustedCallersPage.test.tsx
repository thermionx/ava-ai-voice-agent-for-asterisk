// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import axios from 'axios';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import TrustedCallersPage from './TrustedCallersPage';

vi.mock('axios');
const mocks = vi.hoisted(() => ({ confirm: vi.fn(), success: vi.fn(), error: vi.fn() }));
vi.mock('../hooks/useConfirmDialog', () => ({ useConfirmDialog: () => ({ confirm: mocks.confirm }) }));
vi.mock('sonner', () => ({ toast: { success: mocks.success, error: mocks.error } }));
const caller = { phone_number: '2025550100', caller_name: 'Alice', business_name: 'Workshop' };
beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axios.get).mockImplementation(async url => ({ data: url === '/api/trusted-callers' ? {count: 1, callers: [caller]} : {enabled: true, username: 'phonebook', url: 'http://mini-pc:8790/phonebook/phonebook.xml'} }));
    vi.mocked(axios.post).mockResolvedValue({data: {}});
    vi.mocked(axios.delete).mockResolvedValue({data: {}});
});
describe('Trusted callers', () => {
    it('shows and searches the shared directory', async () => {
        render(<TrustedCallersPage />);
        expect(await screen.findByText('Alice')).toBeInTheDocument();
        fireEvent.change(screen.getByLabelText('Search trusted callers'), {target: {value: 'unmatched'}});
        expect(screen.getByText('No callers match your search.')).toBeInTheDocument();
    });
    it('adds a caller that has never called before', async () => {
        render(<TrustedCallersPage />);
        fireEvent.click(screen.getByText('Add caller'));
        fireEvent.change(screen.getByLabelText('Phone number'), {target:{value:'2025550101'}});
        fireEvent.change(screen.getByLabelText('Name'), {target:{value:'Bob'}});
        fireEvent.click(screen.getByText('Save trusted caller'));
        await waitFor(() => expect(axios.post).toHaveBeenCalledWith('/api/trusted-callers', {caller_number:'2025550101', caller_name:'Bob',business_name:''}));
    });
    it('only revokes trust after confirmation', async () => {
        mocks.confirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
        render(<TrustedCallersPage />);
        fireEvent.click(await screen.findByText('Require screening'));
        await waitFor(() => expect(mocks.confirm).toHaveBeenCalledTimes(1));
        expect(axios.delete).not.toHaveBeenCalled();
        fireEvent.click(screen.getByText('Require screening'));
        await waitFor(() => expect(axios.delete).toHaveBeenCalledWith('/api/trusted-callers', {params:{phone:'2025550100'}}));
    });
    it('preserves the last list on refresh failure', async () => {
        render(<TrustedCallersPage />);
        await screen.findByText('Alice');
        vi.mocked(axios.get).mockRejectedValueOnce(new Error('offline'));
        fireEvent.click(screen.getByText('Refresh'));
        expect(await screen.findByRole('alert')).toHaveTextContent('last successful refresh');
        expect(screen.getByText('Alice')).toBeInTheDocument();
    });
    it('does not request the phonebook password until asked', async () => {
        render(<TrustedCallersPage />);
        await screen.findByText('Alice');
        expect(axios.get).not.toHaveBeenCalledWith('/api/trusted-callers/phonebook/password');
        vi.mocked(axios.get).mockResolvedValueOnce({data:{password:'directory-only'}});
        fireEvent.click(screen.getByText('Show phonebook password'));
        expect(await screen.findByLabelText('HTTP/HTTPS Password')).toHaveValue('directory-only');
    });
});
