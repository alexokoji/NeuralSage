'use client';

import { useState, useEffect, useCallback } from 'react';
import { cn } from '@/lib/utils';
import {
  Bot,
  Plus,
  Play,
  Pause,
  Square,
  Settings2,
  TrendingUp,
  Zap,
  Target,
  Shield,
  Activity,
  Lightbulb,
  Loader2,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Slider } from '@/components/ui/slider';
import { usePolling, useApiKeys, useStrategies } from '@/lib/api/hooks';
import { api } from '@/lib/api/client';
import { Switch } from '@/components/ui/switch';
import type { Agent, Timeframe } from '@/lib/api/types';

const statusConfig = {
  active: {
    label: 'Active',
    color: 'text-green-400',
    bg: 'bg-green-500/10 border-green-500/20',
    dot: 'bg-green-400',
  },
  paused: {
    label: 'Paused',
    color: 'text-orange-400',
    bg: 'bg-orange-500/10 border-orange-500/20',
    dot: 'bg-orange-400',
  },
  idle: {
    label: 'Idle',
    color: 'text-muted-foreground',
    bg: 'bg-border',
    dot: 'bg-muted-foreground',
  },
  stopped: {
    label: 'Stopped',
    color: 'text-red-400',
    bg: 'bg-red-500/10 border-red-500/20',
    dot: 'bg-red-400',
  },
  error: {
    label: 'Error',
    color: 'text-red-400',
    bg: 'bg-red-500/10 border-red-500/20',
    dot: 'bg-red-400',
  },
} as const;

function formatWAT(iso: string | null): string {
  if (!iso) return 'never';
  return new Date(iso).toLocaleTimeString('en-NG', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    timeZone: 'Africa/Lagos',
  });
}

const signalColors: Record<string, string> = {
  enter_long: 'text-green-400',
  enter_short: 'text-red-400',
  exit: 'text-orange-400',
  hold: 'text-muted-foreground',
};

function AgentCard({
  agent,
  onAction,
  onEdit,
}: {
  agent: Agent;
  onAction: (id: string, action: 'start' | 'pause' | 'stop') => Promise<void>;
  onEdit: (agent: Agent) => void;
}) {
  // Re-render every second so "Last scan: Xs ago" and the Analysing badge
  // update in real-time between API polls.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const [suggestions, setSuggestions] = useState<{
    suggestions: { param: string; current: string; recommended: string; reason: string }[];
    risk_assessment: string;
    timeframe_advice?: string;
  } | null>(null);
  const [loadingSuggestions, setLoadingSuggestions] = useState(false);
  const [showSuggestions, setShowSuggestions] = useState(false);

  const fetchSuggestions = useCallback(async () => {
    setLoadingSuggestions(true);
    try {
      const data = await api.agentSuggestions(agent.id);
      setSuggestions(data);
      setShowSuggestions(true);
    } catch (err: any) {
      setSuggestions({ suggestions: [], risk_assessment: err?.message || 'Failed to reach AI service — check that GROQ_API_KEY is set in Render.' });
      setShowSuggestions(true);
    } finally {
      setLoadingSuggestions(false);
    }
  }, [agent.id]);

  const sc = statusConfig[agent.status];
  const winRate =
    agent.total_trades > 0
      ? ((agent.winning_trades / agent.total_trades) * 100).toFixed(0)
      : '0';
  const isProfitable = agent.total_pnl >= 0;
  const isAnalysing =
    agent.status === 'active' &&
    agent.last_tick_at &&
    Date.now() - new Date(agent.last_tick_at).getTime() < 60_000;

  return (
    <div className="bg-card border border-border rounded-xl p-5 space-y-4 hover:border-primary/20 transition-all">
      <div className="flex items-start justify-between">
        <div className="flex items-center gap-3">
          <div
            className={cn(
              'w-10 h-10 rounded-xl flex items-center justify-center border',
              agent.status === 'active'
                ? 'bg-green-500/10 border-green-500/20'
                : 'bg-card border-border',
            )}
          >
            <Bot
              className={cn(
                'w-5 h-5',
                agent.status === 'active' ? 'text-green-400' : 'text-muted-foreground',
              )}
            />
          </div>
          <div>
            <p className="font-semibold text-sm">{agent.name}</p>
            <p className="text-xs text-muted-foreground mt-0.5">{agent.description}</p>
          </div>
        </div>
        <div
          className={cn(
            'flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium border',
            sc.bg,
            sc.color,
          )}
        >
          <div
            className={cn(
              'w-1.5 h-1.5 rounded-full',
              sc.dot,
              agent.status === 'active' && 'animate-pulse',
            )}
          />
          {sc.label}
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {[
          { label: agent.strategy?.name ?? 'Strategy', icon: Zap },
          { label: agent.timeframe, icon: Activity },
          { label: `${Number(agent.max_risk_per_trade).toFixed(1)}% risk`, icon: Shield },
        ].map(({ label, icon: Icon }) => (
          <span
            key={label}
            className="flex items-center gap-1 px-2 py-0.5 bg-accent rounded-md text-[10px] text-muted-foreground"
          >
            <Icon className="w-2.5 h-2.5" />
            {label}
          </span>
        ))}
        {agent.is_paper_trade && (
          <span className="flex items-center gap-1 px-2 py-0.5 bg-blue-500/10 border border-blue-500/20 rounded-md text-[10px] text-blue-400 font-medium">
            Paper
          </span>
        )}
      </div>

      <div className="grid grid-cols-4 gap-3">
        {[
          {
            label: 'Capital',
            value: `$${Number(agent.assigned_capital).toLocaleString()}`,
            color: 'text-foreground',
          },
          {
            label: 'Total P&L',
            value: `${isProfitable ? '+' : ''}$${Number(agent.total_pnl).toFixed(1)}`,
            color: isProfitable ? 'text-profit' : 'text-loss',
          },
          {
            label: "Today's P&L",
            value: `${Number(agent.current_day_pnl) >= 0 ? '+' : ''}$${Number(agent.current_day_pnl || 0).toFixed(2)}`,
            color: Number(agent.current_day_pnl || 0) >= 0 ? 'text-profit' : 'text-loss',
          },
          { label: 'Win Rate', value: `${winRate}%`, color: 'text-foreground' },
          { label: 'Trades', value: String(agent.total_trades), color: 'text-foreground' },
        ].map(({ label, value, color }) => (
          <div key={label} className="text-center">
            <p className={cn('text-sm font-bold font-mono', color)}>{value}</p>
            <p className="text-[10px] text-muted-foreground">{label}</p>
          </div>
        ))}
      </div>

      <div className="space-y-1.5">
        <div className="flex justify-between text-[10px] text-muted-foreground">
          <span>AI Confidence</span>
          <span className="font-mono">{Number(agent.confidence_score).toFixed(0)}%</span>
        </div>
        <div className="h-1.5 bg-border rounded-full overflow-hidden">
          <div
            className={cn(
              'h-full rounded-full transition-all',
              Number(agent.confidence_score) > 70
                ? 'bg-green-400'
                : Number(agent.confidence_score) > 40
                ? 'bg-orange-400'
                : 'bg-red-400',
            )}
            style={{ width: `${agent.confidence_score}%` }}
          />
        </div>
      </div>

      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">Today&apos;s P&L</span>
        <span
          className={cn(
            'font-mono font-medium',
            Number(agent.current_day_pnl) >= 0 ? 'text-profit' : 'text-loss',
          )}
        >
          {Number(agent.current_day_pnl) >= 0 ? '+' : ''}$
          {Number(agent.current_day_pnl).toFixed(2)}
        </span>
      </div>

      {/* Agent state banners */}
      {agent.protect_mode && (
        <div className="flex items-center gap-2 px-3 py-2 bg-emerald-500/10 border border-emerald-500/20 rounded-lg animate-slide-up">
          <Shield className="w-3 h-3 text-emerald-400" />
          <span className="text-[10px] text-emerald-400 font-medium">Daily target hit — protecting gains (higher confidence needed). Resets tomorrow.</span>
        </div>
      )}
      {agent.recovery_mode && agent.status === 'active' && (
        <div className="flex items-center gap-2 px-3 py-2 bg-orange-500/10 border border-orange-500/20 rounded-lg">
          <Zap className="w-3 h-3 text-orange-400" />
          <span className="text-[10px] text-orange-400 font-medium">Recovery mode — trading at half size after losses.</span>
        </div>
      )}
      {agent.status === 'paused' && (
        <div className="flex items-center gap-2 px-3 py-2 bg-red-500/10 border border-red-500/20 rounded-lg">
          <span className="text-[10px] text-red-400 font-medium">Auto-paused after consecutive losses. Resume when ready.</span>
        </div>
      )}

      {/* Session trades counter — always visible so you can track even when paused */}
      <div className="flex justify-between text-[10px] text-muted-foreground">
        <span>Session trades</span>
        <span className="font-mono">{agent.session_trade_count ?? 0}</span>
      </div>

      {/* Coach performance snapshot — shown once the coach agent has reviewed */}
      {(agent.performance_snapshot?.total_trades ?? 0) > 0 && (
        <div className="bg-accent/30 border border-border rounded-lg p-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-medium text-muted-foreground uppercase tracking-wide">Coach Review</span>
            {agent.last_coach_review_at && (
              <span className="text-[9px] text-muted-foreground">{formatWAT(agent.last_coach_review_at)}</span>
            )}
          </div>
          <div className="grid grid-cols-3 gap-2">
            {[
              {
                label: 'Win Rate',
                value: `${agent.performance_snapshot?.win_rate?.toFixed(0) ?? 0}%`,
                color: (agent.performance_snapshot?.win_rate ?? 0) >= 45 ? 'text-profit' : 'text-loss',
              },
              {
                label: 'Profit Factor',
                value: (agent.performance_snapshot?.profit_factor ?? 0).toFixed(2),
                color: (agent.performance_snapshot?.profit_factor ?? 0) >= 1.0 ? 'text-profit' : 'text-loss',
              },
              {
                label: 'Drawdown',
                value: `$${(agent.performance_snapshot?.max_drawdown_usdt ?? 0).toFixed(3)}`,
                color: 'text-muted-foreground',
              },
            ].map(({ label, value, color }) => (
              <div key={label} className="text-center">
                <p className={`text-xs font-bold font-mono ${color}`}>{value}</p>
                <p className="text-[9px] text-muted-foreground">{label}</p>
              </div>
            ))}
          </div>
          {Object.entries(agent.performance_snapshot?.by_regime ?? {}).map(([regime, stats]) => (
            <div key={regime} className="flex items-center justify-between text-[9px]">
              <span className="text-muted-foreground capitalize">{regime.replace('_', ' ')}</span>
              <span className={stats.win_rate >= 45 ? 'text-profit font-mono' : 'text-loss font-mono'}>
                {stats.win_rate}% · {stats.total}t · {stats.pnl >= 0 ? '+' : ''}${stats.pnl.toFixed(3)}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Live activity row */}
      <div className="border-t border-border pt-3 space-y-1.5">
        <div className="flex items-center justify-between text-[10px]">
          <span className="text-muted-foreground flex items-center gap-1">
            <Activity className={cn('w-2.5 h-2.5', isAnalysing && 'animate-pulse text-green-400')} />
            {isAnalysing ? 'Analysing' : 'Last scan'}
          </span>
          <span className="font-mono text-muted-foreground">{formatWAT(agent.last_tick_at)}</span>
        </div>
        {agent.last_signal && (
          <div className="flex items-center justify-between text-[10px]">
            <span className="text-muted-foreground">Signal</span>
            <span className={cn('font-mono font-medium', signalColors[agent.last_signal] ?? 'text-foreground')}>
              {agent.last_signal.replace('_', ' ').toUpperCase()}
              {agent.last_signal_symbol ? ` · ${agent.last_signal_symbol}` : ''}
            </span>
          </div>
        )}
        {agent.last_error && (
          <div className="flex items-start gap-1 bg-red-500/5 border border-red-500/20 rounded-md px-2 py-1.5">
            <span className="text-[9px] text-red-400 leading-relaxed">{agent.last_error}</span>
          </div>
        )}
      </div>

      <div className="flex gap-2 pt-1">
        {agent.status === 'active' ? (
          <button
            onClick={() => onAction(agent.id, 'pause')}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 bg-orange-500/10 hover:bg-orange-500/20 border border-orange-500/20 text-orange-400 rounded-lg text-xs font-medium transition-all"
          >
            <Pause className="w-3.5 h-3.5" /> Pause
          </button>
        ) : (
          <button
            onClick={() => onAction(agent.id, 'start')}
            className="flex-1 flex items-center justify-center gap-1.5 py-2 bg-green-500/10 hover:bg-green-500/20 border border-green-500/20 text-green-400 rounded-lg text-xs font-medium transition-all"
          >
            <Play className="w-3.5 h-3.5" /> Start
          </button>
        )}
        <button
          onClick={() => onAction(agent.id, 'stop')}
          className="py-2 px-3 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 text-red-400 rounded-lg text-xs transition-all"
        >
          <Square className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => onEdit(agent)}
          className="py-2 px-3 bg-accent hover:bg-accent/80 border border-border text-muted-foreground hover:text-foreground rounded-lg text-xs transition-all"
          title="Edit agent"
        >
          <Settings2 className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={fetchSuggestions}
          disabled={loadingSuggestions}
          className="py-2 px-3 bg-cyan-500/10 hover:bg-cyan-500/20 border border-cyan-500/20 text-cyan-400 rounded-lg text-xs transition-all"
          title="AI Suggestions"
        >
          {loadingSuggestions ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Lightbulb className="w-3.5 h-3.5" />}
        </button>
      </div>

      {showSuggestions && suggestions && (
        <div className="border-t border-border pt-3 space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-semibold text-cyan-400 flex items-center gap-1">
              <Lightbulb className="w-3 h-3" /> AI Suggestions
            </span>
            <button onClick={() => setShowSuggestions(false)} className="text-[10px] text-muted-foreground hover:text-foreground">
              Hide
            </button>
          </div>
          {suggestions.risk_assessment && (
            <p className="text-[10px] text-orange-400 bg-orange-500/5 border border-orange-500/20 rounded-md px-2 py-1.5">
              {suggestions.risk_assessment}
            </p>
          )}
          {suggestions.timeframe_advice && (
            <p className="text-[10px] text-muted-foreground bg-accent rounded-md px-2 py-1.5">
              <span className="font-semibold">Timeframe:</span> {suggestions.timeframe_advice}
            </p>
          )}
          {suggestions.suggestions.map((s, i) => (
            <div key={i} className="bg-accent rounded-md px-2.5 py-2 space-y-0.5">
              <div className="flex items-center justify-between text-[10px]">
                <span className="font-semibold text-foreground">{s.param}</span>
                <span className="font-mono">
                  <span className="text-red-400 line-through">{s.current}</span>
                  {' → '}
                  <span className="text-green-400">{s.recommended}</span>
                </span>
              </div>
              <p className="text-[9px] text-muted-foreground">{s.reason}</p>
            </div>
          ))}
          {suggestions.suggestions.length === 0 && (
            <p className="text-[10px] text-muted-foreground text-center py-2">No changes recommended at this time.</p>
          )}
        </div>
      )}
    </div>
  );
}

const TIMEFRAMES: Timeframe[] = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'];

const CRYPTO_MARKETS = [
  'BTCUSDT',
  'ETHUSDT',
  'SOLUSDT',
  'BNBUSDT',
  'XRPUSDT',
  'ADAUSDT',
  'DOGEUSDT',
  'AVAXUSDT',
  'DOTUSDT',
  'LINKUSDT',
  'LTCUSDT',
  'MATICUSDT',
  'TONUSDT',
  'SUIUSDT',
  'APTUSDT',
  'ARBUSDT',
];

const FOREX_MARKETS = [
  'EURUSD',
  'GBPUSD',
  'USDJPY',
  'AUDUSD',
  'USDCAD',
  'USDCHF',
  'NZDUSD',
  'GBPJPY',
  'EURJPY',
  'EURGBP',
  'AUDJPY',
  'EURAUD',
  'GBPAUD',
  'XAUUSD',
  'XAGUSD',
];

function getMarketsForExchange(exchange: string): string[] {
  if (exchange.startsWith('deriv')) return FOREX_MARKETS;
  return CRYPTO_MARKETS;
}

// Backward compat
const MARKETS = CRYPTO_MARKETS;

export default function AgentsPage() {
  const { data: agents, refetch: refetchAgents, error: agentsError } = usePolling(
    () => api.listAgents(),
    5_000,  // poll every 5 s so the UI catches each backend tick within one cycle
  );
  const { data: apiKeys } = useApiKeys();
  const { data: strategies } = useStrategies();

  const [showCreate, setShowCreate] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');

  const [editTarget, setEditTarget] = useState<Agent | null>(null);
  const [editSubmitting, setEditSubmitting] = useState(false);
  const [editError, setEditError] = useState('');
  const [editForm, setEditForm] = useState({
    name: '',
    api_key_id: '',
    capital: 100,
    riskPct: 2,
    pairs: ['BTCUSDT'] as string[],
    timeframe: '15m' as Timeframe,
    isPaperTrade: false,
  });

  function openEdit(agent: Agent) {
    setEditTarget(agent);
    setEditError('');
    setEditForm({
      name: agent.name,
      api_key_id: agent.api_key_id ?? '',
      capital: agent.assigned_capital,
      riskPct: agent.max_risk_per_trade,
      pairs: agent.trading_pairs,
      timeframe: agent.timeframe,
      isPaperTrade: agent.is_paper_trade,
    });
  }

  async function handleEdit(e: React.FormEvent) {
    e.preventDefault();
    if (!editTarget) return;
    setEditSubmitting(true);
    setEditError('');
    try {
      await api.updateAgent(editTarget.id, {
        name: editForm.name,
        api_key_id: editForm.api_key_id || undefined,
        assigned_capital: editForm.capital,
        trading_pairs: editForm.pairs,
        timeframe: editForm.timeframe,
        max_risk_per_trade: editForm.riskPct,
        is_paper_trade: editForm.isPaperTrade,
      });
      await refetchAgents();
      setEditTarget(null);
    } catch (exc) {
      setEditError((exc as Error).message);
    } finally {
      setEditSubmitting(false);
    }
  }

  async function handleDelete(id: string) {
    if (!window.confirm('Delete this agent? This cannot be undone.')) return;
    try {
      await api.deleteAgent(id);
      await refetchAgents();
      setEditTarget(null);
    } catch (exc) {
      setEditError((exc as Error).message);
    }
  }

  const [form, setForm] = useState({
    name: '',
    description: '',
    api_key_id: '',
    strategy_id: '',
    capital: 100,
    riskPct: 2,
    pairs: ['BTCUSDT'] as string[],
    timeframe: '15m' as Timeframe,
    isPaperTrade: false,
  });

  async function handleAction(id: string, action: 'start' | 'pause' | 'stop') {
    try {
      await api.controlAgent(id, action);
      await refetchAgents();
    } catch (exc) {
      // Surface failures non-disruptively.
      console.error('agent action failed', exc);
    }
  }

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    setFormError('');
    if (!form.api_key_id || !form.strategy_id || !form.name) return;
    setSubmitting(true);
    try {
      await api.createAgent({
        name: form.name,
        description: form.description,
        api_key_id: form.api_key_id,
        strategy_id: form.strategy_id,
        assigned_capital: form.capital,
        currency: (() => { const ex = apiKeys?.find(k => k.id === form.api_key_id)?.exchange || ''; return ex.startsWith('deriv') || ex.startsWith('mt5') || ex.startsWith('oanda') ? 'USD' : 'USDT'; })(),
        trading_pairs: form.pairs,
        timeframe: form.timeframe,
        max_risk_per_trade: form.riskPct,
        is_paper_trade: form.isPaperTrade,
      });
      await refetchAgents();
      setShowCreate(false);
      setForm({
        name: '',
        description: '',
        api_key_id: '',
        strategy_id: '',
        capital: 100,
        riskPct: 2,
        pairs: ['BTCUSDT'],
        timeframe: '15m',
        isPaperTrade: false,
      });
    } catch (exc) {
      setFormError((exc as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  const list = agents ?? [];
  const totalCapital = list.reduce((a, b) => a + Number(b.assigned_capital), 0);
  const totalPnl = list.reduce((a, b) => a + Number(b.total_pnl), 0);
  const activeCount = list.filter(a => a.status === 'active').length;

  return (
    <div className="p-4 md:p-6 space-y-4 md:space-y-6 max-w-[1400px]">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-bold">AI Agents</h2>
          <p className="text-xs text-muted-foreground mt-0.5">
            Configure and manage your trading agents
          </p>
        </div>
        <Button onClick={() => setShowCreate(true)} className="h-9 text-xs gap-2 shrink-0">
          <Plus className="w-3.5 h-3.5" /> <span className="hidden sm:inline">New Agent</span><span className="sm:hidden">New</span>
        </Button>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 md:gap-4">
        {[
          {
            label: 'Active Agents',
            value: `${activeCount}/${list.length}`,
            icon: Bot,
            color: 'text-green-400',
          },
          {
            label: 'Total Assigned',
            value: `$${totalCapital.toLocaleString()}`,
            icon: Target,
            color: 'text-primary',
          },
          {
            label: 'Combined P&L',
            value: `${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(2)}`,
            icon: TrendingUp,
            color: totalPnl >= 0 ? 'text-profit' : 'text-loss',
          },
        ].map(({ label, value, icon: Icon, color }) => (
          <div
            key={label}
            className="bg-card border border-border rounded-xl p-4 flex items-center gap-3"
          >
            <Icon className={cn('w-5 h-5', color)} />
            <div>
              <p className="text-lg font-bold font-mono">{value}</p>
              <p className="text-xs text-muted-foreground">{label}</p>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3 px-4 py-3 bg-orange-500/5 border border-orange-500/15 rounded-xl">
        <Shield className="w-4 h-4 text-orange-400 shrink-0" />
        <p className="text-xs text-muted-foreground">
          <span className="text-orange-400 font-medium">Risk Engine Active:</span> Max 2%
          risk/trade is enforced. Agents stop after consecutive losses. Daily drawdown
          limit overrides all AI signals.
        </p>
      </div>

      {agentsError && (
        <div className="px-4 py-3 bg-red-500/5 border border-red-500/15 rounded-xl text-xs text-red-400">
          Failed to load agents: {agentsError.message}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-5">
        {list.map(agent => (
          <AgentCard key={agent.id} agent={agent} onAction={handleAction} onEdit={openEdit} />
        ))}
        {list.length === 0 && agents !== null && (
          <div className="col-span-full text-center py-12 bg-card border border-dashed border-border rounded-xl">
            <Bot className="w-8 h-8 text-muted-foreground mx-auto mb-3" />
            <p className="text-sm font-medium">No agents yet</p>
            <p className="text-xs text-muted-foreground mt-1">
              Create your first AI agent to start trading.
            </p>
          </div>
        )}
      </div>

      <div className="bg-card border border-border rounded-xl p-5 space-y-4">
        <p className="text-sm font-semibold">Available Strategies</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {(strategies ?? []).map(s => (
            <div
              key={s.id}
              className="p-3 bg-background border border-border rounded-lg space-y-2 hover:border-primary/20 transition-all"
            >
              <div className="flex items-center gap-2">
                <Zap className="w-3.5 h-3.5 text-primary" />
                <p className="text-xs font-semibold">{s.name}</p>
              </div>
              <p className="text-[10px] text-muted-foreground">{s.description}</p>
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          <span className="text-primary">AI Optimization:</span> Bayesian parameter tuning
          adjusts stop-loss, take-profit, and entry thresholds. It does NOT make
          autonomous trading decisions.
        </p>
      </div>

      {/* Edit Agent Dialog */}
      <Dialog open={!!editTarget} onOpenChange={open => !open && setEditTarget(null)}>
        <DialogContent className="bg-card border-border max-w-lg">
          <DialogHeader>
            <DialogTitle>Edit Agent</DialogTitle>
          </DialogHeader>
          {editTarget?.status === 'active' && (
            <div className="px-3 py-2 bg-orange-500/5 border border-orange-500/20 rounded-lg text-[11px] text-orange-400">
              Pause the agent before editing its settings.
            </div>
          )}
          <form onSubmit={handleEdit} className="space-y-4 mt-1">
            <div className="space-y-2">
              <Label>Agent Name</Label>
              <Input
                value={editForm.name}
                onChange={e => setEditForm(p => ({ ...p, name: e.target.value }))}
                required
                className="bg-background border-border"
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Exchange Account</Label>
                <Select
                  value={editForm.api_key_id}
                  onValueChange={v => setEditForm(p => ({ ...p, api_key_id: v }))}
                >
                  <SelectTrigger className="bg-background border-border">
                    <SelectValue placeholder="Select API key" />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    {(apiKeys ?? []).map(k => (
                      <SelectItem key={k.id} value={k.id}>
                        {k.label || `${k.exchange}${k.is_testnet ? ' (testnet)' : ''}`}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Assigned Capital (USDT)</Label>
                <Input
                  type="number"
                  min={5}
                  step={5}
                  value={editForm.capital}
                  onChange={e => setEditForm(p => ({ ...p, capital: Number(e.target.value) }))}
                  className="bg-background border-border"
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label>Timeframe</Label>
              <Select
                value={editForm.timeframe}
                onValueChange={v => setEditForm(p => ({ ...p, timeframe: v as Timeframe }))}
              >
                <SelectTrigger className="bg-background border-border">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="bg-card border-border">
                  {TIMEFRAMES.map(t => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>
                Max Risk Per Trade:{' '}
                <span className="text-primary font-mono">{editForm.riskPct}%</span>
              </Label>
              <Slider
                min={0.5}
                max={2}
                step={0.5}
                value={[editForm.riskPct]}
                onValueChange={([v]) => setEditForm(p => ({ ...p, riskPct: v }))}
                className="w-full"
              />
            </div>

            <div className="space-y-2">
              <Label>
                Markets{' '}
                <span className="text-muted-foreground font-normal">
                  ({editForm.pairs.length} selected)
                </span>
              </Label>
              <div className="flex flex-wrap gap-1.5 p-3 bg-background border border-border rounded-lg max-h-36 overflow-y-auto">
                {getMarketsForExchange(apiKeys?.find(k => k.id === editForm.api_key_id)?.exchange || editTarget?.strategy?.type || '').map(m => {
                  const selected = editForm.pairs.includes(m);
                  return (
                    <button
                      key={m}
                      type="button"
                      onClick={() =>
                        setEditForm(p => ({
                          ...p,
                          pairs: selected
                            ? p.pairs.filter(x => x !== m)
                            : [...p.pairs, m],
                        }))
                      }
                      className={cn(
                        'px-2 py-1 rounded-md text-[11px] font-mono font-medium border transition-all',
                        selected
                          ? 'bg-primary/15 border-primary/40 text-primary'
                          : 'bg-accent/50 border-border text-muted-foreground hover:border-primary/20 hover:text-foreground',
                      )}
                    >
                      {m.replace('USDT', '/USDT')}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex items-center justify-between p-3 bg-blue-500/5 border border-blue-500/15 rounded-lg">
              <div>
                <p className="text-xs font-medium text-blue-400">Paper Trading</p>
                <p className="text-[10px] text-muted-foreground mt-0.5">
                  Simulates trades without hitting the exchange.
                </p>
              </div>
              <Switch
                checked={editForm.isPaperTrade}
                onCheckedChange={v => setEditForm(p => ({ ...p, isPaperTrade: v }))}
              />
            </div>

            {editError && (
              <div className="text-xs text-red-400 bg-red-500/5 border border-red-500/20 rounded-lg px-3 py-2">
                {editError}
              </div>
            )}

            <div className="flex gap-3 pt-1">
              <button
                type="button"
                onClick={() => editTarget && handleDelete(editTarget.id)}
                className="px-3 py-2 text-xs text-red-400 hover:text-red-300 hover:bg-red-500/5 border border-red-500/20 rounded-lg transition-all"
              >
                Delete
              </button>
              <div className="flex-1" />
              <Button
                type="button"
                variant="outline"
                onClick={() => setEditTarget(null)}
                className="border-border"
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="bg-primary"
                disabled={editSubmitting || editTarget?.status === 'active' || editForm.pairs.length === 0}
              >
                {editSubmitting ? 'Saving…' : 'Save Changes'}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent className="bg-card border-border max-w-lg">
          <DialogHeader>
            <DialogTitle>Create AI Agent</DialogTitle>
          </DialogHeader>
          <form onSubmit={handleCreate} className="space-y-4 mt-2">
            <div className="space-y-2">
              <Label>Agent Name</Label>
              <Input
                value={form.name}
                onChange={e => setForm(p => ({ ...p, name: e.target.value }))}
                placeholder="My Trading Agent"
                required
                className="bg-background border-border"
              />
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Exchange Account</Label>
                <Select
                  value={form.api_key_id}
                  onValueChange={v => {
                    const key = apiKeys?.find(k => k.id === v);
                    const isForex = key?.exchange.startsWith('deriv') || key?.exchange.startsWith('mt5');
                    const defaultPairs = isForex ? ['EURUSD'] : ['BTCUSDT'];
                    setForm(p => ({ ...p, api_key_id: v, pairs: defaultPairs }));
                  }}
>
                  <SelectTrigger className="bg-background border-border">
                    <SelectValue
                      placeholder={(apiKeys ?? []).length ? 'Select API key' : 'Add an API key first'}
                    />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    {(apiKeys ?? []).map(k => (
                      <SelectItem key={k.id} value={k.id}>
                        {k.label || `${k.exchange}${k.is_testnet ? ' (testnet)' : ''}`}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-2">
                <Label>Strategy</Label>
                <Select
                  value={form.strategy_id}
                  onValueChange={v => setForm(p => ({ ...p, strategy_id: v }))}
                >
                  <SelectTrigger className="bg-background border-border">
                    <SelectValue placeholder="Select strategy" />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    {(strategies ?? []).map(s => (
                      <SelectItem key={s.id} value={s.id}>
                        {s.name}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Assigned Capital (USDT)</Label>
                <Input
                  type="number"
                  min={5}
                  step={5}
                  value={form.capital}
                  onChange={e => setForm(p => ({ ...p, capital: Number(e.target.value) }))}
                  className="bg-background border-border"
                />
              </div>
              <div className="space-y-2">
                <Label>Timeframe</Label>
                <Select
                  value={form.timeframe}
                  onValueChange={v => setForm(p => ({ ...p, timeframe: v as Timeframe }))}
                >
                  <SelectTrigger className="bg-background border-border">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    {TIMEFRAMES.map(t => (
                      <SelectItem key={t} value={t}>
                        {t}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>

            <div className="space-y-2">
              <Label>
                Max Risk Per Trade:{' '}
                <span className="text-primary font-mono">{form.riskPct}%</span>
              </Label>
              <Slider
                min={0.5}
                max={2}
                step={0.5}
                value={[form.riskPct]}
                onValueChange={([v]) => setForm(p => ({ ...p, riskPct: v }))}
                className="w-full"
              />
              <p className="text-[10px] text-muted-foreground">
                System hard cap is 2% per trade.
              </p>
            </div>

            <div className="space-y-2">
              <Label>
                Markets{' '}
                <span className="text-muted-foreground font-normal">
                  ({form.pairs.length} selected)
                </span>
              </Label>
              <div className="flex flex-wrap gap-1.5 p-3 bg-background border border-border rounded-lg max-h-36 overflow-y-auto">
                {getMarketsForExchange(apiKeys?.find(k => k.id === form.api_key_id)?.exchange || '').map(m => {
                  const selected = form.pairs.includes(m);
                  return (
                    <button
                      key={m}
                      type="button"
                      onClick={() =>
                        setForm(p => ({
                          ...p,
                          pairs: selected
                            ? p.pairs.filter(x => x !== m)
                            : [...p.pairs, m],
                        }))
                      }
                      className={cn(
                        'px-2 py-1 rounded-md text-[11px] font-mono font-medium border transition-all',
                        selected
                          ? 'bg-primary/15 border-primary/40 text-primary'
                          : 'bg-accent/50 border-border text-muted-foreground hover:border-primary/20 hover:text-foreground',
                      )}
                    >
                      {m.replace('USDT', '/USDT')}
                    </button>
                  );
                })}
              </div>
              {form.pairs.length === 0 && (
                <p className="text-[10px] text-red-400">Select at least one market.</p>
              )}
            </div>

            <div className="flex items-center justify-between p-3 bg-blue-500/5 border border-blue-500/15 rounded-lg">
              <div>
                <p className="text-xs font-medium text-blue-400">Paper Trading</p>
                <p className="text-[10px] text-muted-foreground mt-0.5">
                  Simulates trades without hitting the exchange — safe for testing.
                </p>
              </div>
              <Switch
                checked={form.isPaperTrade}
                onCheckedChange={v => setForm(p => ({ ...p, isPaperTrade: v }))}
              />
            </div>

            {formError && (
              <div className="text-xs text-red-400 bg-red-500/5 border border-red-500/20 rounded-lg px-3 py-2">
                {formError}
              </div>
            )}

            <div className="flex gap-3 pt-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => setShowCreate(false)}
                className="flex-1 border-border"
              >
                Cancel
              </Button>
              <Button
                type="submit"
                className="flex-1 bg-primary"
                disabled={
                  submitting ||
                  !form.name ||
                  !form.strategy_id ||
                  !form.api_key_id ||
                  form.pairs.length === 0
                }
              >
                {submitting ? 'Creating…' : 'Create Agent'}
              </Button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
}
