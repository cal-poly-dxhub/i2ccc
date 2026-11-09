import internet2Black from '../assets/internet2-black.png';
import internet2White from '../assets/internet2-white.png';

interface NavbarProps {
  theme: 'light' | 'dark';
  onToggleTheme: () => void;
  userEmail?: string;
  onLogout: () => void;
}

export default function Navbar({ theme, onToggleTheme, userEmail, onLogout }: NavbarProps) {
  return (
    <nav className="navbar-glass flex items-center justify-between px-6 py-3 text-[var(--color-text-primary)]">
      <div className="flex items-center gap-4">
        <img src={internet2Black} alt="Internet2" className="h-10 w-32 object-contain object-left dark:hidden" />
        <img src={internet2White} alt="Internet2" className="hidden h-10 w-32 object-contain object-left dark:block" />
        <span className="text-sm font-semibold tracking-tight text-[var(--color-text-secondary)]">CAI Content Admin</span>
      </div>
      <div className="flex items-center">
        {userEmail && <span className="px-2.5 text-[0.8125rem] text-[var(--color-text-secondary)]">{userEmail}</span>}
        <button type="button" className="nav-btn" aria-pressed={theme === 'dark'} onClick={onToggleTheme}>
          {theme === 'dark' ? 'Light mode' : 'Dark mode'}
        </button>
        <span aria-hidden="true" className="px-1 text-[0.8125rem] text-[var(--color-text-secondary)] opacity-40">·</span>
        <button type="button" className="nav-btn" onClick={onLogout}>Sign out</button>
      </div>
    </nav>
  );
}
