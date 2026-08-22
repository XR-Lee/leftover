"""Stands in for a headless CLI (`claude -p --output-format json` style)."""
import json
import os
import sys

if os.environ.get("FAKE_BEHAVIOR") == "structured_error":
    print(json.dumps({
        "type": "error",
        "message": "Couldn't create session: Permission denied.",
        "code": "FS_PERMISSION_DENIED",
    }))
    raise SystemExit(0)

prompt = sys.argv[-1] if len(sys.argv) > 1 else sys.stdin.read()
tail = prompt.strip().splitlines()[-1][:60] if prompt.strip() else "(empty)"
print(json.dumps({"result": f"exec reply to: {tail}", "cost_usd": 0}))
