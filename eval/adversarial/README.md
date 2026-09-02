# Adversarial set

No YAML files here on purpose. The 61-case adversarial suite lives in
[`api/tests/test_ast_guard.py`](../../api/tests/test_ast_guard.py) as
`ADVERSARIAL_SUITE`, and `eval/run_eval.py` imports it directly rather
than duplicating the same SQL strings into a second file here.

That's a deliberate deviation from the project plan's literal file
layout (`eval/adversarial/*.yml`): the adversarial cases are exercised on
every `pytest` run already (that's the Phase 01 checkpoint — "50/50
hostile inputs blocked, run in CI, with no API key present"), and a
second copy in YAML would just be a second copy that silently drifts out
of sync with the real suite the guard is actually tested against. One
source of truth beats matching the file tree exactly.

Run just this gate (fast — no golden/ambiguity, no live model calls):

    cd eval && PYTHONPATH=../api ../.venv/bin/python run_eval.py --adversarial-only
