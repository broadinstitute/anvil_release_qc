version 1.0

# Release QC (standalone): compare an existing Analysis Workspace's data tables to the source TDR
# snapshot (table parity + row/column counts + auth-domain), without re-importing. Runs
# `anvil-release-qc compare` -- useful to re-QC a workspace a prior `qc` run created.
#
# Credentials: the ambient Terra runtime service account (ADC); no config file --
# billing_google_project_id defaults to the prod value (override it for dev). The task always succeeds and
# surfaces the result as outputs (qc_passed + qc_exit_code) so batch runs always produce the report.

workflow Compare {
    input {
        String billing_google_project_id = "terra-ec38e08d"
        String workspace_namespace
        String workspace_name
        String snapshot_id
        String? tier
        String docker = "us-central1-docker.pkg.dev/operations-portal-427515/anvil-qc/anvil-release-qc:0.3.7"
    }

    call compare {
        input:
            billing_google_project_id = billing_google_project_id,
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
        String run_began = compare.run_began
        String run_ended = compare.run_ended
    }

    parameter_meta {
        billing_google_project_id: "Google Cloud project id that BigQuery jobs run in / are billed to. Default terra-ec38e08d (prod); override for dev."
        workspace_namespace: "Analysis Workspace namespace (Terra billing project)."
        workspace_name: "Analysis Workspace name."
        snapshot_id: "TDR snapshot id (the source of truth)."
        tier: "Tier (dev|prod). Optional; defaults to prod."
        docker: "anvil-release-qc image reference, e.g. <registry>/anvil-release-qc:<tag>."
    }
}

task compare {
    input {
        String billing_google_project_id
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
        # without failing the task. 0 = PASS, 1 = QC failure, 2 = safety abort or a preflight
        # failure (e.g. a misconfigured billing_google_project_id).
        # Run start/end wall-clock (UTC, ISO 8601 to the second) bracketing the CLI invocation, surfaced
        # as data-table columns for QC management (sort by run time / track when each check last ran).
        # Captured here (not in the pipeline) so they reflect true task wall-clock and are present on
        # every path (the task always completes; the CLI exit code is captured, not fatal).
        date -u '+%Y-%m-%dT%H:%M:%SZ' > run_began.txt
        set +e
        anvil-release-qc compare \
            --billing-google-project-id "~{billing_google_project_id}" \
            --snapshot-id "~{snapshot_id}" \
            --workspace-namespace "~{workspace_namespace}" \
            --workspace-name "~{workspace_name}" \
            ~{"--tier " + tier} \
            > qc_report.txt 2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e
        date -u '+%Y-%m-%dT%H:%M:%SZ' > run_ended.txt

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
        # Run start/end wall-clock (UTC, ISO 8601, second granularity), captured around the CLI call.
        String run_began = read_string("run_began.txt")
        String run_ended = read_string("run_ended.txt")
    }
}
