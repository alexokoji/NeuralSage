/*
  # AI Crypto Trading Platform - Full Schema

  ## Tables Created:
  - `profiles` - Extended user profiles with settings
  - `api_keys` - Encrypted exchange API keys (Bybit/Bitget)
  - `agents` - AI trading agents with strategy config
  - `strategies` - Trading strategy definitions
  - `trades` - Full trade history with P&L
  - `positions` - Open positions tracker
  - `agent_performance` - Daily performance snapshots
  - `risk_events` - Risk rule violations log
  - `notifications` - User notification queue
  - `market_snapshots` - Cached market data

  ## Security:
  - RLS enabled on all tables
  - Users can only access their own data
  - API keys stored with encryption metadata only (actual encryption in backend)
*/

-- Profiles (extends auth.users)
CREATE TABLE IF NOT EXISTS profiles (
  id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  username text UNIQUE,
  full_name text DEFAULT '',
  avatar_url text DEFAULT '',
  timezone text DEFAULT 'UTC',
  risk_level text DEFAULT 'medium' CHECK (risk_level IN ('low', 'medium', 'high')),
  daily_loss_limit numeric DEFAULT 5.0,
  max_concurrent_trades int DEFAULT 5,
  notifications_enabled boolean DEFAULT true,
  two_factor_enabled boolean DEFAULT false,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own profile"
  ON profiles FOR SELECT TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Users can update own profile"
  ON profiles FOR UPDATE TO authenticated
  USING (auth.uid() = id)
  WITH CHECK (auth.uid() = id);

CREATE POLICY "Users can insert own profile"
  ON profiles FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = id);

-- API Keys (encrypted storage)
CREATE TABLE IF NOT EXISTS api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  exchange text NOT NULL CHECK (exchange IN ('bybit', 'bitget', 'bybit_testnet')),
  label text DEFAULT '',
  encrypted_api_key text NOT NULL,
  encrypted_api_secret text NOT NULL,
  encryption_iv text NOT NULL,
  permissions text[] DEFAULT ARRAY['read', 'trade'],
  is_active boolean DEFAULT true,
  is_testnet boolean DEFAULT false,
  last_verified_at timestamptz,
  verified boolean DEFAULT false,
  balance_cache jsonb DEFAULT '{}',
  balance_updated_at timestamptz,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  CONSTRAINT no_withdrawal_permission CHECK (NOT ('withdraw' = ANY(permissions)))
);

ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own api keys"
  ON api_keys FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own api keys"
  ON api_keys FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own api keys"
  ON api_keys FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own api keys"
  ON api_keys FOR DELETE TO authenticated
  USING (auth.uid() = user_id);

-- Strategies
CREATE TABLE IF NOT EXISTS strategies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  type text NOT NULL CHECK (type IN ('ema_crossover', 'rsi_entry', 'breakout', 'micro_scalping')),
  description text DEFAULT '',
  default_params jsonb DEFAULT '{}',
  is_system boolean DEFAULT true,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE strategies ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Anyone can read strategies"
  ON strategies FOR SELECT TO authenticated
  USING (true);

-- Insert default strategies
INSERT INTO strategies (name, type, description, default_params, is_system) VALUES
('EMA Crossover', 'ema_crossover', 'Uses 9/21 EMA crossover for trend following entries and exits', 
  '{"fast_ema": 9, "slow_ema": 21, "stop_loss_pct": 1.5, "take_profit_pct": 3.0, "position_size_pct": 5}', true),
('RSI Entry', 'rsi_entry', 'Enters on RSI oversold/overbought conditions with confirmation', 
  '{"rsi_period": 14, "oversold": 30, "overbought": 70, "stop_loss_pct": 1.0, "take_profit_pct": 2.0, "position_size_pct": 3}', true),
('Breakout Strategy', 'breakout', 'Trades breakouts from consolidation ranges with volume confirmation', 
  '{"lookback_period": 20, "breakout_threshold": 0.5, "stop_loss_pct": 1.2, "take_profit_pct": 2.5, "position_size_pct": 4}', true),
('Micro Scalping', 'micro_scalping', 'High-frequency small profit captures on 1-5m timeframes', 
  '{"profit_target_pct": 0.3, "stop_loss_pct": 0.2, "max_trades_per_hour": 10, "position_size_pct": 2}', true)
ON CONFLICT DO NOTHING;

-- AI Agents
CREATE TABLE IF NOT EXISTS agents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  api_key_id uuid REFERENCES api_keys(id) ON DELETE SET NULL,
  strategy_id uuid REFERENCES strategies(id) ON DELETE SET NULL,
  name text NOT NULL,
  description text DEFAULT '',
  status text DEFAULT 'idle' CHECK (status IN ('idle', 'active', 'paused', 'stopped', 'error')),
  assigned_capital numeric DEFAULT 0 CHECK (assigned_capital >= 0),
  currency text DEFAULT 'USDT',
  trading_pairs text[] DEFAULT ARRAY['BTCUSDT'],
  timeframe text DEFAULT '15m' CHECK (timeframe IN ('1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d')),
  max_risk_per_trade numeric DEFAULT 2.0 CHECK (max_risk_per_trade <= 5.0),
  daily_profit_target numeric DEFAULT 3.0,
  weekly_profit_target numeric DEFAULT 10.0,
  max_daily_loss numeric DEFAULT 5.0,
  max_concurrent_trades int DEFAULT 3,
  max_consecutive_losses int DEFAULT 3,
  strategy_params jsonb DEFAULT '{}',
  ai_optimization_enabled boolean DEFAULT true,
  optimization_params jsonb DEFAULT '{}',
  total_pnl numeric DEFAULT 0,
  total_trades int DEFAULT 0,
  winning_trades int DEFAULT 0,
  current_day_pnl numeric DEFAULT 0,
  current_week_pnl numeric DEFAULT 0,
  confidence_score numeric DEFAULT 50 CHECK (confidence_score BETWEEN 0 AND 100),
  last_trade_at timestamptz,
  started_at timestamptz,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE agents ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own agents"
  ON agents FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own agents"
  ON agents FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own agents"
  ON agents FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can delete own agents"
  ON agents FOR DELETE TO authenticated
  USING (auth.uid() = user_id);

-- Trades
CREATE TABLE IF NOT EXISTS trades (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES agents(id) ON DELETE SET NULL,
  api_key_id uuid REFERENCES api_keys(id) ON DELETE SET NULL,
  exchange text NOT NULL,
  exchange_order_id text,
  symbol text NOT NULL,
  side text NOT NULL CHECK (side IN ('buy', 'sell')),
  order_type text NOT NULL CHECK (order_type IN ('market', 'limit', 'stop_loss', 'take_profit')),
  status text DEFAULT 'pending' CHECK (status IN ('pending', 'open', 'filled', 'cancelled', 'rejected', 'expired')),
  quantity numeric NOT NULL CHECK (quantity > 0),
  entry_price numeric,
  exit_price numeric,
  stop_loss numeric,
  take_profit numeric,
  pnl numeric DEFAULT 0,
  pnl_pct numeric DEFAULT 0,
  fees numeric DEFAULT 0,
  signal_source text DEFAULT 'strategy',
  signal_data jsonb DEFAULT '{}',
  risk_checks jsonb DEFAULT '{}',
  notes text DEFAULT '',
  opened_at timestamptz DEFAULT now(),
  closed_at timestamptz,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE trades ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own trades"
  ON trades FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own trades"
  ON trades FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own trades"
  ON trades FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Positions (open)
CREATE TABLE IF NOT EXISTS positions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES agents(id) ON DELETE SET NULL,
  trade_id uuid REFERENCES trades(id) ON DELETE CASCADE,
  exchange text NOT NULL,
  symbol text NOT NULL,
  side text NOT NULL CHECK (side IN ('long', 'short')),
  quantity numeric NOT NULL,
  entry_price numeric NOT NULL,
  current_price numeric,
  stop_loss numeric,
  take_profit numeric,
  unrealized_pnl numeric DEFAULT 0,
  unrealized_pnl_pct numeric DEFAULT 0,
  leverage numeric DEFAULT 1,
  margin_used numeric DEFAULT 0,
  is_open boolean DEFAULT true,
  opened_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE positions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own positions"
  ON positions FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own positions"
  ON positions FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own positions"
  ON positions FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Agent Performance Snapshots
CREATE TABLE IF NOT EXISTS agent_performance (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  snapshot_date date NOT NULL DEFAULT CURRENT_DATE,
  starting_capital numeric DEFAULT 0,
  ending_capital numeric DEFAULT 0,
  daily_pnl numeric DEFAULT 0,
  daily_pnl_pct numeric DEFAULT 0,
  total_trades int DEFAULT 0,
  winning_trades int DEFAULT 0,
  losing_trades int DEFAULT 0,
  max_drawdown numeric DEFAULT 0,
  sharpe_ratio numeric,
  win_rate numeric DEFAULT 0,
  avg_win numeric DEFAULT 0,
  avg_loss numeric DEFAULT 0,
  strategy_params_snapshot jsonb DEFAULT '{}',
  created_at timestamptz DEFAULT now(),
  UNIQUE(agent_id, snapshot_date)
);

ALTER TABLE agent_performance ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own agent performance"
  ON agent_performance FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own agent performance"
  ON agent_performance FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Risk Events Log
CREATE TABLE IF NOT EXISTS risk_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  agent_id uuid REFERENCES agents(id) ON DELETE SET NULL,
  event_type text NOT NULL CHECK (event_type IN ('max_loss_hit', 'daily_drawdown', 'consecutive_losses', 'position_limit', 'manual_stop', 'api_error')),
  severity text DEFAULT 'warning' CHECK (severity IN ('info', 'warning', 'critical')),
  message text NOT NULL,
  details jsonb DEFAULT '{}',
  resolved boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE risk_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own risk events"
  ON risk_events FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own risk events"
  ON risk_events FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- Notifications
CREATE TABLE IF NOT EXISTS notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
  type text NOT NULL CHECK (type IN ('trade_opened', 'trade_closed', 'risk_alert', 'agent_stopped', 'profit_target', 'system')),
  title text NOT NULL,
  message text NOT NULL,
  data jsonb DEFAULT '{}',
  read boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);

ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can read own notifications"
  ON notifications FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own notifications"
  ON notifications FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own notifications"
  ON notifications FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_trades_user ON trades(user_id);
CREATE INDEX IF NOT EXISTS idx_trades_agent ON trades(agent_id);
CREATE INDEX IF NOT EXISTS idx_trades_created ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_positions_user ON positions(user_id);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(is_open) WHERE is_open = true;
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, read) WHERE read = false;

-- Auto-create profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS trigger AS $$
BEGIN
  INSERT INTO profiles (id, full_name, avatar_url)
  VALUES (
    new.id,
    COALESCE(new.raw_user_meta_data->>'full_name', ''),
    COALESCE(new.raw_user_meta_data->>'avatar_url', '')
  );
  RETURN new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION handle_new_user();
