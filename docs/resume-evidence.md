# Resume evidence verification — 2026-09-18

This pass adds `scripts/resume-recovery-evidence.py`: a deterministic fault injection at the patch persistence boundary, using a dedicated PostgreSQL database, real disk writes and SIGKILL. It does not call an LLM and is not an end-to-end Celery worker recovery demonstration.

The child records tool intent, applies the patch and pauses before persisting ToolResult. The parent kills that child, reconnects to PostgreSQL, and calls the existing `reconcile_run`. Assertions require a reconciled result, unchanged file content, one tool record, reuse on second recovery and `needs_attention` for an incomplete non-replayable external call. The script only accepts a database URL containing `resume_evidence`, and a new output directory. It reuses the production store/patch/recovery implementations without changing their interfaces.

Prerequisites: the existing review-agent conda environment; a dedicated database migrated through 0004. Run from repository root with `PYTHONPATH=src`, `REVIEW_AGENT_DATABASE_URL` pointing to that dedicated DB, then:

```bash
conda run -n review-agent python scripts/resume-recovery-evidence.py --out /tmp/resume-recovery-unique-run
```

Observed run: `4949f287-fd95-4cf0-971d-e1e4ee3295d9`; child exit -9; first recovery reconciled; second recovery reused; disk unchanged after recovery; one tool record. This is a controlled experiment, not a production incident.

Validation on the existing dirty working tree (not HEAD alone):

- `PYTHONPATH=src REVIEW_AGENT_DATABASE_URL='' conda run -n review-agent python -m pytest -q tests/test_recovery.py tests/test_execution_layer.py tests/test_eval_runner.py`: 36 passed; includes Docker executor cancellation.
- `npm --prefix frontend run test -- --run`: 15 passed.
- Dedicated PostgreSQL/Redis/API/Celery and Web preflight succeeded. No new real-model coding task or paid evaluation ran.
- Initial tests inherited a stale configured database, causing 9 failures. Explicit test configuration resolved these; no production runtime fix was necessary.

Historical evaluation remains 8 tasks × 2 variants × 1 repeat: hidden acceptance 16/16; each variant has only 5/8 succeeded statuses; full total tokens +1.18%. No expanded comparison, model isolation benefit, or token savings is claimed.

Public GitHub Code_review_Agent main was checked at `0d9e6022856edf16dbca6abf901dddb5dc5ca6ff`; it lacks this upgraded TaskService. This local repository has no remote. Publishing requires preparing the existing user changes; this pass does not push or rewrite them.
