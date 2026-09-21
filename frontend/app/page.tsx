"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type Health = {
  status: string;
  version: string;
  mode: string;
  broker: string;
  engine_running: boolean;
  account_connected: boolean;
  instruments: string[];
  server_time: string;
};

type Account = {
  currency: string;
  balance: number;
  equity: number;
  free_margin: number;
  margin_level: number | null;
  leverage: number;
  open_positions: number;
  unrealized_pnl: number;
  ts: string;
};

function Card({
  label,
  value,
  sub,
  tone = "neutral",
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "neutral" | "good" | "bad" | "warn";
}) {
  const toneClass = {
    neutral: "text-neutral-100",
    good: "text-emerald-400",
    bad: "text-rose-400",
    warn: "text-amber-400",
  }[tone];
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-4">
      <div className="text-xs uppercase tracking-wide text-neutral-500">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass}`}>{value}</div>
      {sub ? <div className="mt-0.5 text-xs text-neutral-500">{sub}</div> : null}
    </div>
  );
}

export default function OverviewPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [account, setAccount] = useState<Account | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    async function poll() {
      try {
        const h = await apiFetch<Health>("/health");
        if (stopped) return;
        setHealth(h);
        setError(null);
        if (h.account_connected) {
          const a = await apiFetch<Account>("/api/v1/account");
          if (!stopped) setAccount(a);
        }
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : String(e));
      }
    }
    poll();
    const id = setInterval(poll, 5000);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  }, []);

  return (
    <div className="space-y-6">
      {error ? (
        <div className="rounded-md border border-rose-900 bg-rose-950/40 px-4 py-3 text-sm text-rose-300">
          Cannot reach the trading API: {error}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Card
          label="Balance"
          value={account ? `${account.balance.toFixed(2)} ${account.currency}` : "—"}
          sub={health ? `mode: ${health.mode} · broker: ${health.broker}` : undefined}
        />
        <Card
          label="Equity"
          value={account ? account.equity.toFixed(2) : "—"}
          sub={
            account
              ? `unrealized P/L: ${account.unrealized_pnl >= 0 ? "+" : ""}${account.unrealized_pnl.toFixed(2)}`
              : undefined
          }
          tone={
            account && account.unrealized_pnl < 0 ? "bad" : "good"
          }
        />
        <Card
          label="Free margin"
          value={account ? account.free_margin.toFixed(2) : "—"}
          sub={
            account
              ? `margin level: ${account.margin_level === null ? "n/a" : `${account.margin_level.toFixed(1)}%`} · lev 1:${account.leverage}`
              : undefined
          }
        />
        <Card
          label="Open positions"
          value={account ? String(account.open_positions) : "—"}
          sub={
            health
              ? `engine: ${health.engine_running ? "running" : "stopped"} · account: ${health.account_connected ? "connected" : "pending"}`
              : undefined
          }
          tone={health && !health.engine_running ? "warn" : "neutral"}
        />
      </div>

      <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-4">
        <div className="text-xs uppercase tracking-wide text-neutral-500">
          Connection status
        </div>
        <div className="mt-2 flex flex-wrap gap-x-8 gap-y-2 text-sm">
          <span>
            API:{" "}
            <span className={health ? "text-emerald-400" : "text-rose-400"}>
              {health ? "connected" : "unreachable"}
            </span>
          </span>
          {health ? (
            <>
              <span>
                Version: <span className="text-neutral-300">{health.version}</span>
              </span>
              <span>
                Instruments:{" "}
                <span className="text-neutral-300">{health.instruments.join(", ")}</span>
              </span>
              <span>
                Server time:{" "}
                <span className="tabular-nums text-neutral-300">{health.server_time}</span>
              </span>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
