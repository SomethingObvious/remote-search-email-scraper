"""Ask every keyless source one real question and print the SMS it would send back.

This isn't part of the test run, since it goes online. It makes about 40 requests,
spaced out by a second, so it's fine to run by hand now and then:

    python tests/live_smoke.py
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from remotesearch.router import answer

QUESTIONS = (
    "weather Tofino",
    "forecast Whistler",
    "sun Tofino",
    "tide Tofino",
    "avy Whistler",
    "road hwy 99",
    "road Squamish",
    "drive Vancouver to Whistler",
    "calc 15% of 80",
    "convert 10 km to mi",
    "convert 50 usd to cad",
    "time Tokyo",
    "translate where is the train station to german",
    "news",
    "news wildfire",
    "score canucks",
    "hours Tim Hortons Hope BC",
    "define albedo",
    "wiki photosynthesis",
    "so python yield",
    "reddit best tent for the west coast trail",
    "how long to boil water to purify it",
    "help",
    "help tide",
)


def main() -> int:
    logging.basicConfig(level=logging.ERROR, format="  (%(message)s)")
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    down = []
    for question in QUESTIONS:
        reply = answer(question)
        print(f"> {question}\n< {reply}\n")
        if "isn't answering right now" in reply:
            down.append(question)
        time.sleep(1)
    print(f"{len(QUESTIONS) - len(down)} of {len(QUESTIONS)} answered.", end=" ")
    print(f"Down: {', '.join(down)}" if down else "Nothing was down.")
    return 1 if down else 0


if __name__ == "__main__":
    sys.exit(main())
