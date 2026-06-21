// Typed FastAPI client.
//
// Tokens live in localStorage. On a 401 we attempt one refresh and replay
// the request; if that fails the user is logged out (next request to /me
// will redirect via the auth-context guard).

import {
  Agent,
  AgentCreate,
  ApiKey,
  ApiKeyCreate,
  Candle,
  MarketTicker,
  Notification,
  PortfolioOverview,
  Position,
  Strategy,
  TokenPair,
  Trade,
  User,
} from './types';

const API_BASE =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) || 'http://localhost:8000';

const ACCESS_KEY = 'neuraltrade.access';
const REFRESH_KEY = 'neuraltrade.refresh';

class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

function loadTokens(): { access: string | null; refresh: string | null } {
  if (typeof window === 'undefined') return { access: null, refresh: null };
  return {
    access: window.localStorage.getItem(ACCESS_KEY),
    refresh: window.localStorage.getItem(REFRESH_KEY),
  };
}

function saveTokens(tokens: TokenPair) {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(ACCESS_KEY, tokens.access_token);
  window.localStorage.setItem(REFRESH_KEY, tokens.refresh_token);
}

function clearTokens() {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(ACCESS_KEY);
  window.localStorage.removeItem(REFRESH_KEY);
}

async function refresh(): Promise<TokenPair | null> {
  const { refresh } = loadTokens();
  if (!refresh) return null;
  const resp = await fetch(`${API_BASE}/api/v1/auth/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!resp.ok) {
    clearTokens();
    return null;
  }
  const tokens = (await resp.json()) as TokenPair;
  saveTokens(tokens);
  return tokens;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  retry = true,
): Promise<T> {
  const { access } = loadTokens();
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (access) headers.Authorization = `Bearer ${access}`;
  const resp = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: 'no-store',
  });

  if (resp.status === 401 && retry) {
    const newTokens = await refresh();
    if (newTokens) return request<T>(method, path, body, false);
  }
  if (resp.status === 204) return undefined as T;

  const text = await resp.text();
  const data = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    const detail = (data && (data.detail as string)) || resp.statusText;
    throw new ApiError(resp.status, detail);
  }
  return data as T;
}

export const api = {
  // ----- auth -----
  async login(email: string, password: string): Promise<TokenPair> {
    const tokens = await request<TokenPair>('POST', '/api/v1/auth/login', { email, password });
    saveTokens(tokens);
    return tokens;
  },
  async register(email: string, password: string, full_name: string): Promise<TokenPair> {
    const tokens = await request<TokenPair>('POST', '/api/v1/auth/register', {
      email,
      password,
      full_name,
    });
    saveTokens(tokens);
    return tokens;
  },
  logout() {
    clearTokens();
  },
  hasToken() {
    return !!loadTokens().access;
  },

  // ----- users -----
  me: () => request<User>('GET', '/api/v1/auth/me'),
  updateMe: (patch: Partial<User>) => request<User>('PATCH', '/api/v1/users/me', patch),

  // ----- api keys -----
  listApiKeys: () => request<ApiKey[]>('GET', '/api/v1/api-keys'),
  createApiKey: (body: ApiKeyCreate) => request<ApiKey>('POST', '/api/v1/api-keys', body),
  verifyApiKey: (id: string) =>
    request<{ verified: boolean; permissions: string[]; error: string | null; server_blocked: boolean }>(
      'POST',
      `/api/v1/api-keys/${id}/verify`,
    ),
  trustApiKey: (id: string) => request<ApiKey>('POST', `/api/v1/api-keys/${id}/trust`),
  deleteApiKey: (id: string) => request<void>('DELETE', `/api/v1/api-keys/${id}`),

  // ----- agents -----
  listStrategies: () => request<Strategy[]>('GET', '/api/v1/agents/strategies'),
  listAgents: () => request<Agent[]>('GET', '/api/v1/agents'),
  createAgent: (body: AgentCreate) => request<Agent>('POST', '/api/v1/agents', body),
  getAgent: (id: string) => request<Agent>('GET', `/api/v1/agents/${id}`),
  updateAgent: (id: string, patch: Partial<AgentCreate> & { api_key_id?: string }) =>
    request<Agent>('PATCH', `/api/v1/agents/${id}`, patch),
  deleteAgent: (id: string) => request<void>('DELETE', `/api/v1/agents/${id}`),
  controlAgent: (id: string, action: 'start' | 'pause' | 'stop') =>
    request<Agent>('POST', `/api/v1/agents/${id}/control`, { action }),

  // ----- trades -----
  listTrades: (limit = 50) => request<Trade[]>('GET', `/api/v1/trades?limit=${limit}`),
  listPositions: () => request<Position[]>('GET', '/api/v1/trades/positions'),
  cancelTrade: (id: string) => request<void>('POST', `/api/v1/trades/${id}/cancel`),

  // ----- portfolio -----
  portfolioOverview: () => request<PortfolioOverview>('GET', '/api/v1/portfolio/overview'),

  // ----- market -----
  tickers: (symbols?: string[]) => {
    const qs = symbols && symbols.length ? `?${symbols.map(s => `symbols=${s}`).join('&')}` : '';
    return request<MarketTicker[]>('GET', `/api/v1/market/tickers${qs}`);
  },
  candles: (symbol: string, interval = '15m', limit = 200) =>
    request<Candle[]>('GET', `/api/v1/market/candles?symbol=${symbol}&interval=${interval}&limit=${limit}`),

  // ----- notifications -----
  listNotifications: (unreadOnly = false) =>
    request<Notification[]>('GET', `/api/v1/notifications?unread_only=${unreadOnly}`),
  markNotifRead: (id: string) => request<void>('POST', `/api/v1/notifications/${id}/read`),
  markAllNotifsRead: () => request<void>('POST', '/api/v1/notifications/read-all'),

  // ----- Grok AI -----
  aiStatus: () =>
    request<{ grok_available: boolean; model: string }>('GET', '/api/v1/ai/status'),
  aiChat: (messages: { role: string; content: string }[], extras?: { portfolio_context?: unknown; active_agents?: unknown[] }) =>
    request<{ reply: string }>('POST', '/api/v1/ai/chat', { messages, ...extras }),
  fleetInsight: (strategyType: string, symbol?: string) => {
    const qs = symbol ? `?strategy_type=${strategyType}&symbol=${symbol}` : `?strategy_type=${strategyType}`;
    return request<{ strategy_type: string; symbol: string | null; insight: string }>(
      'GET',
      `/api/v1/ai/fleet-insight${qs}`,
    );
  },
  agentSuggestions: (agentId: string) =>
    request<{
      agent_id: string;
      suggestions: { param: string; current: string; recommended: string; reason: string }[];
      risk_assessment: string;
      recommended_params: Record<string, number> | null;
      timeframe_advice: string;
    }>('GET', `/api/v1/ai/agent-suggestions/${agentId}`),
};

export { ApiError };
