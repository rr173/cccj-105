"""Incremental (semi-naive) evaluation of stratified recursive rules.

Facts and every intermediate relation live on disk in one SQLite database.
The evaluator never builds a Python container of a whole relation: joins run
inside SQL, and only the rows of the current delta wave are read out when they
must be handed to the next round.

Tuple representation
--------------------
Each stored tuple is one row in ``facts`` with fixed columns ``(run_id,
relation, c0..c7)``.  A typed value is encoded as JSON text: strings keep their
exact code points (no case folding, no trimming) and integers keep their signed
64-bit identity.  Because a column's type is fixed per relation, the encoding
never makes a string compare equal to an integer.  A tuple's set identity is
its encoded row, so ``INSERT OR IGNORE`` on ``(run_id, relation, c0..c7)``
gives set semantics even when the input file repeats a line.

Semi-naive rounds
-----------------
Round 0 of a component evaluates every rule with all positive atoms joined
against the **full** relation.  New rows become the first delta.

Every later round evaluates, for each rule, one *variant* per positive body
atom that belongs to the component's recursive relations: that chosen atom is
joined against the **delta** produced by the previous round, every other atom
(recursive or not, in this component or below) against its **full** relation.
The union over all variants is the standard semi-naive expansion and is
complete.  This is especially important when one body mentions two recursive
relations (or the same recursive relation twice): a tuple that is new in
*either* position must be allowed to combine with old tuples in the other
position.  Fixing a single alias to the delta would silently lose derivations,
so it is never done.

A round that inserts nothing ends the component.  Old rows are never re-joined
as delta, and duplicate tuples are ignored by the primary key, so a positive
cycle cannot append the same tuple forever.  ``execute_round`` records, for
every body atom of every fired rule, whether it read ``delta`` or ``full``;
that audit trail is what the report exposes.

Negation and comparisons
------------------------
A negated atom is a ``NOT EXISTS`` subquery over the complete negated relation
(a base relation or a lower stratum — stratification guarantees it has
finished).  It is joined on variables already bound by the rule's positive
atoms and compares the whole tuple: absence means than exact tuple is missing,
not that some other tuple exists.  ``eq``/``neq`` compare already-bound
variables or constants of the same declared type; equal types mean equal JSON
text, and unequal text means unequal value, for both strings and integers.

Budget and atomicity
--------------------
``budget`` caps the total number of stored tuples for the run (base facts plus
derived tuples).  Crossing it raises :class:`BudgetExceeded`; the run is
rolled back completely and no successful result is published — results are
never truncated and labelled complete.  The whole evaluation, its metadata and
the run summary commit in one SQLite transaction.  A bad fact, a budget
overflow or a process death rolls everything back, leaving earlier successful
runs untouched.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .relfacts import (
    FACT_PAD,
    MAX_COLUMNS,
    NoticeView,
    count_changes,
    encode,
    load_jsonl_relation,
    load_notice_relation,
)
from .relrule import (
    NOTICE_COLUMNS,
    NOTICE_RELATION,
    Comparison,
    Rule,
    RuleSet,
)

VIEW = "rel_rule_facts"
_SLOTS = ", ".join(f"c{i}" for i in range(MAX_COLUMNS))


class BudgetExceeded(RuntimeError):
    """The total tuple budget was crossed; the run must fail (not truncate)."""

    def __init__(self, detail: str, detail_json: dict):
        super().__init__(detail)
        self.detail = detail
        self.detail_json = detail_json


class Interrupted(RuntimeError):
    """A controlled failure injected inside the publish transaction."""


class HardExit(RuntimeError):
    """Requested controlled failure inside the transaction (rolls back).

    The CLI's ``--hard-exit`` mode bypasses this and calls ``os._exit`` from
    inside the transaction, which is the process-death case; this exception is
    used by in-process tests and the ``--fail-point`` hook.
    """


class JobError(RuntimeError):
    """Operational failure (unwritable workdir, unusable result database)."""


class RequestViolation(RuntimeError):
    """An impossible read-back request (unknown run/relation/page)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _q(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Result store schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    rules_path   TEXT NOT NULL,
    rules_sha256 TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    budget       INTEGER NOT NULL,
    pipeline_db  TEXT NOT NULL,
    summary_json TEXT,
    error_json   TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    run_id   INTEGER NOT NULL,
    relation TEXT NOT NULL,
    c0 TEXT NOT NULL, c1 TEXT NOT NULL, c2 TEXT NOT NULL, c3 TEXT NOT NULL,
    c4 TEXT NOT NULL, c5 TEXT NOT NULL, c6 TEXT NOT NULL, c7 TEXT NOT NULL,
    PRIMARY KEY (run_id, relation, c0, c1, c2, c3, c4, c5, c6, c7)
);

CREATE INDEX IF NOT EXISTS idx_facts_run_relation
    ON facts(run_id, relation);

CREATE TABLE IF NOT EXISTS rule_relations (
    run_id       INTEGER NOT NULL,
    relation     TEXT NOT NULL,
    position     INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    base         INTEGER NOT NULL,
    recursive    INTEGER NOT NULL,
    stratum      INTEGER NOT NULL,
    PRIMARY KEY (run_id, relation)
);

CREATE TABLE IF NOT EXISTS fact_inputs (
    run_id      INTEGER NOT NULL,
    relation    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    digest_json TEXT NOT NULL,
    PRIMARY KEY (run_id, relation)
);

CREATE TABLE IF NOT EXISTS relation_stats (
    run_id   INTEGER NOT NULL,
    relation TEXT NOT NULL,
    kind     TEXT NOT NULL,
    stratum  INTEGER NOT NULL,
    tuples   INTEGER NOT NULL,
    PRIMARY KEY (run_id, relation)
);

CREATE TABLE IF NOT EXISTS round_stats (
    run_id         INTEGER NOT NULL,
    relation       TEXT NOT NULL,
    component_id   INTEGER NOT NULL,
    round_no       INTEGER NOT NULL,
    variants       INTEGER NOT NULL,
    delta_rows     INTEGER NOT NULL,
    join_plan_json TEXT NOT NULL,
    PRIMARY KEY (run_id, relation, round_no)
);

CREATE TABLE IF NOT EXISTS execution_stats (
    run_id       INTEGER NOT NULL,
    component_id INTEGER NOT NULL,
    relation     TEXT NOT NULL,
    rounds       INTEGER NOT NULL,
    delta_rows   INTEGER NOT NULL,
    joins_delta  INTEGER NOT NULL,
    joins_full   INTEGER NOT NULL,
    PRIMARY KEY (run_id, component_id, relation)
);
"""


@contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class ResultStore:
    """The job's own SQLite database (never the archive pipeline database)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                        timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
            # Intermediate (delta / snapshot) tables go to a temporary file,
            # never to an unbounded in-memory store.
            self.conn.execute("PRAGMA temp_store = FILE")
            self.conn.executescript(SCHEMA)
        except sqlite3.Error as exc:
            raise JobError(f"cannot prepare result database {self.path}: {exc}"
                           ) from exc

    def close(self) -> None:
        self.conn.close()

    def mark_interrupted_runs(self, at: str) -> int:
        """Runs never commit in 'running' state; this only matters for old files."""
        with _tx(self.conn) as conn:
            cur = conn.execute(
                "UPDATE job_runs SET status='incomplete', finished_at=?, "
                "error_json=COALESCE(error_json, ?) WHERE status='running'",
                (at, json.dumps(
                    {"code": "process_interrupted",
                     "detail": "the process died mid-run; no result summary was "
                               "committed"}, ensure_ascii=False)))
            return cur.rowcount

    def latest_successful_run(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM job_runs WHERE status='completed' "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
        return dict(row) if row is not None else None

    def run(self, run_id: int | None = None) -> dict | None:
        if run_id is None:
            row = self.conn.execute(
                "SELECT * FROM job_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM job_runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def runs(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM job_runs ORDER BY run_id")]

    def relation_info(self, run_id: int, relation: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM rule_relations WHERE run_id=? AND relation=?",
            (run_id, relation)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["columns"] = [{"name": n, "type": t}
                          for n, t in json.loads(out.pop("columns_json"))]
        return out

    def relation_rows(self, run_id: int, relation: str, *, offset: int = 0,
                      limit: int = 100) -> list[dict]:
        info = self.relation_info(run_id, relation)
        if info is None or offset < 0 or limit < 0:
            return []
        width = len(info["columns"])
        slot_list = ", ".join(f"c{i}" for i in range(width))
        rows = self.conn.execute(
            f"SELECT {slot_list} FROM facts WHERE run_id=? AND relation=? "
            f"ORDER BY {', '.join(f'c{i}' for i in range(MAX_COLUMNS))} "
            "LIMIT ? OFFSET ?", (run_id, relation, limit, offset)).fetchall()
        names = [column["name"] for column in info["columns"]]
        return [{names[i]: json.loads(row[i]) for i in range(width)}
                for row in rows]

    def relation_count(self, run_id: int, relation: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE run_id=? AND relation=?",
            (run_id, relation)).fetchone()
        return int(row["n"])

    def rounds(self, run_id: int, relation: str | None = None) -> list[dict]:
        if relation is None:
            rows = self.conn.execute(
                "SELECT * FROM round_stats WHERE run_id=? "
                "ORDER BY component_id, relation, round_no", (run_id,))
        else:
            rows = self.conn.execute(
                "SELECT * FROM round_stats WHERE run_id=? AND relation=? "
                "ORDER BY round_no", (run_id, relation))
        return [dict(row) for row in rows]

    def execution(self, run_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM execution_stats WHERE run_id=? "
            "ORDER BY component_id, relation", (run_id,))]

    def relation_stats(self, run_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM relation_stats WHERE run_id=? "
            "ORDER BY stratum, relation", (run_id,))]

    def inputs(self, run_id: int) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM fact_inputs WHERE run_id=? ORDER BY relation",
            (run_id,))]


# ---------------------------------------------------------------------------
# Rule compiler
# ---------------------------------------------------------------------------


def _full_source(relation: str) -> str:
    return _q(f"rel_tmp_full_{relation}")


def _delta_source(relation: str) -> str:
    return _q(f"rel_tmp_delta_{relation}")


class RuleCompiler:
    """Compile one rule variant into a single SQL statement.

    The statement selects the head tuple columns from the joined body rows.
    Positive atoms become table sources (a snapshot of the complete set or a
    delta temp table); repeated variables become equality conditions (the
    repeated-variable self-join); negated atoms become ``NOT EXISTS`` probes.
    ``full_sources`` maps a relation to the table/view that holds its complete
    set for this round; a relation present in ``delta_positions`` is read from
    its delta table at that body position instead.
    """

    def __init__(self, rule: Rule, delta_positions: set[int],
                 full_sources: dict[str, str] | None = None):
        self.rule = rule
        self.delta_positions = delta_positions
        self.full_sources = full_sources or {}

    def full(self, relation: str) -> str:
        return self.full_sources.get(relation, _full_source(relation))

    def compile(self) -> tuple[str, list[dict]]:
        parts: list[str] = []
        sources: list[str] = []
        bound: dict[str, str] = {}
        plan: list[dict] = []
        for offset, atom in enumerate(self.rule.positives):
            alias = f"t{offset}"
            use_delta = offset in self.delta_positions
            source = (_delta_source(atom.relation) if use_delta
                      else self.full(atom.relation))
            sources.append(f"{source} AS {alias}")
            plan.append({"rule_index": self.rule.index, "position": offset,
                         "relation": atom.relation, "alias": alias,
                         "mode": "delta" if use_delta else "full",
                         "negated": False})
            for index, term in enumerate(atom.args):
                column = f"{alias}.c{index}"
                if term.kind == "const":
                    parts.append(f"{column} = {_lit(encode(term.value))}")
                elif term.value in bound:
                    parts.append(f"{column} = {bound[term.value]}")
                else:
                    bound[term.value] = column
        for comparison in self.rule.comparisons:
            parts.append(self._compare(comparison, bound, plan))
        for offset, atom in enumerate(self.rule.negatives):
            alias = f"n{offset}"
            sub = [f"SELECT 1 FROM {self.full(atom.relation)} AS {alias} "
                   f"WHERE 1"]
            for index, term in enumerate(atom.args):
                column = f"{alias}.c{index}"
                if term.kind == "const":
                    sub.append(f"{column} = {_lit(encode(term.value))}")
                else:
                    sub.append(f"{column} = {bound[term.value]}")
            parts.append("NOT EXISTS (" + " AND ".join(sub) + ")")
            plan.append({"rule_index": self.rule.index, "position": offset,
                         "relation": atom.relation, "alias": alias,
                         "mode": "not_exists", "negated": True})
        head = [self._head_expr(term, bound) for term in self.rule.head.args]
        head += [_lit(FACT_PAD)] * (MAX_COLUMNS - len(head))
        sql = f"SELECT {', '.join(head)} FROM {', '.join(sources)}"
        if parts:
            sql += " WHERE " + " AND ".join(parts)
        return sql, plan

    def _head_expr(self, term, bound: dict[str, str]) -> str:
        if term.kind == "const":
            return _lit(encode(term.value))
        return bound[term.value]

    @staticmethod
    def _compare(comparison: Comparison, bound: dict[str, str],
                 plan: list[dict]) -> str:
        op = "=" if comparison.op == "eq" else "<>"

        def side(term) -> str:
            if term.kind == "var":
                return bound[term.value]
            return _lit(encode(term.value))

        sql = f"{side(comparison.left)} {op} {side(comparison.right)}"
        plan.append({"rule_index": None, "position": None, "relation": None,
                     "alias": None, "mode": "compare", "negated": False,
                     "op": comparison.op, "sql": sql})
        return sql


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class RoundRecord:
    relation: str
    component_id: int
    round_no: int
    variants: int
    delta_rows: int
    join_plan: list[dict]


@dataclass
class ComponentRecord:
    component_id: int
    relations: list[str]
    recursive: bool
    rounds: int = 0
    delta_rows: int = 0
    joins_delta: int = 0
    joins_full: int = 0


class Evaluator:
    """Bottom-up semi-naive evaluation inside one caller-owned transaction."""

    def __init__(self, ruleset: RuleSet, conn: sqlite3.Connection,
                 run_id: int, *, budget: int | None = None):
        self.ruleset = ruleset
        self.conn = conn
        self.run_id = run_id
        self.budget = budget if budget is not None else ruleset.budget
        self.total = 0
        self.derived_total = 0
        self.rounds: list[RoundRecord] = []
        self.components: list[ComponentRecord] = []
        self._counts: dict[str, int] = {}
        self._watermark: dict[str, int] = {}
        self.notice_view: NoticeView | None = None
        self.input_digests: list[dict] = []
        self._relation_columns: dict[str, list[list[str]]] = {
            name: [[c.name, c.type] for c in relation.columns]
            for name, relation in ruleset.relations.items()
        }
        self._relation_columns[NOTICE_RELATION] = [[n, t]
                                                   for n, t in NOTICE_COLUMNS]
        self._component_of = ruleset.plan.relation_component

    # -- setup ------------------------------------------------------------
    def _temp(self, kind: str, relation: str) -> str:
        """Stable quoted name for a temporary table/view of one relation."""
        return _q(f"rel_tmp_{kind}_{relation}")

    def _create_sources(self) -> None:
        self.conn.execute(f"DROP VIEW IF EXISTS {VIEW}")
        self.conn.execute(
            f"CREATE TEMP VIEW {VIEW} AS SELECT relation, {_SLOTS} FROM facts "
            f"WHERE run_id = {self.run_id}")
        # The per-relation "full" views are only defaults; each stratum pins
        # its own rowid-watermarked round views before a round.
        for name in list(self.ruleset.relations) + [NOTICE_RELATION]:
            self.conn.execute(f"DROP VIEW IF EXISTS {self._temp('full', name)}")
            self.conn.execute(
                f"CREATE TEMP VIEW {self._temp('full', name)} AS "
                f"SELECT {_SLOTS} FROM facts "
                f"WHERE run_id = {self.run_id} AND relation = {_lit(name)}")

    def _create_delta(self, relation: str) -> None:
        for kind in ("delta", "next"):
            self.conn.execute(f"DROP TABLE IF EXISTS {self._temp(kind, relation)}")
            self.conn.execute(
                f"CREATE TEMP TABLE {self._temp(kind, relation)} "
                f"({', '.join(f'c{i} TEXT NOT NULL' for i in range(MAX_COLUMNS))})")

    def _clear_next(self, relation: str) -> None:
        self.conn.execute(f"DELETE FROM {self._temp('next', relation)}")

    def _swap_delta(self, relation: str) -> None:
        """``next`` (rows of the round just finished) becomes the new delta."""
        self.conn.execute(f"DROP TABLE {self._temp('delta', relation)}")
        self.conn.execute(
            f"ALTER TABLE {self._temp('next', relation)} "
            f"RENAME TO {self._temp('delta', relation)}")
        self.conn.execute(
            f"CREATE TEMP TABLE {self._temp('next', relation)} "
            f"({', '.join(f'c{i} TEXT NOT NULL' for i in range(MAX_COLUMNS))})")

    def _drop_temps(self, relation: str) -> None:
        for kind in ("delta", "next"):
            self.conn.execute(f"DROP TABLE IF EXISTS {self._temp(kind, relation)}")

    def _delta_count(self, relation: str) -> int:
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {self._temp('delta', relation)}"
        ).fetchone()
        return int(row["n"])

    # -- base loading -----------------------------------------------------
    def load_bases(self, pipeline_db: str | Path) -> list[dict]:
        for name, relation in self.ruleset.relations.items():
            if not relation.is_base:
                continue
            source = relation.source
            assert source is not None
            path = Path(source["document_dir"]) / source["path"]
            digest = load_jsonl_relation(path, relation, self.conn, self.run_id)
            self.input_digests.append(digest.as_dict())
            self._counts[name] = digest.distinct
            self.total += digest.distinct
            self._watermark[name] = self._max_rowid(name)
        self.notice_view = load_notice_relation(pipeline_db, self.conn,
                                                self.run_id)
        self.input_digests.append(self.notice_view.as_dict())
        self._counts[NOTICE_RELATION] = self.notice_view.distinct
        self.total += self.notice_view.distinct
        self._watermark[NOTICE_RELATION] = self._max_rowid(NOTICE_RELATION)
        self._create_sources()
        return self.input_digests

    def _max_rowid(self, relation: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(rowid), 0) AS r FROM facts "
            "WHERE run_id=? AND relation=?", (self.run_id, relation)).fetchone()
        return int(row["r"])

    def _check_budget(self, where: str) -> None:
        if self.derived_total > self.budget:
            raise BudgetExceeded(
                f"{where}: derived tuple count reached {self.derived_total}, "
                f"over the budget of {self.budget}", {
                    "budget": self.budget,
                    "would_be_total": self.derived_total,
                    "base_tuples": self.total - self.derived_total,
                    "where": where})

    def _count(self, relation: str) -> int:
        return self._counts.get(relation, 0)

    # -- round execution --------------------------------------------------
    def _capture(self, relation: str) -> int:
        """Copy the rows just inserted into ``_next``; return their count.

        ``changes()`` is read immediately after the INSERT, so only genuinely
        new rows (INSERT OR IGNORE skips duplicates) are counted.  The copy
        is a SQL statement, never a Python list of the whole relation.
        """
        changed = count_changes(self.conn)
        if not changed:
            return 0
        self.conn.execute(
            f"INSERT OR IGNORE INTO {self._temp('next', relation)}({_SLOTS}) "
            f"SELECT {_SLOTS} FROM facts WHERE run_id=? AND relation=? "
            f"AND rowid > ?", (self.run_id, relation,
                               self._watermark.get(relation, 0)))
        self._watermark[relation] = self._max_rowid(relation)
        self._counts[relation] = self._count(relation) + changed
        self.total += changed
        self.derived_total += changed
        return changed

    def _fire_rule(self, rule: Rule, delta_positions: set[int],
                   plan_sink: list[dict],
                   full_sources: dict[str, str] | None = None) -> int:
        compiler = RuleCompiler(rule, delta_positions, full_sources)
        select_sql, plan = compiler.compile()
        self.conn.execute(
            f"INSERT OR IGNORE INTO facts(run_id, relation, {_SLOTS}) "
            f"SELECT {self.run_id}, {_lit(rule.head.relation)}, h.* "
            f"FROM ({select_sql}) AS h")
        plan_sink.extend(plan)
        inserted = self._capture(rule.head.relation)
        if inserted:
            self._check_budget(f"deriving rule {rule.index} "
                               f"for {rule.head.relation}")
        return inserted

    def _pin_full_views(self, stratum) -> dict[str, str]:
        """Point the "full" side of this round at rows known before it started.

        Facts are append-only per run, so a ``rowid <= watermark`` view is an
        exact frozen image of the known set without copying a single row.
        Rows produced during the current round are strictly newer than the
        watermark and therefore invisible to the full side; they are picked up
        as the next delta.  Lower-stratum relations are complete and read
        through their ordinary views.
        """
        sources: dict[str, str] = {}
        for name in stratum.relations:
            view = self._temp("round", name)
            watermark = self._max_rowid(name)
            self.conn.execute(f"DROP VIEW IF EXISTS {view}")
            self.conn.execute(
                f"CREATE TEMP VIEW {view} AS SELECT {_SLOTS} FROM facts "
                f"WHERE run_id = {self.run_id} AND relation = {_lit(name)} "
                f"AND rowid <= {watermark}")
            sources[name] = view
        return sources

    def _drop_snapshot(self, stratum) -> None:
        for name in stratum.relations:
            self.conn.execute(f"DROP VIEW IF EXISTS {self._temp('round', name)}")

    def _fire_seed(self, stratum, full_sources: dict[str, str]) -> None:
        """Round 0: only rules without a positive atom inside this component.

        These are the base-case rules; their results form the first delta.
        Recursive rules cannot fire yet because no delta exists.
        """
        component_set = set(stratum.relations)
        for name in stratum.relations:
            plans: list[dict] = []
            variants = 0
            inserted = 0
            for rule in self.ruleset.relations[name].rules:
                recursive_atoms = [
                    atom for atom in rule.positives
                    if atom.relation in component_set
                    and self._component_of.get(atom.relation) is not None]
                if recursive_atoms:
                    continue
                variants += 1
                inserted += self._fire_rule(rule, set(), plans, full_sources)
            self.rounds.append(RoundRecord(name, stratum.index, 0, variants,
                                           inserted, plans))

    def _fire_wave(self, stratum, round_no: int,
                   full_sources: dict[str, str]) -> int:
        """One semi-naive round; one delta variant per recursive body atom."""
        component_set = set(stratum.relations)
        total_inserted = 0
        for name in stratum.relations:
            plans: list[dict] = []
            variants = 0
            inserted = 0
            for rule in self.ruleset.relations[name].rules:
                positions = [
                    offset for offset, atom in enumerate(rule.positives)
                    if atom.relation in component_set
                    and self._component_of.get(atom.relation) is not None]
                if not positions:
                    continue  # seed rules already fired once
                for position in positions:
                    variants += 1
                    inserted += self._fire_rule(rule, {position}, plans,
                                                full_sources)
            self.rounds.append(RoundRecord(name, stratum.index, round_no,
                                           variants, inserted, plans))
            total_inserted += inserted
        return total_inserted

    def evaluate_stratum(self, stratum_index: int) -> ComponentRecord:
        stratum = self.ruleset.plan.strata[stratum_index]
        record = ComponentRecord(component_id=stratum_index,
                                 relations=list(stratum.relations),
                                 recursive=stratum.recursive)
        self.components.append(record)
        for name in stratum.relations:
            self._create_delta(name)
        if stratum.recursive:
            # -- seed: base-case rules only, no component atom in the body --
            full_sources = self._pin_full_views(stratum)
            self._fire_seed(stratum, full_sources)
            for name in stratum.relations:
                self._swap_delta(name)
                self._clear_next(name)
            round_no = 1
            while any(self._delta_count(name) for name in stratum.relations):
                # Freeze the complete set at round start: a delta tuple in one
                # position may combine with *any* already-known tuple in the
                # other (recursive) position, which is exactly what the full
                # snapshot provides.
                full_sources = self._pin_full_views(stratum)
                inserted = self._fire_wave(stratum, round_no, full_sources)
                for name in stratum.relations:
                    self._swap_delta(name)
                    self._clear_next(name)
                self._drop_snapshot(stratum)
                if not inserted:
                    break
                round_no += 1
        else:
            full_sources = self._pin_full_views(stratum)
            self._fire_seed(stratum, full_sources)
            self._drop_snapshot(stratum)
        for name in stratum.relations:
            self._drop_temps(name)
        self._drop_snapshot(stratum)
        for item in self.rounds:
            if item.component_id != stratum_index:
                continue
            record.rounds += 1
            record.delta_rows += item.delta_rows
            record.joins_delta += sum(1 for plan in item.join_plan
                                      if plan["mode"] == "delta")
            record.joins_full += sum(1 for plan in item.join_plan
                                     if plan["mode"] == "full")
        return record

    def evaluate(self) -> None:
        for stratum in self.ruleset.plan.strata:
            self.evaluate_stratum(stratum.index)
    # -- metadata ---------------------------------------------------------
    def relation_rows_for_meta(self) -> list[dict]:
        out = []
        order = list(self.ruleset.relations) + [NOTICE_RELATION]
        for position, name in enumerate(order):
            relation = self.ruleset.relations.get(name)
            base = 1 if relation is None or relation.is_base else 0
            recursive = int(self.ruleset.plan.relation_component.get(name)
                            is not None)
            stratum = self.ruleset.plan.relation_stratum.get(name, -1)
            kind = "notice" if name == NOTICE_RELATION else (
                "base" if base else "derived")
            out.append({"relation": name, "position": position,
                        "columns": self._relation_columns[name],
                        "base": base, "recursive": recursive,
                        "stratum": stratum, "kind": kind,
                        "tuples": self._count(name)})
        return out
