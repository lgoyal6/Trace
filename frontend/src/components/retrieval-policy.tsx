// frontend/src/components/retrieval-policy.tsx
"use client";

import type { PolicyOut, RetrievalPolicy } from "@/lib/api";

/* §02b - Retrieval breadth.
 *
 * Every number rendered here is measured server-side on THIS request by the
 * real compressor (backend/app/pipeline.py's _compress_for_policy) and handed
 * back on QueryResponse.policy. Nothing on this screen is a stored benchmark
 * figure or a client-side estimate - if the panel shows a token count, the
 * backend counted it on the abstracts that just went into the prompt.
 *
 * The gold-set result this reproduces per-request lives in
 * backend/measurement/results/policy_bench.md: rare-condition recall
 * 0.4118 -> 0.7475 (+81.5%) at -0.34% cost.
 */

export const POLICY_COPY: Record<
  RetrievalPolicy,
  { name: string; dial: string; blurb: string }
> = {
  tight: {
    name: "Tight",
    dial: "10 papers · 4 sentences each",
    blurb:
      "Today's shipped retrieval breadth. Depth over coverage - a rare condition with four papers in the corpus can fall below the cut entirely.",
  },
  generous: {
    name: "Generous",
    dial: "30 papers · 1 sentence each",
    blurb:
      "Three times the papers, a quarter of the text per paper. Across the 28-query gold set this lands inside Tight's token budget (−0.7%) while rare-condition recall rises 81%. On any single query it can run either side of Tight - short abstracts skip compression - so treat the tokens below as this request's, not a guarantee.",
  },
};

export function PolicyToggle({
  value,
  onChange,
  disabled,
}: {
  value: RetrievalPolicy | null;
  onChange: (next: RetrievalPolicy | null) => void;
  disabled?: boolean;
}) {
  const options: Array<{ key: RetrievalPolicy | null; label: string }> = [
    { key: null, label: "Default" },
    { key: "tight", label: "Tight" },
    { key: "generous", label: "Generous" },
  ];

  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="font-data text-[11.5px] uppercase tracking-[0.08em] text-dim">
        Retrieval policy
      </span>
      <div
        role="radiogroup"
        aria-label="Retrieval policy"
        className="inline-flex border border-rule bg-white"
      >
        {options.map((option) => {
          const active = value === option.key;
          return (
            <button
              key={option.label}
              type="button"
              role="radio"
              aria-checked={active}
              disabled={disabled}
              onClick={() => onChange(option.key)}
              className={[
                "font-body text-[13px] px-3.5 py-1.5 transition-colors",
                "border-r border-rule last:border-r-0",
                "disabled:cursor-not-allowed disabled:opacity-50",
                active
                  ? "bg-[var(--blue-100)] font-semibold text-[var(--blue-800)]"
                  : "text-trace-muted hover:bg-[var(--blue-50)]",
              ].join(" ")}
            >
              {option.label}
            </button>
          );
        })}
      </div>
      {value && (
        <span className="font-data text-[11.5px] text-dim">{POLICY_COPY[value].dial}</span>
      )}
    </div>
  );
}

export function PolicyPanel({ policy }: { policy: PolicyOut | null | undefined }) {
  if (!policy) return null;

  const label = (policy.label === "generous" ? "generous" : "tight") as RetrievalPolicy;
  const copy = POLICY_COPY[label];
  const before = policy.promptTokensBeforeCompression;
  const after = policy.promptTokensAfterCompression;
  // Guard the divide rather than assume: a corpus of very short abstracts can
  // legitimately produce before === 0 once compression is skipped.
  const keptPct = before > 0 ? (after / before) * 100 : 100;

  return (
    <div data-testid="policy-panel" data-policy={label} className="border border-rule bg-white">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink px-6 py-[22px] md:px-[30px]">
        <span className="font-body text-[13px] font-semibold text-ink">
          Retrieval breadth · {copy.name}
        </span>
        <span className="font-data text-[11.5px] text-dim">{copy.dial}</span>
      </div>

      <div className="grid grid-cols-2 gap-px bg-rule md:grid-cols-4">
        <Stat testId="policy-papers" label="Papers in prompt" value={String(policy.papersInPrompt)} />
        <Stat testId="policy-sentences" label="Sentences kept" value={String(policy.compressTopN)} suffix="per paper" />
        <Stat
          testId="policy-tokens"
          label="Prompt tokens"
          value={after.toLocaleString()}
          suffix={`of ${before.toLocaleString()}`}
        />
        <Stat
          testId="policy-reduction"
          label="Context compressed"
          value={`${policy.reductionPct.toFixed(1)}%`}
          suffix={`${policy.tokensSaved.toLocaleString()} saved`}
          emphasis
        />
      </div>

      <div className="px-6 py-5 md:px-[30px]">
        <div className="flex h-2 w-full overflow-hidden bg-[var(--blue-100)]">
          <div
            className="h-full bg-[var(--blue-600)]"
            style={{ width: `${Math.max(0, Math.min(100, keptPct))}%` }}
          />
        </div>
        <div className="mt-2 flex justify-between font-data text-[11px] text-dim">
          <span>{after.toLocaleString()} tokens sent to the model</span>
          <span>{policy.tokensSaved.toLocaleString()} compressed away</span>
        </div>
        <p className="mt-4 max-w-[62ch] font-body text-[13.5px] leading-relaxed text-trace-muted">
          {copy.blurb}
        </p>
        <p className="mt-3 max-w-[62ch] font-body text-[12.5px] leading-relaxed text-dim">
          Counted on this request by the same compressor the measurement gate uses - not a
          stored benchmark figure. Compression feeds the summary prompt only; the abstracts
          shown below, and the ones each citation is verified against, are the full originals.
        </p>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  suffix,
  emphasis,
  testId,
}: {
  label: string;
  value: string;
  suffix?: string;
  emphasis?: boolean;
  testId?: string;
}) {
  return (
    <div className="bg-white px-6 py-5 md:px-[30px]">
      <p className="font-data text-[11px] uppercase tracking-[0.08em] text-dim">{label}</p>
      <p
        data-testid={testId}
        className={[
          "mt-2 font-display text-[26px] leading-none",
          emphasis ? "text-[var(--blue-800)]" : "text-ink",
        ].join(" ")}
      >
        {value}
      </p>
      {suffix && <p className="mt-1.5 font-data text-[11px] text-dim">{suffix}</p>}
    </div>
  );
}
