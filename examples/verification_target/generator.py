import json
import sys
from pathlib import Path

context = json.load(sys.stdin)
diagnosis = context["diagnosis"]["failure_type"]
if diagnosis != "reasoning_failure":
    print(f"unsupported failure type: {diagnosis}", file=sys.stderr)
    raise SystemExit(2)

current = Path("value.txt").read_text(encoding="utf-8").strip()
sys.stdout.write(f"--- a/value.txt\n+++ b/value.txt\n@@ -1 +1 @@\n-{current}\n+2\n")
