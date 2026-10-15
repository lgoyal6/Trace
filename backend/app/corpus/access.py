"""Filter retrieval reads using caller-supplied access labels and groups.

Identity authentication remains the caller's responsibility.
See docs/corpus-controls.md for denial and aggregate behavior.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

PUBLIC = "public"
ACCESS_POLICY_ENV = "NEULIT_ACCESS_POLICY"


@dataclass(frozen=True)
class AccessScope:
    """Who is asking and which access groups they hold."""

    principal: str
    groups: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def anonymous(cls) -> "AccessScope":
        return cls(principal="anonymous", groups=frozenset())


@dataclass(frozen=True)
class AccessPolicy:
    """pmid -> access label, plus the set of labels the policy recognises."""

    labels: Mapping[str, str] = field(default_factory=dict)
    known_labels: frozenset[str] = frozenset({PUBLIC})

    def __post_init__(self) -> None:
        object.__setattr__(self, "known_labels", frozenset({PUBLIC, *self.known_labels}))

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "AccessPolicy":
        labels = {str(k): str(v) for k, v in dict(data.get("labels", {})).items()}
        known = frozenset(str(v) for v in data.get("known_labels", [])) or frozenset(labels.values())
        return cls(labels=labels, known_labels=known)

    @classmethod
    def load(cls, path: Path | str) -> "AccessPolicy":
        return cls.from_mapping(json.loads(Path(path).read_text()))

    def label_for(self, pmid: str) -> str:
        return self.labels.get(str(pmid), PUBLIC)

    def allows(self, scope: AccessScope, pmid: str) -> bool:
        label = self.label_for(pmid)
        if label not in self.known_labels:
            return False
        return label == PUBLIC or label in scope.groups

    def denied_pmids(self, scope: AccessScope) -> set[str]:
        return {pmid for pmid in self.labels if not self.allows(scope, pmid)}

    def __bool__(self) -> bool:
        return bool(self.labels)


def policy_from_env() -> AccessPolicy:
    """The policy named by NEULIT_ACCESS_POLICY, or an empty (all-public) one."""
    path = os.environ.get(ACCESS_POLICY_ENV)
    if not path:
        return AccessPolicy()
    return AccessPolicy.load(path)


class AccessFilteredRetrieval:
    """A RetrievalPort that only ever serves records `scope` may read.

    `_inner` is the attribute `grounding.source_name` unwraps, so provenance
    still names the backend that did the search, not this filter.
    """

    def __init__(self, inner, policy: AccessPolicy, scope: AccessScope) -> None:
        self._inner = inner
        self._policy = policy
        self._scope = scope
        self.denied_served = 0  # results the inner port returned that were dropped here

    def search(
        self,
        query: str,
        *,
        secondary_query: str | None = None,
        top_k: int = 10,
        apply_rarity: bool = True,
        exclude_pmids: Sequence[str] = (),
    ):
        denied = self._policy.denied_pmids(self._scope)
        results = self._inner.search(
            query,
            secondary_query=secondary_query,
            top_k=top_k,
            apply_rarity=apply_rarity,
            exclude_pmids=tuple({*exclude_pmids, *denied}),
        )
        out = []
        for sp in results:
            if not self._policy.allows(self._scope, sp.paper.pmid):
                self.denied_served += 1
                continue
            out.append(sp)
        return out

    def get_by_pmids(self, pmids: Sequence[str]):
        allowed = [p for p in pmids if self._policy.allows(self._scope, p)]
        return [p for p in self._inner.get_by_pmids(allowed)
                if self._policy.allows(self._scope, p.pmid)]

    def closest_conditions(self, query: str, top_n: int = 3):
        if self._policy.denied_pmids(self._scope):
            return []
        return self._inner.closest_conditions(query, top_n=top_n)

    def health(self) -> dict:
        health = dict(self._inner.health())
        health["detail"] = (
            f"{health.get('detail', '')} | access: {len(self._policy.labels)} labelled, "
            f"{len(self._policy.denied_pmids(self._scope))} denied to {self._scope.principal}"
        )
        return health


def access_enforced(port, policy: AccessPolicy | None, scope: AccessScope | None):
    """Wrap `port` only when there is a restriction to enforce.

    No policy (the default) returns the port unchanged, so existing profiles
    behave exactly as before. With a policy but no scope, the caller is
    anonymous and sees only public records.
    """
    if not policy:
        return port
    return AccessFilteredRetrieval(port, policy, scope or AccessScope.anonymous())


def restricted_in(pmids: Iterable[str], policy: AccessPolicy, scope: AccessScope) -> list[str]:
    """The pmids in `pmids` that `scope` may not read. Used by audits/tests."""
    return [p for p in pmids if not policy.allows(scope, p)]
