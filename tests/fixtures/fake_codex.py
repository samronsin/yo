#!/usr/bin/env python3
"""Offline CLI double for runner integration tests."""
import json
import os
from pathlib import Path
import sys

if sys.argv[1:] == ["--version"]:
    print("codex-cli test-version")
elif sys.argv[1] == "--print":
    Path(os.environ["FAKE_CODEX_ARGV"]).write_text(json.dumps(sys.argv[1:]))
    print("391")
    sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
elif sys.argv[1] == "exec":
    Path(os.environ["FAKE_CODEX_ARGV"]).write_text(json.dumps(sys.argv[1:]))
    output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    output.write_text("391\n")
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}}))
    sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
elif sys.argv[1] == "app-server":
    # Quota reads: answer the rateLimits request (id 2) from FAKE_CODEX_QUOTA when
    # set (a JSON "primary" object), otherwise with a JSON-RPC error so the
    # caller sees an observation failure rather than a hang.
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("id") != 2:
            continue
        if os.environ.get("FAKE_CODEX_QUOTA"):
            primary = json.loads(os.environ["FAKE_CODEX_QUOTA"])
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": {"primary": primary}}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "fixture has no quota api"}}), flush=True)
        break
else:
    sys.exit("unexpected call: " + repr(sys.argv[1:]))
