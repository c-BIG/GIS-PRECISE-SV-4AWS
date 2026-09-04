#!/bin/bash
# Download GATK-SV reference files from Broad's public GCS buckets and upload to your S3.
# Source: https://github.com/broadinstitute/gatk-sv/blob/main/inputs/values/resources_hg38.json
#
# GCS buckets are public and also reachable via HTTPS, so gsutil is NOT required.
#
# Usage: ./03_setup_references.sh <s3-bucket> <s3-prefix> <profile> [region]
#   e.g. ./03_setup_references.sh my-bucket genome/gatk-sv npm ap-southeast-1
set -euo pipefail

S3_BUCKET="${1:?Usage: $0 <s3-bucket> <s3-prefix> <profile> [region]}"
S3_PREFIX="${2:?Missing s3-prefix}"
PROFILE="${3:-default}"
REGION="${4:-ap-southeast-1}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# GCS gs:// URIs → HTTPS: gs://BUCKET/KEY  ->  https://storage.googleapis.com/BUCKET/KEY
# List of reference gs:// paths needed by the HealthOmics config (genome_references.json).
RESOURCES=(
  # Core genome
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.fai"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.dict"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.alt"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.amb"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.ann"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.bwt"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.pac"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.fasta.64.sa"
  "gs://gcp-public-data--broad-references/hg38/v0/Homo_sapiens_assembly38.dbsnp138.vcf"
  # Contigs / intervals
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/primary_contigs.list"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/contig.fai"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/hg38.genome"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/preprocessed_intervals.interval_list"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/primary_contigs_plus_mito.bed.gz"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/primary_contigs_plus_mito.bed.gz.tbi"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/allosome.fai"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/autosome.fai"
  # SV resources
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.repeatmasker.mei.with_SVA.pad_50_merged.bed.gz"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/wgd_scoring_mask.hg38.gnomad_v3.bed"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.contig_ploidy_priors_homo_sapiens.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.wgs.blacklist.wPAR.bed"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/cytobands_hg38.bed.gz"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/cytobands_hg38.bed.gz.tbi"
  # Clustering / stratification
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/clustering_config.part_one.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/clustering_config.part_two.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/stratify_config.part_one.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/stratify_config.part_two.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.SimpRep.sorted.pad_100.merged.bed"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.SegDup.sorted.merged.bed"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.RM.sorted.merged.bed"
  # PESR / depth exclude
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/PESR.encode.peri_all.repeats.delly.hg38.blacklist.sorted.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/PESR.encode.peri_all.repeats.delly.hg38.blacklist.sorted.bed.gz.tbi"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/depth_blacklist.sorted.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/depth_blacklist.sorted.bed.gz.tbi"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/bin_exclude.hg38.gatkcov.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/bin_exclude.hg38.gatkcov.bed.gz.tbi"
  # MELT / MEI references
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/HERVK.sorted.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/LINE1.sorted.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/melt_standard_vcf_header.txt"
  # Annotation
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/gencode.v47.basic.protein_coding.canonical.gtf"
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/noncoding.sort.hg38.bed"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/gencode.v39.CDS.intron.tsv.gz"
  # cn-MOPS / depth / segdup / rmsk / par
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/GRCh38_Nmask.bed"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.SD_gaps_Cen_Tel_Heter_Satellite_lumpy.blacklist.sorted.merged.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.SD_gaps_Cen_Tel_Heter_Satellite_lumpy.blacklist.sorted.merged.bed.gz.tbi"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.randomForest_blacklist.withRepMask.bed.gz"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.randomForest_blacklist.withRepMask.bed.gz.tbi"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/hg38.par.bed"
  # Genotyping / filtering cutoffs + GQ recalibrator model
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/seed_cutoff.txt"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/baseline_sl_cutoffs.tsv"
  "gs://gatk-sv-resources-public/hg38/v0/sv-resources/resources/v1/gatk-sv-recalibrator.aou_phase_1.v1.model"
  # Wham include list
  "gs://gcp-public-data--broad-references/hg38/v0/sv-resources/resources/v1/wham_whitelist.bed"
  # External allele frequency (annotation) — large file
  "gs://gatk-sv-resources-public/gnomad_AF/gnomad_v4_SV.Freq.tsv.gz"
)

echo "Transferring ${#RESOURCES[@]} reference files to s3://${S3_BUCKET}/${S3_PREFIX}/"
echo ""

for gs in "${RESOURCES[@]}"; do
    fname=$(basename "$gs")
    https_url="https://storage.googleapis.com/${gs#gs://}"
    s3_dest="s3://${S3_BUCKET}/${S3_PREFIX}/${fname}"

    # Skip if already present in S3
    if aws s3 ls "$s3_dest" --profile "$PROFILE" --region "$REGION" >/dev/null 2>&1; then
        echo "  [skip] ${fname} (exists)"
        continue
    fi

    echo "  [get ] ${fname}"
    if command -v gsutil >/dev/null 2>&1; then
        gsutil -q cp "$gs" "${TMPDIR}/${fname}"
    else
        curl -fsSL "$https_url" -o "${TMPDIR}/${fname}"
    fi
    echo "  [put ] ${fname}"
    aws s3 cp "${TMPDIR}/${fname}" "$s3_dest" --profile "$PROFILE" --region "$REGION" --quiet
    rm -f "${TMPDIR}/${fname}"
done

echo ""
echo "Done. References at s3://${S3_BUCKET}/${S3_PREFIX}/"
echo "Set RefS3Prefix=\"s3://${S3_BUCKET}/${S3_PREFIX}\" in samconfig.toml"
echo ""
echo "NOTE: genome_references.json in the config references some files not in this"
echo "core list (e.g. autosome.fai as cnmops_chrom_file). Review genome_references.json"
echo "against your uploaded files and adjust as needed."
