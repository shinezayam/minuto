# AI minutes: what is enforced, what is not

| Guarantee | Mechanism | Strength |
|---|---|---|
| A proposal is not shown as a decision | Decision `status` (`confirmed/proposed/rejected/unclear`) required in the prompt; exports and UI list non-confirmed items separately | Prompt + display rule. The model can still mislabel; a human reviews. |
| "Confirmed" needs quoted evidence | `evidence[] = {segment_id, quote}`; `verify_minutes` checks that the quote really occurs in the cited segment (normalised, ≥6 chars). Unproven `confirmed` is downgraded to `unclear` | Deterministic. Proves the quote exists, **not** that the conclusion is right. |
| A segment id alone is not verification | No quote ⇒ `verification = no_evidence` ⇒ flagged `needs_review` | Deterministic |
| Garbled speech is not filled in | Prompt tells the model to choose `unclear`; eval fixture covers it | Prompt only (measured only once a real eval exists) |
| AI output is a draft until a person approves | `minutes_status: draft → in_review → approved` (approve = owner/admin), `approved_by/at`; per-item accept/edit/reject; any content change drops `approved` to `in_review`; exports carry a "AI-generated, not approved" notice on drafts | Enforced in code |
| Summary regeneration is cheap and isolated | `generate_summary` (short prompt) touches only `summary` | Enforced in code |
| Ask context is bounded | latest 200 meetings loaded, 60 000-character prompt budget with ranked excerpts | Enforced in code |

UI wording says **"cited"**, never "verified": a found quote means the source text exists, nothing more.

Legacy minutes (created before this change) have no status/evidence: their decisions read as `unclear`,
`draft`, `no_evidence` and appear under "proposals / unclear" in exports until a person reviews them.

Not done: an LLM-based entailment check (second paid call) – the deterministic quote check was chosen first
because it is free, repeatable and cannot hallucinate; entailment can be added behind the same
`verification` field once the eval set shows it is worth the cost.
