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
else:
    sys.exit("unexpected call: " + repr(sys.argv[1:]))
