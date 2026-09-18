# WDL Workflows

These WDL workflows are derived from the Broad Institute's **GATK-SV** pipeline,
pinned to the **v1.1** release:

https://github.com/broadinstitute/gatk-sv/tree/v1.1

They are **Copyright (c) 2009-2026, Broad Institute, Inc.** and licensed under the
BSD 3-Clause License (see `../LICENSE.TXT`).

## Modifications

These files have been adapted to run on the **AWS HealthOmics** WDL engine. The changes
are additive (no upstream logic removed) and cover file co-location / explicit index
inputs, WDL strict-mode fixes, removal of GCP-native calls, the ~50KB parameter manifest
pattern, and runtime tuning. See:

- `../docs/HEALTHOMICS_WDL_ADAPTATIONS.md` — what changed and why
- `../docs/GATKSV_UPGRADE_CHECKLIST.md` — re-port checklist for the next GATK-SV release

For the pipeline's scientific documentation and original sources, refer to the upstream
repository and https://broadinstitute.github.io/gatk-sv/docs/intro.
