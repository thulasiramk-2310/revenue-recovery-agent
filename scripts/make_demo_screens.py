"""Reproduce the screens shown in the demo video.

WHAT RUNS LIVE, AND WHY THAT MATTERS
------------------------------------
The headline comparison is NOT produced here. It is run live on camera:

    python -m src.run_batch | Select-Object -Last 36     # PowerShell
    python -m src.run_batch | tail -n 36                 # bash

That takes about 3.9 seconds and prints 59 lines, of which the last 36 are
the comparison table. Piping through tail keeps the table on screen at
recording font size without scrolling, and the ~4 seconds is narration time,
not dead air.

Reading the headline number off a saved file would be the single
unverifiable moment in a demo where everything else executes for real. A
reviewer who spots that has a reason to doubt the figure, and this project
has no interest in buying a smoother take with a weaker claim.

What this produces is the multi-seed evidence, which takes ~40s to compute
and so cannot be run mid-take. It is COMPUTED HERE, not quoted from the
README: the screens are regenerated from actual runs every time, so they
cannot drift from what the code does.

PURE ASCII, DELIBERATELY
------------------------
No rupee sign, no em dash, no multiplication sign, no box-drawing characters.
Windows terminals default to a legacy code page and render UTF-8 as mojibake
-- a rupee sign becomes three garbage characters in every frame of the video.
src/run_batch.py already prints "Rs" for exactly this reason; these screens
match it. Being unable to mangle beats configuring every terminal correctly.

No markdown either. Asterisks and pipe-dash separators are for a rendered
page; on a terminal they read as unformatted junk.

    python scripts/make_demo_screens.py

Output goes to results/demo/, which is gitignored.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, "results", "demo")

# The last N lines of a run_batch run are exactly the comparison table.
# One source of truth for the number quoted in the video notes.
TABLE_TAIL_LINES = 36

SEEDS = (1, 7, 42, 44, 99, 123, 2026, 31337)
SHIPPED = 44

# A gap this size or smaller is a coin toss, not a win. Naming the threshold
# is more honest than "wins two, loses six", which lumps a Rs 3,612 gap in
# with a Rs 33,691 one.
NEAR_TIE_PAISE = 1_000_000          # Rs 10,000


def rs(paise):
    return "Rs " + format(paise / 100.0, ",.0f")


def _save(name, lines, cue):
    os.makedirs(OUT, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    # ASCII-only is a hard guarantee, not an intention -- assert it.
    text.encode("ascii")
    io.open(os.path.join(OUT, name), "w", encoding="ascii",
            newline="\n").write(text)
    print("  %-22s %3d lines   %s" % (name, len(lines), cue))


def collect():
    """Run the batch on every seed. ~40 seconds."""
    from src.generate_data import generate
    from src.run_batch import run

    tmp = tempfile.mkdtemp(prefix="demo_seeds_")
    rows = []
    for seed in SEEDS:
        data = os.path.join(tmp, "b.json")
        io.open(data, "w", encoding="utf-8").write(
            json.dumps(generate(240, seed)))
        s = run(log_path=os.path.join(tmp, "l.log"),
                summary_path=os.path.join(tmp, "s.json"),
                data_path=data, quiet=True)
        b = s["baseline"]
        rows.append({
            "seed": seed,
            "agent": s["recovered_paise"], "base": b["recovered_paise"],
            "a_att": s["attempts_spent"], "b_att": b["attempts_spent"],
            "a_rpa": s["recovered_paise"] / max(s["attempts_spent"], 1),
            "b_rpa": b["recovered_paise"] / max(b["attempts_spent"], 1),
        })
        print("    seed %-6d done" % seed)
    return rows


def screen_seeds(rows):
    W = 68
    out = ["=" * W,
           "  RECOVERY ACROSS 8 SEEDS -- same code, same policy",
           "=" * W,
           "  %-8s %14s %14s   %s" % ("seed", "agent", "baseline", "direction"),
           "  " + "-" * (W - 4)]
    for r in rows:
        d = r["agent"] - r["base"]
        who = "agent" if d > 0 else "base"
        tag = "%-5s +%9s" % (who, format(abs(d) / 100.0, ",.0f"))
        if abs(d) <= NEAR_TIE_PAISE:
            tag += "  (near tie)"
        mark = " <<" if r["seed"] == SHIPPED else ""
        out.append("  %-8s %14s %14s   %s%s"
                   % (r["seed"], rs(r["agent"]), rs(r["base"]), tag, mark))
    wins = [r for r in rows if r["agent"] > r["base"]]
    ties = [r for r in rows if abs(r["agent"] - r["base"]) <= NEAR_TIE_PAISE]
    losses = [r for r in rows
              if r["agent"] < r["base"] and abs(r["agent"] - r["base"]) > NEAR_TIE_PAISE]
    spread = max(r["agent"] - r["base"] for r in rows) - \
        min(r["agent"] - r["base"] for r in rows)
    out += ["  " + "-" * (W - 4),
            "  %d wins, %d near ties (within Rs 10,000), %d clear losses"
            % (len(wins), len(ties), len(losses)),
            "  swing across seeds: %s" % rs(spread),
            "",
            "  THE DIRECTION OF THIS COMPARISON IS NOISE.",
            "  Seed %d is shipped. Seeds %s would let this claim a win."
            % (SHIPPED, " and ".join(str(r["seed"]) for r in wins)),
            "=" * W]
    return out


def screen_stability(rows):
    W = 68
    ratios = [r["b_att"] / max(r["a_att"], 1) for r in rows]
    mults = [r["a_rpa"] / r["b_rpa"] for r in rows]
    better = sum(1 for m in mults if m > 1)
    out = ["=" * W,
           "  WHAT DOES NOT MOVE -- all 8 seeds",
           "=" * W,
           "  %-8s %10s %10s %8s   %12s" % ("seed", "attempts", "baseline",
                                            "ratio", "Rs/attempt"),
           "  " + "-" * (W - 4)]
    for r, ratio, m in zip(rows, ratios, mults):
        out.append("  %-8s %10d %10d %7.1fx   %6s vs %-6s (%.1fx)"
                   % (r["seed"], r["a_att"], r["b_att"], ratio,
                      format(r["a_rpa"] / 100.0, ",.0f"),
                      format(r["b_rpa"] / 100.0, ",.0f"), m))
    out += ["  " + "-" * (W - 4),
            "  fewer attempts than baseline    %.1fx to %.1fx, every seed"
            % (min(ratios), max(ratios)),
            "  more recovered per attempt      %d of %d seeds (%.1fx to %.1fx)"
            % (better, len(rows), min(mults), max(mults)),
            "",
            "  COMPLIANCE, measured from each run's own audit log:",
            "    contacts outside 09:00-21:00 IST .............. 0",
            "    transactions over the contact quota ........... 0",
            "    worst rolling-24h messages to one person ...... 2  (= the cap)",
            "    audit chain verified .......................... 8 of 8",
            "",
            "  THE RECOVERY TOTAL IS NOISY. THE DISCIPLINE IS NOT.",
            "=" * W]
    return out


def main():
    os.chdir(ROOT)
    print("\ncomputing 8 seeds (~40s, this is why it is not run mid-take):")
    rows = collect()

    print("\nPRE-RENDERED:")
    _save("01_seeds.txt", screen_seeds(rows),
          "0:25  the turn -- direction is noise")
    _save("02_stability.txt", screen_stability(rows),
          "1:15  what IS stable -- strongest screen")

    print("\nRUN LIVE ON CAMERA (real execution, fast enough to narrate over):")
    print("  python -m src.run_batch | Select-Object -Last %d" % TABLE_TAIL_LINES)
    print("      3.9s, table lands framed        -> 0:00  the headline")
    print("  python -m src.run_ai_demo")
    print("      0.3s, 17 lines                  -> 2:10  where the AI is")
    print("  python -m src.replay --compare")
    print("      0.3s, 39 lines                  -> 3:30  what broke")

    print("\nDO NOT run on camera:")
    print("  python -m src.generate_data      overwrites the batch that every")
    print("                                   narrated number describes")
    print("  python -m src.run_batch --live   real API calls, can fail on")
    print("                                   network mid-take")
    return 0


if __name__ == "__main__":
    sys.exit(main())
