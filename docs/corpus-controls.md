# Retrieval access, provenance and answer checks

## Access filtering

`AccessPolicy` assigns each PMID a visibility label. Unlisted records default to
`public` because the shipped fixture is public PubMed text. A governed deployment
must label every non-public record. A scope may read a record when its label is
public or belongs to the scope's groups. Unknown labels are denied to everyone.

`AccessFilteredRetrieval` pushes denied PMIDs into `exclude_pmids` and filters
returned rows again, preserving enforcement even if an adapter ignores that hint.
The same rule applies to search and direct lookup before summary construction,
citation checks or provenance export. Filtering is read-only and idempotent.
Condition aggregates are withheld whenever the scope cannot read every labelled
record because the aggregate has no per-source provenance for recomputation.

An `AccessScope` is caller-supplied context, not a verified identity. The project
has no authenticated binding for these scopes. Private-corpus deployment requires
that binding before this mechanism can establish authorization.

## Source versions and stored citations

The content hash is `provenance.record_hash` over `(pmid, title, abstract)`, using
canonical JSON and SHA-256 hex. It identifies the text used for lexical grounding;
it does not authenticate the publisher or establish semantic equivalence.

`revalidate_citations` reads the current record through the caller's retrieval
port, which must already enforce retention and access. It never relies on the
stored answer's copy. Each citation receives one state:

| State | Meaning |
| --- | --- |
| `current` | Same content hash as when the answer was built. |
| `changed` | Record is readable, but its text changed and the claim needs rechecking. |
| `revoked` | The port no longer serves the record, including deletion or access loss. |
| `redacted` | The port serves a redaction placeholder. |

An answer can be served unchanged only when every citation is current. See
[retention.md](retention.md) for deletion precedence and derived-store behavior.

## Lexical grounding

The optional LLM citation judge evaluates claims that carry citation markers.
The deterministic grounding check also identifies assertions with no marker and
markers referring to unavailable source slots, so those failures cannot disappear
from coverage accounting. It operates locally even when the judge is unavailable.

A claim must share enough content words with a cited abstract and every number
in the claim must occur there. Stopwords do not count toward overlap. The numeric
check prevents high overall word overlap from hiding a changed patient count or
other number. Threshold selection and held-out scores belong to the original
`backend/measurement/run_grounding_eval.py` protocol; wording changes do not
create a new measurement.

| Verdict | Meaning |
| --- | --- |
| `grounded` | At least one cited abstract passes the lexical checks. |
| `unsupported` | A citation exists but its abstract fails the checks. |
| `uncited` | An assertion has no citation marker. |
| `dangling` | A citation marker points outside the retrieved set. |

Lexical containment does not establish entailment or clinical correctness. The
semantic judge remains a separate opinion, and fake-profile judge results are
not independent external validation.

## Instruction screening

A malicious abstract can contain the exact words of an instruction, allowing a
copied payload to pass lexical grounding. The output-side injection screen checks
summary claims for known operator-directed instructions, credential requests,
host paths and shell/SQL payload patterns. If any claim matches, the pipeline
serves its fixed abstention text instead of that summary.

This pattern screen is not a general semantic injection detector. False refusals
and missed attacks require valid-text and hostile-text controls. Current local
studies use the finite public corpus and retained fixtures; they do not establish
held-out researcher utility or production protection.
