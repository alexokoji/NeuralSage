'use client';

import { usePathname } from 'next/navigation';
import { Menu, TrendingUp, TrendingDown, Brain } from 'lucide-react';
import { usePolling } from '@/lib/api/hooks';
import { api } from '@/lib/api/client';
import { NotificationBell } from './notification-bell';
import { useEffect, useState } from 'react';

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
  const { data: tickers } = usePolling(() => api.tickers(), 15000);

  const [aiStatus, setAiStatus] = useState<{
    primary_provider: string;
    gpt_available: boolean;
    grok_available: boolean;
    keys_available: number;
  } | null>(null);

  useEffect(() => {
    let mounted = true;
    const fetchStatus = async () => {
      try {
        const s = await api.aiStatus();
        if (mounted) setAiStatus(s as any);
      } catch { /* ignore */ }
    };
    fetchStatus();
    const interval = setInterval(fetchStatus, 30_000);
    return () => { mounted = false; clearInterval(interval); };
  }, []);

  return (
    <header className="h-14 border-b border-border bg-card/50 backdrop-blur-sm flex items-center justify-between gap-3 px-4 md:px-6 shrink-0 relative z-50">
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

      <div className="flex items-center gap-2 shrink-0">
        {aiStatus && (
          <div
            className={`flex items-center gap-1 px-2 py-1 rounded-full text-[10px] font-medium ${
              aiStatus.primary_provider === 'gpt'
                ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/20'
                : aiStatus.grok_available
                ? 'bg-blue-500/10 text-blue-400 border border-blue-500/20'
                : 'bg-red-500/10 text-red-400 border border-red-500/20'
            }`}
            title={`AI: ${aiStatus.primary_provider?.toUpperCase()} | GPT: ${aiStatus.gpt_available ? 'ON' : 'OFF'} | Groq: ${aiStatus.grok_available ? 'ON' : 'OFF'} | Keys: ${aiStatus.keys_available}`}
          >
            <Brain className="w-3 h-3" />
            <span className="hidden sm:inline">
              {aiStatus.primary_provider === 'gpt' ? 'GPT' : aiStatus.grok_available ? 'Groq' : 'OFF'}
            </span>
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                aiStatus.primary_provider === 'gpt'
                  ? 'bg-emerald-400 animate-pulse'
                  : aiStatus.grok_available
                  ? 'bg-blue-400 animate-pulse'
                  : 'bg-red-400'
              }`}
            />
          </div>
        )}
        <NotificationBell />
        <div className="text-[11px] md:text-xs text-muted-foreground font-mono">
          {new Date().toLocaleTimeString('en-NG', { hour: '2-digit', minute: '2-digit', timeZone: 'Africa/Lagos' })} WAT
        </div>
      </div>
    </header>
  );
}
