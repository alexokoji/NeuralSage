'use client';

import { useRef, useState, useEffect, Suspense, useCallback } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import { Sphere, Torus, MeshDistortMaterial, Float } from '@react-three/drei';
import * as THREE from 'three';
import { cn } from '@/lib/utils';
import { Bot, Zap, TrendingUp, TriangleAlert as AlertTriangle, Activity, Send, Loader2 } from 'lucide-react';

// ------------------------------------------------------------------ //
// 3D Core orb
// ------------------------------------------------------------------ //

function CoreOrb({ mood, confidence }: { mood: 'profit' | 'loss' | 'neutral'; confidence: number }) {
  const meshRef = useRef<THREE.Mesh>(null!);
  const torusRef = useRef<THREE.Mesh>(null!);

  const moodColor = mood === 'profit' ? '#22c55e' : mood === 'loss' ? '#ef4444' : '#06b6d4';
  const distort = 0.2 + (confidence / 100) * 0.35;

  useFrame((state) => {
    const t = state.clock.getElapsedTime();
    if (meshRef.current) {
      meshRef.current.rotation.y = t * 0.3;
      meshRef.current.rotation.x = Math.sin(t * 0.5) * 0.15;
    }
    if (torusRef.current) {
      torusRef.current.rotation.z = t * 0.5;
      torusRef.current.rotation.x = Math.PI / 3 + Math.sin(t * 0.4) * 0.1;
    }
  });

  return (
    <group>
      <mesh ref={torusRef}>
        <torusGeometry args={[1.4, 0.04, 16, 60]} />
        <meshStandardMaterial color={moodColor} emissive={moodColor} emissiveIntensity={0.8} transparent opacity={0.6} />
      </mesh>
      <mesh rotation={[Math.PI / 2, 0, 0]}>
        <torusGeometry args={[1.1, 0.025, 16, 50]} />
        <meshStandardMaterial color={moodColor} emissive={moodColor} emissiveIntensity={0.5} transparent opacity={0.35} />
      </mesh>
      <Float speed={2} rotationIntensity={0.3} floatIntensity={0.5}>
        <mesh ref={meshRef}>
          <sphereGeometry args={[0.8, 64, 64]} />
          <MeshDistortMaterial
            color={moodColor}
            emissive={moodColor}
            emissiveIntensity={0.4}
            distort={distort}
            speed={3}
            roughness={0.1}
            metalness={0.8}
          />
        </mesh>
      </Float>
      <mesh>
        <sphereGeometry args={[1.05, 32, 32]} />
        <meshStandardMaterial color={moodColor} transparent opacity={0.05} side={THREE.BackSide} />
      </mesh>
      <DataParticles color={moodColor} count={40} radius={1.8} />
    </group>
  );
}

function DataParticles({ color, count, radius }: { color: string; count: number; radius: number }) {
  const points = useRef<THREE.Points>(null!);

  const positions = new Float32Array(count * 3);
  for (let i = 0; i < count; i++) {
    const theta = Math.random() * Math.PI * 2;
    const phi = Math.acos(2 * Math.random() - 1);
    const r = radius + (Math.random() - 0.5) * 0.6;
    positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
    positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
    positions[i * 3 + 2] = r * Math.cos(phi);
  }

  useFrame((state) => {
    if (points.current) {
      points.current.rotation.y = state.clock.getElapsedTime() * 0.1;
    }
  });

  return (
    <points ref={points}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial color={color} size={0.03} transparent opacity={0.7} sizeAttenuation />
    </points>
  );
}

// ------------------------------------------------------------------ //
// Types
// ------------------------------------------------------------------ //

interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

interface AIAssistantProps {
  compact?: boolean;
  mood?: 'profit' | 'loss' | 'neutral';
  confidence?: number;
  status?: 'active' | 'idle' | 'analyzing' | 'alert';
  winRate?: number;
  totalTrades?: number;
  signalCount?: number;
  portfolioContext?: Record<string, unknown>;
  activeAgents?: Record<string, unknown>[];
}

// ------------------------------------------------------------------ //
// Component
// ------------------------------------------------------------------ //

export function AIAssistant({
  compact = false,
  mood = 'profit',
  confidence = 78,
  status = 'active',
  winRate = 68,
  totalTrades = 127,
  signalCount = 8,
  portfolioContext,
  activeAgents,
}: AIAssistantProps) {
  const [chatOpen, setChatOpen] = useState(false);
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(false);
  const [streamingReply, setStreamingReply] = useState('');
  const chatBottomRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom of chat
  useEffect(() => {
    chatBottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, streamingReply]);

  const sendMessage = useCallback(async () => {
    const text = input.trim();
    if (!text || loading) return;
    setInput('');

    const newMessages: ChatMessage[] = [...messages, { role: 'user', content: text }];
    setMessages(newMessages);
    setLoading(true);
    setStreamingReply('');

    try {
      const API_BASE =
        (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_API_URL) ||
        'http://localhost:8000';
      const token =
        typeof window !== 'undefined' ? window.localStorage.getItem('neuraltrade.access') : null;
      const res = await fetch(`${API_BASE}/api/v1/ai/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          messages: newMessages,
          portfolio_context: portfolioContext ?? null,
          active_agents: activeAgents ?? null,
        }),
      });

      if (!res.ok) throw new Error(`API error ${res.status}`);
      const data = await res.json();
      const reply: string = data.reply ?? 'No response.';
      setMessages(prev => [...prev, { role: 'assistant', content: reply }]);
    } catch (err) {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Sorry, I could not reach the AI service right now.' },
      ]);
    } finally {
      setLoading(false);
      setStreamingReply('');
    }
  }, [input, loading, messages, portfolioContext, activeAgents]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const statusConfig = {
    active: { color: 'text-green-400', bg: 'bg-green-500/10 border-green-500/20', label: 'Active' },
    idle: { color: 'text-muted-foreground', bg: 'bg-border', label: 'Idle' },
    analyzing: { color: 'text-cyan-400', bg: 'bg-cyan-500/10 border-cyan-500/20', label: 'Analyzing' },
    alert: { color: 'text-orange-400', bg: 'bg-orange-500/10 border-orange-500/20', label: 'Alert' },
  };
  const sc = statusConfig[status];

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-5 pt-5 pb-3 border-b border-border flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Bot className="w-4 h-4 text-primary" />
          <p className="text-sm font-semibold">Grok AI</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setChatOpen(o => !o)}
            className="text-[10px] text-primary underline underline-offset-2 hover:text-primary/80"
          >
            {chatOpen ? 'Show orb' : 'Chat'}
          </button>
          <div className={cn('flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-medium border', sc.bg, sc.color)}>
            <div className={cn('w-1.5 h-1.5 rounded-full', status === 'active' ? 'bg-green-400 animate-pulse' : status === 'analyzing' ? 'bg-cyan-400' : 'bg-muted-foreground')} />
            {sc.label}
          </div>
        </div>
      </div>

      {chatOpen ? (
        /* ── CHAT VIEW ─────────────────────────────── */
        <div className="flex flex-col flex-1 overflow-hidden">
          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3">
            {messages.length === 0 && (
              <p className="text-[11px] text-muted-foreground text-center pt-4">
                Ask Grok anything about your trades, agents, or market conditions.
              </p>
            )}
            {messages.map((m, i) => (
              <div
                key={i}
                className={cn(
                  'max-w-[85%] rounded-lg px-3 py-2 text-xs leading-relaxed',
                  m.role === 'user'
                    ? 'ml-auto bg-primary text-primary-foreground'
                    : 'bg-background border border-border text-foreground',
                )}
              >
                {m.content}
              </div>
            ))}
            {loading && (
              <div className="max-w-[85%] rounded-lg px-3 py-2 text-xs bg-background border border-border flex items-center gap-2 text-muted-foreground">
                <Loader2 className="w-3 h-3 animate-spin" />
                Grok is thinking…
              </div>
            )}
            <div ref={chatBottomRef} />
          </div>

          {/* Input */}
          <div className="px-4 pb-4 pt-2 border-t border-border flex gap-2">
            <input
              type="text"
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="Ask Grok…"
              className="flex-1 bg-background border border-border rounded-lg px-3 py-2 text-xs outline-none focus:ring-1 focus:ring-primary"
              disabled={loading}
            />
            <button
              onClick={sendMessage}
              disabled={loading || !input.trim()}
              className="p-2 rounded-lg bg-primary text-primary-foreground disabled:opacity-40 hover:bg-primary/90 transition-colors"
            >
              <Send className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      ) : (
        /* ── ORB VIEW ──────────────────────────────── */
        <>
          {/* 3D Canvas */}
          <div className="flex-1 relative min-h-[200px]">
            <Canvas
              camera={{ position: [0, 0, 4], fov: 50 }}
              gl={{ antialias: true, alpha: true }}
              style={{ background: 'transparent' }}
            >
              <ambientLight intensity={0.3} />
              <pointLight position={[5, 5, 5]} intensity={1.5} color="#06b6d4" />
              <pointLight position={[-5, -5, -5]} intensity={0.8} color="#22c55e" />
              <pointLight position={[0, 5, -3]} intensity={0.6} color="#ffffff" />
              <Suspense fallback={null}>
                <CoreOrb mood={mood} confidence={confidence} />
              </Suspense>
            </Canvas>

            {/* Confidence overlay */}
            <div className="absolute bottom-3 left-1/2 -translate-x-1/2 flex flex-col items-center gap-1">
              <p className="text-[10px] text-muted-foreground">Confidence</p>
              <div className="flex gap-1">
                {Array.from({ length: 10 }).map((_, i) => (
                  <div
                    key={i}
                    className={cn('w-2 h-3 rounded-sm transition-all', i < Math.round(confidence / 10) ? 'bg-primary' : 'bg-border')}
                  />
                ))}
              </div>
              <p className="text-xs font-mono font-bold text-primary">{confidence}%</p>
            </div>
          </div>

          {/* Status metrics */}
          <div className="px-5 py-3 grid grid-cols-3 gap-3 border-t border-border">
            {[
              { icon: TrendingUp, label: 'Win Rate', value: `${winRate}%`, color: 'text-green-400' },
              { icon: Activity, label: 'Trades', value: String(totalTrades), color: 'text-primary' },
              { icon: Zap, label: 'Signals', value: String(signalCount), color: 'text-orange-400' },
            ].map(({ icon: Icon, label, value, color }) => (
              <div key={label} className="text-center">
                <Icon className={cn('w-3.5 h-3.5 mx-auto mb-1', color)} />
                <p className="text-xs font-bold font-mono">{value}</p>
                <p className="text-[10px] text-muted-foreground">{label}</p>
              </div>
            ))}
          </div>

          {/* Prompt to chat */}
          <div className="px-5 pb-5">
            <button
              onClick={() => setChatOpen(true)}
              className="w-full bg-background border border-border rounded-lg p-3 text-left hover:border-primary/50 transition-colors group"
            >
              <p className="text-[10px] text-muted-foreground mb-1">Powered by Grok</p>
              <p className="text-xs text-foreground group-hover:text-primary transition-colors">
                Ask about your portfolio, agents, or market conditions →
              </p>
            </button>
          </div>
        </>
      )}
    </div>
  );
}
