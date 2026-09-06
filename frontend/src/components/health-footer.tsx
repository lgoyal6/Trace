"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { getHealth, type Health } from "@/lib/api";

const PORTS: { key: keyof Health["ports"]; label: string }[] = [
  { key: "retrieval", label: "Retrieval" },
  { key: "llm", label: "Inference" },
  { key: "memory", label: "Memory" },
  { key: "ledger", label: "Ledger" },
];

export function HealthFooter() {
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
  }, []);

  return (
    <footer className="flex flex-wrap items-center justify-between gap-4 border-t border-rule px-6 py-[26px] md:px-16">
      {/* A bare div takes no accessible name, so the label was being dropped.
          The role is what lets it carry one. */}
      <div aria-label="Backend health" className="flex flex-wrap gap-6" role="group">
        {PORTS.map(({ key, label }) => {
          const port = health?.ports[key];
          const ok = port?.ok === true;
          return (
            <span key={key} className="flex items-center gap-2" title={port?.detail ?? "Status unavailable"}>
              <span
                className={`h-[7px] w-[7px] rounded-full ${
                  !health ? "border border-rule bg-white" : ok ? "bg-blue-600" : "border border-warn bg-white"
                }`}
              />
              <span className={`font-body text-xs ${!health ? "text-dim" : ok ? "text-ink" : "text-warn"}`}>
                {label}
                {/* The dot's colour is the only thing that says whether this
                    port is up. Say it in words too. */}
                <span className="sr-only">
                  {!health ? ", status unknown" : ok ? ", healthy" : ", degraded"}
                </span>
              </span>
              {port?.detail && (
                <span className={`font-data text-xs ${ok ? "text-dim" : "text-warn"}`}>{port.detail}</span>
              )}
            </span>
          );
        })}
      </div>
      <div className="flex items-center gap-4">
        <span className="rounded-[2px] border border-rule px-3 py-1.5 font-body text-xs text-dim">
          Literature verification, not a diagnostic tool.
        </span>
        <Link href="/cost" className="font-body text-sm text-blue-700 hover:underline">
          Cost ledger
        </Link>
      </div>
    </footer>
  );
}
