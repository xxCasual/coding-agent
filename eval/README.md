# Coding eval assets

Frozen sample trees, task manifests, and trusted hidden acceptance live here.

- `samples/` — three self-contained example projects. Runner copies a clean git snapshot per run.
- `manifests/dev.json` — 8 development tasks (4 fix / 3 feature / 1 contract).
- `manifests/heldout.json` — 24 reserved tasks (12 / 8 / 4). Do not use for prompt tuning.
- `hidden/<task_id>/check.py` — independent acceptance. Never copy into an Agent workspace.
- `profiles/` — baseline vs full runtime switches.
- `results/` — local JSONL output (gitignored).

Do not read held-out hidden checks when authoring prompts. Formal model runs belong to M12.
