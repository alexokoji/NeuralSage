'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import { Bell, Check, CheckCheck, X, TrendingUp, TrendingDown, Bot, AlertTriangle, Zap } from 'lucide-react';
import { cn } from '@/lib/utils';
import { api } from '@/lib/api/client';
import type { Notification } from '@/lib/api/types';

const typeIcons: Record<string, typeof Bell> = {
  trade_opened: TrendingUp,
  trade_closed: TrendingDown,
  agent_optimized: Zap,
  risk_event: AlertTriangle,
  agent_status: Bot,
};

const typeColors: Record<string, string> = {
  trade_opened: 'text-green-400 bg-green-500/10',
  trade_closed: 'text-orange-400 bg-orange-500/10',
  agent_optimized: 'text-cyan-400 bg-cyan-500/10',
  risk_event: 'text-red-400 bg-red-500/10',
  agent_status: 'text-blue-400 bg-blue-500/10',
};

function timeAgo(iso: string): string {
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [loading, setLoading] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const fetchNotifications = useCallback(async () => {
    try {
      const data = await api.listNotifications();
      setNotifications(data);
    } catch {
      // silently fail
    }
  }, []);

  // Poll every 10 seconds
  useEffect(() => {
    fetchNotifications();
    const id = setInterval(fetchNotifications, 10_000);
    return () => clearInterval(id);
  }, [fetchNotifications]);

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, []);

  const unreadCount = notifications.filter(n => !n.read).length;

  const markRead = async (id: string) => {
    try {
      await api.markNotifRead(id);
      setNotifications(prev => prev.map(n => n.id === id ? { ...n, read: true } : n));
    } catch {}
  };

  const markAllRead = async () => {
    try {
      await api.markAllNotifsRead();
      setNotifications(prev => prev.map(n => ({ ...n, read: true })));
    } catch {}
  };

  const list = notifications.slice(0, 25);

  return (
    <div ref={ref} className="relative">
      {/* Bell button */}
      <button
        onClick={() => setOpen(o => !o)}
        className="relative p-2 rounded-xl hover:bg-accent text-muted-foreground hover:text-foreground transition-all"
        aria-label="Notifications"
      >
        <Bell className="w-5 h-5" />
        {unreadCount > 0 && (
          <span className="notif-badge">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>

      {/* Dropdown */}
      {open && (
        <div className="absolute right-0 top-full mt-2 w-80 md:w-96 bg-card border border-border rounded-2xl shadow-2xl z-50 overflow-hidden animate-scale-in">
          {/* Header */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-accent/30">
            <p className="text-sm font-semibold">Notifications</p>
            <div className="flex items-center gap-2">
              {unreadCount > 0 && (
                <button
                  onClick={markAllRead}
                  className="text-[10px] text-primary hover:text-primary/80 flex items-center gap-1 font-medium"
                >
                  <CheckCheck className="w-3 h-3" /> Mark all read
                </button>
              )}
              <button onClick={() => setOpen(false)} className="text-muted-foreground hover:text-foreground p-1 rounded-lg hover:bg-accent">
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>

          {/* List */}
          <div className="max-h-[420px] overflow-y-auto">
            {list.length === 0 && (
              <div className="py-12 text-center text-xs text-muted-foreground">
                <Bell className="w-8 h-8 mx-auto mb-2 opacity-30" />
                No notifications yet
              </div>
            )}
            {list.map((n, i) => {
              const Icon = typeIcons[n.type] ?? Bell;
              const color = typeColors[n.type] ?? 'text-muted-foreground bg-accent';
              return (
                <div
                  key={n.id}
                  style={{ animationDelay: `${i * 30}ms` }}
                  className={cn(
                    'flex gap-3 px-4 py-3 border-b border-border/30 hover:bg-accent/50 transition-all cursor-pointer animate-slide-up',
                    !n.read && 'bg-primary/5',
                  )}
                  onClick={() => !n.read && markRead(n.id)}
                >
                  <div className={cn('w-9 h-9 rounded-xl flex items-center justify-center shrink-0 badge-3d', color)}>
                    <Icon className="w-4 h-4" />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-start justify-between gap-2">
                      <p className={cn('text-xs font-medium truncate', !n.read ? 'text-foreground' : 'text-muted-foreground')}>
                        {n.title}
                      </p>
                      <span className="text-[10px] text-muted-foreground shrink-0 font-mono">{timeAgo(n.created_at)}</span>
                    </div>
                    <p className="text-[11px] text-muted-foreground mt-0.5 line-clamp-2 leading-relaxed">{n.message}</p>
                  </div>
                  {!n.read && (
                    <div className="w-2.5 h-2.5 rounded-full bg-primary shrink-0 mt-1.5 shadow-[0_0_8px_rgba(6,182,212,0.4)]" />
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
