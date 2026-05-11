'use client';

import { usePathname } from 'next/navigation';
import { Menu, TrendingUp, TrendingDown } from 'lucide-react';
import { usePolling } from '@/lib/api/hooks';
import { api } from '@/lib/api/client';

const pageTitles: Record<string, string> = {
  '/dashboard': 'Dashboard',
  '/portfolio': 'Portfolio',
  '/agents': 'AI Agents',
  '/trades': 'Trade History',
  '/settings': 'Settings',
};

interface TopbarProps {
  onMobileMenuClick: () => void;
}

export function Topbar({ onMobileMenuClick }: TopbarProps) {
  const pathname = usePathname();
  const title =
    Object.entries(pageTitles).find(([p]) => pathname.startsWith(p))?.[1] || 'NeuralTrade';
  // Poll the public ticker endpoint every 15s.
  const { data: tickers } = usePolling(() => api.tickers(), 15000);

  return (
    <header className="h-14 border-b border-border bg-card/50 backdrop-blur-sm flex items-center justify-between gap-3 px-4 md:px-6 shrink-0">
      <div className="flex items-center gap-2 min-w-0">
        <button
          onClick={onMobileMenuClick}
          className="md:hidden p-1.5 rounded-md hover:bg-accent text-muted-foreground -ml-1"
          aria-label="Open menu"
        >
          <Menu className="w-5 h-5" />
        </button>
        <h1 className="text-sm font-semibold text-foreground truncate">{title}</h1>
      </div>

      {/* Live ticker — desktop only */}
      <div className="hidden lg:flex items-center gap-4">
        {(tickers ?? []).slice(0, 4).map(m => (
          <div key={m.symbol} className="flex items-center gap-1.5">
            <span className="text-xs text-muted-foreground font-mono">
              {m.symbol.replace('USDT', '')}
            </span>
            <span className="text-xs font-mono font-medium">
              {m.price.toLocaleString(undefined, {
                minimumFractionDigits: m.price > 100 ? 1 : 4,
              })}
            </span>
            <span
              className={`text-[10px] font-medium flex items-center gap-0.5 ${
                m.change_24h_pct >= 0 ? 'text-profit' : 'text-loss'
              }`}
            >
              {m.change_24h_pct >= 0 ? (
                <TrendingUp className="w-2.5 h-2.5" />
              ) : (
                <TrendingDown className="w-2.5 h-2.5" />
              )}
              {Math.abs(m.change_24h_pct).toFixed(2)}%
            </span>
          </div>
        ))}
      </div>

      {/* Compact ticker — tablet only: just BTC + 24h change */}
      <div className="hidden md:flex lg:hidden items-center gap-2">
        {(tickers ?? []).slice(0, 1).map(m => (
          <div key={m.symbol} className="flex items-center gap-1.5">
            <span className="text-xs text-muted-foreground font-mono">
              {m.symbol.replace('USDT', '')}
            </span>
            <span className="text-xs font-mono font-medium">
              {m.price.toLocaleString(undefined, { maximumFractionDigits: 0 })}
            </span>
            <span
              className={`text-[10px] font-medium ${
                m.change_24h_pct >= 0 ? 'text-profit' : 'text-loss'
              }`}
            >
              {m.change_24h_pct >= 0 ? '+' : ''}
              {m.change_24h_pct.toFixed(1)}%
            </span>
          </div>
        ))}
      </div>

      <div className="text-[11px] md:text-xs text-muted-foreground font-mono shrink-0">
        {new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} UTC
      </div>
    </header>
  );
}
