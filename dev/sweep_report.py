#!/usr/bin/env python
"""Print what one finished sweep found, and what the title filter alone would have found.

Read-only by construction: it opens the configured database, runs the two SELECTs behind
`Repo.get_sweep_run()` and `Repo.sweep_candidates()`, and hands the rows to
`engine.sweep_report`. It never migrates, never writes, and never touches `items` or a
standing query — a sweep's results must stay out of `items` (MEM007), and a reporting tool
is exactly the kind of thing that would breach that by accident.

    .venv/bin/python dev/sweep_report.py 7
    .venv/bin/python dev/sweep_report.py 7 --json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vinted_sniper.config import Settings
from vinted_sniper.db import Database
from vinted_sniper.db.repo import Repo
from vinted_sniper.engine.sweep_report import SweepReport, build_report


async def _load(db_path: Path, sweep_id: int) -> SweepReport | None:
    """The whole database contact: open, two reads, close. No migrations, no writes.

    `PRAGMA query_only` makes that a guarantee rather than a promise — SQLite refuses any
    write on this connection, so a future edit here cannot quietly start mutating the
    live database. Opening still creates the usual `-wal`/`-shm` sidecars; every WAL
    reader does, including a strictly read-only one, and the database file itself is left
    byte-for-byte unchanged.
    """
    async with Database(db_path) as db:
        await db.fetch_one("PRAGMA query_only=ON")
        repo = Repo(db)
        run = await repo.get_sweep_run(sweep_id)
        if run is None:
            return None
        candidates = await repo.sweep_candidates(sweep_id)
    return build_report(run, candidates)


def _as_text(report: SweepReport) -> str:
    lines = [
        f"Sweep {report.sweep_id} — {report.status}",
        f"  keywords:   {' '.join(report.keywords) or '(none)'}",
        f"  pages read: {report.pages_fetched}",
        f"  listings:   {report.items_seen} seen, {report.candidates_kept} kept",
    ]
    if report.funnel:
        stages = ", ".join(f"{name} {count}" for name, count in report.funnel.items())
        lines.append(f"  funnel:     {stages}")
    lines.append(f"  spent:      {report.tokens} tokens, {report.cost_eur:.4f} EUR")
    lines.append("")
    lines.append(
        f"Title filter alone would have kept {report.baseline_count} "
        f"of the {len(report.candidates)} the sweep kept."
    )
    lines.append("")
    lines.append("Candidates, best first:")
    if not report.candidates:
        lines.append("  (none)")
    baseline_ids = {c.item_id for c in report.baseline}
    for position, candidate in enumerate(report.candidates, start=1):
        mark = "T" if candidate.item_id in baseline_ids else "-"
        match = {True: "yes", False: "no", None: "?"}[candidate.matches_target]
        price = f"{candidate.total_price:.2f}" if candidate.total_price is not None else "?"
        lines.append(
            f"  {position:>3}. [{mark}] rank {candidate.rank_score:.2f}  "
            f"looks right: {match}  {price} {candidate.currency or ''}  {candidate.title}"
        )
        lines.append(f"       {candidate.url}")
    lines.append("")
    lines.append("[T] = the title contains every keyword, i.e. a title-filter-only hit.")
    return "\n".join(lines)


def _as_json(report: SweepReport) -> str:
    payload = dataclasses.asdict(report)
    payload["baseline_count"] = report.baseline_count
    payload["baseline"] = [c.item_id for c in report.baseline]
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sweep_id", type=int, help="id of a sweep in sweep_runs")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="database file; defaults to the configured VINTED_SNIPER_DB_PATH",
    )
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else Settings().db_path
    if not Path(db_path).exists():
        print(f"No database at {db_path}.", file=sys.stderr)
        return 1

    report = asyncio.run(_load(Path(db_path), args.sweep_id))
    if report is None:
        print(f"No sweep with id {args.sweep_id} in {db_path}.", file=sys.stderr)
        return 1

    print(_as_json(report) if args.json else _as_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
