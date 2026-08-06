import sys

print("simulated provider outage", file=sys.stderr)
raise SystemExit(2)
