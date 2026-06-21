'use client';

// Data-fetching hooks with auto-polling and focus-refetch.
// Every hook polls on an interval AND refetches when the browser tab
// regains focus — so data is always live without manual reload.

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

// Polling hook: auto-refreshes on interval + on window focus.
export function usePolling<T>(loader: () => Promise<T>, intervalMs: number, deps: unknown[] = []): AsyncState<T> {
  const state = useAsync(loader, deps);
  useEffect(() => {
    const id = setInterval(state.refetch, intervalMs);

    // Refetch immediately when user returns to the tab
    const onFocus = () => state.refetch();
    window.addEventListener('focus', onFocus);

    return () => {
      clearInterval(id);
      window.removeEventListener('focus', onFocus);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);
  return state;
}

// All data hooks now auto-poll so pages stay live without manual reload.
export const useApiKeys = () => usePolling<ApiKey[]>(() => api.listApiKeys(), 15_000);
export const useStrategies = () => useAsync<Strategy[]>(() => api.listStrategies(), []);
export const useAgents = () => usePolling<Agent[]>(() => api.listAgents(), 5_000);
export const useTrades = (limit = 50) => usePolling<Trade[]>(() => api.listTrades(limit), 8_000);
export const usePositions = () => usePolling<Position[]>(() => api.listPositions(), 8_000);
export const usePortfolio = () => usePolling<PortfolioOverview>(() => api.portfolioOverview(), 5_000);
export const useTickers = (symbols?: string[]) =>
  usePolling<MarketTicker[]>(() => api.tickers(symbols), 10_000);
export const useNotifications = () => usePolling<Notification[]>(() => api.listNotifications(), 10_000);
