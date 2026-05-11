'use client';

// Auth context backed by the FastAPI backend.
// Public surface intentionally mirrors the previous Supabase-based version
// so existing pages keep working without changes.

import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

import { api, ApiError } from '@/lib/api/client';
import type { User } from '@/lib/api/types';

interface AuthContextType {
  user: User | null;
  // Kept for backwards-compat with components that read `profile.full_name`
  // etc. The backend's User row IS the profile.
  profile: User | null;
  loading: boolean;
  signIn: (email: string, password: string) => Promise<{ error: Error | null }>;
  signUp: (
    email: string,
    password: string,
    fullName: string,
  ) => Promise<{ error: Error | null }>;
  signOut: () => Promise<void>;
  refreshProfile: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  const refreshProfile = useCallback(async () => {
    try {
      const me = await api.me();
      setUser(me);
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 401) {
        api.logout();
        setUser(null);
      }
    }
  }, []);

  useEffect(() => {
    (async () => {
      if (api.hasToken()) {
        await refreshProfile();
      }
      setLoading(false);
    })();
  }, [refreshProfile]);

  async function signIn(email: string, password: string) {
    try {
      await api.login(email, password);
      await refreshProfile();
      return { error: null };
    } catch (exc) {
      const err = exc as Error;
      return { error: err };
    }
  }

  async function signUp(email: string, password: string, fullName: string) {
    try {
      await api.register(email, password, fullName);
      await refreshProfile();
      return { error: null };
    } catch (exc) {
      const err = exc as Error;
      return { error: err };
    }
  }

  async function signOut() {
    api.logout();
    setUser(null);
  }

  return (
    <AuthContext.Provider
      value={{ user, profile: user, loading, signIn, signUp, signOut, refreshProfile }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within AuthProvider');
  return ctx;
}
