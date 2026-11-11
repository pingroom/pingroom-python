# Golden requests

`scenarios.json` lists one call per public method (and several awkward inputs: quotes, control characters, emoji, `/` in ids, empty and padded idempotency keys). `ts-requests.json` is what the TypeScript SDK (`@pingroom/sdk`) sent for each, recorded by `generate.mjs`. `tests/test_golden.py` replays every scenario through this SDK, blocking and asynchronous, and requires the same method, path with query, headers (`accept`, `authorization`, `content-type`, `idempotency-key`) and byte-identical JSON body.

Only the `User-Agent` differs, on purpose (`pingroom-python/<version>`).

## Refreshing

When the TypeScript SDK changes a request, or a scenario is added to `scenarios.json`:

```sh
cd path/to/typescript-sdk && npm run build
cd path/to/pingroom-python
PINGROOM_TS_SDK=path/to/typescript-sdk/dist/index.js node tests/golden/generate.mjs
pytest tests/test_golden.py
```

`generate.mjs` needs a built checkout of the TypeScript SDK, which this repository does not contain. `ts-requests.json` records the SDK version it was generated from.

Adding a public method here means adding a scenario for it: `test_every_public_method_in_scope_has_a_golden_scenario` fails until there is one. The multipart attachment upload is the exception, because the two HTTP stacks write multipart bodies differently; `tests/test_metrics_attachments.py` covers it.
