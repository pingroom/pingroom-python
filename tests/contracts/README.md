# Contract fixtures

These four files are copies of the PingRoom cross-repo contracts in `docs/contracts/` of the PingRoom workspace. Laravel's tests write them from the real server code, and each SDK replays them, so a rule that drifts fails a test here instead of surfacing as a 422 in production.

| File | What it pins here |
| --- | --- |
| `structured-ping-vectors.json` | The `data` size and key-count rules (8 KB as PHP's `json_encode` measures it, 25 keys), the `Idempotency-Key` rules and the link button label length |
| `text-limits.json` | Ping message and title limits |
| `ping-actions.json` | Button limits, reserved labels, label and URL verdicts, and the per-Ping signing-secret vector |
| `live-templates.json` | The live-status template list, per-template fields and limits |

Do not edit them by hand. To refresh, copy the four files over from the workspace's `docs/contracts/` after Laravel regenerates them (each file names its own regeneration command), then run `pytest tests/test_contracts.py`.
