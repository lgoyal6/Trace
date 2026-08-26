// frontend/src/components/rarity-comparison.tsx
"use client";

import { useEffect, useState } from "react";
import { getConditions, type Condition } from "@/lib/api";

// Prevalence figures aren't in the /conditions contract (Condition only
// carries rarity: "rare" | "common", not a numeric paper count or
// population prevalence) - real ordering signal beyond the rare/common
// split doesn't exist here, so this groups rather than fabricates a rank.
// Bar length is the real retrieval boost ratio (rare 1.6x vs common's 1.0x
// baseline, backend/app/retrieval/rarity.py's RARE_BOOST), not invented
// population-prevalence percentages.
const RARE_BAR_PCT = 100;
const COMMON_BAR_PCT = Math.round((1 / 1.6) * 100);

export function RarityComparison() {
  const [conditions, setConditions] = useState<Condition[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let active = true;
    getConditions()
      .then((data) => active && setConditions(data))
      .catch(() => active && setError(true));
    return () => {
      active = false;
    };
  }, []);

  const rare = (conditions ?? []).filter((c) => c.rarity === "rare").sort((a, b) => a.name.localeCompare(b.name));
  const common = (conditions ?? []).filter((c) => c.rarity !== "rare").sort((a, b) => a.name.localeCompare(b.name));

  return (
    <div className="border border-rule bg-white">
      <div className="flex items-center justify-between border-b border-ink px-6 py-[22px] md:px-[30px]">
        <span className="font-body text-[13px] font-semibold text-ink">Corpus, ordered by prevalence</span>
        <span className="font-data text-[11.5px] text-dim">Rarest first · Bar length = retrieval boost</span>
      </div>

      {error && <p className="p-6 font-body text-sm text-warn">Corpus scope is unavailable.</p>}
      {!error && !conditions && <p className="p-6 font-body text-sm text-dim">Loading corpus scope.</p>}

      {conditions && (
        <div>
          {rare.map((condition, index) => (
            <Row key={condition.name} index={index + 1} condition={condition} isRare barPct={RARE_BAR_PCT} />
          ))}
          {rare.length > 0 && (
            <div className="flex items-center gap-4 px-6 py-2.5 md:px-[30px]">
              <span className="font-data text-[10px] uppercase tracking-wide text-dim">
                Rarity floor - no boost applied below this line
              </span>
              <span className="h-px flex-1 bg-ink" aria-hidden="true" />
            </div>
          )}
          {common.map((condition, index) => (
            <Row
              key={condition.name}
              index={rare.length + index + 1}
              condition={condition}
              isRare={false}
              barPct={COMMON_BAR_PCT}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function Row({
  index,
  condition,
  isRare,
  barPct,
}: {
  index: number;
  condition: Condition;
  isRare: boolean;
  barPct: number;
}) {
  return (
    <div
      className={`grid items-center gap-[22px] px-6 py-[11px] md:px-[30px] ${
        isRare ? "border-b border-blue-200 bg-blue-50" : "border-b border-rule bg-white"
      }`}
      style={{ gridTemplateColumns: "32px minmax(0,1fr) minmax(0,1.05fr)" }}
    >
      <span className={`font-data text-xs ${isRare ? "text-blue-800" : "text-dim"}`}>
        {String(index).padStart(2, "0")}
      </span>
      <span className="flex items-center gap-2.5 min-w-0">
        <span
          className={`h-[7px] w-[7px] flex-shrink-0 rounded-full ${
            isRare ? "bg-blue-500" : "border border-rule bg-white"
          }`}
        />
        <span className={`truncate font-body text-sm ${isRare ? "font-semibold text-blue-900" : "text-trace-muted"}`}>
          {condition.name}
        </span>
      </span>
      <span className="flex items-center gap-3">
        <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-blue-50">
          <span
            className={`block h-full ${isRare ? "bg-blue-500" : "bg-rule"}`}
            style={{ width: `${barPct}%` }}
          />
        </span>
      </span>
    </div>
  );
}
