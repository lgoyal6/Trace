"use client";

import { useEffect, useState } from "react";
import {
  askEconomics,
  getEconomicsSummary,
  type EconomicsAnswer,
  type EconomicsSummary,
} from "@/lib/api";

const EXAMPLES = [
  "which pipeline step is most expensive?",
  "what did the last 10 queries cost?",
  "how many calls degraded?",
];

const WINDOWS = [
  ["1h", "1 h"],
  ["24h", "24 h"],
  ["7d", "7 d"],
] as const;

function SpendChart({ data }: { data: EconomicsSummary["by_hour"] }) {
  if (!data.length) return <p className="font-body text-sm text-dim">No hourly spend in this window.</p>;
  const width = 1200;
  const height = 260;
  const maxTokens = Math.max(...data.map((point) => point.tokens), 1);
  const maxCost = Math.max(...data.map((point) => point.cost_usd), 0.000001);
  const points = (field: "tokens" | "cost_usd", max: number) =>
    data
      .map((point, index) => {
        const x = data.length === 1 ? width / 2 : (index / (data.length - 1)) * width;
        const y = height - 20 - (point[field] / max) * (height - 40);
        return `${x},${y}`;
      })
      .join(" ");

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Tokens and USD spend over time" className="w-full">
        {[0, 1, 2, 3, 4].map((i) => (
          <line
            key={i}
            x1={0}
            x2={width}
            y1={20 + i * ((height - 40) / 4)}
            y2={20 + i * ((height - 40) / 4)}
            stroke="var(--blue-200)"
          />
        ))}
        <polyline fill="none" stroke="var(--blue-400)" strokeWidth={2} points={points("tokens", maxTokens)} />
        <polyline fill="none" stroke="var(--blue-600)" strokeWidth={2.5} points={points("cost_usd", maxCost)} />
      </svg>
      <div className="mt-3 flex justify-between font-data text-[10.5px] text-dim">
        {data
          .filter((_, i) => i % 4 === 0)
          .map((point) => (
            <span key={point.hour_iso}>{new Date(point.hour_iso).getHours().toString().padStart(2, "0")}:00</span>
          ))}
      </div>
      <div className="mt-4 flex gap-5 font-data text-[11px] text-dim">
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-0.5 w-3.5 bg-blue-600" />
          USD
        </span>
        <span className="flex items-center gap-1.5">
          <i className="inline-block h-0.5 w-3.5 bg-blue-400" />
          Tokens
        </span>
      </div>
    </div>
  );
}

export function EconomyDashboard() {
  const [window, setWindow] = useState("24h");
  const [summary, setSummary] = useState<EconomicsSummary | null>(null);
  const [summaryError, setSummaryError] = useState(false);
  const [question, setQuestion] = useState(EXAMPLES[0]);
  const [answer, setAnswer] = useState<EconomicsAnswer | null>(null);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState(false);

  useEffect(() => {
    let active = true;
    getEconomicsSummary(window)
      .then((data) => active && setSummary(data))
      .catch(() => active && setSummaryError(true));
    return () => {
      active = false;
    };
  }, [window]);

  async function submitQuestion(event: React.FormEvent) {
    event.preventDefault();
    if (!question.trim()) return;
    setAsking(true);
    setAskError(false);
    try {
      setAnswer(await askEconomics(question.trim()));
    } catch {
      setAskError(true);
    } finally {
      setAsking(false);
    }
  }

  const callSites = [...(summary?.by_call_site ?? [])].sort((a, b) => b.cost_usd - a.cost_usd);
  const maxSpend = Math.max(...callSites.map((site) => site.cost_usd), 0);
  const rowKeys = answer?.rows[0] ? Object.keys(answer.rows[0]) : [];

  return (
    <div>
      <header className="flex items-center justify-between border-b border-rule px-6 py-5 md:px-16">
        {/* The lockup is this page's title and there was no h1 at all. The
            type sizes live on the inner spans, so the tag change is invisible. */}
        <h1 className="flex items-center gap-3">
          <span aria-hidden="true" className="h-[22px] w-[22px] rounded-[4px] bg-blue-500" />
          <span className="font-display text-xl font-semibold text-ink">Trace</span>
          <span className="font-data text-sm text-dim">/cost</span>
        </h1>
        <div aria-label="Ledger window" className="flex gap-[3px] border border-rule p-[3px]" role="group">
          {WINDOWS.map(([value, label]) => (
            <button
              aria-pressed={window === value}
              key={value}
              type="button"
              onClick={() => {
                setSummary(null);
                setSummaryError(false);
                setWindow(value);
              }}
              className={`px-3 py-1 font-body text-xs ${
                window === value ? "bg-blue-100 text-blue-800" : "text-dim"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </header>

      <div className="mx-auto max-w-[1440px] px-6 py-14 md:px-16">
        {summaryError && (
          <p className="mb-6 font-body text-sm text-warn" role="alert">Ledger summary is unavailable.</p>
        )}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1.35fr_1fr]">
          <div
            className="border border-rule p-10 shadow-[var(--shadow-glow)]"
            style={{
              background:
                "radial-gradient(ellipse at 88% 15%, oklch(0.6781 0.1215 258.28 / 0.22) 0%, var(--blue-50) 58%)",
            }}
          >
            <p className="eyebrow">Median cost per query</p>
            <p className="mt-1 font-data text-[11px] text-dim">
              {/* Before the ledger answers there is no n. Printing 0 asserts
                  something the page does not know yet. */}
              n = {summary ? summary.total_requests : "-"} requests · Last {window}
            </p>

            {summary ? (
              <>
                <p className="mt-4 font-body text-sm text-dim">
                  Ledger reachable but no priced rows in this window. Cost is unavailable, not zero.
                </p>
                <p className="mt-2 font-body text-xs text-dim">
                  The <code className="font-data">median_cost_usd</code> field is not yet in the economics
                  contract - see Blockers.md.
                </p>
              </>
            ) : summaryError ? (
              // Without this branch a failed fetch left the card reading
              // "Loading ledger." underneath the error banner, for good.
              <p className="mt-6 font-body text-sm text-warn">
                The ledger did not answer, so the figures below are unknown rather than zero. Pick a
                window again to retry.
              </p>
            ) : (
              <p className="mt-6 font-body text-sm text-dim">Loading ledger.</p>
            )}

            <div className="mt-8 flex flex-wrap gap-6">
              {[
                [summary ? summary.total_requests.toLocaleString() : "-", "REQUESTS"],
                [summary ? `$${summary.total_cost_usd.toFixed(4)}` : "-", "TOTAL SPEND"],
                [summary ? summary.total_tokens.toLocaleString() : "-", "TOKENS"],
              ].map(([value, label]) => (
                <div key={label} className="flex flex-col gap-1">
                  <span className="font-data text-xl text-ink">{value}</span>
                  <span className="font-body text-[10.5px] uppercase tracking-[0.08em] text-dim">{label}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="border border-rule bg-white p-8">
            <p className="eyebrow mb-6">Spend by stage</p>
            {callSites.length === 0 && <p className="font-body text-sm text-dim">No call-site data in this window.</p>}
            <div className="grid gap-4">
              {callSites.map((site, index) => (
                <div key={site.call_site}>
                  <div className="mb-1.5 flex justify-between gap-3 font-body text-sm">
                    <span className="text-ink">{site.call_site}</span>
                    <span className="font-data text-xs text-dim">
                      ${site.cost_usd.toFixed(4)} · {summary && summary.total_cost_usd > 0 ? Math.round((site.cost_usd / summary.total_cost_usd) * 100) : 0}%
                    </span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-blue-100">
                    <div
                      className={`h-full rounded-full ${index === 0 ? "bg-blue-600" : "bg-blue-400"}`}
                      style={{ width: `${maxSpend ? (site.cost_usd / maxSpend) * 100 : 0}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="mt-6 border border-rule bg-white p-8">
          <p className="eyebrow mb-6">Spend over time</p>
          <SpendChart data={summary?.by_hour ?? []} />
        </div>

        <div className="mt-6 border border-rule bg-white p-8">
          <p className="eyebrow">Ask the ledger</p>
          <p className="mt-1 font-data text-xs text-dim">Natural language · Answers from TOKEN_LEDGER</p>

          <div className="mb-4 mt-5 flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => setQuestion(example)}
                className="rounded-[2px] border border-rule px-3 py-1.5 font-data text-[11.5px] text-dim hover:border-blue-300"
              >
                {example}
              </button>
            ))}
          </div>
          <form onSubmit={submitQuestion} className="flex flex-col gap-3 sm:flex-row">
            <label className="sr-only" htmlFor="ledger-question">Ask the ledger a question</label>
            <input
              aria-describedby={askError ? "ledger-error" : undefined}
              value={question}
              id="ledger-question"
              onChange={(event) => setQuestion(event.target.value)}
              className="min-w-0 flex-1 rounded-[3px] border border-rule px-4 py-3 font-body text-ink outline-none focus-visible:border-blue-300 focus-visible:[outline-style:solid] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-700"
            />
            <button
              disabled={asking || !question.trim()}
              className="rounded-[3px] bg-blue-700 px-6 py-3 font-body text-sm font-semibold text-white disabled:opacity-40"
            >
              {asking ? "Asking…" : "Ask"}
            </button>
          </form>
          {askError && (
            <p className="mt-4 font-body text-sm text-warn" id="ledger-error" role="alert">
              Cortex Analyst is unavailable.
            </p>
          )}
          <p aria-live="polite" className="sr-only" role="status">
            {asking ? "Asking the ledger." : ""}
          </p>
          {answer && (
            <div className="mt-6 grid gap-5">
              <p className="border-l-2 border-blue-500 pl-4 font-display text-[27px] text-ink">{answer.answer}</p>
              {rowKeys.length > 0 && (
                <div className="overflow-x-auto">
                  <table className="w-full border-collapse font-body text-sm">
                    <thead>
                      <tr>
                        {rowKeys.map((key) => (
                          <th key={key} scope="col" className="border-b border-rule p-3 text-left font-data text-xs text-dim">
                            {key}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {answer.rows.map((row, index) => (
                        <tr key={index}>
                          {rowKeys.map((key) => (
                            <td key={key} className="border-b border-blue-200 p-3 text-ink">
                              {String(row[key] ?? "")}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <details className="font-body text-sm text-dim">
                <summary className="cursor-pointer">Generated SQL</summary>
                <pre className="mt-3 overflow-x-auto border border-blue-200 bg-blue-50 p-4 font-data text-[11.5px] text-blue-800">
                  {answer.sql}
                </pre>
              </details>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
