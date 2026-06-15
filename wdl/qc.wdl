version 1.0

# Release QC (full export/import + verify): export a TDR snapshot to a NEW Analysis Workspace and QC
# it (AW vs snapshot + auth-domain). Runs `anvil-data-qc qc`.
#
# OPEN-ACCESS ONLY: the CLI consent gate blocks anything but consentCode == "NRES", so Azul is queried
# anonymously and NO Azul token is needed. Controlled-access support (and a token input) is deferred
# until after the security review.
#
# Side effects: creates a real, RETAINED Analysis Workspace and runs the import (minutes to ~1 hr). The
# task always succeeds and surfaces the result as outputs; the detailed log (with the QC report, stage
# durations, and the created workspace name) is an output. Credentials = the ambient Terra runtime
# service account; config supplied as a File input.

workflow Qc {
    input {
        File config
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run = false
        String docker
    }

    call qc {
        input:
            config = config,
            snapshot_id = snapshot_id,
            tier = tier,
            catalog = catalog,
            dry_run = dry_run,
            docker = docker
    }

    output {
        File qc_report = qc.qc_report
        File detailed_log = qc.detailed_log
        Int qc_exit_code = qc.qc_exit_code
        Boolean qc_passed = qc.qc_passed
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        snapshot_id: "TDR snapshot id to export and QC (must be open-access; consentCode == NRES)."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        catalog: "Azul catalog (e.g. anvil14 in prod). Omit in dev."
        dry_run: "Validate the export side only (TDR -> BigQuery -> Azul); create no workspace, no import."
        docker: "anvil-data-qc image reference, e.g. <registry>/anvil-data-qc:<tag>."
    }
}

task qc {
    input {
        File config
        String snapshot_id
        String? tier
        String? catalog
        Boolean dry_run
        String docker
        Int cpu = 1
        Int memory_gb = 4
        Int disk_gb = 20
    }

    command <<<
        set -euo pipefail

        # OPEN-ACCESS ONLY (the consent gate enforces NRES). The pipeline logs to stderr; capture the
        # exit code without failing the task. 0 = PASS, 1 = QC failure, 2 = safety abort (e.g. a
        # controlled-access snapshot blocked by the consent gate).
        set +e
        anvil-data-qc qc \
            --config "~{config}" \
            --snapshot-id "~{snapshot_id}" \
            ~{"--tier " + tier} \
            ~{"--catalog " + catalog} \
            ~{true="--dry-run" false="" dry_run} \
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
