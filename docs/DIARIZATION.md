# Speaker diarization: options and recommendation (evaluation plan – nothing measured yet)

**Today:** audio is cut into ~30 s chunks, each transcribed independently; the LLM maps the per-chunk labels
("Яригч 1/2") to meeting-wide speakers from the text alone. There is no acoustic speaker model, so the same
person can get different labels across chunks (F-25).

| Option | Accuracy on Mongolian | Cost | Operational impact |
|---|---|---|---|
| **A. Keep LLM-only mapping** (now) | Unknown; weakest with short turns, similar voices, interrupted speech | $0 extra | None |
| **B. Local pyannote.audio (speaker-diarization-3.x)** | Language-independent (acoustic), generally the best open option; real DER on Mongolian meeting audio **unmeasured** | $0 licence (gated model, free HF token + accepted terms); needs 2+ GB RAM, torch; CPU is slower than real time on long audio | Does not fit Render free/512 MB; needs a paid instance or a separate worker. **You wrote: no heavy local models on the 16 GB Mac** – so evaluate on a server, not on your laptop |
| **C. Hosted diarization API** (e.g. AssemblyAI / Deepgram / Azure Speech) | Vendor-dependent; Mongolian support for *transcription* varies, diarization itself is acoustic | Per audio-hour (typically cents to ~$1/h; **verify current prices**) | Sends recordings to another third party (privacy review) |
| **D. ASR provider that returns speaker labels** | Depends on the provider; Qwen-Omni is prompted for labels per chunk only | Same as ASR | Not available today |

## Recommendation
1. Keep A for the pilot, and make the limitation visible: speaker names are editable per turn (done in 4.1).
2. Collect the evidence before spending anything: take 3–5 recordings you may share (2–4 speakers each), hand-label
   speaker turns, and compute diarization error rate (DER) for A vs B. B can be run once, on a rented CPU/GPU box or a
   one-off cloud job, without touching production.
3. Switch to B (as a separate job in the durable job queue, 2.1) only if DER for A is clearly worse and speaker
   attribution matters for decisions/owners. Decide C only after a privacy review.

No audio was processed for this note and no paid service was called. The numbers above are qualitative.
