'use client';

import { useState } from 'react';
import { cn } from '@/lib/utils';
import {
  Zap,
  Plus,
  Loader2,
  Sparkles,
  Shield,
  TrendingUp,
  TrendingDown,
  Activity,
  ChevronDown,
  ChevronUp,
  Copy,
  Check,
} from 'lucide-react';
import { useStrategies } from '@/lib/api/hooks';
import { api } from '@/lib/api/client';

const riskColors = {
  low: 'text-green-400 bg-green-500/10 border-green-500/20',
  medium: 'text-orange-400 bg-orange-500/10 border-orange-500/20',
  high: 'text-red-400 bg-red-500/10 border-red-500/20',
};

const indicatorLabels: Record<string, string> = {
  rsi: 'RSI',
  ema_cross: 'EMA Crossover',
  ema: 'EMA',
  volume: 'Volume',
  price_change: 'Price Change',
  atr: 'ATR',
};

function RuleDisplay({ rule }: { rule: Record<string, unknown> }) {
  const indicator = String(rule.indicator || '');
  const label = indicatorLabels[indicator] || indicator;

  if (indicator === 'ema_cross') {
    return (
      <span className="text-xs">
        {label} ({rule.fast || 9}/{rule.slow || 21}) → {String(rule.direction || 'bullish')}
      </span>
    );
  }
  if (indicator === 'rsi') {
    return (
      <span className="text-xs">
        {label}({rule.period || 14}) {rule.op} {String(rule.value)}
      </span>
    );
  }
  return (
    <span className="text-xs">
      {label} {rule.op} {String(rule.value)}
    </span>
  );
}

interface GeneratedStrategy {
  name?: string;
  description?: string;
  rules?: Record<string, unknown[]>;
  params?: Record<string, number>;
  explanation?: string;
  error?: string;
}

export default function StrategiesPage() {
  const { data: systemStrategies } = useStrategies();
  const [showBuilder, setShowBuilder] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [riskLevel, setRiskLevel] = useState<'low' | 'medium' | 'high'>('medium');
  const [timeframe, setTimeframe] = useState('15m');
  const [generating, setGenerating] = useState(false);
  const [generated, setGenerated] = useState<GeneratedStrategy | null>(null);
  const [copied, setCopied] = useState(false);
  const [expandedSystem, setExpandedSystem] = useState<string | null>(null);

  const generate = async () => {
    if (!prompt.trim() || generating) return;
    setGenerating(true);
    setGenerated(null);
    try {
      const result = await api.generateStrategy(prompt, riskLevel, timeframe);
      setGenerated(result);
    } catch {
      setGenerated({ error: 'Failed to reach AI service' });
    } finally {
      setGenerating(false);
    }
  };

  const copyRules = () => {
    if (generated?.rules) {
      navigator.clipboard.writeText(JSON.stringify({
        rules: generated.rules,
        ...generated.params,
      }, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const systemStrategyInfo: Record<string, { description: string; indicators: string[]; bestFor: string }> = {
    ema_crossover: {
      description: 'Enters when the fast EMA crosses the slow EMA. Trend-following strategy that catches momentum shifts.',
      indicators: ['EMA 9 (fast)', 'EMA 21 (slow)'],
      bestFor: '1h — 4h timeframes, trending markets',
    },
    rsi_entry: {
      description: 'Buys oversold rebounds (RSI < 30) and sells overbought reversals (RSI > 70) with trend confirmation.',
      indicators: ['RSI 14', 'EMA 50 (trend filter)'],
      bestFor: '15m — 1h timeframes, ranging markets',
    },
    breakout: {
      description: 'Enters when price breaks above/below the 20-bar high/low channel with volume confirmation.',
      indicators: ['20-bar Donchian channel', 'Volume multiplier', 'ATR'],
      bestFor: '1h — 4h timeframes, after consolidation',
    },
    micro_scalping: {
      description: 'Captures small mean-reversion moves when price deviates from a short EMA. Fast entries and exits.',
      indicators: ['EMA 8', 'Deviation %'],
      bestFor: '1m — 5m timeframes, liquid pairs',
    },
    composite: {
      description: 'Custom strategy built from multiple indicator conditions. Created via the AI Strategy Builder.',
      indicators: ['User-defined'],
      bestFor: 'Any timeframe, depends on rules',
    },
  };

  return (
    <div className="p-4 md:p-6 space-y-6 max-w-[1200px]">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-bold">Strategies</h2>
          <p className="text-xs text-muted-foreground mt-0.5">System strategies & AI-powered custom builder</p>
        </div>
        <button
          onClick={() => setShowBuilder(o => !o)}
          className="flex items-center gap-2 px-4 py-2 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 transition-colors"
        >
          {showBuilder ? <ChevronUp className="w-4 h-4" /> : <Sparkles className="w-4 h-4" />}
          {showBuilder ? 'Close Builder' : 'AI Strategy Builder'}
        </button>
      </div>

      {/* AI Strategy Builder */}
      {showBuilder && (
        <div className="bg-card border border-primary/20 rounded-xl p-6 space-y-5">
          <div className="flex items-center gap-2">
            <Sparkles className="w-5 h-5 text-primary" />
            <h3 className="font-semibold">AI Strategy Builder</h3>
          </div>
          <p className="text-xs text-muted-foreground">
            Describe the strategy you want in plain English. The AI will generate indicator rules,
            entry/exit conditions, and risk parameters that you can assign to any agent.
          </p>

          <div className="space-y-4">
            <div>
              <label className="text-xs font-medium text-muted-foreground mb-1.5 block">
                Describe your strategy
              </label>
              <textarea
                value={prompt}
                onChange={e => setPrompt(e.target.value)}
                placeholder="e.g. I want a strategy that buys when RSI is oversold and there's a bullish EMA crossover, with tight stops for scalping on 5-minute charts..."
                className="w-full bg-background border border-border rounded-lg px-4 py-3 text-sm outline-none focus:ring-1 focus:ring-primary resize-none h-24"
              />
            </div>

            <div className="flex gap-4">
              <div className="flex-1">
                <label className="text-xs font-medium text-muted-foreground mb-1.5 block">Risk Level</label>
                <div className="flex gap-2">
                  {(['low', 'medium', 'high'] as const).map(r => (
                    <button
                      key={r}
                      onClick={() => setRiskLevel(r)}
                      className={cn(
                        'flex-1 py-2 rounded-lg text-xs font-medium border transition-all capitalize',
                        riskLevel === r ? riskColors[r] : 'border-border text-muted-foreground hover:border-primary/30',
                      )}
                    >
                      {r}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="text-xs font-medium text-muted-foreground mb-1.5 block">Timeframe</label>
                <select
                  value={timeframe}
                  onChange={e => setTimeframe(e.target.value)}
                  className="bg-background border border-border rounded-lg px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
                >
                  {['1m', '5m', '15m', '30m', '1h', '4h'].map(tf => (
                    <option key={tf} value={tf}>{tf}</option>
                  ))}
                </select>
              </div>
            </div>

            <button
              onClick={generate}
              disabled={generating || !prompt.trim()}
              className="w-full flex items-center justify-center gap-2 py-3 bg-primary text-primary-foreground rounded-lg text-sm font-medium hover:bg-primary/90 disabled:opacity-40 transition-all"
            >
              {generating ? (
                <>
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Generating strategy...
                </>
              ) : (
                <>
                  <Sparkles className="w-4 h-4" />
                  Generate Strategy
                </>
              )}
            </button>
          </div>

          {/* Generated result */}
          {generated && !generated.error && (
            <div className="border border-border rounded-xl p-5 space-y-4 bg-background">
              <div className="flex items-start justify-between">
                <div>
                  <h4 className="font-semibold text-sm">{generated.name}</h4>
                  <p className="text-xs text-muted-foreground mt-1">{generated.description}</p>
                </div>
                <button
                  onClick={copyRules}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-accent rounded-lg text-xs hover:bg-accent/80 transition-colors"
                >
                  {copied ? <Check className="w-3 h-3 text-green-400" /> : <Copy className="w-3 h-3" />}
                  {copied ? 'Copied!' : 'Copy Rules'}
                </button>
              </div>

              {/* Rules display */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {['entry_long', 'entry_short', 'exit_long', 'exit_short'].map(key => {
                  const rules = (generated.rules?.[key] || []) as Record<string, unknown>[];
                  if (!rules.length) return null;
                  const isEntry = key.startsWith('entry');
                  const isLong = key.includes('long');
                  return (
                    <div key={key} className="border border-border rounded-lg p-3 space-y-2">
                      <div className="flex items-center gap-1.5">
                        {isEntry ? (
                          isLong ? <TrendingUp className="w-3 h-3 text-green-400" /> : <TrendingDown className="w-3 h-3 text-red-400" />
                        ) : (
                          <Activity className="w-3 h-3 text-orange-400" />
                        )}
                        <span className="text-[10px] font-medium uppercase text-muted-foreground">
                          {key.replace('_', ' ')}
                        </span>
                      </div>
                      {rules.map((rule, i) => (
                        <div key={i} className="flex items-center gap-1.5 pl-4">
                          <div className="w-1 h-1 rounded-full bg-muted-foreground" />
                          <RuleDisplay rule={rule} />
                        </div>
                      ))}
                    </div>
                  );
                })}
              </div>

              {/* Params */}
              {generated.params && (
                <div className="flex flex-wrap gap-2">
                  {Object.entries(generated.params).map(([k, v]) => (
                    <span key={k} className="px-2 py-1 bg-accent rounded-md text-[10px] font-mono">
                      {k}: {typeof v === 'number' ? v.toFixed(2) : v}
                    </span>
                  ))}
                </div>
              )}

              {generated.explanation && (
                <p className="text-xs text-muted-foreground bg-accent/50 rounded-lg p-3">
                  {generated.explanation}
                </p>
              )}

              <p className="text-[10px] text-muted-foreground">
                To use this strategy: create a new agent → choose &quot;Composite&quot; strategy → paste the copied rules into strategy params.
              </p>
            </div>
          )}

          {generated?.error && (
            <div className="border border-red-500/20 bg-red-500/5 rounded-lg p-4">
              <p className="text-xs text-red-400">{generated.error}</p>
            </div>
          )}
        </div>
      )}

      {/* System Strategies */}
      <div className="space-y-3">
        <h3 className="text-sm font-semibold text-muted-foreground">System Strategies</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {(systemStrategies ?? []).map(strat => {
            const info = systemStrategyInfo[strat.type] || {
              description: strat.description,
              indicators: [],
              bestFor: 'Any',
            };
            const isExpanded = expandedSystem === strat.type;

            return (
              <div
                key={strat.id}
                className="bg-card border border-border rounded-xl p-5 space-y-3 hover:border-primary/20 transition-all cursor-pointer"
                onClick={() => setExpandedSystem(isExpanded ? null : strat.type)}
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-center gap-2.5">
                    <div className="w-9 h-9 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
                      <Zap className="w-4 h-4 text-primary" />
                    </div>
                    <div>
                      <p className="text-sm font-semibold">{strat.name}</p>
                      <p className="text-[10px] text-muted-foreground font-mono">{strat.type}</p>
                    </div>
                  </div>
                  {strat.is_system && (
                    <span className="px-2 py-0.5 bg-accent rounded text-[10px] text-muted-foreground">System</span>
                  )}
                </div>

                <p className="text-xs text-muted-foreground">{info.description}</p>

                {isExpanded && (
                  <div className="space-y-2 pt-2 border-t border-border">
                    <div>
                      <p className="text-[10px] font-medium text-muted-foreground mb-1">Indicators</p>
                      <div className="flex flex-wrap gap-1">
                        {info.indicators.map(ind => (
                          <span key={ind} className="px-2 py-0.5 bg-accent rounded text-[10px] font-mono">{ind}</span>
                        ))}
                      </div>
                    </div>
                    <div>
                      <p className="text-[10px] font-medium text-muted-foreground mb-1">Best For</p>
                      <p className="text-xs">{info.bestFor}</p>
                    </div>
                    {strat.default_params && Object.keys(strat.default_params).length > 0 && (
                      <div>
                        <p className="text-[10px] font-medium text-muted-foreground mb-1">Default Parameters</p>
                        <div className="flex flex-wrap gap-1">
                          {Object.entries(strat.default_params).map(([k, v]) => (
                            <span key={k} className="px-2 py-0.5 bg-accent rounded text-[10px] font-mono">
                              {k}: {String(v)}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
