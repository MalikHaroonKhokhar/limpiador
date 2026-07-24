"""Working memory and compaction (ARCHITECTURE.md §7, property #3).

The context object distinguishes durable state (the task, the plan, resolved
sub-goals, symbol-level findings) from transient payloads (the full text a tool
returned). Durable state always survives; transient payloads are summarized and
evicted when they are no longer needed for the immediate next step, so a
twenty-plus-call investigation stays roughly flat in size instead of growing
quadratically.

Compaction is threshold-triggered, not per-call, and the threshold and eviction
policy are named configuration (CLEAN_CODE.md §7) — not magic numbers buried in
the loop. See ARCH_DEBT_002 in .clauderules for the known lossy-summary edge.

The eviction policy is written out so a reviewer can read *how* limpiador decides
what to drop: when the estimated footprint crosses the threshold, every raw
payload except the most-recent one (the immediate next step) and any the caller
pinned is replaced by a compact summary. Durable state — and symbol facts in
particular — is never touched, which is the mitigation that lets an import edge
noticed on call 4 still be available on call 22 without re-reading the file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from limpiador.observability.tracing import COMPACTION_TAG, CONTEXT_REREAD_TAG, emit

# Named configuration (CLEAN_CODE.md §7), not magic numbers in the loop. The
# threshold is the footprint at which compaction triggers; the chars-per-token
# ratio is the rough estimator behind ``estimate_tokens`` — a heuristic for
# *when* to compact, never an exact accounting (real accounting lives in the
# adapter's TokenUsage). Both are tunable in one place.
DEFAULT_COMPACTION_THRESHOLD_TOKENS = 8000
_CHARS_PER_TOKEN = 4

# The tracer signature the context emits debt tags through: ``(tag, message)``,
# matching :func:`limpiador.observability.tracing.emit`.
Tracer = Callable[[str, str], None]


def estimate_tokens(text: str) -> int:
    """A cheap, deterministic token estimate for footprint accounting.

    Not exact — that is the adapter's job via real usage — but stable and
    monotonic in length, which is all the compaction trigger needs.
    """
    return len(text) // _CHARS_PER_TOKEN


class PayloadKind(str, Enum):
    """What a transient payload is — only ``FILE`` reads can be a *re-read*.

    ``RESULT`` is the opaque kind the loop records: a folded tool result whose
    semantics it cannot (and must not) inspect, since the loop never branches on
    tool identity. A future tool-aware layer can refine a ``RESULT`` into a
    ``FILE``/``DIFF``/``LOG`` so re-read detection and richer summaries apply.
    """

    FILE = "file"
    DIFF = "diff"
    LOG = "log"
    RESULT = "result"


class SymbolKind(str, Enum):
    """The durable, symbol-level facts kept across the whole run (ARCH_DEBT_002)."""

    DEFINITION = "definition"
    REFERENCE = "reference"
    IMPORT = "import"


@dataclass(frozen=True)
class SymbolFact:
    """A durable symbol-level finding — never evicted.

    Definitions, references, and import edges distilled from a raw read. Keeping
    these — and dropping only the raw text they came from — is exactly what lets
    a long refactor stay coherent without re-reading files (ARCH_DEBT_002).
    """

    symbol: str
    kind: SymbolKind
    location: str
    detail: str = ""


@dataclass(frozen=True)
class SubGoal:
    """A planned step. A ``resolved`` sub-goal is durable and never re-litigated."""

    description: str
    resolved: bool = False


@dataclass
class Payload:
    """A transient raw tool output — the candidate for summarize-then-evict.

    ``raw`` holds the full text (file contents, a diff, a log) until it is no
    longer the immediate next step; ``summary`` is the compact stand-in that
    replaces it. After eviction ``raw`` is ``None`` and only the summary remains,
    so the footprint drops while the gist survives. ``pinned`` marks a payload
    the caller still needs, exempting it from eviction.
    """

    key: str
    kind: PayloadKind
    summary: str
    raw: str | None
    pinned: bool = False

    @property
    def evicted(self) -> bool:
        """True once the raw bulk has been dropped in favor of the summary."""
        return self.raw is None

    def footprint(self) -> int:
        """The payload's current contribution to the context footprint."""
        return estimate_tokens(self.summary if self.evicted else self.raw or "")


@dataclass(frozen=True)
class CompactionResult:
    """What one :meth:`Context.compact` pass did — a no-op reports ``evicted == 0``."""

    evicted: int
    footprint_before: int
    footprint_after: int


class Context:
    """Working memory for a run: durable state plus evictable transient payloads.

    The loop records what it learns here — the plan and its sub-goals, symbol
    facts, and raw tool payloads — and calls :meth:`compact` when the footprint
    crosses the threshold. Durable state is never evicted; raw payloads are
    summarized and dropped oldest-first, sparing the most-recent payload and any
    pinned one. The context never inspects payload *content* (that classification
    is the caller's, keeping the loop tool-ignorant); it only manages footprint.
    """

    def __init__(
        self,
        task: str,
        *,
        threshold_tokens: int = DEFAULT_COMPACTION_THRESHOLD_TOKENS,
        tracer: Tracer = emit,
    ) -> None:
        self._task = task
        self._threshold = threshold_tokens
        self._trace = tracer
        self._sub_goals: list[SubGoal] = []
        self._symbol_facts: list[SymbolFact] = []
        self._payloads: list[Payload] = []
        self._files_read: set[str] = set()

    # ---- durable state ------------------------------------------------------
    @property
    def task(self) -> str:
        return self._task

    def add_sub_goal(self, description: str) -> None:
        """Add a planned step, unresolved, unless it is already on the plan."""
        if not any(g.description == description for g in self._sub_goals):
            self._sub_goals.append(SubGoal(description))

    def resolve_sub_goal(self, description: str) -> None:
        """Mark a sub-goal resolved so it is never re-litigated.

        Lenient by design: resolving a sub-goal the plan never listed records it
        as a resolved step rather than raising — a run may discover work mid-flight.
        """
        for i, goal in enumerate(self._sub_goals):
            if goal.description == description:
                self._sub_goals[i] = replace(goal, resolved=True)
                return
        self._sub_goals.append(SubGoal(description, resolved=True))

    def record_symbol_fact(self, fact: SymbolFact) -> None:
        """Note a durable symbol fact (deduplicated). These are never evicted."""
        if fact not in self._symbol_facts:
            self._symbol_facts.append(fact)

    @property
    def sub_goals(self) -> tuple[SubGoal, ...]:
        return tuple(self._sub_goals)

    @property
    def resolved_sub_goals(self) -> tuple[SubGoal, ...]:
        return tuple(g for g in self._sub_goals if g.resolved)

    @property
    def unresolved_sub_goals(self) -> tuple[SubGoal, ...]:
        return tuple(g for g in self._sub_goals if not g.resolved)

    @property
    def symbol_facts(self) -> tuple[SymbolFact, ...]:
        return tuple(self._symbol_facts)

    # ---- transient payloads -------------------------------------------------
    def record_payload(
        self,
        key: str,
        raw: str,
        *,
        kind: PayloadKind = PayloadKind.FILE,
        summary: str | None = None,
        pinned: bool = False,
    ) -> Payload:
        """Record a raw tool payload as a candidate for later eviction.

        Re-reading a file (the same key, ``FILE`` kind, seen before) emits the
        ``[CONTEXT REREAD]`` debt tag (ARCH_DEBT_002) — the signal that a payload
        we summarized away was needed again. A summary is generated if the caller
        does not supply one, so the gist always outlives the raw text.
        """
        if kind is PayloadKind.FILE:
            if key in self._files_read:
                self._trace(CONTEXT_REREAD_TAG, key)
            self._files_read.add(key)
        payload = Payload(
            key=key,
            kind=kind,
            summary=summary if summary is not None else self._default_summary(key, kind, raw),
            raw=raw,
            pinned=pinned,
        )
        self._payloads.append(payload)
        return payload

    def pin(self, key: str) -> None:
        """Protect every live payload with ``key`` from eviction."""
        for payload in self._payloads:
            if payload.key == key:
                payload.pinned = True

    def release(self, key: str) -> None:
        """Drop protection from every payload with ``key``."""
        for payload in self._payloads:
            if payload.key == key:
                payload.pinned = False

    @property
    def payloads(self) -> tuple[Payload, ...]:
        return tuple(self._payloads)

    @property
    def live_payloads(self) -> tuple[Payload, ...]:
        return tuple(p for p in self._payloads if not p.evicted)

    # ---- footprint & compaction ---------------------------------------------
    def estimated_tokens(self) -> int:
        """The current footprint: durable state plus every payload's contribution."""
        return self._durable_tokens() + sum(p.footprint() for p in self._payloads)

    def compact(self) -> CompactionResult:
        """Summarize-then-evict stale raw payloads if over the threshold.

        Below the threshold this is a no-op (``evicted == 0``). Over it, every
        live, unpinned payload except the most-recent one gives up its raw text
        for its summary — durable state is never touched. The most-recent payload
        is spared because it is the immediate next step the model will act on.
        """
        before = self.estimated_tokens()
        if before <= self._threshold:
            return CompactionResult(0, before, before)

        protected = self._most_recent_live()
        evicted = 0
        for payload in self._payloads:
            if payload is protected or payload.pinned or payload.evicted:
                continue
            payload.raw = None
            evicted += 1

        after = self.estimated_tokens()
        # Property #3 is only credible if it is observable: record what this pass
        # actually dropped so a long run's trace *shows* compaction firing.
        if evicted:
            self._trace(COMPACTION_TAG, f"evicted {evicted} payload(s): {before} -> {after} tokens")
        return CompactionResult(evicted, before, after)

    # ---- internals ----------------------------------------------------------
    def _durable_tokens(self) -> int:
        parts = [self._task]
        parts += [g.description for g in self._sub_goals]
        parts += [f"{f.symbol}{f.location}{f.detail}" for f in self._symbol_facts]
        return estimate_tokens("".join(parts))

    def _most_recent_live(self) -> Payload | None:
        for payload in reversed(self._payloads):
            if not payload.evicted:
                return payload
        return None

    @staticmethod
    def _default_summary(key: str, kind: PayloadKind, raw: str) -> str:
        """A compact stand-in when the caller supplies none — keeps the key visible."""
        return f"[{kind.value}] {key}: {len(raw)} chars, summarized"
