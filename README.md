# anvil-data-qc
Processes and tools for AnVIL Data Release quality control

## Developer setup

This repository is in early development; the steps below cover the tooling that exists today. See
[`CLAUDE.md`](CLAUDE.md) for the architecture and design context.

### 1. Secret-scanning hooks (do this before committing)

Install [git-secrets](https://github.com/awslabs/git-secrets), then run the setup script to install
the commit hooks and register the Google / Terra / AnVIL secret patterns:

```bash
brew install git-secrets          # macOS (see the git-secrets page for other platforms)
./scripts/setup-git-secrets.sh    # run once per clone; idempotent
```

This blocks accidental commits of tokens, JWTs, private keys, API keys, and unredacted signed-URL
signatures. git-secrets config is local to each clone, so every contributor must run this.

### 2. Capturing & scrubbing HTTP traffic

When capturing API flows (e.g. after a service API changes), follow
[`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md) and **always** scrub a capture
before sharing or storing it:

```bash
python scripts/scrub_har.py <capture>.har    # writes <capture>.scrubbed.har
```

Raw captures belong under `tmp/` (gitignored) and must never be committed.

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture, design decisions, and conventions.
- [`docs/services.md`](docs/services.md) — service registry (repos, OpenAPI specs, per-tier URLs).
- [`docs/export_import_flow.md`](docs/export_import_flow.md) — the resolved ADE→Terra export/import API flow.
- [`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md) — capturing & scrubbing HAR flows.
- [`docs/risk_register.md`](docs/risk_register.md) — tracked project risks.
