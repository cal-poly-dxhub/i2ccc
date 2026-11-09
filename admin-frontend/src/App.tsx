import { Play } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';
import AddSourcePanel from './components/AddSourcePanel';
import Navbar from './components/Navbar';
import SourcesTable from './components/SourcesTable';
import { useAuth } from './hooks/useAuth';
import { useSources } from './hooks/useSources';

export default function App() {
  const { isAuthenticated, isLoading, authError, user, logout } = useAuth();
  const { sources, addSource, rerunSource, deleteSource, ingestAll, pendingCount } = useSources();
  const [theme, setTheme] = useState<'light' | 'dark'>(() =>
    localStorage.getItem('i2-theme') === 'dark' ? 'dark' : 'light',
  );

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    localStorage.setItem('i2-theme', theme);
  }, [theme]);

  if (isLoading) {
    return <Message>Authenticating...</Message>;
  }

  if (!isAuthenticated) {
    return authError ? (
      <Message>
        <p>Authentication failed: {authError}</p>
        <button type="button" className="btn-primary mt-4" onClick={() => window.location.reload()}>
          Try again
        </button>
      </Message>
    ) : null;
  }

  return (
    <div className="min-h-screen">
      <Navbar
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
        userEmail={user?.email}
        onLogout={logout}
      />
      <main className="mx-auto flex max-w-6xl flex-col gap-6 px-6 py-8">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Sources</h1>
          <button type="button" className="btn-primary" disabled={pendingCount === 0} onClick={ingestAll}>
            <Play size={14} aria-hidden="true" />
            Ingest new documents{pendingCount > 0 ? ` (${pendingCount})` : ''}
          </button>
        </div>
        <AddSourcePanel onAdd={addSource} />
        <SourcesTable sources={sources} onRerun={rerunSource} onDeleteSource={deleteSource} />
      </main>
    </div>
  );
}

function Message({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-screen flex-col items-center justify-center text-sm text-[var(--color-text-secondary)]">
      {children}
    </div>
  );
}
