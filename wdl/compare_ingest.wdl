version 1.0

# Ingestion QC: compare a submitter (ADSW) workspace's tables to the source TDR snapshot.
#
# Runs `anvil-release-qc compare-ingest` in the code-only anvil-release-qc Docker image. Credentials come
# from the ambient Terra runtime service account (ADC). No config file -- billing_google_project_id
# defaults to the prod value (override it for dev). No Azul token needed.
#
# The task always succeeds and surfaces the QC result as outputs (qc_passed + qc_exit_code), so batch
# runs over many datasets always produce the report (failed Cromwell tasks delocalize no outputs).
# Gate on `qc_passed` downstream.

workflow CompareIngest {
    input {
        String billing_google_project_id = "terra-ec38e08d"
        String submitter_workspace_namespace
        String submitter_workspace_name
        String snapshot_id
        String? tier
        String docker = "us-central1-docker.pkg.dev/operations-portal-427515/anvil-qc/anvil-release-qc:0.3.7"
    }

    call compare_ingest {
        input:
            billing_google_project_id = billing_google_project_id,
            submitter_workspace_namespace = submitter_workspace_namespace,
            submitter_workspace_name = submitter_workspace_name,
            snapshot_id = snapshot_id,
            tier = tier,
            docker = docker
    }

    output {
        File qc_report = compare_ingest.qc_report
        File detailed_log = compare_ingest.detailed_log
        Int qc_exit_code = compare_ingest.qc_exit_code
        Boolean qc_passed = compare_ingest.qc_passed
        String run_began = compare_ingest.run_began
        String run_ended = compare_ingest.run_ended
    }

    parameter_meta {
        billing_google_project_id: "Google Cloud project id that BigQuery jobs run in / are billed to. Default terra-ec38e08d (prod); override for dev."
        submitter_workspace_namespace: "Submitter (ADSW) workspace namespace (Terra billing project)."
        submitter_workspace_name: "Submitter (ADSW) workspace name."
        snapshot_id: "TDR snapshot id (the system of record to compare against)."
        tier: "Tier (dev|prod). Optional; defaults to prod."
        docker: "anvil-release-qc image reference, e.g. <registry>/anvil-release-qc:<tag>."
    }
}

task compare_ingest {
    input {
        String billing_google_project_id
        String submitter_workspace_namespace
        String submitter_workspace_name
        String snapshot_id
        String? tier
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # Run the ingestion QC. stdout -> the report; stderr -> the detailed request/response log.
        # Capture the exit code without failing the task (set +e), so the report is always delocalized
        # and batch runs can aggregate. 0 = PASS, 1 = QC failure, 2 = safety abort or a preflight
        # failure (e.g. a misconfigured billing_google_project_id).
        # Run start/end wall-clock (UTC, ISO 8601 to the second) bracketing the CLI invocation, surfaced
        # as data-table columns for QC management (sort by run time / track when each check last ran).
        # Captured here (not in the pipeline) so they reflect true task wall-clock and are present on
        # every path (the task always completes; the CLI exit code is captured, not fatal).
        date -u '+%Y-%m-%dT%H:%M:%SZ' > run_began.txt
        set +e
        anvil-release-qc compare-ingest \
            --billing-google-project-id "~{billing_google_project_id}" \
            --submitter-workspace-namespace "~{submitter_workspace_namespace}" \
            --submitter-workspace-name "~{submitter_workspace_name}" \
            --snapshot-id "~{snapshot_id}" \
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
