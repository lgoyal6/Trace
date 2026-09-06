"use client";

import { useEffect, useRef, useState } from "react";
import { Masthead } from "@/components/masthead";
import { Hero } from "@/components/hero";
import { QueryForm } from "@/components/query-form";
import { SourcedSummary } from "@/components/sourced-summary";
import { BrainViewer } from "@/components/brain-viewer";
import { ProfilePanel } from "@/components/memory/profile-panel";
import { PolicyPanel } from "@/components/retrieval-policy";
import { SectionRail } from "@/components/section-rail";
import type { QueryResult } from "@/lib/api";

export default function Home() {
  const [result, setResult] = useState<QueryResult | null>(null);
  const [lastQuery, setLastQuery] = useState<string | null>(null);
  const [profileRefresh, setProfileRefresh] = useState(0);
  const resultsRef = useRef<HTMLDivElement>(null);
  const citedConditionNames = result
    ? Array.from(new Set(result.papers.map((paper) => paper.paper.condition)))
    : [];

  useEffect(() => {
    if (result) {
      resultsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      // The scroll is the whole answer to "where did my results go" for a
      // sighted reader. Someone driving the page from the keyboard is still
      // parked on the Search button, so move the caret with the viewport.
      resultsRef.current?.focus({ preventScroll: true });
    }
  }, [result]);

  function handleResult(nextResult: QueryResult) {
    setResult(nextResult);
    setProfileRefresh((count) => count + 1);
  }

  return (
    <>
      <Masthead />
      <main className="flex flex-1 flex-col">
        <Hero />
        <QueryForm onResult={handleResult} onCurrentQueryChange={setLastQuery} />
        <BrainViewer citedConditionNames={citedConditionNames} />
        {result?.policy && (
          <section className="mx-auto w-full max-w-[1100px] px-6 pb-2 pt-[70px] md:px-16">
            <div className="mb-7 flex flex-col items-center gap-5 text-center">
              <SectionRail number="§02b" eyebrow="What it cost to look wider" />
            </div>
            <PolicyPanel policy={result.policy} />
          </section>
        )}
        {result && (
          <div className="outline-none" ref={resultsRef} tabIndex={-1}>
            <SourcedSummary result={result} />
          </div>
        )}
        <ProfilePanel refreshKey={profileRefresh} result={result} lastQuery={lastQuery} />
      </main>
    </>
  );
}
