"""Frontend freeze guard.

    python qa/freeze_check.py --record     # store sha256 of every file under nosh/static (the finalized UI)
    python qa/freeze_check.py              # verify nothing under nosh/static changed; exit code 1 if it did

The UI is finalized by the owner: the QA work below may change backend code only.
"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "nosh", "static")
STORE = os.path.join(ROOT, "qa", "baseline", "frontend_hashes.json")


def snapshot():
    out = {}
    for base, _, files in os.walk(STATIC):
        for f in sorted(files):
            p = os.path.join(base, f)
            rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
            out[rel] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


if __name__ == "__main__":
    now = snapshot()
    if "--record" in sys.argv:
        os.makedirs(os.path.dirname(STORE), exist_ok=True)
        json.dump(now, open(STORE, "w"), indent=1)
        print(f"recorded {len(now)} frontend files")
        sys.exit(0)
    base = json.load(open(STORE))
    changed = [k for k in base if now.get(k) != base[k]]
    added = [k for k in now if k not in base]
    if changed or added:
        print("FRONTEND CHANGED:", changed, "added:", added)
        sys.exit(1)
    print(f"OK - all {len(base)} frontend files are byte-identical to the baseline")
