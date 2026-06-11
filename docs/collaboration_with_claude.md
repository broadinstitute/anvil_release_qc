Hi Claude!
Welcome to this new project repo!
I am am excited to be working with you on this new project!
It is a substantial project that will be used for years to come, if we do a good job, which I am confident we will!
It will be enhanced over time, by myself and others, so clarity, ease of use, and maintainability are important.

It is a big project, so I will provide context before working on plans, designs, and implementation.

High-Level Overview
The domain is data management for biomedical datasets, where each dataset includes relational tabular data and cloud blobs.
Here is a Mermaid overview of the high-level flow.
---
sequenceDiagram
  participant ADSW as AnVIL Data Submittal Workspace
  participant TDR as TDR Dataset and Snapshot
  participant ADE as AnVIL Data Explorer
  participant AW as Analysis Workspace
  %% One consistent annotation per participant
  Note over ADSW: Terra workspace (dataset-specific) containing tabular + file data
  Note over TDR: Dataset-specific; contains tabular + file data
  Note over ADE: AnVIL-wide indexing service with research web UI
  Note over AW: Researcher Terra workspace; tabular data + DRS references to cloud blobs

  ADSW->>TDR: Data ingestion process
  TDR->>ADE: Indexing of TDR AnVIL datasets
  ADE->>AW: PFB export (dataset tabular data + GA4GH DRS references)
---
The focus will be quality control of individual datasets as they are released.
There are hundreds of datasets, so thourogh automation is important/necessary.
The initial/primary focus is as follows, for a each single, given dataset:
1) API-based export a given dataset from the AnVIL Data Explorer and API-based import to an Analysis Workspace
2) Comparing the tabular data in the Terra Analysis Workspace data tables to the TDR snapshot which is the source of the dataset. Both should have the same tables, columns, row counts, etc.
There are many more details and specifics that will be provided as we work through the plan and design.

We will need to discuss high-level implementation approaches.
I REALLY like the approach being taken in the drs-test-notebooks repo you are helping me with in another session.
The patterns used there, and the use of Notebooks for interactive development, troubleshooting, etc. is great.
Yet, another objective of this project is to be able to deploy the functionality of this repo as a Docker image and run it in a Terra workflows using a WDL.
I would like your recommendations on tools to use (e.g., pure Python, Jupyter Python notebooks, both, or something else)

I have rather deep domain knowledge in each of these services and their APIs, which should help with effective collaboration.
The main exception to that is the use of SQL with BigQuery, to query the tabular data from the TDR snapshot for comparison purposes.
I trust you can help me with that.

I'll provide resources, including Swagger files for each of the services as we proceed.

The initial/key functionality must be developed on a rather short timeframe.
I would like to focus on getting and end-to-end flow working quickly, then refine each phase as needed over time.
I want to derisk this first, with the biggest risk being the ability to automate the export from the AnVIL Data Explorer to the Terra Analysis workspace.
That system flow itself is already solidly and reliably implemented, but the the flow is normally performed interactively.
I think all the APIs neded to do that are available in the AnVIL Data Explorer Azul service, and the Terra services such as Firecloud Orchestation and Rawls.
I expect there will be details that we will need to address as we work through that.

What do you think? Can/will you help me with this?

===
AnVIL Data Explorer, Azul Service:
GitHub Repo: https://github.com/DataBiosphere/azul
Swagger spec: https://raw.githubusercontent.com/DataBiosphere/azul/refs/heads/develop/lambdas/service/openapi.json
Dev deployment URL: https://service.anvil.gi.ucsc.edu/
Prod deployment URL: https://service.explore.anvilproject.org/

Terra Firecloud Orchestration Service:
GitHub Repo: https://github.com/broadinstitute/firecloud-orchestration
Swagger spec: https://raw.githubusercontent.com/broadinstitute/firecloud-orchestration/refs/heads/develop/src/main/resources/swagger/api-docs.yaml
Dev deployment URL: https://firecloud-orchestration.dsde-dev.broadinstitute.org/
Prod deployment URL: https://api.firecloud.org/

Terra Rawls Service:
GitHub Repo: https://github.com/broadinstitute/rawls
Swagger spec: https://raw.githubusercontent.com/broadinstitute/rawls/refs/heads/develop/core/src/main/resources/swagger/api-docs.yaml
Dev deployment URL: https://rawls.dsde-dev.broadinstitute.org/
Prod deployment URL: https://rawls.dsde-prod.broadinstitute.org/

Terra Sam (Auth) Service:
GitHub Repo: https://github.com/broadinstitute/sam
Swagger spec: https://raw.githubusercontent.com/broadinstitute/sam/refs/heads/develop/src/main/resources/swagger/api-docs.yaml
  - Note: Sam's spec is under `src/main/...` (no `core/` prefix). Rawls (above) uses `core/src/main/...` — the two repos differ, so do not add `core/` to the Sam URL.
Dev deployment URL: https://sam.dsde-dev.broadinstitute.org/
Prod deployment URL: https://sam.dsde-prod.broadinstitute.org/

Terra Data Repository Service:
GitHub Repo: https://github.com/DataBiosphere/jade-data-repo
Swagger spec: https://raw.githubusercontent.com/DataBiosphere/jade-data-repo/refs/heads/develop/src/main/resources/api/data-repository-openapi.yaml
Dev deployment URL: https://jade.datarepo-dev.broadinstitute.org/
Prod deployment URL: https://data.terra.bio/

Note that for some, but not all, of the Terra services, the service URL is the same except for substitutable values ("dev", "prod") indicating the deployment tier.

---
Let's talk more before proceeding. Please request explicit approval from be before proceeding to ❯ Scaffold the repo skeleton, etc.

Development will be done on my local system (macOS 15.7), which has access to all service APIs, both in dev and production.

Use auth can be based on Google ADC, both on local system and when this is deployed in a Docker and run in Terra workflows.
The Google user identities to be used in each tier are different.
Dev: mbaumann@test.firecloud.org
Prod: sa-mbaumann@broadinstitute.org
Google access tokens should be used for both the ADE/Azul and Terra services.
The same user credentials are used for both/all services in a given tier.
Other developers may be working with this repo in the future, so it should be easy for a given developer to set their Google account ids/email addreses to be used in each tier.

The dataset is best identified by its TDR Snapshot ID. This will identify the dataset in TDR, and I belive the TDR Snapshot ID can be used as an Azul filter criteria to obtain the PFB manifest (via a signed URL the Azul service will provide) which is what is the export payload that will be imported into the Terra Analysis Workspace via its APIs.
---

Scaffold the repo skeleton and start the export spike

---
> My hypothesis: AnVIL enforces file access at the DRS layer (the
PFB carries DRS URIs needing RAS/passport auth — your drs-test-notebooks world), so the workspace needs no auth domain; the tabular data is just metadata.

The PFB does contain DRS URIs, and those do require auth to access the file data.
BUT THE TABULAR/METADATA IS SENSITIVE CONTROLLED ACCESS DATA ALSO, and must be protected by an Auth Domain.

The resulting workspace DID end up with an Authorization Domain on it (AUTH_ANVIL_CCDG_Broad_AI_IBD_Kugathasan_WGS).

My understanding is that the PFB import process inspects the PFB internally, locates the snapshot id it contains, accesses the Authorization Domain(s) on the snapshot, and sets those on the workspace also. I think the setting of the Authorization Domain on the workspace is automatically performed, as a privileged operation, after the workspace is created.

---

Regarding:
QC (this is where it gets valuable): because the metadata is controlled-access, verifying it ended up correctly protected is itself a core QC check (your risk R2). So the Sam authDomain endpoint you found earlier isn't dead — it's repurposed
  as a verifier:
  - expected ← GET sam …/datasnapshot/{snapshotId}/authDomain
  - actual ← GET rawls …/workspaces/{ns}/{name} → workspace.authorizationDomain[].membersGroupName
  - assert expected == actual after the import completes. A mismatch (or an empty auth domain on a controlled dataset) is a QC failure — exactly the kind of compliance gap worth catching automatically.
  
  One timing note: the auth domain is applied asynchronously by the import, so our verify step must run after the import job reaches a terminal state (we're polling it anyway).

Yes, that's exactly right. Verifying the workspace Auth Domain(s) match the source snapshot's Auth Domain(s) is a critically important verification. One that I have done manually to date, and will be great to have automated support for soon. :-)
And, yes, the QC effort must wait until the import is finished and verify that it completed successfully.


  To run it locally (your machine — needs the 3.13 venv + network)

  pyenv install 3.13.5 && pyenv local 3.13.5     # if not already available
  python -m venv .venv && source .venv/bin/activate
  pip install -e ".[dev]"                         # installs deps + the package (editable)
  pip-compile pyproject.toml -o requirements.txt  # optional: generate the pinned runtime lock
  pytest                                           # runs test_config (should pass)
  cp config/config.example.yaml config/config.yaml # then fill in your tier values
  gcloud auth application-default login            # per tier; save creds to the configured paths
  anvil-data-qc qc --snapshot-id <tdr-snapshot-id> --tier dev
  That last command will run config → logging → identity guard, then stop at the not-yet-built export step — confirming the foundations work before we wire the flow.   