version 1.0

# Release QC via the TDR direct export (full export/import + verify): export a TDR snapshot directly
# from TDR (no ADE/Azul) to a NEW Analysis Workspace and QC it (AW vs snapshot + auth-domain). Runs
# `anvil-data-qc tdr-qc`. For very large datasets that fail to export via ADE/Azul.
#
# Uses the ambient Terra runtime service account throughout -- NO Azul token needed (unlike ade_qc.wdl).
# OPEN-ACCESS ONLY for now: the CLI consent gate blocks anything but consentCode == "NRES"; controlled-
# access tdr-qc is not yet enabled (pending confirmation of the importJob tdrSyncPermissions / auth-
# domain behavior for controlled data). allow_controlled_access is accepted for parity but still blocks.
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
        snapshot_id: "TDR snapshot id to export and QC. Open-access only for now."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        dry_run: "Validate the export side only (TDR snapshot export); create no workspace, no import."
        allow_controlled_access: "Accepted for parity, but controlled-access tdr-qc is not yet enabled (open-access only)."
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

        # Open-access only for now (controlled-access tdr-qc not yet enabled). The pipeline logs to
        # stderr; capture the exit code without failing the task. 0 = PASS, 1 = QC failure,
        # 2 = safety abort (e.g. a controlled-access snapshot, which tdr-qc blocks).
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
