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
    if sys.argv[-1] == "/usage":
        # Headless /usage as Claude Code 2.1.265 answers it: a JSON result
        # envelope whose "result" is the usage view. FAKE_CLAUDE_USAGE
        # overrides the text, FAKE_CLAUDE_TURNS the (normally zero) turn count.
        text = os.environ.get("FAKE_CLAUDE_USAGE",
                              "You are currently using your subscription to power your Claude Code usage\n\n"
                              "Current session: 18% used \u00b7 resets Sep 10 at 4:30pm (Europe/Paris)\n"
                              "Current week (all models): 26% used \u00b7 resets Sep 14 at 5am (Europe/Paris)\n"
                              "Current week (Fable): 49% used \u00b7 resets Sep 14 at 5am (Europe/Paris)\n\n"
                              "What's contributing to your limits usage?\n")
        print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "num_turns": int(os.environ.get("FAKE_CLAUDE_TURNS", "0")),
                          "total_cost_usd": 0, "result": text}))
    else:
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
    # set (a limit object or a whole rateLimits object), otherwise with a JSON-RPC error so the
    # caller sees an observation failure rather than a hang.
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if request.get("id") != 2:
            continue
        if os.environ.get("FAKE_CODEX_QUOTA"):
            # Either one limit object (served as "primary") or a whole rateLimits
            # object with "primary"/"secondary" slots.
            quota = json.loads(os.environ["FAKE_CODEX_QUOTA"])
            limits = quota if "primary" in quota else {"primary": quota}
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"rateLimits": limits}}), flush=True)
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "fixture has no quota api"}}), flush=True)
        break
else:
    sys.exit("unexpected call: " + repr(sys.argv[1:]))
