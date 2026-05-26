import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from 'react-query';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import App from './App';
import { fetchPipelines } from './request/PipelineType';

jest.mock('./request/PipelineType', () => ({
    fetchPipelines: jest.fn(),
}));

jest.mock('./layout/GridColumns', () => () => (
    <div data-testid="build-grid" />
));

test('renders build list page', async () => {
    fetchPipelines.mockResolvedValue([
        {
            pipeline_id: 1,
            pipeline_name: 'TiDB',
        },
    ]);

    const queryClient = new QueryClient({
        defaultOptions: {
            queries: {
                retry: false,
            },
        },
    });

    render(
        <QueryClientProvider client={queryClient}>
            <MemoryRouter initialEntries={['/home/list/dev']}>
                <Routes>
                    <Route path="/home/list/:type" element={<App />} />
                </Routes>
            </MemoryRouter>
        </QueryClientProvider>
    );

    expect(await screen.findByText('TiDB')).toBeInTheDocument();
    expect(screen.getByTestId('build-grid')).toBeInTheDocument();
});
