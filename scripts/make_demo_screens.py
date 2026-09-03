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

What this script does produce is the SUPPORTING screens -- multi-seed tables
that take minutes to compute, and excerpts of files that already exist in the
repo. Those are quotations, not evidence, and pre-rendering them costs
nothing.

It is committed for the same reason everything else here is: so a reviewer
can see exactly how the screens in the video were produced, and regenerate
them.

    python scripts/make_demo_screens.py

Output goes to results/demo/, which is gitignored.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "results", "demo")

# The last N lines of a run_batch run are exactly the comparison table.
# Kept here so the number in the video notes has one source of truth.
TABLE_TAIL_LINES = 36


def _save(name, text, cue):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    io.open(path, "w", encoding="utf-8", newline="").write(text.rstrip() + "\n")
    print("  %-22s %3d lines   %s" % (name, text.rstrip().count("\n") + 1, cue))


def _section(text, start, end):
    """Excerpt between two markers, so the screens cannot drift from the docs."""
    i = text.index(start)
    return text[i:text.index(end, i)]


def main():
    os.chdir(ROOT)
    readme = io.open("README.md", encoding="utf-8").read()

    print("\nPRE-RENDERED (quotations and slow computations):")

    _save("01_seeds.txt",
          _section(readme, "**What is NOT stable", "**What IS stable"),
          "0:25  the turn -- the direction is noise")

    _save("02_stability.txt",
          _section(readme, "**What IS stable", "That is the actual claim"),
          "1:15  what IS stable -- the strongest screen")

    exceptions = os.path.join("results", "exceptions.md")
    if os.path.exists(exceptions):
        txt = io.open(exceptions, encoding="utf-8").read()
        if "CARD_BLOCKED" in txt:
            j = txt.index("CARD_BLOCKED")
            _save("03_forgone.txt", txt[max(0, j - 400):j + 900],
                  "2:50  the money deliberately not taken")
    else:
        print("  (skipped 03_forgone -- run `python -m src.run_batch` first)")

    print("\nRUN LIVE ON CAMERA (real execution, fast enough to narrate over):")
    print("  python -m src.run_batch | Select-Object -Last %d"
          % TABLE_TAIL_LINES)
    print("      3.9s, table lands framed          -> 0:00  the headline")
    print("  python -m src.run_ai_demo")
    print("      0.3s, 17 lines                    -> 2:10  where the AI is")
    print("  python -m src.replay --compare")
    print("      0.3s, 39 lines                    -> 3:30  what broke")

    print("\nDO NOT run on camera:")
    print("  python -m src.generate_data   overwrites the batch every number")
    print("                                you just narrated describes")
    print("  python -m src.run_batch --live   real API calls, can fail on")
    print("                                   network mid-take")
    return 0


if __name__ == "__main__":
    sys.exit(main())
