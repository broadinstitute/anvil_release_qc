version 1.0

# Release QC via the ADE/Azul export (full export/import + verify): export a TDR snapshot to a NEW
# Analysis Workspace and QC it (AW vs snapshot + auth-domain). Runs `anvil-data-qc ade-qc`.
#
# Open-access by default: the CLI consent gate blocks anything but consentCode == "NRES" (Azul queried
# anonymously). For CONTROLLED-ACCESS (security-team approved, all AnVIL data): set
# allow_controlled_access = true AND provide azul_token (a fresh Azul token file in a secure bucket) --
# Azul requires a token for controlled data.
#
# Side effects: creates a real, RETAINED Analysis Workspace and runs the import (minutes to ~1 hr). The
# task always succeeds and surfaces the result as outputs; the detailed log (with the QC report, stage
# durations, and the created workspace name) is an output. Credentials = the ambient Terra runtime
# service account; config supplied as a File input.

workflow AdeQc {
    input {
        File config
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run = false
        Boolean allow_controlled_access = false
        File? azul_token
        String docker = "us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:0.3.5"
    }

    call ade_qc {
        input:
            config = config,
            snapshot_id = snapshot_id,
            tier = tier,
            catalog = catalog,
            dry_run = dry_run,
            allow_controlled_access = allow_controlled_access,
            azul_token = azul_token,
            docker = docker
    }

    output {
        File qc_report = ade_qc.qc_report
        File detailed_log = ade_qc.detailed_log
        Int qc_exit_code = ade_qc.qc_exit_code
        Boolean qc_passed = ade_qc.qc_passed
        String run_began = ade_qc.run_began
        String run_ended = ade_qc.run_ended
        String workspace_namespace = ade_qc.workspace_namespace
        String workspace_name = ade_qc.workspace_name
        Float azul_manifest_minutes = ade_qc.azul_manifest_minutes
        Float workspace_create_minutes = ade_qc.workspace_create_minutes
        Float import_minutes = ade_qc.import_minutes
        Int total_table_count = ade_qc.total_table_count
        Int total_row_count = ade_qc.total_row_count
        Int total_value_count = ade_qc.total_value_count
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        snapshot_id: "TDR snapshot id to export and QC. Open-access unless allow_controlled_access=true."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        catalog: "Azul catalog (e.g. anvil15 in prod). Omit in dev."
        dry_run: "Validate the export side only (TDR -> BigQuery -> Azul); create no workspace, no import."
        allow_controlled_access: "Opt in to controlled-access snapshots (any consentCode). Default false = open-access only."
        azul_token: "Azul token file (secure bucket) for controlled-access; required when allow_controlled_access=true."
        docker: "anvil-data-qc image reference, e.g. <registry>/anvil-data-qc:<tag>."
    }
}

task ade_qc {
    input {
        File config
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run
        Boolean allow_controlled_access
        File? azul_token
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # Open-access by default; controlled-access requires allow_controlled_access=true + azul_token.
        # The pipeline logs to stderr; capture the exit code without failing the task. 0 = PASS,
        # 1 = QC failure, 2 = safety abort (e.g. a controlled snapshot without the opt-in) or a
        # preflight/config failure (e.g. a misconfigured billing_google_project_id).
        # Run start/end wall-clock (UTC, ISO 8601 to the second) bracketing the CLI invocation, surfaced
        # as data-table columns for QC management -- e.g. sort by run time, or spot rows QC'd before a
        # UCSC reindex/fix. Captured here (not in the pipeline) so they reflect true task wall-clock and
        # are present on every path (the task always completes; the CLI exit code is captured, not fatal).
        date -u '+%Y-%m-%dT%H:%M:%SZ' > run_began.txt
        set +e
        anvil-data-qc ade-qc \
            --config "~{config}" \
            --snapshot-id "~{snapshot_id}" \
            ~{"--tier " + tier} \
            ~{"--catalog " + catalog} \
            ~{true="--dry-run" false="" dry_run} \
            ~{true="--allow-controlled-access" false="" allow_controlled_access} \
            ~{"--azul-token-file " + azul_token} \
            2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e
        date -u '+%Y-%m-%dT%H:%M:%SZ' > run_ended.txt

        # The pipeline logs the QC report into the detailed log; extract it as a clean report output,
        # then append the end-of-run stage-timing summary line (minutes) so it rides with the report.
        sed -n '/QC report:/,/Overall QC/p' detailed_log.txt > qc_report.txt || true
        TIMINGS="$(grep -F 'Stage timings (minutes)' detailed_log.txt | tail -1 || true)"
        if [ -n "$TIMINGS" ]; then echo "$TIMINGS" >> qc_report.txt; fi

        # Per-stage durations as Float outputs, in MINUTES (the user-experienced delays). Parse them
        # from the summary line; a stage that did not run (e.g. a dry run) defaults to 0.
        for stage in azul_manifest workspace_create import; do
            echo "$TIMINGS" | grep -oE "${stage}=[0-9.]+" | cut -d= -f2 | head -1 > "${stage}_minutes.txt" || true
            [ -s "${stage}_minutes.txt" ] || echo 0 > "${stage}_minutes.txt"
        done

        # Data-volume metrics, logged before the export so they're present even if the import failed.
        # Append the line to the report and parse the counts (commas stripped) into Int outputs;
        # default 0 if absent (e.g. an auth/consent failure before BigQuery).
        VOLUME="$(grep -F 'Data volume (TDR snapshot)' detailed_log.txt | tail -1 || true)"
        if [ -n "$VOLUME" ]; then echo "$VOLUME" >> qc_report.txt; fi
        for metric in total_table_count total_row_count total_value_count; do
            echo "$VOLUME" | grep -oE "${metric}=[0-9,]+" | head -1 | cut -d= -f2 | tr -d ',' > "${metric}.txt" || true
            [ -s "${metric}.txt" ] || echo 0 > "${metric}.txt"
        done

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
        Int qc_exit_code = read_int("qc_exit_code.txt")
        Boolean qc_passed = read_int("qc_exit_code.txt") == 0
        # Run start/end wall-clock (UTC, ISO 8601, second granularity), captured around the CLI call.
        String run_began = read_string("run_began.txt")
        String run_ended = read_string("run_ended.txt")
        # The created Analysis Workspace (namespace/name); empty if none was created (dry run /
        # pre-import failure). Present even on a failed import (the retained workspace exists).
        String workspace_namespace = read_string("workspace_namespace.txt")
        String workspace_name = read_string("workspace_name.txt")
        # Per-stage durations in minutes (the user-experienced delays); 0 if the stage did not run.
        Float azul_manifest_minutes = read_float("azul_manifest_minutes.txt")
        Float workspace_create_minutes = read_float("workspace_create_minutes.txt")
        Float import_minutes = read_float("import_minutes.txt")
        # Snapshot data volume (non-system, non-empty tables); present even if the import failed.
        Int total_table_count = read_int("total_table_count.txt")
        Int total_row_count = read_int("total_row_count.txt")
        Int total_value_count = read_int("total_value_count.txt")
    }
}
