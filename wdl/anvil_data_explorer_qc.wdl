version 1.0

# Release QC via the ADE/Azul export (full export/import + verify): export a TDR snapshot to a NEW
# Analysis Workspace and QC it (AW vs snapshot + auth-domain). Runs `anvil-release-qc ade-qc`.
#
# Open-access by default: the CLI consent gate blocks anything but consentCode == "NRES" (Azul queried
# anonymously). For CONTROLLED-ACCESS (security-team approved, all AnVIL data): set
# allow_controlled_access = true AND have a fresh Azul token at
# gs://<azul_token_bucket>/azul_token/<username>.txt (<username> = the submitter's Terra email before the
# @; upload it with `anvil-release-qc azul-login`) -- Azul requires a token for controlled data.
#
# Side effects: creates a real, RETAINED Analysis Workspace and runs the import (minutes to ~1 hr). The
# task always succeeds and surfaces the result as outputs; the detailed log (with the QC report, stage
# durations, and the created workspace name) is an output. Credentials = the ambient Terra runtime
# service account; no config file -- billing_project / billing_google_project_id default to the prod
# values (override them for dev).

workflow AnvilDataExplorerQc {
    input {
        String billing_project = "anvil-developer-work"
        String billing_google_project_id = "terra-ec38e08d"
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run = false
        Boolean allow_controlled_access = false
        Boolean keep_workspace = false
        String azul_token_bucket = "fc-secure-886bea29-8c4e-4568-920a-a8cfee18e659"
        String docker = "us-central1-docker.pkg.dev/operations-portal-427515/anvil-qc/anvil-release-qc:0.3.6"
    }

    call anvil_data_explorer_qc {
        input:
            billing_project = billing_project,
            billing_google_project_id = billing_google_project_id,
            snapshot_id = snapshot_id,
            tier = tier,
            catalog = catalog,
            dry_run = dry_run,
            allow_controlled_access = allow_controlled_access,
            keep_workspace = keep_workspace,
            azul_token_bucket = azul_token_bucket,
            docker = docker
    }

    output {
        File qc_report = anvil_data_explorer_qc.qc_report
        File detailed_log = anvil_data_explorer_qc.detailed_log
        Boolean qc_passed = anvil_data_explorer_qc.qc_passed
        String workspace_namespace = anvil_data_explorer_qc.workspace_namespace
        String workspace_name = anvil_data_explorer_qc.workspace_name
    }

    parameter_meta {
        billing_project: "Terra billing project (workspace namespace) for the created QC workspace. Default anvil-developer-work (prod); override for dev."
        billing_google_project_id: "Google Cloud project id that BigQuery jobs run in / are billed to. Default terra-ec38e08d (prod); override for dev."
        snapshot_id: "TDR snapshot id to export and QC. Open-access unless allow_controlled_access=true."
        tier: "Tier (dev|prod). Optional; defaults to prod."
        catalog: "Azul catalog (e.g. anvil15 in prod). Omit in dev."
        dry_run: "Validate the export side only (TDR -> BigQuery -> Azul); create no workspace, no import."
        allow_controlled_access: "Opt in to controlled-access snapshots (any consentCode). Default false = open-access only."
        keep_workspace: "Keep the created QC workspace even when QC passes. Default false = it is deleted after a passing QC (always retained on a QC failure or failed import)."
        azul_token_bucket: "GCS bucket holding each operator's Azul token at azul_token/<username>.txt (the submitter's Terra email before the @). Needed for controlled-access; a missing token = open-access only."
        docker: "anvil-release-qc image reference, e.g. <registry>/anvil-release-qc:<tag>."
    }
}

task anvil_data_explorer_qc {
    input {
        String billing_project
        String billing_google_project_id
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run
        Boolean allow_controlled_access
        Boolean keep_workspace
        String azul_token_bucket
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # Open-access by default; controlled-access requires allow_controlled_access=true + the
        # submitter's Azul token in azul_token_bucket.
        # The pipeline logs to stderr; capture the exit code without failing the task. 0 = PASS,
        # 1 = QC failure, 2 = safety abort (e.g. a controlled snapshot without the opt-in) or a
        # preflight failure (e.g. a misconfigured billing_google_project_id).
        set +e
        anvil-release-qc ade-qc \
            --billing-project "~{billing_project}" \
            --billing-google-project-id "~{billing_google_project_id}" \
            --snapshot-id "~{snapshot_id}" \
            ~{"--tier " + tier} \
            ~{"--catalog " + catalog} \
            ~{true="--dry-run" false="" dry_run} \
            ~{true="--allow-controlled-access" false="" allow_controlled_access} \
            ~{true="--keep-workspace" false="" keep_workspace} \
            --azul-token-bucket "~{azul_token_bucket}" \
            2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e

        # The pipeline logs the QC report into the detailed log; extract it as a clean report output,
        # then append the end-of-run stage-timing summary line (minutes) so it rides with the report.
        sed -n '/QC report:/,/Overall QC/p' detailed_log.txt > qc_report.txt || true
        TIMINGS="$(grep -F 'Stage timings (minutes)' detailed_log.txt | tail -1 || true)"
        if [ -n "$TIMINGS" ]; then echo "$TIMINGS" >> qc_report.txt; fi

        # Data-volume line, logged before the export so it is present even if the import failed; append it
        # to the report.
        VOLUME="$(grep -F 'Data volume (TDR snapshot)' detailed_log.txt | tail -1 || true)"
        if [ -n "$VOLUME" ]; then echo "$VOLUME" >> qc_report.txt; fi

        # Created Analysis Workspace (namespace/name), logged by the pipeline BEFORE the import so it is
        # present even if the import failed (the retained workspace exists) -- for a later `compare`,
        # cross-operator access, or cleanup. Empty if no workspace was created (dry run / pre-import
        # failure). namespace + name are [A-Za-z0-9_-] (no spaces/slashes), so the single '/' splits them.
        WS="$(grep -F 'QC workspace:' detailed_log.txt | tail -1 | sed -E 's#.*QC workspace: ##' || true)"
        printf '%s' "${WS%%/*}" > workspace_namespace.txt
        printf '%s' "${WS#*/}"  > workspace_name.txt

        cat detailed_log.txt  # surface the full log in the Cromwell task stdout
    >>>

    runtime {
        docker: docker
        cpu: cpu
        memory: "~{memory_gb} GB"
        disks: "local-disk ~{disk_gb} HDD"
    }

    output {
        File qc_report = "qc_report.txt"
        File detailed_log = "detailed_log.txt"
        Boolean qc_passed = read_int("qc_exit_code.txt") == 0
        # The created Analysis Workspace (namespace/name); empty if none was created (dry run /
        # pre-import failure). Present even on a failed import (the retained workspace exists).
        String workspace_namespace = read_string("workspace_namespace.txt")
        String workspace_name = read_string("workspace_name.txt")
    }
}
