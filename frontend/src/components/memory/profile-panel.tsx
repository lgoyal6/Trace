"use client";

import { useEffect, useState } from "react";
import {
  forgetMemory,
  getMemoryProfile,
  setMemorySpecialty,
  type MemoryProfile,
  type QueryResult,
} from "@/lib/api";
import { SectionRail } from "@/components/section-rail";

function ProfileSidebar({ refreshKey }: { refreshKey: number }) {
  const [profile, setProfile] = useState<MemoryProfile | null>(null);
  const [specialty, setSpecialty] = useState("");
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let active = true;
    getMemoryProfile()
      .then((nextProfile) => {
        if (!active) return;
        setProfile(nextProfile);
        setSpecialty(nextProfile.specialty ?? "");
        setStatus("ready");
      })
      .catch(() => active && setStatus("error"));
    return () => {
      active = false;
    };
  }, [refreshKey]);

  async function saveSpecialty(event: React.FormEvent) {
    event.preventDefault();
    await setMemorySpecialty(specialty.trim());
    setProfile(await getMemoryProfile());
  }

  async function resetProfile() {
    if (!window.confirm("Forget this demo profile and start cold?")) return;
    await forgetMemory();
    const nextProfile = await getMemoryProfile();
    setProfile(nextProfile);
    setSpecialty(nextProfile.specialty ?? "");
  }

  return (
    <div className="border border-rule bg-white">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-5 py-4 font-body text-[13px] font-semibold text-ink"
      >
        Profile
        <span className="font-data text-dim">{open ? "-" : "+"}</span>
      </button>
      {open && (
        <div className="border-t border-rule p-5">
          {status === "loading" && <p className="font-body text-sm text-dim">Loading memory.</p>}
          {status === "error" && <p className="font-body text-sm text-warn">Memory is unavailable.</p>}
          {profile && status === "ready" && (
            <div className="grid gap-5">
              <form onSubmit={saveSpecialty} className="grid gap-2">
                <label htmlFor="memory-specialty" className="eyebrow">Specialty</label>
                <div className="flex gap-2">
                  <input
                    id="memory-specialty"
                    value={specialty}
                    onChange={(event) => setSpecialty(event.target.value)}
                    placeholder="e.g. nuclear medicine"
                    className="min-w-0 flex-1 rounded-[3px] border border-rule px-3 py-2 font-body text-sm text-ink outline-none focus:border-blue-300"
                  />
                  <button className="rounded-[3px] border border-blue-700 px-3 font-body text-sm font-medium text-blue-700">
                    Save
                  </button>
                </div>
              </form>

              <div className="grid grid-cols-2 gap-3">
                <div className="border border-blue-200 bg-blue-50 p-3">
                  <p className="font-data text-2xl text-ink">{profile.query_count}</p>
                  <p className="font-body text-[10.5px] uppercase tracking-[0.08em] text-dim">Queries</p>
                </div>
                <div className="border border-blue-200 bg-blue-50 p-3">
                  <p className="font-data text-2xl text-ink">{profile.seen_pmid_count}</p>
                  <p className="font-body text-[10.5px] uppercase tracking-[0.08em] text-dim">Papers read</p>
                </div>
              </div>

              <div>
                <p className="eyebrow mb-2">Conditions explored</p>
                <div className="flex flex-wrap gap-1.5">
                  {profile.conditions_explored.length ? (
                    profile.conditions_explored.map((condition) => (
                      <span
                        key={condition}
                        className="rounded-[2px] border border-blue-200 bg-blue-100 px-2 py-1 font-data text-[11px] text-blue-800"
                      >
                        {condition}
                      </span>
                    ))
                  ) : (
                    <span className="font-body text-sm text-dim">None yet</span>
                  )}
                </div>
              </div>

              <div>
                <p className="eyebrow mb-2">Distilled context</p>
                <blockquote className="border-l-2 border-blue-500 pl-4 font-body text-[13px] italic leading-relaxed text-trace-muted">
                  {profile.distilled_context || "No context distilled yet. Run a few personalized queries."}
                </blockquote>
                {profile.distilled_context && (
                  <p className="mt-1.5 font-data text-[10px] text-dim">Written by the system · Shown verbatim</p>
                )}
              </div>

              <button
                type="button"
                onClick={resetProfile}
                className="justify-self-start font-body text-xs text-blue-700 underline"
              >
                Reset profile and start cold
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ColdWarmComparison({ result, query }: { result: QueryResult | null; query: string | null }) {
  if (!result) {
    return (
      <div className="border border-rule bg-white p-9 text-center">
        <p className="font-body text-sm text-dim">
          No comparison yet. Run a personalized query and the cold-vs-warm result set appears here.
        </p>
      </div>
    );
  }

  const warm = result.papers;
  const cold = [...warm].sort((a, b) => b.score / b.memoryMultiplier - a.score / a.memoryMultiplier);
  const coldRank = new Map(cold.map((p, i) => [p.paper.pmid, i + 1]));

  const promoted = warm.filter((p) => p.memoryMultiplier > 1).length;
  const suppressed = warm.filter((p) => p.memoryMultiplier < 1).length;
  const multipliers = warm.map((p) => p.memoryMultiplier);
  const range =
    multipliers.length > 0
      ? `${Math.min(...multipliers).toFixed(2)}–${Math.max(...multipliers).toFixed(2)}`
      : "-";

  return (
    <div className="border border-rule bg-white">
      <div className="border-b border-rule px-6 py-4">
        <span className="font-body text-sm font-semibold text-ink">
          {query ? `"${query}"` : "Latest query"}
        </span>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 md:divide-x md:divide-rule">
        <div>
          <div className="border-b border-rule bg-white px-5 py-2.5">
            <span className="font-data text-[10.5px] uppercase tracking-[0.08em] text-dim">
              Cold · personalization off
            </span>
          </div>
          {cold.map((p, i) => (
            <div key={p.paper.pmid} className="flex items-baseline gap-3 border-t border-rule px-5 py-2.5 first:border-t-0">
              <span className="font-data text-xs text-dim">{i + 1}</span>
              <span className="truncate font-body text-sm text-trace-muted">{p.paper.title}</span>
              <span className="ml-auto flex-shrink-0 font-data text-[10.5px] text-dim">{p.paper.pmid}</span>
            </div>
          ))}
        </div>
        <div>
          <div className="border-b border-rule bg-blue-50 px-5 py-2.5">
            <span className="font-data text-[10.5px] uppercase tracking-[0.08em] text-dim">
              Warm · personalization on
            </span>
          </div>
          {warm.map((p, i) => {
            const moved = (coldRank.get(p.paper.pmid) ?? i + 1) - (i + 1);
            const promotedRow = p.memoryMultiplier > 1;
            const demotedRow = p.memoryMultiplier < 1;
            return (
              <div
                key={p.paper.pmid}
                className={`flex items-baseline gap-3 border-t border-rule px-5 py-2.5 first:border-t-0 ${
                  promotedRow ? "bg-blue-50" : "bg-white"
                }`}
              >
                <span className={`font-data text-xs ${promotedRow ? "text-blue-800" : "text-dim"}`}>{i + 1}</span>
                <span className={`truncate font-body text-sm ${promotedRow ? "text-ink" : "text-trace-muted"}`}>
                  {p.paper.title}
                </span>
                {promotedRow && (
                  <span className="ml-auto flex-shrink-0 rounded-[2px] border border-blue-200 bg-blue-100 px-1.5 py-0.5 font-data text-[10px] text-blue-800">
                    {moved > 0 ? `↑ ${moved} · ` : ""}
                    {p.paper.isRare ? "Rare" : "Common"}
                  </span>
                )}
                {demotedRow && (
                  <span className="ml-auto flex-shrink-0 rounded-[2px] border border-rule bg-white px-1.5 py-0.5 font-data text-[10px] text-dim">
                    {moved < 0 ? `↓ ${-moved} · ` : ""}Read before
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div className="border-t border-rule px-6 py-3.5">
        <p className="font-body text-xs text-trace-muted">
          Memory moved {promoted} paper{promoted === 1 ? "" : "s"}, suppressed {suppressed} already read, and
          raised no result above the rarity ceiling. Multiplier range applied: {range}.
        </p>
      </div>
    </div>
  );
}

type Props = { refreshKey?: number; result?: QueryResult | null; lastQuery?: string | null };

export function ProfilePanel({ refreshKey = 0, result = null, lastQuery = null }: Props) {
  return (
    <section className="bg-blue-50 px-6 py-[110px] md:px-16">
      <SectionRail number="§04" eyebrow="Memory" />
      <h2 className="mt-5 font-display text-[clamp(28px,4vw,44px)] font-medium text-ink">
        What it remembers, and what that changed.
      </h2>
      <p className="mt-4 max-w-xl font-body text-[15px] text-trace-muted">
        The re-rank multiplier is capped to [0.6, 1.2]. Memory can suppress repetition and reinforce a
        thread, but it cannot outrank the rarity signal.
      </p>

      <div className="mt-8 grid grid-cols-1 items-start gap-6 md:grid-cols-[340px_1fr]">
        <ProfileSidebar refreshKey={refreshKey} />
        <ColdWarmComparison result={result} query={lastQuery} />
      </div>
    </section>
  );
}
