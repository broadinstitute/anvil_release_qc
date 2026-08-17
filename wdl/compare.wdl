version 1.0

# Release QC (standalone): compare an existing Analysis Workspace's data tables to the source TDR
# snapshot (table parity + row/column counts + auth-domain), without re-importing. Runs
# `anvil-data-qc compare` -- useful to re-QC a workspace a prior `qc` run created.
#
# Credentials: the ambient Terra runtime service account (ADC), whose email must match the configured
# tier email (the CLI identity guard); config supplied as a File input. The task always succeeds and
# surfaces the result as outputs (qc_passed + qc_exit_code) so batch runs always produce the report.

workflow Compare {
    input {
        File config
        String workspace_namespace
        String workspace_name
        String snapshot_id
        String? tier
        String docker = "us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:0.3.3"
    }

    call compare {
        input:
            config = config,
            workspace_namespace = workspace_namespace,
            workspace_name = workspace_name,
            snapshot_id = snapshot_id,
            tier = tier,
            docker = docker
    }

    output {
        File qc_report = compare.qc_report
        File detailed_log = compare.detailed_log
        Int qc_exit_code = compare.qc_exit_code
        Boolean qc_passed = compare.qc_passed
        Int total_table_count = compare.total_table_count
        Int total_row_count = compare.total_row_count
        Int total_value_count = compare.total_value_count
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        workspace_namespace: "Analysis Workspace namespace (Terra billing project)."
        workspace_name: "Analysis Workspace name."
        snapshot_id: "TDR snapshot id (the source of truth)."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        docker: "anvil-data-qc image reference, e.g. <registry>/anvil-data-qc:<tag>."
    }
}

task compare {
    input {
        File config
        String workspace_namespace
        String workspace_name
        String snapshot_id
        String? tier
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # stdout -> the report; stderr -> the detailed request/response log. Capture the exit code
        # without failing the task. 0 = PASS, 1 = QC failure, 2 = safety abort or a preflight/config
        # failure (e.g. a misconfigured billing_google_project_id).
        set +e
        anvil-data-qc compare \
            --config "~{config}" \
            --snapshot-id "~{snapshot_id}" \
            --workspace-namespace "~{workspace_namespace}" \
            --workspace-name "~{workspace_name}" \
            ~{"--tier " + tier} \
            > qc_report.txt 2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e

        # Data-volume counts: the `compare` command echoes the line into the report (stdout). Parse the
        # counts (commas stripped) into Int outputs; default 0 if absent.
        VOLUME="$(grep -F 'Data volume (TDR snapshot)' qc_report.txt | tail -1 || true)"
        for metric in total_table_count total_row_count total_value_count; do
            echo "$VOLUME" | grep -oE "${metric}=[0-9,]+" | head -1 | cut -d= -f2 | tr -d ',' > "${metric}.txt" || true
            [ -s "${metric}.txt" ] || echo 0 > "${metric}.txt"
        done

        cat qc_report.txt  # also surface the report in the Cromwell task stdout
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
        # Snapshot data volume (non-system, non-empty tables) — useful for the post-import-timeout re-check.
        Int total_table_count = read_int("total_table_count.txt")
        Int total_row_count = read_int("total_row_count.txt")
        Int total_value_count = read_int("total_value_count.txt")
    }
}
