"""Rescore the runs whose scorer call failed.

    python -m scripts.backfill_scores --dry-run        # show, write nothing
    python -m scripts.backfill_scores --limit 5        # do the first few
    python -m scripts.backfill_scores                  # do them all

The scorer died on an expired key while everything else kept running, so the
headlines and the Brent price for each failed run are still in the sheet. This
feeds each run its own headlines back to the model and fills the blank score
columns.

What it deliberately does NOT do:

  * touch `action` or `position_after`. The rules never ran on these scores, so
    the trade record stays as it happened. Replaying the rules over backfilled
    scores is a separate exercise and its trades are not real.
  * hide itself. Every row it writes is stamped `score_source=backfilled:<date>`
    so the analysis can report with and without them.

The model sees only the headlines from that run's own 90 minute window and the
rows before it, exactly as the live run would have. It cannot see what Brent
did afterwards, so the score is not contaminated by hindsight.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from typing import Dict, List

from src import config, scorer, sheets
from src.sheets import HEADLINES, SIGNALS

SCORE_COLUMNS = (
    "score", "confidence", "direction", "key_headlines", "new_information", "reasoning",
)


def headlines_by_run(client) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for row in client.records(HEADLINES):
        out.setdefault(str(row.get("run_utc", "")).strip(), []).append(row)
    return out


def needs_score(row: dict) -> bool:
    return (
        str(row.get("action", "")).strip() == "error"
        and str(row.get("score", "")).strip() == ""
    )


def as_headline_dict(row: dict) -> dict:
    return {
        "title": row.get("title", ""),
        "source": row.get("source", ""),
        "published_utc": row.get("published_utc", ""),
        "url": row.get("url", ""),
        "hash": row.get("hash", ""),
    }


def run(dry_run: bool, limit: int) -> int:
    client = sheets.open_client(dry_run=False)
    if not dry_run:
        for change in client.ensure_tabs():
            print("sheet: %s" % change)

    rows = client.records(SIGNALS)
    heads = headlines_by_run(client)
    stamp = "backfilled:%s" % date.today().isoformat()

    targets = [(i, r) for i, r in enumerate(rows) if needs_score(r)]
    if limit:
        targets = targets[:limit]
    print("rows needing a score: %d (doing %d)" % (
        sum(1 for r in rows if needs_score(r)), len(targets)))

    done = skipped = 0
    spend = 0.0

    # Ascending order matters: each row is scored with the rows before it as
    # context, and those include ones this pass has just filled in, which is
    # what the live run would have seen.
    for index, row in targets:
        run_utc = str(row.get("run_utc", "")).strip()
        fresh = [as_headline_dict(h) for h in heads.get(run_utc, [])]
        if not fresh:
            print("  %s  SKIP, no headlines stored" % run_utc[:16])
            skipped += 1
            continue

        context = rows[max(0, index - config.CONTEXT_SIGNAL_ROWS):index]
        scored, usage = scorer.score_news(fresh, context)
        if usage:
            spend += usage.cost_usd
        if scored is None:
            print("  %s  FAILED: %s" % (run_utc[:16], (usage.error if usage else "unknown")))
            skipped += 1
            continue

        patch = {
            "score": scored.score,
            "confidence": scored.confidence,
            "direction": scored.direction,
            "key_headlines": " | ".join(scored.key_headlines),
            "new_information": scored.new_information,
            "reasoning": scored.reasoning,
            "score_source": stamp,
        }
        print("  %s  score %+d  %-6s  %d headlines%s" % (
            run_utc[:16], scored.score, scored.confidence, len(fresh),
            "  (dry run)" if dry_run else ""))

        if not dry_run:
            client.update_cells(SIGNALS, row["_row"], patch)
        # Keep the in memory copy current so the next row's context sees it.
        rows[index].update(patch)
        done += 1

    print("\nbackfilled: %d   skipped: %d   spend: $%.2f%s" % (
        done, skipped, spend, "  (DRY RUN, nothing written)" if dry_run else ""))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Rescore runs whose scorer call failed.")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    parser.add_argument("--limit", type=int, default=0, help="only do the first N")
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
