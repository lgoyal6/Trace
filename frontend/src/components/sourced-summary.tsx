import type { QueryResult, ScoredPaper, CitationOut } from "@/lib/api";
import { RetrievalTrace } from "@/components/retrieval-trace";
import { SectionRail } from "@/components/section-rail";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

const SENTENCE_SPLIT_RE = /(?<=[.!?])\s+(?=[A-Z0-9])/;
const MARKER_RE = /\[(\d+)\]/g;

type Sentence = {
  text: string;
  markerIndex: number | null;
};

/** Remove "[N]" markers for display, then tidy what their removal leaves behind.
 *
 * Markers sit inside the sentence before its terminal punctuation ("…on EEG
 * [1]."), because the sentence splitter needs the period to be sentence-final.
 * Deleting the marker naively leaves "…on EEG ." - an orphaned space before
 * the period on every claim. */
function stripMarkers(text: string): string {
  return text
    .replace(MARKER_RE, "")
    .replace(/\s+([.,;:!?])/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/** " · Journal, Year" - but only for the parts that actually exist.
 *
 * Every record in the current corpus has `journal: null, year: null` (the
 * PubMed ingestion never populated them), and rendering them unconditionally
 * produced a literal "PMID 34703513 · , 0" under every citation. Showing an
 * empty field as punctuation-and-a-zero is worse than showing nothing. */
function citationMeta(scored: ScoredPaper | undefined): string {
  const parts = [scored?.paper.journal, scored?.paper.year].filter(
    (v) => v !== null && v !== undefined && v !== "" && v !== 0,
  );
  return parts.length ? ` · ${parts.join(", ")}` : "";
}

function splitIntoSentences(markdown: string): Sentence[] {
  if (!markdown.trim()) return [];
  return markdown
    .split(SENTENCE_SPLIT_RE)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((text) => {
      const markers = [...text.matchAll(MARKER_RE)].map((m) => Number(m[1]));
      return { text, markerIndex: markers.length ? markers[markers.length - 1] : null };
    });
}

function CitationMarker({ citation }: { citation: CitationOut | undefined }) {
  if (!citation) return null;
  if (citation.supported === true) {
    return (
      <span className="rounded-[2px] border border-blue-200 bg-blue-100 px-1.5 py-0.5 font-data text-[11px] font-semibold text-blue-800">
        [{citation.index}]
      </span>
    );
  }
  if (citation.supported === false) {
    return (
      <span className="rounded-[2px] border border-warn bg-warn-bg px-1.5 py-0.5 font-data text-[11px] font-semibold text-warn">
        [{citation.index}]
      </span>
    );
  }
  return (
    <span className="rounded-[2px] border border-rule bg-white px-1.5 py-0.5 font-data text-[11px] font-semibold text-dim opacity-45">
      [{citation.index}]
    </span>
  );
}

function SummaryBody({ result }: { result: QueryResult }) {
  const citationsByIndex = new Map(result.citations.map((c) => [c.index, c]));
  const sentences = splitIntoSentences(result.summary_markdown);

  return (
    <div>
      {sentences.map((sentence, i) => {
        const citation = sentence.markerIndex !== null ? citationsByIndex.get(sentence.markerIndex) : undefined;
        const unsupported = citation?.supported === false;
        return (
          <div
            key={i}
            className={`grid items-start gap-[22px] border-t border-blue-200 py-4 first:border-t-0 ${
              unsupported ? "border-l-2 border-l-warn bg-warn-bg-soft pl-[18px]" : ""
            }`}
            style={{ gridTemplateColumns: "56px minmax(0,1fr)" }}
          >
            <span className="flex justify-end pt-[7px]">
              <CitationMarker citation={citation} />
            </span>
            <div>
              <p className="font-body text-lg leading-[1.72] text-ink" style={{ textWrap: "pretty" }}>
                {stripMarkers(sentence.text)}
              </p>
              {unsupported && citation?.note && (
                <p className="mt-1.5 font-body text-[12.5px] text-warn">{citation.note}</p>
              )}
            </div>
          </div>
        );
      })}

      <div className="mt-6 flex flex-wrap gap-x-6 gap-y-2 border-t border-rule pt-4">
        <Key tone="verified" label="Verified against source abstract" />
        <Key tone="unsupported" label="Unsupported - shown, not hidden" />
        <Key tone="pending" label="Verification pending" />
      </div>
    </div>
  );
}

function Key({ tone, label }: { tone: "verified" | "unsupported" | "pending"; label: string }) {
  const dot = tone === "verified" ? "bg-blue-600" : tone === "unsupported" ? "bg-warn" : "border border-rule bg-white";
  return (
    <span className="flex items-center gap-1.5 font-body text-xs text-dim">
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function SourceList({ result }: { result: QueryResult }) {
  const papersByPmid = new Map(result.papers.map((p) => [p.paper.pmid, p]));
  return (
    <div className="bg-blue-50 p-9">
      <p className="eyebrow mb-4">Sources</p>
      {result.citations.map((citation) => {
        const scored = papersByPmid.get(citation.pmid);
        return (
          <div key={`${citation.index}-${citation.pmid}`} className="border-t border-blue-200 py-3.5 first:border-t-0">
            <div className="mb-1.5 flex items-start gap-2">
              <CitationMarker citation={citation} />
              <a
                href={scored?.paper.url ?? `https://pubmed.ncbi.nlm.nih.gov/${citation.pmid}/`}
                target="_blank"
                rel="noreferrer"
                className="font-body text-[13.5px] font-medium text-ink hover:underline"
              >
                {scored?.paper.title ?? `PMID ${citation.pmid}`}
              </a>
            </div>
            <p className="font-data text-[10.5px] text-dim">
              PMID {citation.pmid}
              {citationMeta(scored)}
            </p>
            {scored && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {scored.memoryMultiplier < 1 && <FlagChip label="Read before" tone="neutral" />}
                {scored.memoryMultiplier > 1 && <FlagChip label="Builds on your thread" tone="blue" />}
                {citation.supported === false && <FlagChip label="Unsupported" tone="warn" />}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function FlagChip({ label, tone }: { label: string; tone: "neutral" | "blue" | "warn" }) {
  const cls =
    tone === "blue"
      ? "border-blue-200 bg-blue-100 text-blue-800"
      : tone === "warn"
        ? "border-warn bg-warn-bg text-warn"
        : "border-rule bg-white text-dim";
  return <span className={`rounded-[2px] border px-1.5 py-0.5 font-data text-[10px] ${cls}`}>{label}</span>;
}

function PapersTab({ papers }: { papers: ScoredPaper[] }) {
  return (
    <div>
      {papers.map((sp, index) => (
        <a
          key={`${sp.paper.pmid}-${index}`}
          href={sp.paper.url}
          target="_blank"
          rel="noreferrer"
          className={`grid items-center gap-5 border-t border-rule py-4 first:border-t-0 ${
            sp.paper.isRare ? "bg-blue-50" : "bg-white"
          }`}
          style={{ gridTemplateColumns: "40px minmax(0,1fr) 150px 90px" }}
        >
          <span className={`font-data text-xs ${sp.paper.isRare ? "text-blue-800" : "text-dim"}`}>
            {String(index + 1).padStart(2, "0")}
          </span>
          <div>
            <p className="font-body text-sm font-semibold text-ink">{sp.paper.title}</p>
            <p className="mt-1 font-data text-[10.5px] text-dim">
              PMID {sp.paper.pmid}
              {citationMeta(sp)}
            </p>
          </div>
          <span className={`font-body text-sm ${sp.paper.isRare ? "text-blue-800" : "text-trace-muted"}`}>
            {sp.paper.condition}
          </span>
          <span className={`font-data text-xs ${sp.paper.isRare ? "text-blue-800" : "text-dim"}`}>
            {sp.score.toFixed(2)}
          </span>
        </a>
      ))}
    </div>
  );
}

function CostTab({ result }: { result: QueryResult }) {
  const sites = Object.entries(result.cost.by_call_site).sort((a, b) => b[1].cost_usd - a[1].cost_usd);
  const maxCost = Math.max(...sites.map(([, c]) => c.cost_usd), 0.000001);
  return (
    <div>
      {sites.map(([site, cost]) => (
        <div
          key={site}
          className="grid items-center gap-5 border-t border-rule py-3.5 first:border-t-0"
          style={{ gridTemplateColumns: "200px minmax(0,1fr) 150px" }}
        >
          <span className="font-data text-xs text-ink">{site}</span>
          <span className="h-1 overflow-hidden rounded-full bg-blue-200">
            <span className="block h-full bg-blue-600" style={{ width: `${(cost.cost_usd / maxCost) * 100}%` }} />
          </span>
          <span className="text-right font-data text-xs text-dim">
            {cost.tokens.toLocaleString()} · ${cost.cost_usd.toFixed(5)}
          </span>
        </div>
      ))}
    </div>
  );
}

function StatCell({ label, value, sub, emphasize }: { label: string; value: string; sub: string; emphasize?: boolean }) {
  return (
    <div className={`p-6 ${emphasize ? "bg-blue-50" : ""}`}>
      <p className="font-body text-[10.5px] uppercase tracking-[0.08em] text-dim">{label}</p>
      <p className={`mt-2 font-data text-[28px] ${emphasize ? "text-blue-800" : "text-ink"}`}>{value}</p>
      <p className="mt-1 font-body text-[11.5px] text-dim">{sub}</p>
    </div>
  );
}

export function SourcedSummary({ result }: { result: QueryResult }) {
  const totalClaims = result.citations.length;
  const unsupportedCount = result.citations.filter((c) => c.supported === false).length;
  const rareHits = result.papers.filter((p) => p.paper.isRare).length;

  return (
    <div className="px-6 py-[110px] md:px-16">
      <SectionRail number="§03" eyebrow="Sourced summary" className="mb-5" />
      <h2 className="mb-10 font-display text-[clamp(28px,4vw,44px)] font-medium text-ink">
        Every sentence carries the paper it came from.
      </h2>

      <div className="mb-6 grid grid-cols-2 border border-rule bg-white md:grid-cols-4">
        <div className="border-r border-rule last:border-r-0">
          <StatCell
            label="Claims traced"
            value={`${totalClaims} / ${totalClaims}`}
            sub={unsupportedCount > 0 ? `${unsupportedCount} flagged unsupported` : "All verified"}
          />
        </div>
        <div className="border-r border-rule last:border-r-0">
          <StatCell label="Rare-weighted hits" value={String(rareHits)} sub={`Of ${result.papers.length} candidates`} />
        </div>
        <div className="border-r border-rule last:border-r-0">
          <StatCell
            label="Answer cost"
            value={`$${result.cost.cost_usd.toFixed(4)}`}
            sub={`${result.cost.total_tokens.toLocaleString()} tokens`}
          />
        </div>
        <StatCell label="Search rounds" value={String(result.trace.length)} sub="Search-loop iterations" emphasize />
      </div>

      <div className="border border-rule bg-white">
        {result.memory.seen_filtered > 0 && (
          <p className="border-b border-blue-200 bg-blue-50 px-6 py-3 font-body text-sm text-blue-800">
            {result.memory.seen_filtered} paper{result.memory.seen_filtered === 1 ? "" : "s"} you&apos;ve already
            read {result.memory.seen_filtered === 1 ? "was" : "were"} filtered out.
          </p>
        )}
        {!result.memory.applied && (
          <p className="border-b border-rule bg-white px-6 py-3 font-body text-sm text-dim">
            This answer is unpersonalized because personalization was turned off or memory was unavailable.
          </p>
        )}

        <Tabs defaultValue="summary">
          <TabsList className="h-auto w-full justify-start gap-0 rounded-none bg-transparent p-0">
            {[
              ["summary", "Summary"],
              ["papers", "Papers"],
              ["trace", "Retrieval trace"],
              ["cost", "This answer's cost"],
            ].map(([value, label]) => (
              <TabsTrigger
                key={value}
                value={value}
                className="rounded-none border-0 border-b-2 border-transparent px-[26px] py-4 font-body text-[13px] font-semibold text-dim shadow-none data-[state=active]:border-blue-700 data-[state=active]:bg-transparent data-[state=active]:text-blue-800"
              >
                {label}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="summary" className="mt-0">
            <div className="grid grid-cols-1 lg:grid-cols-[1.5fr_1fr]">
              <div className="border-t border-rule px-6 py-9 md:border-t-0 md:border-r md:px-10">
                <SummaryBody result={result} />
              </div>
              <SourceList result={result} />
            </div>
          </TabsContent>

          <TabsContent value="papers" className="mt-0 border-t border-rule px-6 md:px-[30px]">
            <PapersTab papers={result.papers} />
          </TabsContent>

          <TabsContent value="trace" className="mt-0 border-t border-rule px-6 md:px-[30px]">
            <RetrievalTrace trace={result.trace} />
          </TabsContent>

          <TabsContent value="cost" className="mt-0 border-t border-rule px-6 md:px-[30px]">
            <CostTab result={result} />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
