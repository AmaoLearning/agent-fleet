# Harbor Ante Structure

```text
Agents/Harbor-ante/
├── __init__.py
├── ante_harbor.py       # Harbor 0.18 adapter and event-to-context bridge
├── ante_events.py       # JSON event parsing, metrics, failure classes, ATIF
├── prepare_runtime.sh   # exact manifest download and runner cache
├── README.md
└── tests/
    └── test_ante_harbor.py
```

The shared runner selects `ante_harbor:AnteAgent` with
`HARBOR_AGENT_IMPORT_PATH`. `prepare_runtime.sh` materializes the exact binary
before workers start. `ante_harbor.py` uploads it through the configured Harbor
environment, which lets YiCloud OpenSandbox reuse the existing S3 transport.
