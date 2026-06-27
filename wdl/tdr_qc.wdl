version 1.0

# Release QC via the TDR direct export (full export/import + verify): export a TDR snapshot directly
# from TDR (no ADE/Azul) to a NEW Analysis Workspace and QC it (AW vs snapshot + auth-domain). Runs
# `anvil-data-qc tdr-qc`. For very large datasets that fail to export via ADE/Azul.
#
# Uses the ambient Terra runtime service account throughout -- NO Azul token needed (unlike ade_qc.wdl),
# for open- AND controlled-access. Open-access by default; controlled-access via allow_controlled_access
# (same default-off opt-in as ade_qc.wdl). The runtime identity's own Terra access must cover controlled
# data (TDR /export 403s otherwise); the import applies the snapshot's auth domain (verified by the QC
# auth-domain check). tdrSyncPermissions stays false for all data -- see docs/tdr_export_flow.md.
#
# Side effects: creates a real, RETAINED Analysis Workspace and runs the import (minutes to ~1 hr). The
# task always succeeds and surfaces the result as outputs; the detailed log (with the QC report, stage
# durations, and the created workspace name) is an output. Config supplied as a File input.

workflow TdrQc {
    input {
        File config
        String snapshot_id
        String? tier
        Boolean dry_run = false
        Boolean allow_controlled_access = false
        String docker = "us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:0.2.1"
    }

    call tdr_qc {
        input:
            config = config,
            snapshot_id = snapshot_id,
            tier = tier,
            dry_run = dry_run,
            allow_controlled_access = allow_controlled_access,
            docker = docker
    }

    output {
        File qc_report = tdr_qc.qc_report
        File detailed_log = tdr_qc.detailed_log
        Int qc_exit_code = tdr_qc.qc_exit_code
        Boolean qc_passed = tdr_qc.qc_passed
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        snapshot_id: "TDR snapshot id to export and QC. Open-access unless allow_controlled_access=true."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        dry_run: "Validate the export side only (TDR snapshot export); create no workspace, no import."
        allow_controlled_access: "Opt in to controlled-access snapshots (any consentCode). Default false = open-access only. No Azul token needed."
        docker: "anvil-data-qc image reference, e.g. <registry>/anvil-data-qc:<tag>."
    }
}

task tdr_qc {
    input {
        File config
        String snapshot_id
        String? tier
        Boolean dry_run
        Boolean allow_controlled_access
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # Open-access by default; controlled-access via allow_controlled_access. The pipeline logs to
        # stderr; capture the exit code without failing the task. 0 = PASS, 1 = QC failure,
        # 2 = safety abort (e.g. a controlled-access snapshot without the opt-in).
        set +e
        anvil-data-qc tdr-qc \
            --config "~{config}" \
            --snapshot-id "~{snapshot_id}" \
            ~{"--tier " + tier} \
            ~{true="--dry-run" false="" dry_run} \
            ~{true="--allow-controlled-access" false="" allow_controlled_access} \
            2> detailed_log.txt
        echo "$?" > qc_exit_code.txt
        set -e

        # The pipeline logs the QC report into the detailed log; extract it as a clean report output.
        sed -n '/QC report:/,/Overall QC/p' detailed_log.txt > qc_report.txt || true
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
    }
}
