import json
import sys
from pathlib import Path

context = json.load(sys.stdin)
diagnosis = context["diagnosis"]["failure_type"]
if diagnosis != "reasoning_failure":
    print(f"unsupported failure type: {diagnosis}", file=sys.stderr)
    raise SystemExit(2)

previous = context["previous_attempts"]
if not previous:
    candidate = 0  # Deliberately rejected so the feedback loop is visible.
else:
    latest = previous[-1]
    if latest["phase"] != "evaluation" or not latest["rejection_reasons"]:
        print("expected evaluation feedback from the first attempt", file=sys.stderr)
        raise SystemExit(3)
    candidate = 2

current = Path("value.txt").read_text(encoding="utf-8").strip()
sys.stdout.write(f"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-{current}\n+{candidate}\n")
