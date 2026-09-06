"""Deletion and redaction of stored documents, and an honest account of what
"deleted" does and does not mean.

Measured before this module existed: there was no per-document deletion or
redaction path anywhere in `backend/` (grep for tombstone|redact|def delete_|
purge returned nothing), and removing a record from `backend/data/corpus.json`
did not remove it from a running process - `FakeRetrieval` reads the corpus
through a `functools.lru_cache(maxsize=1)`, so `get_by_pmids` still returned
the record after it was gone from disk.

--- THE FOUR PLACES A RECORD LIVES -----------------------------------------

1. SOURCE OF RECORD   `backend/data/corpus.json`, and `NEULIT.CORE.PAPERS`.
2. INDEX              the Cortex Search Service `NEULIT.CORE.PAPERS_SEARCH`,
                      which is derived from PAPERS with `TARGET_LAG = '1 hour'`.
3. CACHE              `backend.contracts.fakes._load_corpus`'s lru_cache (an
                      in-process copy of the whole corpus), and the tombstone
                      log's own mtime cache in this module.
4. EXPORT             `corpus.json` again (it is both the store and the
                      shipped fixture), and every API response that carries
                      paper text: `POST /query`, `POST /query/stream`,
                      `GET /demo-contrast`. The four JSON files under
                      `backend/measurement/results/` were checked and contain
                      no pmids or paper text, so they are not an export
                      surface for this.

`delete_record` writes a tombstone, then walks all four.

--- THE PART THAT CANNOT BE INSTANT, AND IS NOT CLAIMED TO BE --------------

The index is a materialisation with a stated one-hour lag. Purging PAPERS
does not purge PAPERS_SEARCH; the service catches up on its own schedule, or
sooner if an operator re-runs `snowflake/sql/03_search_service.sql`. So
between the tombstone and the refresh there is a real window in which the
index still holds the record.

`RetentionFilteredRetrieval` is what closes that window at read time: it wraps
any `RetrievalPort` and drops tombstoned pmids out of `search()`,
`get_by_pmids()` and the condition counts, so a record that is still in the
index is still never served. That is a read-path guarantee on top of an
eventually-consistent purge, and it is stated that way rather than as
"deletion is instant".

--- BACKUP RETENTION: STATED SEPARATELY, BECAUSE IT IS DIFFERENT -----------

Purging a live store does not reach a backup, and no code in this repo can
make it. Snowflake keeps deleted rows recoverable through Time Travel
(`DATA_RETENTION_TIME_IN_DAYS`, this account's setting is not readable from
here) and then through Fail-safe, which is a fixed 7 days on permanent tables
and is NOT operator-controllable - a Snowflake account cannot shorten it, and
cannot query or purge it. Git history holds every past version of
`corpus.json` and a deletion commit does not remove the earlier blobs.

So the truthful statement, and the one `DeletionReceipt.summary()` prints, is:

    the record stops being served immediately, is purged from the live stores
    on this run, leaves the derived index within its refresh lag, and remains
    recoverable from backups until each backup's own retention expires.

`BACKUP_STORES` below is the declared inventory. It is a *declaration*, not a
measurement: the Snowflake retention values are this repo's stated intent and
must be reconciled against the account's real `DATA_RETENTION_TIME_IN_DAYS`
before anyone quotes them to a data subject. That reconciliation needs
credentials this environment does not have, and it is listed as BLOCKED
rather than guessed.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DELETE = "delete"
REDACT = "redact"
MODES = (DELETE, REDACT)

#: What a redacted record's document text is replaced with. Kept short and
#: unmistakable: a redacted abstract must not read as a real abstract, and it
#: must not tokenise into anything a retriever would match.
REDACTION_PLACEHOLDER = "[redacted]"

DEFAULT_TOMBSTONE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "tombstones.json"
)

TOMBSTONE_PATH_ENV = "NEULIT_TOMBSTONE_LOG"


@dataclass(frozen=True)
class BackupStore:
    """One place a purged record can still exist, and for how long."""

    name: str
    mechanism: str
    retention: str
    operator_controllable: bool
    purgeable_by_this_code: bool


#: Declared, not measured. See the module docstring and BLOCKED in the record.
BACKUP_STORES: tuple[BackupStore, ...] = (
    BackupStore(
        name="NEULIT.CORE.PAPERS Time Travel",
        mechanism="Snowflake Time Travel (DATA_RETENTION_TIME_IN_DAYS)",
        retention="account/table setting, not readable without credentials",
        operator_controllable=True,
        purgeable_by_this_code=False,
    ),
    BackupStore(
        name="NEULIT.CORE.PAPERS Fail-safe",
        mechanism="Snowflake Fail-safe on a permanent table",
        retention="7 days after Time Travel expires, fixed by Snowflake",
        operator_controllable=False,
        purgeable_by_this_code=False,
    ),
    BackupStore(
        name="backend/data/corpus.json in git history",
        mechanism="git object store",
        retention="indefinite until the history is rewritten",
        operator_controllable=True,
        purgeable_by_this_code=False,
    ),
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass(frozen=True)
class Tombstone:
    pmid: str
    mode: str
    reason: str
    requested_at_iso: str

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not str(self.pmid).strip():
            raise ValueError("tombstone needs a pmid")


class TombstoneLog:
    """Append-only record of deletion and redaction requests.

    Append-only on purpose. The log is the evidence that a deletion was asked
    for and honoured; a log you can quietly remove entries from proves
    nothing. Reinstating a record is a new decision that would need its own
    entry, and this module deliberately offers no "untombstone" call.
    """

    def __init__(self, entries: Sequence[Tombstone] = ()) -> None:
        self._entries: list[Tombstone] = list(entries)

    # -- persistence ----------------------------------------------------
    @classmethod
    def load(cls, path: Path | str | None = None) -> "TombstoneLog":
        path = Path(path or default_path())
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls([Tombstone(**e) for e in data.get("tombstones", [])])

    def save(self, path: Path | str | None = None) -> Path:
        path = Path(path or default_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"tombstones": [asdict(e) for e in self._entries]},
                indent=2,
                sort_keys=True,
            )
        )
        return path

    # -- writing --------------------------------------------------------
    def add(self, pmid: str, mode: str, reason: str, *, at_iso: str | None = None) -> Tombstone:
        entry = Tombstone(
            pmid=str(pmid), mode=mode, reason=reason, requested_at_iso=at_iso or _now_iso()
        )
        self._entries.append(entry)
        return entry

    # -- reading --------------------------------------------------------
    @property
    def entries(self) -> list[Tombstone]:
        return list(self._entries)

    def mode_for(self, pmid: str) -> str | None:
        """The effective mode for a pmid.

        `delete` wins over `redact` regardless of order: once someone has
        asked for the record to be gone, a later or earlier redaction request
        cannot downgrade that to "keep it but blank the text".
        """
        modes = {e.mode for e in self._entries if e.pmid == str(pmid)}
        if DELETE in modes:
            return DELETE
        if REDACT in modes:
            return REDACT
        return None

    def deleted_pmids(self) -> set[str]:
        return {e.pmid for e in self._entries if self.mode_for(e.pmid) == DELETE}

    def redacted_pmids(self) -> set[str]:
        return {e.pmid for e in self._entries if self.mode_for(e.pmid) == REDACT}

    def __len__(self) -> int:
        return len(self._entries)

    def __bool__(self) -> bool:
        return bool(self._entries)


def default_path() -> Path:
    return Path(os.environ.get(TOMBSTONE_PATH_ENV) or DEFAULT_TOMBSTONE_PATH)


# --- the log's own cache ----------------------------------------------------
#
# Re-reading a JSON file on every retrieval call would be silly, and caching it
# forever would reintroduce exactly the staleness this module exists to fix
# (a deletion that a running process never notices). Keyed on (path, mtime_ns,
# size), so writing a tombstone takes effect on the next call without a
# restart, and `invalidate()` forces a re-read for a filesystem too coarse to
# show the change.
_log_cache: dict[tuple[str, int, int], TombstoneLog] = {}


def active_log(path: Path | str | None = None) -> TombstoneLog:
    path = Path(path or default_path())
    try:
        stat = path.stat()
    except OSError:
        return TombstoneLog()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _log_cache.get(key)
    if cached is None:
        cached = TombstoneLog.load(path)
        _log_cache.clear()
        _log_cache[key] = cached
    return cached


def invalidate_caches() -> list[str]:
    """Drop every in-process copy of corpus text. Returns what was cleared."""
    cleared = []
    _log_cache.clear()
    cleared.append("retention tombstone-log cache")
    try:
        from backend.contracts.fakes import _load_corpus

        _load_corpus.cache_clear()
        cleared.append("contracts.fakes._load_corpus lru_cache")
    except Exception:  # pragma: no cover - fakes always import in this repo
        pass
    try:
        from backend.app.llm.cache import get_default_cache

        get_default_cache().clear()
        cleared.append("llm TTLPromptCache")
    except Exception:  # pragma: no cover
        pass
    return cleared


# --- applying a tombstone to data ------------------------------------------


def redact_record(record: Mapping[str, Any]) -> dict:
    """A copy with the document-derived text replaced.

    Only `title` and `abstract` are blanked: they are what came out of the
    fetched document. `condition`/`rarity`/`atlas_label` are local curation,
    and a redacted record still has to be countable (a condition's paper count
    must not silently change because one abstract was redacted).
    """
    out = dict(record)
    for field_name in ("title", "abstract"):
        if field_name in out:
            out[field_name] = REDACTION_PLACEHOLDER
    out["redacted"] = True
    return out


def apply_to_records(records: Iterable[Mapping[str, Any]], log: TombstoneLog) -> list[dict]:
    """Deleted records dropped, redacted records blanked, order preserved."""
    deleted = log.deleted_pmids()
    redacted = log.redacted_pmids()
    out: list[dict] = []
    for record in records:
        pmid = str(record.get("pmid", ""))
        if pmid in deleted:
            continue
        out.append(redact_record(record) if pmid in redacted else dict(record))
    return out


def purge_corpus_file(
    corpus_path: Path | str, log: TombstoneLog, *, dry_run: bool = False
) -> dict:
    """Rewrite the source-of-record JSON with tombstones applied.

    corpus.json is both the store and the shipped export, so this one call
    covers store and export for the local profile.
    """
    corpus_path = Path(corpus_path)
    records = json.loads(corpus_path.read_text())
    kept = apply_to_records(records, log)
    result = {
        "path": str(corpus_path),
        "before": len(records),
        "after": len(kept),
        "removed": len(records) - len(kept),
        "redacted": sum(1 for r in kept if r.get("redacted")),
        "dry_run": dry_run,
    }
    if not dry_run:
        corpus_path.write_text(json.dumps(kept, indent=2))
    return result


def snowflake_purge_statements(log: TombstoneLog) -> list[tuple[str, list]]:
    """(sql, params) pairs an operator with credentials must run.

    Generated rather than executed: no Snowflake account is reachable from
    here (see BLOCKED). Every pmid is a bound parameter, matching the
    binding rule the corpus loader already follows - a pmid arrives off the
    network and is never interpolated into a statement.
    """
    statements: list[tuple[str, list]] = []
    deleted = sorted(log.deleted_pmids())
    redacted = sorted(log.redacted_pmids())
    if deleted:
        placeholders = ",".join("?" for _ in deleted)
        statements.append(
            (f"DELETE FROM NEULIT.CORE.PAPERS WHERE PMID IN ({placeholders})", list(deleted))
        )
    if redacted:
        placeholders = ",".join("?" for _ in redacted)
        statements.append(
            (
                "UPDATE NEULIT.CORE.PAPERS SET TITLE = ?, ABSTRACT = ?, SEARCH_BLOB = ? "
                f"WHERE PMID IN ({placeholders})",
                [REDACTION_PLACEHOLDER, REDACTION_PLACEHOLDER, REDACTION_PLACEHOLDER, *redacted],
            )
        )
    if statements:
        # PAPERS_SEARCH is a materialisation of PAPERS with TARGET_LAG='1 hour'.
        # Purging the base table is not purging the index; this forces the
        # rebuild instead of waiting out the lag.
        statements.append((_SEARCH_SERVICE_REFRESH, []))
    return statements


_SEARCH_SERVICE_REFRESH = (
    "ALTER CORTEX SEARCH SERVICE NEULIT.CORE.PAPERS_SEARCH REFRESH"
)


# --- read-path enforcement --------------------------------------------------


class RetentionFilteredRetrieval:
    """A `RetrievalPort` that never serves a tombstoned record.

    Wraps any port, so it applies identically to `FakeRetrieval` and to
    `CortexSearchRetriever`. This is the layer that holds while the derived
    index catches up: the record can still be in PAPERS_SEARCH and it still
    will not reach a caller.

    Deleted pmids are also passed down as `exclude_pmids` so the underlying
    retriever does not spend a result slot on a record that is about to be
    dropped - filtering only after the fact would silently shrink top_k.
    """

    def __init__(self, inner, log: TombstoneLog | None = None) -> None:
        self._inner = inner
        self._log = log

    def _active(self) -> TombstoneLog:
        return self._log if self._log is not None else active_log()

    def search(
        self,
        query: str,
        *,
        secondary_query: str | None = None,
        top_k: int = 10,
        apply_rarity: bool = True,
        exclude_pmids: Sequence[str] = (),
    ):
        log = self._active()
        deleted = log.deleted_pmids()
        redacted = log.redacted_pmids()
        results = self._inner.search(
            query,
            secondary_query=secondary_query,
            top_k=top_k,
            apply_rarity=apply_rarity,
            exclude_pmids=tuple({*exclude_pmids, *deleted}),
        )
        out = []
        for sp in results:
            if sp.paper.pmid in deleted:
                continue
            if sp.paper.pmid in redacted:
                sp = replace(
                    sp,
                    paper=replace(
                        sp.paper, title=REDACTION_PLACEHOLDER, abstract=REDACTION_PLACEHOLDER
                    ),
                )
            out.append(sp)
        return out

    def get_by_pmids(self, pmids: Sequence[str]):
        log = self._active()
        deleted = log.deleted_pmids()
        redacted = log.redacted_pmids()
        papers = self._inner.get_by_pmids([p for p in pmids if p not in deleted])
        out = []
        for paper in papers:
            if paper.pmid in deleted:
                continue
            if paper.pmid in redacted:
                paper = replace(
                    paper, title=REDACTION_PLACEHOLDER, abstract=REDACTION_PLACEHOLDER
                )
            out.append(paper)
        return out

    def closest_conditions(self, query: str, top_n: int = 3):
        return self._inner.closest_conditions(query, top_n=top_n)

    def health(self) -> dict:
        health = dict(self._inner.health())
        log = self._active()
        health["detail"] = (
            f"{health.get('detail', '')} | retention: "
            f"{len(log.deleted_pmids())} deleted, {len(log.redacted_pmids())} redacted"
        )
        return health


def enforced(port, log: TombstoneLog | None = None):
    """Wrap `port` only if there is anything to enforce.

    An empty tombstone log returns the port unchanged, so on the overwhelmingly
    common path (no deletions requested) there is no wrapper, no extra
    allocation, and nothing to go wrong.
    """
    active = log if log is not None else active_log()
    if not active:
        return port
    return RetentionFilteredRetrieval(port, active)


# --- the receipt ------------------------------------------------------------


@dataclass
class StoreOutcome:
    store: str
    status: str  # "purged" | "pending" | "read-path-filtered" | "retained"
    detail: str


@dataclass
class DeletionReceipt:
    pmid: str
    mode: str
    reason: str
    requested_at_iso: str
    outcomes: list[StoreOutcome] = field(default_factory=list)
    backups: list[BackupStore] = field(default_factory=lambda: list(BACKUP_STORES))

    def summary(self) -> str:
        lines = [
            f"{self.mode} {self.pmid} ({self.reason}) requested {self.requested_at_iso}",
            "live stores:",
        ]
        lines += [f"  [{o.status}] {o.store}: {o.detail}" for o in self.outcomes]
        lines.append("backups (NOT purged by this operation):")
        lines += [
            f"  [retained] {b.name}: {b.mechanism}, retention {b.retention}"
            for b in self.backups
        ]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "pmid": self.pmid,
            "mode": self.mode,
            "reason": self.reason,
            "requested_at_iso": self.requested_at_iso,
            "outcomes": [asdict(o) for o in self.outcomes],
            "backups": [asdict(b) for b in self.backups],
        }


def delete_record(
    pmid: str,
    reason: str,
    *,
    mode: str = DELETE,
    corpus_path: Path | str | None = None,
    tombstone_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
    dry_run: bool = False,
) -> DeletionReceipt:
    """Tombstone one record and walk every live store it reaches.

    Returns a receipt that says, per store, whether the record is gone now,
    gone once an operator runs the generated SQL, held out at read time while
    a derived index catches up, or retained in a backup until that backup's
    own retention expires. The last category is why this returns a receipt
    instead of a bool.
    """
    from backend.app.corpus.build_corpus import DEFAULT_OUT_PATH
    from backend.app.corpus.provenance import DEFAULT_MANIFEST_PATH, Manifest

    corpus_path = Path(corpus_path or DEFAULT_OUT_PATH)
    tombstone_path = Path(tombstone_path or default_path())
    manifest_path = Path(manifest_path or DEFAULT_MANIFEST_PATH)

    log = TombstoneLog.load(tombstone_path)
    entry = log.add(pmid, mode, reason)
    receipt = DeletionReceipt(
        pmid=entry.pmid, mode=entry.mode, reason=entry.reason,
        requested_at_iso=entry.requested_at_iso,
    )

    if not dry_run:
        log.save(tombstone_path)
    receipt.outcomes.append(
        StoreOutcome(
            "tombstone log",
            "purged" if not dry_run else "pending",
            f"{tombstone_path} now holds {len(log)} entr{'y' if len(log) == 1 else 'ies'}",
        )
    )

    corpus_result = purge_corpus_file(corpus_path, log, dry_run=dry_run)
    receipt.outcomes.append(
        StoreOutcome(
            "backend/data/corpus.json (store + export)",
            "pending" if dry_run else "purged",
            f"{corpus_result['before']} -> {corpus_result['after']} records, "
            f"{corpus_result['redacted']} redacted",
        )
    )

    manifest = Manifest.load(manifest_path)
    had_provenance = manifest.forget(entry.pmid) if entry.mode == DELETE else False
    if had_provenance and not dry_run:
        manifest.save(manifest_path)
    receipt.outcomes.append(
        StoreOutcome(
            "provenance manifest",
            "purged" if had_provenance else "pending",
            "provenance row removed" if had_provenance
            else "no provenance row for this pmid (corpus predates the manifest)",
        )
    )

    cleared = invalidate_caches()
    receipt.outcomes.append(
        StoreOutcome("in-process caches", "purged", ", ".join(cleared))
    )

    statements = snowflake_purge_statements(log)
    receipt.outcomes.append(
        StoreOutcome(
            "NEULIT.CORE.PAPERS + PAPERS_SEARCH",
            "pending",
            f"{len(statements)} statement(s) generated, not executed: no Snowflake "
            "credentials reachable from here",
        )
    )
    receipt.outcomes.append(
        StoreOutcome(
            "read path (every RetrievalPort caller)",
            "read-path-filtered",
            "RetentionFilteredRetrieval drops this pmid immediately, including "
            "while PAPERS_SEARCH is still stale within its 1 hour TARGET_LAG",
        )
    )
    return receipt
