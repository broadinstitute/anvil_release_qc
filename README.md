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

## Configuration & running

The flow is **not yet validated end-to-end for controlled-access** (see the risk register and the
limitations in the flow doc). These are the steps the tooling supports today.

1. **Config.** Copy the template and fill in your per-tier values (gitignored):

   ```bash
   cp config/config.example.yaml config/config.yaml
   ```

   Each tier needs `{email, adc_credentials_file, billing_project, azul_token_file}`.

2. **Google sign-in** (serves TDR, BigQuery, and Terra). A plain ADC login per tier; the active
   account must match that tier's configured `email` (enforced at runtime):

   ```bash
   gcloud auth application-default login
   ```

3. **Azul sign-in** (for controlled-access datasets). Azul needs a token issued to *its own* OAuth
   client — a gcloud token is rejected. Capture one interactively: this opens the Azul Swagger UI, you
   sign in and copy the token, and it is validated (`azp`, account, expiry) and stored for the tier.

   ```bash
   anvil-data-qc azul-login --tier dev
   ```

   Re-run when it expires (~1 h). Open-access datasets need no Azul token (Azul is called anonymously).

4. **Run QC** for one TDR snapshot:

   ```bash
   anvil-data-qc qc --snapshot-id <tdr-snapshot-id> --tier dev
   # add --dry-run to validate the export side only (no workspace created, no import run)
   ```

   The pipeline ends by comparing the new workspace's data tables to the snapshot (table parity, row
   and column counts) and verifying the auth domain. To QC an **existing** workspace without
   re-importing:

   ```bash
   anvil-data-qc compare --snapshot-id <id> \
     --workspace-namespace <billing-project> --workspace-name <name> --tier dev
   ```

See [`docs/export_import_flow.md`](docs/export_import_flow.md) → Authentication for why two tokens are
needed, and [`docs/comparison.md`](docs/comparison.md) for what the comparison checks (and its limits).

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — architecture, design decisions, and conventions.
- [`docs/services.md`](docs/services.md) — service registry (repos, OpenAPI specs, per-tier URLs).
- [`docs/export_import_flow.md`](docs/export_import_flow.md) — the resolved ADE→Terra export/import API flow.
- [`docs/comparison.md`](docs/comparison.md) — the AW↔snapshot table comparison (QC) and its limitations.
- [`docs/capturing_http_traffic.md`](docs/capturing_http_traffic.md) — capturing & scrubbing HAR flows.
- [`docs/risk_register.md`](docs/risk_register.md) — tracked project risks.
