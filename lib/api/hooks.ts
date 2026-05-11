'use client';

// Tiny data-fetching hooks. We avoid pulling SWR/React-Query to keep
// the bundle lean and the wiring obvious. Each hook exposes
// {data, loading, error, refetch} and refetches on focus.

import { useCallback, useEffect, useRef, useState } from 'react';

import { api } from './client';
import type {
  Agent,
  ApiKey,
  MarketTicker,
  Notification,
  PortfolioOverview,
  Position,
  Strategy,
  Trade,
} from './types';

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refetch: () => Promise<void>;
}

function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await loaderRef.current();
      setData(result);
    } catch (exc) {
      setError(exc as Error);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refetch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, loading, error, refetch };
}

export const useApiKeys = () => useAsync<ApiKey[]>(() => api.listApiKeys(), []);
export const useStrategies = () => useAsync<Strategy[]>(() => api.listStrategies(), []);
export const useAgents = () => useAsync<Agent[]>(() => api.listAgents(), []);
export const useTrades = (limit = 50) => useAsync<Trade[]>(() => api.listTrades(limit), [limit]);
export const usePositions = () => useAsync<Position[]>(() => api.listPositions(), []);
export const usePortfolio = () => useAsync<PortfolioOverview>(() => api.portfolioOverview(), []);
export const useTickers = (symbols?: string[]) =>
  useAsync<MarketTicker[]>(() => api.tickers(symbols), [symbols?.join(',')]);
export const useNotifications = () => useAsync<Notification[]>(() => api.listNotifications(), []);

// Polling variant for dashboards that should auto-refresh.
export function usePolling<T>(loader: () => Promise<T>, intervalMs: number, deps: unknown[] = []): AsyncState<T> {
  const state = useAsync(loader, deps);
  useEffect(() => {
    const id = setInterval(state.refetch, intervalMs);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);
  return state;
}
