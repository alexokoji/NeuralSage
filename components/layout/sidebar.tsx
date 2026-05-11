'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useAuth } from '@/lib/auth-context';
import { cn } from '@/lib/utils';
import {
  LayoutDashboard,
  Wallet,
  Bot,
  History,
  Settings,
  TrendingUp,
  LogOut,
  ChevronLeft,
  ChevronRight,
  Bell,
  Activity,
} from 'lucide-react';
import { useState } from 'react';

const navItems = [
  { href: '/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { href: '/portfolio', icon: Wallet, label: 'Portfolio' },
  { href: '/agents', icon: Bot, label: 'AI Agents' },
  { href: '/trades', icon: History, label: 'Trade History' },
  { href: '/settings', icon: Settings, label: 'Settings' },
];

export function Sidebar() {
  const pathname = usePathname();
  const { signOut, profile } = useAuth();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <aside
      className={cn(
        'h-screen flex flex-col bg-card border-r border-border transition-all duration-300 relative',
        collapsed ? 'w-16' : 'w-60'
      )}
    >
      {/* Logo */}
      <div className={cn('flex items-center h-16 px-4 border-b border-border shrink-0', collapsed ? 'justify-center' : 'gap-3')}>
        <div className="w-8 h-8 rounded-lg bg-primary/15 border border-primary/25 flex items-center justify-center shrink-0">
          <TrendingUp className="w-4 h-4 text-primary" />
        </div>
        {!collapsed && (
          <div>
            <p className="font-bold text-sm leading-none">NeuralTrade</p>
            <p className="text-[10px] text-muted-foreground mt-0.5">AI Trading Platform</p>
          </div>
        )}
      </div>

      {/* Collapse toggle */}
      <button
        onClick={() => setCollapsed(!collapsed)}
        className="absolute -right-3 top-20 w-6 h-6 rounded-full bg-card border border-border flex items-center justify-center hover:bg-accent transition-colors z-10"
      >
        {collapsed ? <ChevronRight className="w-3 h-3" /> : <ChevronLeft className="w-3 h-3" />}
      </button>

      {/* Live indicator */}
      {!collapsed && (
        <div className="mx-4 mt-4 mb-2 flex items-center gap-2 px-3 py-2 bg-green-500/5 border border-green-500/15 rounded-lg">
          <div className="relative w-2 h-2">
            <div className="w-2 h-2 rounded-full bg-green-500" />
            <div className="absolute inset-0 rounded-full bg-green-500 animate-ping opacity-40" />
          </div>
          <span className="text-[11px] text-green-400 font-medium">Markets Live</span>
          <Activity className="w-3 h-3 text-green-400 ml-auto" />
        </div>
      )}
      {collapsed && (
        <div className="flex justify-center mt-4 mb-2">
          <div className="relative w-2 h-2">
            <div className="w-2 h-2 rounded-full bg-green-500" />
            <div className="absolute inset-0 rounded-full bg-green-500 animate-ping opacity-40" />
          </div>
        </div>
      )}

      {/* Nav */}
      <nav className="flex-1 px-3 py-2 space-y-1 overflow-y-auto">
        {navItems.map(({ href, icon: Icon, label }) => {
          const active = pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                'flex items-center gap-3 px-3 py-2.5 rounded-lg transition-all text-sm group',
                active
                  ? 'bg-primary/10 text-primary border border-primary/15'
                  : 'text-muted-foreground hover:text-foreground hover:bg-accent',
                collapsed && 'justify-center px-2'
              )}
              title={collapsed ? label : undefined}
            >
              <Icon className={cn('w-4 h-4 shrink-0', active ? 'text-primary' : '')} />
              {!collapsed && <span className="font-medium">{label}</span>}
              {!collapsed && active && (
                <div className="ml-auto w-1.5 h-1.5 rounded-full bg-primary" />
              )}
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-3 py-3 border-t border-border space-y-1">
        {!collapsed && (
          <div className="flex items-center gap-3 px-3 py-2 mb-1">
            <div className="w-7 h-7 rounded-full bg-primary/15 flex items-center justify-center text-xs font-bold text-primary shrink-0">
              {profile?.full_name?.charAt(0) || 'U'}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-medium truncate">{profile?.full_name || 'Trader'}</p>
              <p className="text-[10px] text-muted-foreground truncate">{profile?.risk_level || 'medium'} risk</p>
            </div>
            <Bell className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
          </div>
        )}
        <button
          onClick={() => signOut()}
          className={cn(
            'flex items-center gap-3 w-full px-3 py-2 rounded-lg text-sm text-muted-foreground hover:text-red-400 hover:bg-red-500/5 transition-all',
            collapsed && 'justify-center'
          )}
          title={collapsed ? 'Sign Out' : undefined}
        >
          <LogOut className="w-4 h-4 shrink-0" />
          {!collapsed && <span>Sign Out</span>}
        </button>
      </div>
    </aside>
  );
}
