import type { Metadata } from "next";
import "./globals.css";
import { Nav } from "@/components/Nav";

export const metadata: Metadata = {
  title: "AI Trading Bot — Dashboard",
  description: "Demo trading system dashboard (simulation & Exness demo only).",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" className="dark">
      <body className="bg-neutral-950 text-neutral-100 antialiased min-h-screen">
        <div className="flex min-h-screen">
          <Nav />
          <main className="flex-1 p-6 lg:p-8 overflow-x-hidden">
            <div className="mb-6 flex items-center justify-between">
              <div>
                <h1 className="text-xl font-semibold tracking-tight">AI Trading Bot</h1>
                <p className="text-sm text-neutral-500">
                  Demo trading system — no live trading in this codebase.
                </p>
              </div>
            </div>
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
