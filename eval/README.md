# Minutes quality evaluation

Deterministic rubric + runner so that **no prompt or model change ships without a before/after number**.

```bash
# score saved outputs (free)
python -m eval.run_eval --provider file:eval/canned/good_model_outputs.json
# real run against Qwen (PAID – needs QWEN_API_KEY, run by a person, never in CI)
python -m eval.run_eval --provider qwen --out before.json
#   ...change the prompt / model...
python -m eval.run_eval --provider qwen --baseline before.json --out after.json   # prints ↑ better / ↓ WORSE per metric
```

Metrics (per case, then averaged): decision precision/recall, **false_confirmed** (a proposal reported as a
confirmed decision – the worst error), action-item owner/due accuracy, **hallucinations** (asserted items that
match nothing in the gold list or whose quote is not in the cited segment), evidence_found_rate.
The model output is scored *as the model produced it* (before the system downgrades unproven "confirmed" items),
so the guard cannot hide a regression in the model.

## What is and is not here
* `fixtures/*.json` are **synthetic** (written by hand, marked `"synthetic": true`). They exercise the rubric
  and show the format; they are **not** a measurement of real-world accuracy.
* **There is no real baseline yet.** Producing one needs your consent and 5–10 anonymised real Mongolian meetings
  with gold labels (decisions with status, action items with owner/due date, deliberately garbled passages) and a
  paid Qwen run. Until then, no claim about accuracy should be made from this directory.
* Adding a real case: copy a fixture, replace `transcript` (segments with ids) and `gold`, keep personal data out
  (names → "Бат", "Сараа"), set `"synthetic": false` only for genuinely recorded meetings you have permission to use.
* `tests/test_eval_harness.py` checks the scorer itself on canned "good" and "bad" model outputs (no API calls).
