from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    session_id: str
    user_id: str
    personalize: bool = False

    # Retrieval policy by label ("tight" | "generous"), per
    # backend/app/retrieval/policy.py. Omitted/null keeps today's exact
    # behaviour (RETRIEVAL_TOP_K papers, no compression), so every existing
    # caller is unaffected. An unknown label is a 422, never a silent
    # fallback -- a demo that quietly runs the wrong arm is worse than one
    # that errors.
    policy: str | None = None

    @field_validator("policy")
    @classmethod
    def _known_policy(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from backend.app.retrieval.policy import policy_for_label

        policy_for_label(v)  # raises ValueError -> 422
        return v


class PaperOut(CamelModel):
    pmid: str
    title: str
    abstract: str
    journal: str
    year: int
    condition: str
    is_rare: bool
    url: str


class ScoredPaperOut(CamelModel):
    paper: PaperOut
    score: float
    lexical_score: float
    semantic_score: float
    rarity_multiplier: float
    memory_multiplier: float


class TraceRoundOut(BaseModel):
    iteration: int
    retrieved_pmids: list[str]
    relevant: bool
    confidence: float
    note: str
    memory_applied: bool
    seen_filtered: int


class CitationOut(BaseModel):
    """`index` is the [N] slot in the prompt the summary was written against.
    `retrieval_rank` and `source` are the retrieval provenance: the position
    the record held in the ordering the answer was built from, and the backend
    that served it. Both are carried through rather than re-derived, so a
    stored answer can still name its sources after the prompt is gone.
    """

    index: int
    pmid: str
    supported: bool | None
    note: str | None
    source: str = ""
    retrieval_rank: int = 0


class RecordProvenanceOut(BaseModel):
    pmid: str
    source: str
    retrieval_rank: int
    score: float


class ClaimVerdictOut(BaseModel):
    position: int
    text: str
    cited_indices: list[int]
    cited_pmids: list[str]
    verdict: str        # grounded | unsupported | uncited | dangling
    best_overlap: float
    numeric_conflict: bool


class GroundingOut(BaseModel):
    """Per-claim grounding for this answer.

    `uncited` counts assertions that carry no [N] at all -- the ones the
    citation list structurally cannot contain, because it is built by
    enumerating the markers that are present.
    """

    total_claims: int
    grounded: int
    unsupported: int
    uncited: int
    dangling: int
    skipped_fragments: int
    records_available: int
    grounded_rate: float
    answerable: bool
    threshold: float
    claims: list[ClaimVerdictOut]


class BrainRegionOut(BaseModel):
    name: str
    atlas_label: str
    region_literature: str


class MemoryOut(BaseModel):
    applied: bool
    seen_filtered: int
    profile_used: bool
    distilled_context: str


class CallSiteCostOut(BaseModel):
    tokens: int
    cost_usd: float


class CostOut(BaseModel):
    total_tokens: int
    cost_usd: float
    by_call_site: dict[str, CallSiteCostOut]


class PolicyOut(CamelModel):
    """Which retrieval policy ran and what it cost the prompt. Null when the
    request didn't ask for one (today's default path)."""

    label: str
    top_k: int
    compress_top_n: int
    papers_in_prompt: int
    prompt_tokens_before_compression: int
    prompt_tokens_after_compression: int
    tokens_saved: int
    reduction_pct: float


class QueryResponse(BaseModel):
    request_id: str
    summary_markdown: str
    citations: list[CitationOut]
    papers: list[ScoredPaperOut]
    trace: list[TraceRoundOut]
    region: BrainRegionOut | None
    memory: MemoryOut
    cost: CostOut
    policy: PolicyOut | None = None
    grounding: GroundingOut | None = None
    retrieval_provenance: list[RecordProvenanceOut] = []
    #: True when nothing retrieved supported an answer, so `summary_markdown`
    #: is the fixed abstention line instead of a summary.
    abstained: bool = False


class ContrastPaperOut(BaseModel):
    pmid: str
    title: str
    condition: str
    rarity: str


class DemoContrastResponse(BaseModel):
    query: str
    naive: list[ContrastPaperOut]
    weighted: list[ContrastPaperOut]
    rare_case_pmid: str


class ConditionOut(BaseModel):
    """Consumed by backend/api/routes/conditions.py (Card 1). Keep in sync
    with backend.app.corpus.conditions.Condition if that shape ever changes.
    """

    name: str
    rarity: str
    region_literature: str
    atlas_label: str
    overlaps_with: list[str]


class ProfileOut(BaseModel):
    user_id: str
    specialty: str | None
    conditions_explored: list[str]
    query_count: int
    distilled_context: str
    seen_pmid_count: int


class SpecialtyRequest(BaseModel):
    user_id: str
    specialty: str


class ForgetRequest(BaseModel):
    user_id: str


class ThreadOut(BaseModel):
    session_id: str
    user_id: str
    queries: list[str]
    pmids_shown: list[str]
