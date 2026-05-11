import { cn } from '@/lib/utils';
import { TrendingUp, TrendingDown, type LucideIcon } from 'lucide-react';

interface StatCardProps {
  title: string;
  value: string;
  change?: number;
  changeLabel?: string;
  icon: LucideIcon;
  iconColor?: string;
  className?: string;
  badge?: string;
  badgeColor?: string;
}

export function StatCard({
  title,
  value,
  change,
  changeLabel,
  icon: Icon,
  iconColor = 'text-primary',
  className,
  badge,
  badgeColor,
}: StatCardProps) {
  const isPositive = (change ?? 0) >= 0;

  return (
    <div className={cn('bg-card border border-border rounded-xl p-5 space-y-3', className)}>
      <div className="flex items-start justify-between">
        <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">{title}</p>
        <div className={cn('w-8 h-8 rounded-lg flex items-center justify-center', 'bg-card border border-border')}>
          <Icon className={cn('w-4 h-4', iconColor)} />
        </div>
      </div>

      <div>
        <p className="text-2xl font-bold font-mono tracking-tight">{value}</p>
        {(change !== undefined || badge) && (
          <div className="flex items-center gap-2 mt-1">
            {change !== undefined && (
              <span className={cn('flex items-center gap-1 text-xs font-medium', isPositive ? 'text-profit' : 'text-loss')}>
                {isPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                {isPositive ? '+' : ''}{change.toFixed(2)}%
              </span>
            )}
            {changeLabel && <span className="text-xs text-muted-foreground">{changeLabel}</span>}
            {badge && (
              <span className={cn('text-[10px] font-medium px-1.5 py-0.5 rounded-full', badgeColor || 'bg-primary/10 text-primary')}>
                {badge}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
