'use client';

import { useMemo, useState } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
} from 'recharts';
import { Wallet, TrendingUp, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAgents, usePortfolio, useTrades } from '@/lib/api/hooks';

const COLORS = [
  'hsl(199 89% 48%)',
  'hsl(142 76% 42%)',
  'hsl(38 92% 50%)',
  'hsl(270 70% 60%)',
  'hsl(0 72% 51%)',
];

interface Row {
  exchange: string;
  asset: string;
  balance: number;
  usd_value: number;
  is_testnet: boolean;
}

export default function PortfolioPage() {
  const { data: portfolio, refetch, loading } = usePortfolio();
  const { data: agents } = useAgents();
  const { data: trades } = useTrades(200);
  const [exchangeFilter, setExchangeFilter] = useState<'all' | 'bybit' | 'bitget'>('all');

  const rows: Row[] = useMemo(() => {
    if (!portfolio) return [];
    const out: Row[] = [];
    for (const ex of portfolio.exchanges) {
      for (const b of ex.balances) {
        out.push({
          exchange: ex.exchange,
          asset: b.asset,
          balance: b.total,
          usd_value: b.usd_value ?? (b.asset === 'USDT' ? b.total : 0),
          is_testnet: ex.is_testnet,
        });
      }
    }
    return out;
  }, [portfolio]);

  const totalValue = portfolio?.total_balance_usd ?? 0;
  const filtered = rows.filter(b =>
    exchangeFilter === 'all' || b.exchange.includes(exchangeFilter),
  );
  const pieData = filtered.map(b => ({
    name: `${b.exchange} ${b.asset}`,
    value: b.usd_value,
  }));

  const assignedToAi = (agents ?? [])
    .filter(a => a.status === 'active' || a.status === 'paused')
    .reduce((a, b) => a + Number(b.assigned_capital), 0);
  const activeAgents = (agents ?? []).filter(a => a.status === 'active').length;

  const pnlHistory = useMemo(() => {
    const buckets: Record<string, number> = {};
    const labels: string[] = [];
    const today = new Date();
    for (let i = 6; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(today.getDate() - i);
      const key = d.toLocaleDateString([], { weekday: 'short' });
      labels.push(key);
      buckets[key] = 0;
    }
    for (const t of trades ?? []) {
      if (!t.closed_at) continue;
      const key = new Date(t.closed_at).toLocaleDateString([], { weekday: 'short' });
      if (buckets[key] !== undefined) buckets[key] += Number(t.pnl);
    }
    return labels.map(date => ({ date, pnl: Math.round(buckets[date] * 100) / 100 }));
  }, [trades]);

  const best = pnlHistory.reduce((a, b) => Math.max(a, b.pnl), 0);
  const worst = pnlHistory.reduce((a, b) => Math.min(a, b.pnl), 0);

  return (
    <div className="p-4 md:p-6 space-y-4 md:space-y-6 max-w-[1400px]">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-bold">Portfolio</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            Connected exchange balances (read-only)
          </p>
        </div>
        <button
          onClick={refetch}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-1.5 bg-card border border-border rounded-lg text-xs hover:bg-accent transition-colors disabled:opacity-50 shrink-0"
        >
          <RefreshCw className={cn('w-3.5 h-3.5', loading && 'animate-spin')} />
          <span className="hidden sm:inline">Refresh Balances</span>
        </button>
      </div>

      <div className="bg-card border border-border rounded-xl p-4 md:p-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground uppercase tracking-wider">
              Total Portfolio Value
            </p>
            <p className="text-3xl font-bold font-mono">
              ${totalValue.toLocaleString(undefined, { maximumFractionDigits: 2 })}
            </p>
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  'text-xs flex items-center gap-1',
                  (portfolio?.daily_pnl ?? 0) >= 0 ? 'text-profit' : 'text-loss',
                )}
              >
                <TrendingUp className="w-3 h-3" />
                {(portfolio?.daily_pnl ?? 0) >= 0 ? '+' : ''}$
                {(portfolio?.daily_pnl ?? 0).toFixed(2)} today (
                {(portfolio?.daily_pnl_pct ?? 0).toFixed(2)}%)
              </span>
            </div>
          </div>
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground uppercase tracking-wider">
              Assigned to AI
            </p>
            <p className="text-3xl font-bold font-mono">
              ${assignedToAi.toLocaleString(undefined, { maximumFractionDigits: 0 })}
            </p>
            <p className="text-xs text-muted-foreground">
              across {activeAgents} active agent{activeAgents === 1 ? '' : 's'}
            </p>
          </div>
          <div className="space-y-2">
            <p className="text-xs text-muted-foreground uppercase tracking-wider">
              Available Capital
            </p>
            <p className="text-3xl font-bold font-mono">
              $
              {Math.max(0, totalValue - assignedToAi).toLocaleString(undefined, {
                maximumFractionDigits: 2,
              })}
            </p>
            <p className="text-xs text-muted-foreground">unassigned funds</p>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="xl:col-span-2 space-y-4">
          <div className="flex items-center gap-2">
            <div className="flex bg-card border border-border rounded-lg p-1 gap-1">
              {(['all', 'bybit', 'bitget'] as const).map(e => (
                <button
                  key={e}
                  onClick={() => setExchangeFilter(e)}
                  className={cn(
                    'px-3 py-1 text-xs rounded-md transition-all font-medium capitalize',
                    exchangeFilter === e
                      ? 'bg-primary text-white'
                      : 'text-muted-foreground hover:text-foreground',
                  )}
                >
                  {e}
                </button>
              ))}
            </div>
          </div>

          <div className="bg-card border border-border rounded-xl overflow-x-auto">
            <table className="w-full text-xs min-w-[600px]">
              <thead>
                <tr className="border-b border-border">
                  {['Asset', 'Exchange', 'Balance', 'USD Value', 'Allocation'].map(h => (
                    <th
                      key={h}
                      className="text-left p-4 text-muted-foreground font-medium"
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {filtered.map((b, i) => {
                  const pct =
                    totalValue > 0 ? ((b.usd_value / totalValue) * 100).toFixed(1) : '0.0';
                  return (
                    <tr key={i} className="hover:bg-accent/30 transition-colors">
                      <td className="p-4">
                        <div className="flex items-center gap-2">
                          <div className="w-7 h-7 rounded-full bg-primary/10 border border-primary/20 flex items-center justify-center text-[11px] font-bold text-primary">
                            {b.asset.slice(0, 3)}
                          </div>
                          <span className="font-medium">{b.asset}</span>
                        </div>
                      </td>
                      <td className="p-4">
                        <span className="px-2 py-0.5 bg-accent rounded text-[10px] font-medium">
                          {b.exchange}
                          {b.is_testnet ? ' (testnet)' : ''}
                        </span>
                      </td>
                      <td className="p-4 font-mono">
                        {Number(b.balance).toLocaleString(undefined, {
                          maximumFractionDigits: 6,
                        })}
                      </td>
                      <td className="p-4 font-mono font-medium">
                        $
                        {Number(b.usd_value).toLocaleString(undefined, {
                          maximumFractionDigits: 2,
                        })}
                      </td>
                      <td className="p-4">
                        <div className="flex items-center gap-2">
                          <div className="flex-1 h-1.5 bg-border rounded-full overflow-hidden max-w-[80px]">
                            <div
                              className="h-full bg-primary rounded-full"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                          <span className="text-muted-foreground w-10 text-right">
                            {pct}%
                          </span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {!filtered.length && (
                  <tr>
                    <td colSpan={5} className="p-6 text-center text-muted-foreground">
                      No balances yet — add an exchange API key in Settings.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-4">
          <div className="bg-card border border-border rounded-xl p-5 space-y-3">
            <p className="text-sm font-semibold">Allocation</p>
            <ResponsiveContainer width="100%" height={160}>
              <PieChart>
                <Pie
                  data={pieData.length ? pieData : [{ name: 'empty', value: 1 }]}
                  cx="50%"
                  cy="50%"
                  innerRadius={45}
                  outerRadius={72}
                  paddingAngle={2}
                  dataKey="value"
                >
                  {(pieData.length ? pieData : [{ name: 'empty', value: 1 }]).map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(v: number) => [
                    `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`,
                    '',
                  ]}
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="grid grid-cols-2 gap-1">
              {pieData.slice(0, 4).map((d, i) => (
                <div key={i} className="flex items-center gap-1.5">
                  <div
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ background: COLORS[i % COLORS.length] }}
                  />
                  <span className="text-[10px] text-muted-foreground truncate">
                    {d.name}
                  </span>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-card border border-border rounded-xl p-5 space-y-3">
            <p className="text-sm font-semibold">Weekly P&L</p>
            <ResponsiveContainer width="100%" height={120}>
              <BarChart data={pnlHistory} barSize={18}>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke="hsl(220 12% 14%)"
                  vertical={false}
                />
                <XAxis
                  dataKey="date"
                  tick={{ fill: 'hsl(215 16% 47%)', fontSize: 10 }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis hide />
                <Tooltip formatter={(v: number) => [`$${v.toFixed(2)}`, 'P&L']} />
                <Bar dataKey="pnl" radius={3}>
                  {pnlHistory.map((d, i) => (
                    <Cell
                      key={i}
                      fill={d.pnl >= 0 ? 'hsl(142 76% 42%)' : 'hsl(0 72% 51%)'}
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
            <div className="flex justify-between text-xs">
              <span className="text-profit">Best: +${best.toFixed(2)}</span>
              <span className="text-loss">Worst: ${worst.toFixed(2)}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-start gap-3 p-4 bg-cyan-500/5 border border-cyan-500/15 rounded-xl">
        <Wallet className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
        <div className="text-xs text-muted-foreground space-y-1">
          <p className="text-cyan-400 font-medium">Read + Trade Only</p>
          <p>
            Balances are fetched via exchange APIs with <strong>read</strong> and{' '}
            <strong>trade</strong> permissions only. The platform refuses keys that grant
            withdrawal — verification will mark them inactive immediately.
          </p>
        </div>
      </div>
    </div>
  );
}
