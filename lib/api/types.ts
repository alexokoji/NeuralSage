// Types mirror the backend Pydantic schemas in backend/app/schemas/.
// Keep these in sync if you change the backend models.

export type Exchange = 'bybit' | 'bitget' | 'bybit_testnet';
export type Permission = 'read' | 'trade';
export type AgentStatus = 'idle' | 'active' | 'paused' | 'stopped' | 'error';
export type StrategyType = 'ema_crossover' | 'rsi_entry' | 'breakout' | 'micro_scalping';
export type Timeframe = '1m' | '3m' | '5m' | '15m' | '30m' | '1h' | '4h' | '1d';
export type RiskLevel = 'low' | 'medium' | 'high';
export type OrderSide = 'buy' | 'sell';
export type OrderType = 'market' | 'limit' | 'stop_loss' | 'take_profit';
export type TradeStatus = 'pending' | 'open' | 'filled' | 'cancelled' | 'rejected' | 'expired';

export interface User {
  id: string;
  email: string;
  full_name: string;
  username: string | null;
  avatar_url: string;
  timezone: string;
  risk_level: RiskLevel;
  daily_loss_limit: number;
  max_concurrent_trades: number;
  notifications_enabled: boolean;
  two_factor_enabled: boolean;
  created_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
  expires_in: number;
}

export interface ApiKey {
  id: string;
  exchange: Exchange;
  label: string;
  permissions: string[];
  is_active: boolean;
  is_testnet: boolean;
  verified: boolean;
  last_verified_at: string | null;
  balance_cache: Record<string, number>;
  balance_updated_at: string | null;
  created_at: string;
}

export interface ApiKeyCreate {
  exchange: Exchange;
  label?: string;
  api_key: string;
  api_secret: string;
  is_testnet?: boolean;
  permissions?: Permission[];
}

export interface Strategy {
  id: string;
  name: string;
  type: StrategyType;
  description: string;
  default_params: Record<string, number>;
  is_system: boolean;
}

export interface Agent {
  id: string;
  user_id: string;
  api_key_id: string | null;
  strategy_id: string | null;
  name: string;
  description: string;
  status: AgentStatus;
  assigned_capital: number;
  currency: string;
  trading_pairs: string[];
  timeframe: Timeframe;
  max_risk_per_trade: number;
  daily_profit_target: number;
  weekly_profit_target: number;
  max_daily_loss: number;
  max_concurrent_trades: number;
  max_consecutive_losses: number;
  strategy_params: Record<string, unknown>;
  ai_optimization_enabled: boolean;
  optimization_params: Record<string, unknown>;
  total_pnl: number;
  total_trades: number;
  winning_trades: number;
  current_day_pnl: number;
  current_week_pnl: number;
  confidence_score: number;
  last_trade_at: string | null;
  started_at: string | null;
  created_at: string;
  strategy?: Strategy | null;
  last_tick_at: string | null;
  last_signal: string | null;
  last_signal_symbol: string | null;
  last_error: string | null;
  tick_count: number;
}

export interface AgentCreate {
  name: string;
  description?: string;
  api_key_id: string;
  strategy_id: string;
  assigned_capital: number;
  currency?: string;
  trading_pairs: string[];
  timeframe: Timeframe;
  max_risk_per_trade: number;
  daily_profit_target?: number;
  weekly_profit_target?: number;
  max_daily_loss?: number;
  max_concurrent_trades?: number;
  max_consecutive_losses?: number;
  strategy_params?: Record<string, unknown>;
  ai_optimization_enabled?: boolean;
}

export interface Trade {
  id: string;
  user_id: string;
  agent_id: string | null;
  api_key_id: string | null;
  exchange: string;
  exchange_order_id: string | null;
  symbol: string;
  side: OrderSide;
  order_type: OrderType;
  status: TradeStatus;
  quantity: number;
  entry_price: number | null;
  exit_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  pnl: number;
  pnl_pct: number;
  fees: number;
  signal_source: string;
  signal_data: Record<string, unknown>;
  risk_checks: Record<string, unknown>;
  notes: string;
  opened_at: string;
  closed_at: string | null;
}

export interface Position {
  id: string;
  agent_id: string | null;
  exchange: string;
  symbol: string;
  side: 'long' | 'short';
  quantity: number;
  entry_price: number;
  current_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  leverage: number;
  margin_used: number;
  is_open: boolean;
  opened_at: string;
  updated_at: string;
}

export interface Notification {
  id: string;
  type: string;
  title: string;
  message: string;
  data: Record<string, unknown>;
  read: boolean;
  created_at: string;
}

export interface BalanceEntry {
  asset: string;
  available: number;
  total: number;
  usd_value: number | null;
}

export interface ExchangeBalance {
  api_key_id: string;
  exchange: string;
  is_testnet: boolean;
  balances: BalanceEntry[];
  updated_at: string;
}

export interface PortfolioOverview {
  total_balance_usd: number;
  total_pnl: number;
  total_pnl_pct: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  open_positions_count: number;
  active_agents_count: number;
  exchanges: ExchangeBalance[];
}

export interface MarketTicker {
  symbol: string;
  price: number;
  change_24h_pct: number;
  volume_24h: number;
  high_24h: number;
  low_24h: number;
}

export interface Candle {
  t: number;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}
