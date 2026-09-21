import { SectionPlaceholder } from "@/components/SectionPlaceholder";

export const metadata = { title: "Open Positions — AI Trading Bot" };

export default function PositionsPage() {
  return (
    <SectionPlaceholder
      title="Open Positions"
      note="Instrument, type, entry/current price, SL/TP, unrealized P/L, duration — built in Phase 7."
    />
  );
}
