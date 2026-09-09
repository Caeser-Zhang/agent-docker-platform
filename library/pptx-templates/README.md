This directory is the add-only seed source for the shared PPTX template
library. The backend ingests every *.pptx found here at startup (and via
POST /api/admin/library/templates/seed), normalizes it, and stores a single
copy on the agent-pptx-lib named volume.

Rules:
  - Drop files flat: <anything>.pptx (subdirectories are ignored except
    samples/, which is gitignored).
  - Re-ingesting an unchanged file is a no-op (de-duplicated by content sha12).
  - Never commit third-party/redistribution-restricted material here; put it
    in samples/ instead (gitignored) and enable it locally with
    AGENT_PPTX_LIBRARY_ALLOW_SAMPLES=true.
