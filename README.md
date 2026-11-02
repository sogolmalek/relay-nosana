# relay-nosana

Relay's verifiable lane on Nosana: measure which open models can do a piece of AI work, run the work that needs proof on Nosana, and give every result a receipt that anyone can check against Solana and IPFS.

**Status: work in progress, built for a Nosana grant application.**

- **Demo (mockup, simulated data):** https://sogolmalek.github.io/relay-nosana/demo/
- **Built so far:** the measurement pipeline on a real backlog (huggingface/transformers, 19,741 issues). It covers ingest, ground truth for three task types with leakage guards, task compilation, calibration, routing, a ledger and reports. 99 tests pass.
- **Next:** first runs on Nosana, receipts anchored to Nosana job records, and an open-source verifier.

The full build spec is in [SPEC.md](SPEC.md).

## Run the tests

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```
