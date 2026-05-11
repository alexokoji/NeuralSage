'use client';

import { useState } from 'react';
import { useAuth } from '@/lib/auth-context';
import { cn } from '@/lib/utils';
import {
  Plus,
  Eye,
  EyeOff,
  CircleCheck as CheckCircle2,
  Trash2,
  TriangleAlert as AlertTriangle,
  Lock,
  Shield,
  Zap,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { maskApiKey } from '@/lib/encryption';
import { useApiKeys } from '@/lib/api/hooks';
import { api } from '@/lib/api/client';
import type { Exchange } from '@/lib/api/types';

const exchangeColors: Record<string, string> = {
  bybit: 'text-orange-400',
  bitget: 'text-blue-400',
  bybit_testnet: 'text-orange-400',
};

const tabs = ['API Keys', 'Risk Settings', 'Notifications', 'Account'];

export default function SettingsPage() {
  const { profile, refreshProfile } = useAuth();
  const [activeTab, setActiveTab] = useState('API Keys');

  const { data: apiKeys, refetch } = useApiKeys();
  const [showAddKey, setShowAddKey] = useState(false);
  const [showSecret, setShowSecret] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState('');
  const [verifyingId, setVerifyingId] = useState<string | null>(null);
  const [newKey, setNewKey] = useState({
    exchange: 'bybit' as Exchange,
    label: '',
    apiKey: '',
    apiSecret: '',
    passphrase: '',
  });

  const [riskSettings, setRiskSettings] = useState({
    maxRiskPerTrade: 2,
    maxDailyLoss: 5,
    maxConcurrentTrades: 5,
    maxConsecutiveLosses: 3,
    stopOnDrawdown: true,
    notifications: true,
  });

  async function handleAddKey(e: React.FormEvent) {
    e.preventDefault();
    setFormError('');
    setSubmitting(true);
    try {
      // Bitget passphrase is appended to the secret with a newline; backend
      // splits and forwards. Bybit/Bybit-testnet ignore the passphrase.
      const apiSecret =
        newKey.exchange === 'bitget'
          ? `${newKey.apiSecret}\n${newKey.passphrase}`
          : newKey.apiSecret;
      const created = await api.createApiKey({
        exchange: newKey.exchange,
        label: newKey.label,
        api_key: newKey.apiKey,
        api_secret: apiSecret,
        is_testnet: newKey.exchange === 'bybit_testnet',
      });
      // Best-effort verification immediately after add.
      try {
        await api.verifyApiKey(created.id);
      } catch {
        // ignore — the user can re-verify from the list.
      }
      await refetch();
      setShowAddKey(false);
      setNewKey({
        exchange: 'bybit',
        label: '',
        apiKey: '',
        apiSecret: '',
        passphrase: '',
      });
    } catch (exc) {
      setFormError((exc as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(id: string) {
    if (!window.confirm('Delete this API key? Agents using it will be disconnected.')) return;
    try {
      await api.deleteApiKey(id);
      await refetch();
    } catch (exc) {
      console.error(exc);
    }
  }

  async function handleVerify(id: string) {
    setVerifyingId(id);
    try {
      const result = await api.verifyApiKey(id);
      if (!result.verified && result.error) {
        window.alert(`Verification failed: ${result.error}`);
      }
      await refetch();
    } finally {
      setVerifyingId(null);
    }
  }

  async function handleSaveProfile(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    await api.updateMe({
      full_name: String(fd.get('full_name') || ''),
      timezone: String(fd.get('timezone') || 'UTC'),
    });
    await refreshProfile();
  }

  async function handleSaveRisk() {
    await api.updateMe({
      daily_loss_limit: riskSettings.maxDailyLoss,
      max_concurrent_trades: riskSettings.maxConcurrentTrades,
      notifications_enabled: riskSettings.notifications,
    });
    await refreshProfile();
  }

  return (
    <div className="p-4 md:p-6 space-y-4 md:space-y-6 max-w-[900px]">
      <div>
        <h2 className="text-lg font-bold">Settings</h2>
        <p className="text-xs text-muted-foreground mt-0.5">
          Manage your account, API keys, and risk parameters
        </p>
      </div>

      <div className="flex border-b border-border gap-1 overflow-x-auto -mx-4 px-4 md:mx-0 md:px-0">
        {tabs.map(tab => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={cn(
              'px-4 py-2.5 text-xs font-medium border-b-2 -mb-px transition-all whitespace-nowrap shrink-0',
              activeTab === tab
                ? 'border-primary text-primary'
                : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {tab}
          </button>
        ))}
      </div>

      {activeTab === 'API Keys' && (
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold">Connected Exchanges</p>
            <Button onClick={() => setShowAddKey(true)} className="h-8 text-xs gap-1.5">
              <Plus className="w-3.5 h-3.5" /> Add API Key
            </Button>
          </div>

          <div className="flex items-start gap-3 p-4 bg-cyan-500/5 border border-cyan-500/15 rounded-xl">
            <Lock className="w-4 h-4 text-cyan-400 shrink-0 mt-0.5" />
            <div className="text-xs space-y-1">
              <p className="text-cyan-400 font-medium">AES-256-GCM Encryption</p>
              <p className="text-muted-foreground">
                API keys are encrypted server-side under an authenticated cipher before
                storage. We never store plaintext credentials. Keys must have{' '}
                <strong>Read + Trade</strong> permissions only —{' '}
                <strong>withdrawal access is rejected at verification.</strong>
              </p>
            </div>
          </div>

          <div className="space-y-3">
            {(apiKeys ?? []).map(key => (
              <div key={key.id} className="bg-card border border-border rounded-xl p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="w-9 h-9 rounded-lg bg-accent border border-border flex items-center justify-center text-lg">
                      {key.exchange.startsWith('bybit') ? '⬡' : '◈'}
                    </div>
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="text-sm font-semibold">
                          {key.label || key.exchange}
                        </p>
                        {key.is_testnet && (
                          <span className="px-1.5 py-0.5 bg-orange-500/10 border border-orange-500/20 text-orange-400 text-[10px] rounded-full font-medium">
                            Testnet
                          </span>
                        )}
                      </div>
                      <p
                        className={cn(
                          'text-xs capitalize mt-0.5',
                          exchangeColors[key.exchange],
                        )}
                      >
                        {key.exchange}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    {key.verified ? (
                      <span className="flex items-center gap-1 text-xs text-green-400">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Verified
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-xs text-orange-400">
                        <AlertTriangle className="w-3.5 h-3.5" /> Unverified
                      </span>
                    )}
                    <button
                      onClick={() => handleVerify(key.id)}
                      disabled={verifyingId === key.id}
                      className="px-2 py-1 text-[10px] bg-accent hover:bg-accent/80 rounded text-muted-foreground"
                    >
                      {verifyingId === key.id ? 'Verifying…' : 'Re-verify'}
                    </button>
                    <button
                      onClick={() => handleDelete(key.id)}
                      className="p-1.5 text-muted-foreground hover:text-red-400 hover:bg-red-500/5 rounded-lg transition-colors"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>

                <div className="mt-3 grid grid-cols-3 gap-3 text-xs">
                  <div>
                    <p className="text-muted-foreground">API Key</p>
                    <p className="font-mono mt-0.5">
                      {/* The plaintext key is never returned by the backend.
                          Show a stable masked fingerprint of the ciphertext. */}
                      {maskApiKey(key.id)}
                    </p>
                  </div>
                  <div>
                    <p className="text-muted-foreground">Permissions</p>
                    <div className="flex gap-1 mt-0.5 flex-wrap">
                      {key.permissions.map(p => (
                        <span
                          key={p}
                          className="px-1.5 py-0.5 bg-green-500/10 border border-green-500/20 text-green-400 text-[10px] rounded font-medium"
                        >
                          {p}
                        </span>
                      ))}
                      <span className="px-1.5 py-0.5 bg-red-500/10 border border-red-500/20 text-red-400 text-[10px] rounded font-medium line-through">
                        withdraw
                      </span>
                    </div>
                  </div>
                  <div>
                    <p className="text-muted-foreground">Added</p>
                    <p className="mt-0.5">
                      {new Date(key.created_at).toLocaleDateString()}
                    </p>
                  </div>
                </div>
              </div>
            ))}
            {(apiKeys ?? []).length === 0 && (
              <div className="text-center py-10 bg-card border border-dashed border-border rounded-xl">
                <Lock className="w-7 h-7 text-muted-foreground mx-auto mb-2" />
                <p className="text-sm font-medium">No exchange keys yet</p>
                <p className="text-xs text-muted-foreground mt-1">
                  Add a Bybit or Bitget key with read + trade permissions to get started.
                </p>
              </div>
            )}
          </div>

          <Dialog open={showAddKey} onOpenChange={setShowAddKey}>
            <DialogContent className="bg-card border-border max-w-md">
              <DialogHeader>
                <DialogTitle>Add Exchange API Key</DialogTitle>
              </DialogHeader>
              <form onSubmit={handleAddKey} className="space-y-4 mt-2">
                <div className="space-y-2">
                  <Label>Exchange</Label>
                  <Select
                    value={newKey.exchange}
                    onValueChange={v =>
                      setNewKey(p => ({ ...p, exchange: v as Exchange }))
                    }
                  >
                    <SelectTrigger className="bg-background border-border">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent className="bg-card border-border">
                      <SelectItem value="bybit">Bybit (mainnet)</SelectItem>
                      <SelectItem value="bybit_testnet">Bybit Testnet</SelectItem>
                      <SelectItem value="bitget">Bitget</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>Label</Label>
                  <Input
                    value={newKey.label}
                    onChange={e => setNewKey(p => ({ ...p, label: e.target.value }))}
                    placeholder="My Bybit Key"
                    required
                    className="bg-background border-border"
                  />
                </div>
                <div className="space-y-2">
                  <Label>API Key</Label>
                  <Input
                    value={newKey.apiKey}
                    onChange={e => setNewKey(p => ({ ...p, apiKey: e.target.value }))}
                    placeholder="Paste your API key"
                    required
                    className="bg-background border-border font-mono text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <Label>API Secret</Label>
                  <div className="relative">
                    <Input
                      type={showSecret ? 'text' : 'password'}
                      value={newKey.apiSecret}
                      onChange={e =>
                        setNewKey(p => ({ ...p, apiSecret: e.target.value }))
                      }
                      placeholder="Paste your API secret"
                      required
                      className="bg-background border-border font-mono text-xs pr-10"
                    />
                    <button
                      type="button"
                      onClick={() => setShowSecret(!showSecret)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                    >
                      {showSecret ? (
                        <EyeOff className="w-4 h-4" />
                      ) : (
                        <Eye className="w-4 h-4" />
                      )}
                    </button>
                  </div>
                </div>
                {newKey.exchange === 'bitget' && (
                  <div className="space-y-2">
                    <Label>Passphrase (Bitget only)</Label>
                    <Input
                      value={newKey.passphrase}
                      onChange={e =>
                        setNewKey(p => ({ ...p, passphrase: e.target.value }))
                      }
                      placeholder="API passphrase"
                      required
                      className="bg-background border-border"
                    />
                  </div>
                )}
                <div className="p-3 bg-orange-500/5 border border-orange-500/15 rounded-lg">
                  <p className="text-[10px] text-orange-400 font-medium mb-1">
                    Required Permissions
                  </p>
                  <p className="text-[10px] text-muted-foreground">
                    Enable: <strong>Read</strong> and <strong>Trade</strong> only. Do NOT
                    enable Withdraw permissions — verification will reject the key.
                  </p>
                </div>
                {formError && (
                  <div className="text-xs text-red-400 bg-red-500/5 border border-red-500/20 rounded-lg px-3 py-2">
                    {formError}
                  </div>
                )}
                <div className="flex gap-3">
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => setShowAddKey(false)}
                    className="flex-1 border-border"
                  >
                    Cancel
                  </Button>
                  <Button type="submit" className="flex-1 bg-primary" disabled={submitting}>
                    {submitting ? 'Adding…' : 'Add & Encrypt'}
                  </Button>
                </div>
              </form>
            </DialogContent>
          </Dialog>
        </div>
      )}

      {activeTab === 'Risk Settings' && (
        <div className="space-y-5">
          <div className="p-4 bg-orange-500/5 border border-orange-500/15 rounded-xl flex items-start gap-3">
            <Shield className="w-4 h-4 text-orange-400 shrink-0 mt-0.5" />
            <p className="text-xs text-muted-foreground">
              <span className="text-orange-400 font-medium">Risk Engine:</span> These
              settings are enforced at the system level and override all AI agent
              decisions. Max 2% per trade is a hard-coded global cap.
            </p>
          </div>

          {[
            {
              label: 'Max Risk Per Trade (%)',
              key: 'maxRiskPerTrade' as const,
              min: 0.5,
              max: 2,
              desc: 'Maximum capital at risk per individual trade (system cap: 2%)',
            },
            {
              label: 'Daily Loss Limit (%)',
              key: 'maxDailyLoss' as const,
              min: 1,
              max: 10,
              desc: 'Stop trading when daily drawdown exceeds this percentage',
            },
            {
              label: 'Max Concurrent Trades',
              key: 'maxConcurrentTrades' as const,
              min: 1,
              max: 20,
              desc: 'Maximum number of open positions at any time',
            },
            {
              label: 'Max Consecutive Losses',
              key: 'maxConsecutiveLosses' as const,
              min: 1,
              max: 10,
              desc: 'Pause agent after this many losing trades in a row',
            },
          ].map(({ label, key, min, max, desc }) => (
            <div
              key={key}
              className="bg-card border border-border rounded-xl p-4 space-y-3"
            >
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium">{label}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">{desc}</p>
                </div>
                <div className="text-xl font-bold font-mono text-primary">
                  {riskSettings[key]}
                  {key === 'maxRiskPerTrade' || key === 'maxDailyLoss' ? '%' : ''}
                </div>
              </div>
              <input
                type="range"
                min={min}
                max={max}
                step={0.5}
                value={riskSettings[key]}
                onChange={e =>
                  setRiskSettings(prev => ({ ...prev, [key]: Number(e.target.value) }))
                }
                className="w-full accent-primary"
              />
              <div className="flex justify-between text-[10px] text-muted-foreground">
                <span>Min: {min}</span>
                <span>Max: {max}</span>
              </div>
            </div>
          ))}

          {[
            {
              label: 'Stop Trading on Drawdown',
              key: 'stopOnDrawdown' as const,
              desc: 'Automatically halt all agents when daily loss limit is reached',
            },
            {
              label: 'Risk Alert Notifications',
              key: 'notifications' as const,
              desc: 'Receive alerts when risk thresholds are approached',
            },
          ].map(({ label, key, desc }) => (
            <div
              key={key}
              className="bg-card border border-border rounded-xl p-4 flex items-center justify-between"
            >
              <div>
                <p className="text-sm font-medium">{label}</p>
                <p className="text-xs text-muted-foreground mt-0.5">{desc}</p>
              </div>
              <Switch
                checked={riskSettings[key]}
                onCheckedChange={v =>
                  setRiskSettings(prev => ({ ...prev, [key]: Boolean(v) }))
                }
              />
            </div>
          ))}

          <Button onClick={handleSaveRisk} className="w-full bg-primary">
            Save Risk Settings
          </Button>
        </div>
      )}

      {activeTab === 'Notifications' && (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Backend triggers these events automatically. Master toggle is in account
            settings.
          </p>
          {[
            { label: 'Trade Opened', desc: 'When an agent opens a new position' },
            { label: 'Trade Closed', desc: 'When a position is closed with P&L details' },
            { label: 'Risk Alert', desc: 'When drawdown or loss limits are approached' },
            { label: 'Agent Stopped', desc: 'When an agent is automatically halted' },
            {
              label: 'Profit Target Hit',
              desc: 'When daily/weekly profit targets are reached',
            },
            { label: 'API Key Issues', desc: 'Connection errors with exchange APIs' },
          ].map(({ label, desc }) => (
            <div
              key={label}
              className="bg-card border border-border rounded-xl p-4 flex items-center justify-between"
            >
              <div>
                <p className="text-sm font-medium">{label}</p>
                <p className="text-xs text-muted-foreground mt-0.5">{desc}</p>
              </div>
              <Switch defaultChecked />
            </div>
          ))}
        </div>
      )}

      {activeTab === 'Account' && (
        <div className="space-y-5">
          <form
            onSubmit={handleSaveProfile}
            className="bg-card border border-border rounded-xl p-5 space-y-4"
          >
            <p className="text-sm font-semibold">Profile</p>
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Full Name</Label>
                <Input
                  name="full_name"
                  defaultValue={profile?.full_name || ''}
                  className="bg-background border-border"
                />
              </div>
              <div className="space-y-2">
                <Label>Timezone</Label>
                <Select name="timezone" defaultValue={profile?.timezone || 'UTC'}>
                  <SelectTrigger className="bg-background border-border">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent className="bg-card border-border">
                    <SelectItem value="UTC">UTC</SelectItem>
                    <SelectItem value="America/New_York">New York</SelectItem>
                    <SelectItem value="Europe/London">London</SelectItem>
                    <SelectItem value="Asia/Tokyo">Tokyo</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <Button type="submit" className="bg-primary h-9 text-xs">
              Save Profile
            </Button>
          </form>

          <div className="bg-card border border-border rounded-xl p-5 space-y-3">
            <p className="text-sm font-semibold">Email</p>
            <p className="text-xs text-muted-foreground">{profile?.email}</p>
            <p className="text-[11px] text-muted-foreground flex items-center gap-1.5">
              <Zap className="w-3 h-3 text-primary" /> Password changes happen via reset
              email — coming next release.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
