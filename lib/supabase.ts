// Compatibility shim: pages / components historically imported types from
// here when the project was Supabase-backed. The backend is now FastAPI;
// the Supabase JS client has been removed. We re-export the shape-equivalent
// types from `lib/api/types` so legacy imports keep working.

export type {
  Exchange,
  AgentStatus,
  TradeStatus,
  StrategyType,
  RiskLevel,
  Timeframe,
  User as Profile,
  ApiKey,
  Strategy,
  Agent,
  Trade,
  Position,
  Notification,
} from './api/types';

// Marker so that any code path which still tries to use `supabase.foo()`
// fails fast with a clear message instead of a runtime undefined-access.
export const supabase = new Proxy({} as Record<string, never>, {
  get(_target, prop) {
    throw new Error(
      `Supabase client is no longer available. Use the FastAPI \`api\` client from '@/lib/api/client'. (called: ${String(
        prop,
      )})`,
    );
  },
});

// AgentPerformance is currently only used by typed displays on the dashboard.
// Define a minimal compatible shape here.
export interface AgentPerformance {
  id: string;
  agent_id: string;
  user_id: string;
  snapshot_date: string;
  starting_capital: number;
  ending_capital: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  max_drawdown: number;
  sharpe_ratio: number | null;
  win_rate: number;
  avg_win: number;
  avg_loss: number;
  strategy_params_snapshot: Record<string, unknown>;
  created_at: string;
}
