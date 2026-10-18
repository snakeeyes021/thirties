# Legacy SLM evaluation harnesses

These two scripts were the SLM research harness before the event-span rework. They score the model by **block numbers** ("clear block 26", `BlockKind.WORK`), an interface that no longer exists, so they **do not run** against the current core.

They are kept as source material for the SLM brain harness described in `docs/SLM_BRAIN.md`:

- `run_eval_harness.py`: `PersistentGemmaEngine` (a persistent LiteRT/Gemma GPU daemon with auto-restart, still used by `tests/interactive_eval.py` when available) and `ScenarioGenerator` (personas, messy communication styles, task loads). Its `EvaluatorEngine` is block-based and obsolete.
- `run_qualitative_battery.py`: 25 curated scenarios worth porting. Its keyword-based scoring is not a real judge.

Port what is useful into span-based harnesses; don't fix these in place.
