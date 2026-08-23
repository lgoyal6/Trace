# NeuLitTrace - 80-second demo voiceover

**Video:** `neulittrace-demo.mp4` · 1440×900 · 79.9s
**Recorder:** `frontend/tests/record-demo.mjs`

Plain narration, ~2.5 words/second. Every number spoken is on screen at that
moment.

---

## Script

**0:00 – 0:07 · Hero**
> NeuLitTrace searches published case reports for rare neuroimaging findings.
> Every claim it writes carries a citation.

**0:07 – 0:13 · Corpus**
> The corpus is 329 papers across 14 conditions. Ten of the fourteen are rare.

**0:13 – 0:22 · Query**
> You type a finding in plain language. The system expands the query, retrieves
> papers, checks relevance, writes a summary, and verifies each citation.

**0:22 – 0:33 · Tight panel**
> This is the current setting. Ten papers go into the prompt, four sentences
> from each. That is 1,984 tokens. Rare conditions have as few as four papers in
> the corpus, so they often miss the top ten.

**0:33 – 0:47 · Generous panel**
> Same query, one setting changed. Thirty papers instead of ten, one sentence
> from each. That is 1,286 tokens. Three times the papers, 35 percent fewer
> tokens. On our 28-query test set, rare-condition recall improves 81 percent.

**0:47 – 0:55 · Summary**
> Here is the summary. Four claims, four citations. Each one was checked against
> the abstract it cites before display. All four passed.

**0:55 – 1:01 · Papers**
> These are the retrieved papers. 29 of 30 were rare-weighted. Real PubMed IDs,
> linked to source.

**1:01 – 1:06 · Cost**
> This answer cost 1.4 cents and 7,093 tokens, broken down by pipeline step and
> priced against Snowflake's published Cortex rates.

**1:06 – 1:15 · Memory**
> Memory runs on EverOS. It stores the researcher's specialty, the conditions
> they've searched, and the papers they've read, then reorders results to favor
> what they haven't seen.

**1:15 – 1:20 · Close**
> Wider retrieval, same token budget, every claim sourced.

---

## Short version (20 seconds)

Play **0:33–0:47** only:

> Same query, one setting changed. Thirty papers instead of ten, at 35 percent
> fewer tokens. Rare-condition recall improves 81 percent.

---

## Number sources

| Spoken | Where it comes from |
|---|---|
| 329 papers, 14 conditions | `backend/data/corpus.json` |
| 10 papers, 1,984 tokens | live request, Tight - on screen |
| 30 papers, 1,286 tokens | live request, Generous - on screen |
| 35% fewer tokens | 1,286 vs 1,984 |
| 81% recall improvement | 0.4118 → 0.7475, 28-query gold set - `results/policy_bench.md` |
| 4 of 4 claims verified | on screen, §03 |
| 29 of 30 rare-weighted | on screen, §03 |
| 1.4 cents, 7,093 tokens | on screen, §03 |

---

## Two things to know before you record

**1. Say "on average" for the 81% figure.** The −35% token result is what this
one request did. Across all 28 test queries the average is −0.7%, and on 14 of
them Generous costs slightly more, because short abstracts skip compression.
The accurate claim is *"three times the papers for the same token budget on
average."* Do not say "always cheaper."

**2. If asked what wrote the summary.** This recording runs without Snowflake
credentials. On that path the summary is produced by a deterministic extractor
in `backend/contracts/fakes.py`: it takes the abstracts that were actually
retrieved, picks the sentence from each with the most query-word overlap, and
attaches that paper's real citation marker. The sentences are verbatim from real
abstracts and the PMIDs are real. It is extraction, not a language model. With
credentials configured this call goes to Snowflake Cortex instead.

Answer: *"Offline it's an extractive stand-in over the real retrieved abstracts.
With our Snowflake account connected, it's Cortex."*

**Not in the video:** the `/cost` dashboard, because it reads Snowflake views and
is empty without an account. Per-request cost is shown instead.

---

## Re-recording

```bash
NEULIT_PROFILE=fake .venv/bin/python -m uvicorn backend.api.main:app --port 8000 &
cd frontend && npm run build && npm run start &
node tests/record-demo.mjs          # prints a beat sheet
ffmpeg -y -i <webm> -vf "fps=30,scale=1440:900:flags=lanczos" \
  -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -movflags +faststart demo.mp4
```
