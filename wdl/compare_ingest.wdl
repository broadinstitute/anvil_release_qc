version 1.0

# Ingestion QC: compare a submitter (ADSW) workspace's tables to the source TDR snapshot.
#
# Runs `anvil-data-qc compare-ingest` in the code-only anvil-data-qc Docker image. Credentials come
# from the ambient Terra runtime service account (ADC) -- its email must match the configured tier's
# email (the CLI identity guard). The per-tier config is supplied as a File input (set
# adc_credentials_file: null in it, so ADC falls back to that runtime identity). No Azul token needed.
#
# The task always succeeds and surfaces the QC result as outputs (qc_passed + qc_exit_code), so batch
# runs over many datasets always produce the report (failed Cromwell tasks delocalize no outputs).
# Gate on `qc_passed` downstream.

workflow CompareIngest {
    input {
        File config
        String submitter_workspace_namespace
        String submitter_workspace_name
        String snapshot_id
        String? tier
        String docker = "us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:0.3.1"
    }

    call compare_ingest {
        input:
            config = config,
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
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        submitter_workspace_namespace: "Submitter (ADSW) workspace namespace (Terra billing project)."
        submitter_workspace_name: "Submitter (ADSW) workspace name."
        snapshot_id: "TDR snapshot id (the system of record to compare against)."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        docker: "anvil-data-qc image reference, e.g. <registry>/anvil-data-qc:<tag>."
    }
}

task compare_ingest {
    input {
        File config
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
        # and batch runs can aggregate. 0 = PASS, 1 = QC failure, 2 = safety abort.
        set +e
        anvil-data-qc compare-ingest \
            --config "~{config}" \
            --submitter-workspace-namespace "~{submitter_workspace_namespace}" \
            --submitter-workspace-name "~{submitter_workspace_name}" \
            --snapshot-id "~{snapshot_id}" \
            ~{"--tier " + tier} \
            > qc_report.txt 2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e

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
    }
}
