"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";

type RiskState = {
  trading_mode: string;
  kill_switch: boolean;
  kill_reason: string | null;
  trading_paused: boolean;
  pause_reason: string | null;
  updated_ts: string;
};

export default function RiskPage() {
  const [risk, setRisk] = useState<RiskState | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    try {
      setRisk(await apiFetch<RiskState>("/api/v1/risk"));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 10000);
    return () => clearInterval(id);
  }, []);

  async function setKillSwitch(enabled: boolean) {
    try {
      await fetch(`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/api/v1/risk/kill-switch`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(process.env.NEXT_PUBLIC_API_TOKEN
            ? { Authorization: `Bearer ${process.env.NEXT_PUBLIC_API_TOKEN}` }
            : {}),
        },
        body: JSON.stringify({ enabled, reason: enabled ? "manual: dashboard" : "" }),
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="space-y-6">
      {error ? (
        <div className="rounded-md border border-rose-900 bg-rose-950/40 px-4 py-3 text-sm text-rose-300">
          {error}
        </div>
      ) : null}

      <div className="flex items-center justify-between rounded-lg border border-neutral-800 bg-neutral-900/50 p-5">
        <div>
          <div className="text-sm font-medium">Global kill switch</div>
          <div className="mt-1 text-xs text-neutral-500">
            {risk?.kill_switch
              ? `ENGAGED — new trade execution is stopped (${risk.kill_reason ?? "no reason"})`
              : "Disarmed — trading allowed (subject to all other risk controls)"}
          </div>
        </div>
        <button
          onClick={() => setKillSwitch(!(risk?.kill_switch ?? false))}
          className={`rounded-md px-4 py-2 text-sm font-semibold transition-colors ${
            risk?.kill_switch
              ? "bg-emerald-600 hover:bg-emerald-500 text-white"
              : "bg-rose-700 hover:bg-rose-600 text-white"
          }`}
        >
          {risk?.kill_switch ? "RELEASE KILL SWITCH" : "ENGAGE KILL SWITCH"}
        </button>
      </div>

      <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-4 text-sm text-neutral-400">
        <div className="text-xs uppercase tracking-wide text-neutral-500">
          Risk parameters
        </div>
        <p className="mt-2">
          Max risk per trade, max daily loss, position limits, spread limits and
          session windows are configured and editable here from Phase 5 onward.
          Current mode:{" "}
          <span className="text-neutral-200">{risk?.trading_mode ?? "—"}</span>
        </p>
      </div>
    </div>
  );
}
