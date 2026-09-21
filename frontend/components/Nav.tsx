"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const sections = [
  { href: "/", label: "Overview" },
  { href: "/market", label: "Market Monitor" },
  { href: "/signals", label: "Trading Signals" },
  { href: "/positions", label: "Open Positions" },
  { href: "/trades", label: "Trade History" },
  { href: "/risk", label: "Risk Controls" },
  { href: "/logs", label: "System Logs" },
  { href: "/settings", label: "Settings" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <aside className="w-56 shrink-0 border-r border-neutral-800 p-4 hidden md:block">
      <div className="mb-6 px-2">
        <span className="text-xs font-semibold uppercase tracking-widest text-neutral-500">
          Demo mode
        </span>
      </div>
      <nav className="space-y-1">
        {sections.map((s) => {
          const active =
            s.href === "/" ? pathname === "/" : pathname.startsWith(s.href);
          return (
            <Link
              key={s.href}
              href={s.href}
              className={`block rounded-md px-3 py-2 text-sm transition-colors ${
                active
                  ? "bg-neutral-800 text-neutral-100"
                  : "text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200"
              }`}
            >
              {s.label}
            </Link>
          );
        })}
      </nav>
    </aside>
  );
}
