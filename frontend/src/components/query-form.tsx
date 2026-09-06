// frontend/src/components/query-form.tsx
"use client";

import { useState } from "react";
import { queryLiteratureStream, type QueryResult, type RetrievalPolicy } from "@/lib/api";
import { SectionRail } from "@/components/section-rail";
import { ProgressTimeline, stageMessage, type ProgressStageEvent } from "@/components/progress-timeline";
import { PolicyToggle } from "@/components/retrieval-policy";

type Props = {
  onResult: (result: QueryResult) => void;
  onCurrentQueryChange?: (query: string) => void;
};

export function QueryForm({ onResult, onCurrentQueryChange }: Props) {
  const [query, setQuery] = useState("");
  const [personalize, setPersonalize] = useState(true);
  // null == omit the field == today's default path. Kept as a tri-state rather
  // than a boolean so "I did not ask for a policy" stays distinguishable from
  // "I explicitly asked for tight" - they retrieve the same 10 papers but the
  // default path applies no compression at all.
  const [policy, setPolicy] = useState<RetrievalPolicy | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "error">("idle");
  const [stages, setStages] = useState<ProgressStageEvent[]>([]);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    submitQuery(query.trim());
  }

  async function submitQuery(queryText: string) {
    if (!queryText.trim()) return;

    onCurrentQueryChange?.(queryText.trim());
    setStages([]);
    setStatus("loading");
    try {
      const result = await queryLiteratureStream(
        queryText.trim(),
        personalize,
        (stage, detail) => {
          setStages((prev) => [...prev, { stage, iteration: detail.iteration }]);
        },
        policy,
      );
      onResult(result);
      setStatus("idle");
    } catch {
      setStatus("error");
    }
  }

  function handleQueryChange(value: string) {
    setQuery(value);
    onCurrentQueryChange?.(value);
  }

  return (
    <section id="query" className="mx-auto max-w-[760px] px-6 py-[110px] md:px-16">
      <div className="mb-10 flex flex-col items-center gap-5 text-center">
        <SectionRail number="§01" eyebrow="Describe the finding" />
        <h2 className="font-display text-[clamp(28px,4vw,44px)] font-medium text-ink">
          Plain language in. Cited literature out.
        </h2>
      </div>

      <form aria-busy={status === "loading"} onSubmit={handleSubmit} className="border border-rule bg-white shadow-[var(--shadow-input)]">
        {/* The placeholder is an example, not a name: it disappears the moment
            anything is typed, so the field needs a label of its own. It is
            visually hidden because the section heading already says this in
            display type. */}
        <label className="sr-only" htmlFor="query-input">
          Describe the finding
        </label>
        <textarea
          aria-describedby={status === "error" ? "query-error query-hint" : "query-hint"}
          value={query}
          id="query-input"
          onChange={(event) => handleQueryChange(event.target.value)}
          placeholder="asymmetric parietal hypometabolism on FDG-PET with progressive apraxia"
          rows={3}
          className="w-full resize-none bg-transparent px-[26px] pb-3 pt-[26px] font-body text-lg leading-[1.5] text-ink outline-none focus-visible:[outline-style:solid] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-700 placeholder:text-dim"
          disabled={status === "loading"}
        />
        <div className="border-t border-rule px-[26px] py-3.5">
          <PolicyToggle value={policy} onChange={setPolicy} disabled={status === "loading"} />
        </div>
        <div className="flex flex-col gap-4 border-t border-rule px-[26px] pb-[22px] pt-3.5 sm:flex-row sm:items-center sm:justify-between">
          <label className="flex cursor-pointer items-center gap-3">
            <span className="relative inline-flex h-5 w-[34px] items-center">
              <input
                type="checkbox"
                checked={personalize}
                onChange={(event) => setPersonalize(event.target.checked)}
                disabled={status === "loading"}
                className="peer sr-only"
              />
              <span className="absolute inset-0 rounded-full bg-rule transition-colors peer-checked:bg-blue-700" />
              <span className="relative h-4 w-4 translate-x-0.5 rounded-full bg-white shadow-[var(--shadow-input)] transition-transform peer-checked:translate-x-[18px]" />
            </span>
            <span className="font-body text-[13px] font-medium text-ink">Personalize with memory</span>
          </label>
          <button
            type="submit"
            disabled={status === "loading" || !query.trim()}
            className="rounded-[3px] bg-blue-700 px-[30px] py-[13px] font-body text-sm font-semibold text-white shadow-[0_4px_16px_oklch(0.6781_0.1215_258.28_/_0.34)] transition-all hover:-translate-y-px hover:shadow-[0_6px_24px_oklch(0.6781_0.1215_258.28_/_0.5),0_0_0_5px_oklch(0.6781_0.1215_258.28_/_0.16)] disabled:pointer-events-none disabled:opacity-40"
          >
            {status === "loading" ? "Searching" : "Search"}
          </button>
        </div>
        {status === "loading" && <ProgressTimeline stages={stages} />}
        {/* Mounted at all times. A live region that appears together with its
            first message is usually announced by nothing, because there was no
            region to observe when the text arrived. */}
        <p aria-live="polite" className="sr-only" role="status">
          {status === "loading" ? stageMessage(stages) : ""}
        </p>
        {status === "error" && (
          <p
            className="border-t border-warn bg-warn-bg px-[26px] py-4 font-body text-sm text-warn"
            id="query-error"
            role="alert"
          >
            Retrieval degraded. The backend did not respond. Confirm it is running and try again.
          </p>
        )}
      </form>
      <p className="mt-4 font-body text-[13px] text-dim" id="query-hint">
        Name a scan type, a symptom, or a region. First request after idle takes 4–8 s while the retrieval
        index warms.
      </p>
    </section>
  );
}
