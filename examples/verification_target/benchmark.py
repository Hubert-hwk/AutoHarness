import json
from pathlib import Path

score = float(Path("value.txt").read_text(encoding="utf-8").strip())
Path(".autoharness-metrics.json").write_text(
    json.dumps({"score": score}),
    encoding="utf-8",
)
