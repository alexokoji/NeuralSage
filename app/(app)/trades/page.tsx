'use client';

import { useMemo, useState } from 'react';
import { cn } from '@/lib/utils';
import { formatDateTimeWAT } from '@/lib/utils/timezone';
import {
  Search,
  Download,
  ArrowUpRight,
  ArrowDownRight,
  ChevronDown,
  ChevronRight,
  Brain,
  ShieldCheck,
  TrendingDown,
} from 'lucide-react';
import { Input } from '@/components/ui/input';
import { useTrades } from '@/lib/api/hooks';

export default function TradesPage() {
  const [search, setSearch] = useState('');
  const [sideFilter, setSideFilter] = useState<'all' | 'buy' | 'sell'>('all');
  const [exchangeFilter, setExchangeFilter] = useState<'all' | 'bybit' | 'bitget'>('all');
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  function toggleExpand(id: string) {
    setExpandedIds(prev => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  const { data: trades, loading, error } = useTrades(200);
  const list = trades ?? [];

  const filtered = useMemo(
    () =>
      list.filter(t => {
        if (search && !t.symbol.toLowerCase().includes(search.toLowerCase())) return false;
        if (sideFilter !== 'all' && t.side !== sideFilter) return false;
        if (exchangeFilter !== 'all' && !t.exchange.includes(exchangeFilter)) return false;
        return true;
      }),
    [list, search, sideFilter, exchangeFilter],
  );

  const totalPnl = list.reduce((a, b) => a + Number(b.pnl), 0);
  const totalFees = list.reduce((a, b) => a + Number(b.fees), 0);
  const winCount = list.filter(t => Number(t.pnl) > 0).length;

  function exportCsv() {
    const rows = [
      ['symbol', 'side', 'type', 'entry', 'exit', 'qty', 'pnl', 'fees', 'source', 'exchange', 'opened_at', 'closed_at'],
      ...filtered.map(t => [
        t.symbol,
        t.side,
        t.order_type,
        t.entry_price ?? '',
        t.exit_price ?? '',
        t.quantity,
        t.pnl,
        t.fees,
        t.signal_source,
        t.exchange,
        t.opened_at,
        t.closed_at ?? '',
      ]),
    ];
    const csv = rows.map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `trades-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="p-4 md:p-6 space-y-4 md:space-y-6 max-w-[1400px]">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-bold">Trade History</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            Complete log of all AI agent trades
          </p>
        </div>
        <button
          onClick={exportCsv}
          className="flex items-center gap-2 px-3 py-1.5 bg-card border border-border rounded-lg text-xs hover:bg-accent transition-colors shrink-0"
        >
          <Download className="w-3.5 h-3.5" /> <span className="hidden sm:inline">Export CSV</span>
        </button>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          { label: 'Total Trades', value: String(list.length), color: 'text-foreground' },
          {
            label: 'Net P&L',
            value: `${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`,
            color: totalPnl >= 0 ? 'text-profit' : 'text-loss',
          },
          {
            label: 'Win Rate',
            value: list.length ? `${((winCount / list.length) * 100).toFixed(0)}%` : '—',
            color: 'text-foreground',
          },
          {
            label: 'Total Fees',
            value: `$${totalFees.toFixed(2)}`,
            color: 'text-muted-foreground',
          },
        ].map(({ label, value, color }) => (
          <div
            key={label}
            className="bg-card border border-border rounded-xl p-4 text-center"
          >
            <p className={cn('text-xl font-bold font-mono', color)}>{value}</p>
            <p className="text-xs text-muted-foreground mt-1">{label}</p>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative flex-1 min-w-[200px] max-w-[300px]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted-foreground" />
          <Input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search symbol..."
            className="pl-9 h-9 text-xs bg-card border-border"
          />
        </div>
        <div className="flex bg-card border border-border rounded-lg p-1 gap-1">
          {(['all', 'buy', 'sell'] as const).map(f => (
            <button
              key={f}
              onClick={() => setSideFilter(f)}
              className={cn(
                'px-3 py-1 text-xs rounded-md font-medium capitalize transition-all',
                sideFilter === f
                  ? 'bg-primary text-white'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {f}
            </button>
          ))}
        </div>
        <div className="flex bg-card border border-border rounded-lg p-1 gap-1">
          {(['all', 'bybit', 'bitget'] as const).map(f => (
            <button
              key={f}
              onClick={() => setExchangeFilter(f)}
              className={cn(
                'px-3 py-1 text-xs rounded-md font-medium capitalize transition-all',
                exchangeFilter === f
                  ? 'bg-primary text-white'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <div className="bg-card border border-border rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-muted-foreground">
                {[
                  '',
                  'Symbol',
                  'Side',
                  'Type',
                  'Entry',
                  'Exit',
                  'Qty',
                  'P&L',
                  'Fees',
                  'Source',
                  'Exchange',
                  'Opened',
                  'Closed',
                ].map(h => (
                  <th
                    key={h}
                    className="text-left px-4 py-3 font-medium whitespace-nowrap"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {filtered.map(trade => {
                const sd = (trade.signal_data ?? {}) as Record<string, unknown>;
                const reason = sd.reason as string | undefined;
                const confidence = sd.confidence != null ? Number(sd.confidence) : null;
                const deviation = sd.deviation_pct != null ? Number(sd.deviation_pct) : null;
                const ema = sd.ema != null ? Number(sd.ema) : null;
                const aiUsed = sd.ai_available !== false;
                const recoveryEntry = sd.recovery_mode_at_entry === true;
                const hasDecisionData = !!(reason || confidence != null);
                const isExpanded = expandedIds.has(trade.id);

                return (
                  <>
                    <tr
                      key={trade.id}
                      className={cn('hover:bg-accent/30 transition-colors', isExpanded && 'bg-accent/20')}
                    >
                      <td className="px-2 py-3 w-6">
                        {hasDecisionData && (
                          <button
                            onClick={() => toggleExpand(trade.id)}
                            className="text-muted-foreground hover:text-foreground transition-colors"
                            title="Show AI decision"
                          >
                            {isExpanded
                              ? <ChevronDown className="w-3.5 h-3.5" />
                              : <ChevronRight className="w-3.5 h-3.5" />}
                          </button>
                        )}
                      </td>
                      <td className="px-4 py-3 font-mono font-semibold">{trade.symbol}</td>
                      <td className="px-4 py-3">
                        <span
                          className={cn(
                            'inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-bold uppercase',
                            trade.side === 'buy'
                              ? 'bg-profit text-green-300'
                              : 'bg-loss text-red-300',
                          )}
                        >
                          {trade.side === 'buy' ? (
                            <ArrowUpRight className="w-2.5 h-2.5" />
                          ) : (
                            <ArrowDownRight className="w-2.5 h-2.5" />
                          )}
                          {trade.side}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground capitalize">
                        {trade.order_type}
                      </td>
                      <td className="px-4 py-3 font-mono">
                        {trade.entry_price != null
                          ? `$${Number(trade.entry_price).toLocaleString(undefined, { maximumFractionDigits: 4 })}`
                          : '—'}
                      </td>
                      <td className="px-4 py-3 font-mono">
                        {trade.exit_price != null
                          ? `$${Number(trade.exit_price).toLocaleString(undefined, { maximumFractionDigits: 4 })}`
                          : '—'}
                      </td>
                      <td className="px-4 py-3 font-mono">{trade.quantity}</td>
                      <td className="px-4 py-3">
                        <div
                          className={cn(
                            'font-mono font-semibold',
                            Number(trade.pnl) >= 0 ? 'text-profit' : 'text-loss',
                          )}
                        >
                          <span>
                            {Number(trade.pnl) >= 0 ? '+' : ''}${Number(trade.pnl).toFixed(2)}
                          </span>
                          <span className="ml-1 text-[10px] opacity-70">
                            ({Number(trade.pnl_pct).toFixed(2)}%)
                          </span>
                        </div>
                      </td>
                      <td className="px-4 py-3 font-mono text-muted-foreground">
                        ${Number(trade.fees).toFixed(2)}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {trade.signal_source.replace(/_/g, ' ')}
                      </td>
                      <td className="px-4 py-3">
                        <span className="px-1.5 py-0.5 bg-accent rounded text-[10px] capitalize">
                          {trade.exchange}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground whitespace-nowrap">
                        {formatDateTimeWAT(trade.opened_at)}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground whitespace-nowrap">
                        {trade.closed_at ? formatDateTimeWAT(trade.closed_at) : '—'}
                      </td>
                    </tr>

                    {isExpanded && hasDecisionData && (
                      <tr key={`${trade.id}-detail`} className="bg-accent/10">
                        <td colSpan={13} className="px-6 py-3 border-b border-border">
                          <div className="flex flex-wrap gap-4 text-xs">
                            {reason && (
                              <div className="flex items-start gap-2 min-w-0">
                                <Brain className="w-3.5 h-3.5 text-primary shrink-0 mt-0.5" />
                                <div>
                                  <p className="text-muted-foreground font-medium mb-0.5">AI Reason</p>
                                  <p className="text-foreground">{reason}</p>
                                </div>
                              </div>
                            )}
                            {confidence != null && (
                              <div className="flex items-start gap-2">
                                <ShieldCheck className="w-3.5 h-3.5 text-primary shrink-0 mt-0.5" />
                                <div>
                                  <p className="text-muted-foreground font-medium mb-0.5">Confidence</p>
                                  <p className={cn('font-mono font-semibold', confidence >= 0.65 ? 'text-profit' : confidence >= 0.45 ? 'text-foreground' : 'text-loss')}>
                                    {(confidence * 100).toFixed(0)}%
                                  </p>
                                </div>
                              </div>
                            )}
                            {deviation != null && (
                              <div className="flex items-start gap-2">
                                <TrendingDown className="w-3.5 h-3.5 text-primary shrink-0 mt-0.5" />
                                <div>
                                  <p className="text-muted-foreground font-medium mb-0.5">EMA Deviation</p>
                                  <p className="font-mono">{deviation.toFixed(3)}%{ema != null ? ` (EMA ${ema.toFixed(4)})` : ''}</p>
                                </div>
                              </div>
                            )}
                            <div className="flex gap-3 ml-auto items-center flex-wrap">
                              {aiUsed && (
                                <span className="px-2 py-0.5 rounded bg-primary/10 text-primary text-[10px] font-medium">AI-confirmed</span>
                              )}
                              {recoveryEntry && (
                                <span className="px-2 py-0.5 rounded bg-yellow-500/10 text-yellow-400 text-[10px] font-medium">Recovery mode entry</span>
                              )}
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                );
              })}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={13} className="text-center py-10 text-muted-foreground">
                    {error
                      ? `Failed to load trades: ${error.message}`
                      : 'No trades match these filters yet.'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="px-4 py-3 border-t border-border flex items-center justify-between">
          <p className="text-xs text-muted-foreground">
            {filtered.length} of {list.length} trades
          </p>
          <p className="text-xs text-muted-foreground">
            Click <ChevronRight className="inline w-3 h-3" /> on any row to see why the AI opened that trade.
          </p>
        </div>
      </div>
    </div>
  );
}
