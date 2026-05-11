'use client';

import { useMemo, useState } from 'react';
import dynamic from 'next/dynamic';
import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  LineChart,
  Line,
} from 'recharts';
import { StatCard } from '@/components/ui/stat-card';
import {
  Wallet,
  TrendingUp,
  Bot,
  Activity,
  ArrowUpRight,
  ArrowDownRight,
  TriangleAlert as AlertTriangle,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  useAgents,
  usePolling,
  usePortfolio,
  useTickers,
  useTrades,
} from '@/lib/api/hooks';
import { api } from '@/lib/api/client';
import type { Agent, Trade } from '@/lib/api/types';

const AIAssistant = dynamic(
  () => import('@/components/trading/ai-assistant').then(m => m.AIAssistant),
  {
    ssr: false,
    loading: () => (
      <div className="h-full flex items-center justify-center">
        <div className="w-8 h-8 rounded-full border-2 border-primary border-t-transparent animate-spin" />
      </div>
    ),
  },
);

const CustomTooltip = ({ active, payload, label }: any) => {
  if (active && payload?.length) {
    return (
      <div className="bg-card border border-border rounded-lg px-3 py-2 shadow-xl">
        <p className="text-xs text-muted-foreground mb-1">{label}</p>
        {payload.map((p: any) => (
          <p key={p.dataKey} className="text-xs font-mono" style={{ color: p.color }}>
            {p.name}:{' '}
            {typeof p.value === 'number' && p.dataKey === 'value'
              ? `$${p.value.toLocaleString()}`
              : typeof p.value === 'number'
              ? `${p.value > 0 ? '+' : ''}${p.value.toFixed(2)}%`
              : p.value}
          </p>
        ))}
      </div>
    );
  }
  return null;
};

// Build a simple 10-day equity curve from filled trades.
function buildPortfolioSeries(
  trades: Trade[],
  basis: number,
): { date: string; value: number; pnl: number }[] {
  const days = 10;
  const today = new Date();
  const buckets: { date: string; pnl: number }[] = [];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    buckets.push({ date: `${d.getMonth() + 1}/${d.getDate()}`.padStart(5, '0'), pnl: 0 });
  }
  for (const t of trades) {
    if (!t.closed_at) continue;
    const d = new Date(t.closed_at);
    const stamp = `${d.getMonth() + 1}/${d.getDate()}`.padStart(5, '0');
    const bucket = buckets.find(b => b.date === stamp);
    if (bucket) bucket.pnl += Number(t.pnl);
  }
  let running = basis - buckets.reduce((a, b) => a + b.pnl, 0);
  return buckets.map(b => {
    running += b.pnl;
    return { ...b, value: Math.round(running) };
  });
}

function buildAgentSeries(agents: Agent[]) {
  const top = agents.slice(0, 3);
  return Array.from({ length: 7 }).map((_, i) => {
    const point: Record<string, number | string> = { date: `Day ${i + 1}` };
    top.forEach((a, idx) => {
      const seed = (a.id.charCodeAt(0) || 0) + i * (idx + 1);
      point[a.name || `agent${idx + 1}`] = ((seed % 9) - 4) * 0.4;
    });
    return point;
  });
}

export default function DashboardPage() {
  const [timeRange, setTimeRange] = useState<'7d' | '30d' | 'all'>('7d');
  const { data: portfolio } = usePolling(() => api.portfolioOverview(), 15000);
  const { data: trades } = usePolling(() => api.listTrades(50), 15000);
  const { data: agents } = useAgents();
  const { data: tickers } = useTickers();

  const totalBalance = portfolio?.total_balance_usd ?? 0;
  const totalPnl = portfolio?.total_pnl ?? 0;
  const totalPnlPct = portfolio?.total_pnl_pct ?? 0;
  const dailyPnl = portfolio?.daily_pnl ?? 0;
  const dailyPnlPct = portfolio?.daily_pnl_pct ?? 0;
  const activeAgents = portfolio?.active_agents_count ?? 0;
  const openPositions = portfolio?.open_positions_count ?? 0;

  const portfolioSeries = useMemo(
    () => buildPortfolioSeries(trades ?? [], totalBalance || 10000),
    [trades, totalBalance],
  );
  const agentSeries = useMemo(() => buildAgentSeries(agents ?? []), [agents]);
  const recentTrades = (trades ?? []).slice(0, 6);

  return (
    <div className="p-6 space-y-6 max-w-[1600px]">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold">Overview</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            Real-time portfolio &amp; agent monitoring
          </p>
        </div>
        <div className="flex items-center gap-1 bg-card border border-border rounded-lg p-1">
          {(['7d', '30d', 'all'] as const).map(r => (
            <button
              key={r}
              onClick={() => setTimeRange(r)}
              className={cn(
                'px-3 py-1.5 text-xs rounded-md transition-all font-medium',
                timeRange === r
                  ? 'bg-primary text-white'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {r === 'all' ? 'All' : r}
            </button>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard
          title="Total Balance"
          value={`$${totalBalance.toLocaleString(undefined, { maximumFractionDigits: 0 })}`}
          change={totalPnlPct}
          changeLabel="all time"
          icon={Wallet}
          iconColor="text-cyan-400"
        />
        <StatCard
          title="Total P&L"
          value={`${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`}
          change={dailyPnlPct}
          changeLabel="24h"
          icon={TrendingUp}
          iconColor={totalPnl >= 0 ? 'text-green-400' : 'text-red-400'}
          className={totalPnl >= 0 ? 'bg-profit' : ''}
        />
        <StatCard
          title="Active Agents"
          value={String(activeAgents)}
          badge={activeAgents ? 'Running' : 'Idle'}
          badgeColor={
            activeAgents
              ? 'bg-green-500/10 text-green-400'
              : 'bg-muted/30 text-muted-foreground'
          }
          icon={Bot}
          iconColor="text-primary"
        />
        <StatCard
          title="Open Positions"
          value={String(openPositions)}
          badge={openPositions ? 'Live' : '—'}
          badgeColor="bg-cyan-500/10 text-cyan-400"
          icon={Activity}
          iconColor="text-orange-400"
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="xl:col-span-2 bg-card border border-border rounded-xl p-5 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-semibold">Portfolio Value</p>
              <p className="text-2xl font-bold font-mono mt-1">
                ${totalBalance.toLocaleString(undefined, { maximumFractionDigits: 0 })}
              </p>
              <p
                className={cn(
                  'text-xs flex items-center gap-1 mt-0.5',
                  dailyPnl >= 0 ? 'text-profit' : 'text-loss',
                )}
              >
                <TrendingUp className="w-3 h-3" />{' '}
                {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)} (
                {dailyPnlPct >= 0 ? '+' : ''}
                {dailyPnlPct.toFixed(2)}%) 24h
              </p>
            </div>
          </div>
          <ResponsiveContainer width="100%" height={200}>
            <AreaChart data={portfolioSeries}>
              <defs>
                <linearGradient id="portfolioGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="hsl(199 89% 48%)" stopOpacity={0.25} />
                  <stop offset="95%" stopColor="hsl(199 89% 48%)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 12% 14%)" />
              <XAxis
                dataKey="date"
                tick={{ fill: 'hsl(215 16% 47%)', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: 'hsl(215 16% 47%)', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={v => `$${(v / 1000).toFixed(0)}k`}
              />
              <Tooltip content={<CustomTooltip />} />
              <Area
                type="monotone"
                dataKey="value"
                stroke="hsl(199 89% 48%)"
                strokeWidth={2}
                fill="url(#portfolioGrad)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        <div className="bg-card border border-border rounded-xl overflow-hidden">
          <AIAssistant
            compact
            mood={dailyPnl > 0 ? 'profit' : dailyPnl < 0 ? 'loss' : 'neutral'}
            confidence={
              agents && agents.length
                ? Math.round(
                    agents.reduce((a, b) => a + Number(b.confidence_score), 0) / agents.length,
                  )
                : 60
            }
            status={activeAgents ? 'active' : 'idle'}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="xl:col-span-2 bg-card border border-border rounded-xl p-5 space-y-4">
          <p className="text-sm font-semibold">Agent Daily Trend (synthetic preview)</p>
          <ResponsiveContainer width="100%" height={180}>
            <LineChart data={agentSeries}>
              <CartesianGrid strokeDasharray="3 3" stroke="hsl(220 12% 14%)" />
              <XAxis
                dataKey="date"
                tick={{ fill: 'hsl(215 16% 47%)', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
              />
              <YAxis
                tick={{ fill: 'hsl(215 16% 47%)', fontSize: 11 }}
                axisLine={false}
                tickLine={false}
                tickFormatter={v => `${v}%`}
              />
              <Tooltip content={<CustomTooltip />} />
              {(agents ?? []).slice(0, 3).map((a, idx) => (
                <Line
                  key={a.id}
                  type="monotone"
                  dataKey={a.name || `agent${idx + 1}`}
                  stroke={
                    idx === 0
                      ? 'hsl(199 89% 48%)'
                      : idx === 1
                      ? 'hsl(142 76% 42%)'
                      : 'hsl(38 92% 50%)'
                  }
                  strokeWidth={2}
                  dot={false}
                  name={a.name}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
          <p className="text-[10px] text-muted-foreground">
            Real per-agent equity curves populate once daily snapshots are generated.
          </p>
        </div>

        <div className="bg-card border border-border rounded-xl p-5 space-y-3">
          <p className="text-sm font-semibold">Market Prices</p>
          <div className="space-y-2">
            {(tickers ?? []).slice(0, 6).map(m => (
              <div
                key={m.symbol}
                className="flex items-center justify-between py-2 border-b border-border last:border-0"
              >
                <div className="flex items-center gap-2">
                  <div className="w-6 h-6 rounded-full bg-primary/10 border border-primary/20 flex items-center justify-center text-[10px] font-bold text-primary">
                    {m.symbol.replace('USDT', '').charAt(0)}
                  </div>
                  <div>
                    <p className="text-xs font-medium">{m.symbol.replace('USDT', '')}</p>
                    <p className="text-[10px] text-muted-foreground">
                      {(m.volume_24h / 1e9).toFixed(1)}B vol
                    </p>
                  </div>
                </div>
                <div className="text-right">
                  <p className="text-xs font-mono font-medium">
                    $
                    {m.price > 1000
                      ? m.price.toLocaleString(undefined, { maximumFractionDigits: 0 })
                      : m.price.toFixed(m.price > 1 ? 2 : 4)}
                  </p>
                  <p
                    className={`text-[10px] font-medium flex items-center justify-end gap-0.5 ${
                      m.change_24h_pct >= 0 ? 'text-profit' : 'text-loss'
                    }`}
                  >
                    {m.change_24h_pct >= 0 ? (
                      <ArrowUpRight className="w-2.5 h-2.5" />
                    ) : (
                      <ArrowDownRight className="w-2.5 h-2.5" />
                    )}
                    {Math.abs(m.change_24h_pct).toFixed(2)}%
                  </p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="bg-card border border-border rounded-xl p-5 space-y-4">
        <div className="flex items-center justify-between">
          <p className="text-sm font-semibold">Recent Trades</p>
          <a href="/trades" className="text-xs text-primary hover:underline">
            View all
          </a>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-muted-foreground border-b border-border">
                {[
                  'Symbol',
                  'Side',
                  'Entry',
                  'Exit',
                  'Qty',
                  'P&L',
                  'Source',
                  'Exchange',
                  'Time',
                ].map(h => (
                  <th key={h} className="text-left pb-2 pr-4 font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {recentTrades.map(trade => (
                <tr key={trade.id} className="hover:bg-accent/30 transition-colors">
                  <td className="py-2.5 pr-4 font-mono font-medium">{trade.symbol}</td>
                  <td className="py-2.5 pr-4">
                    <span
                      className={cn(
                        'px-1.5 py-0.5 rounded text-[10px] font-bold uppercase',
                        trade.side === 'buy'
                          ? 'bg-profit text-green-300'
                          : 'bg-loss text-red-300',
                      )}
                    >
                      {trade.side}
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 font-mono">
                    {trade.entry_price != null
                      ? `$${Number(trade.entry_price).toLocaleString()}`
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4 font-mono">
                    {trade.exit_price != null
                      ? `$${Number(trade.exit_price).toLocaleString()}`
                      : '—'}
                  </td>
                  <td className="py-2.5 pr-4 font-mono">{trade.quantity}</td>
                  <td
                    className={cn(
                      'py-2.5 pr-4 font-mono font-semibold',
                      Number(trade.pnl) >= 0 ? 'text-profit' : 'text-loss',
                    )}
                  >
                    {Number(trade.pnl) >= 0 ? '+' : ''}${Number(trade.pnl).toFixed(2)}
                    <span className="ml-1 text-[10px] opacity-70">
                      ({Number(trade.pnl_pct).toFixed(2)}%)
                    </span>
                  </td>
                  <td className="py-2.5 pr-4 text-muted-foreground">
                    {trade.signal_source}
                  </td>
                  <td className="py-2.5 pr-4">
                    <span className="px-1.5 py-0.5 rounded bg-accent text-[10px] capitalize">
                      {trade.exchange}
                    </span>
                  </td>
                  <td className="py-2.5 text-muted-foreground">
                    {new Date(trade.opened_at).toLocaleDateString()}
                  </td>
                </tr>
              ))}
              {!recentTrades.length && (
                <tr>
                  <td colSpan={9} className="py-6 text-center text-muted-foreground">
                    No trades yet — agents will populate this table as they run.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center gap-3 px-4 py-3 bg-orange-500/5 border border-orange-500/15 rounded-xl">
        <AlertTriangle className="w-4 h-4 text-orange-400 shrink-0" />
        <p className="text-xs text-muted-foreground">
          <span className="text-orange-400 font-medium">Risk Reminder:</span> All agents
          operate under a 2% per-trade hard cap. Daily drawdown limits halt trading
          automatically. AI optimization only tunes parameters — it does not override risk
          rules.
        </p>
      </div>
    </div>
  );
}
