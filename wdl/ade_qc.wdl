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
        String docker = "us-central1-docker.pkg.dev/dsp-sa-development/anvil-qc/anvil-data-qc:0.2.1"
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
    }

    parameter_meta {
        config: "Per-tier config.yaml (set adc_credentials_file: null for deployment)."
        snapshot_id: "TDR snapshot id to export and QC. Open-access unless allow_controlled_access=true."
        tier: "Tier (dev|prod). Optional; defaults to the config's default_tier."
        catalog: "Azul catalog (e.g. anvil14 in prod). Omit in dev."
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
        # 1 = QC failure, 2 = safety abort (e.g. a controlled-access snapshot without the opt-in).
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
