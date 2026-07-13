# Audit records

Process documentation beyond the commit history. This project is built by
an LLM coding agent under human direction (see the release notes'
provenance sections); part of demonstrating that workflow is leaving a
precise historic record of *how* defects were found — the methodology,
the dead ends, and the ground truth behind each claim — not just the
fixes that landed. Reviewers who want to go deeper than the diff start
here.

Each audit ships as a narrative report (`.md`) plus a structured findings
ledger (`.json`) with severities, evidence and commit hashes.
