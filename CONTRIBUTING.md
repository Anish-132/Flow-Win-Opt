# Contributing to Flow

## The one hard rule

Every registry key, service name, or PowerShell command in `TWEAK_DATABASE` needs a **real, checkable source** — official Microsoft docs, a well-known established tool's source (ChrisTitusTech/winutil, etc.), or your own verified testing. No entries invented "because it sounds right." If you can't point to where a key came from, don't add it.

## Adding a tweak

1. Check `TWEAK_DATABASE` for an existing entry touching the same registry path/name — duplicates have shipped before, don't add another.
2. Pick the right `tier` (minimal/standard/maximal/extreme) based on real risk, not where it's convenient to put it.
3. Set `min_os`/`max_os` honestly — if you haven't verified it against real version history, leave `os_verified=False`.
4. Write the `description` for a non-technical reader: what it does, what you trade away, why it matters on this project's target hardware (mechanical HDD, 4GB RAM class of machine) specifically if relevant.
5. Gate with `applies_to` if the tweak is actively wrong on the wrong hardware (e.g. an HDD-only optimization that hurts SSDs).

## Testing before a PR

```powershell
python -m py_compile flow.py
python flow.py list-tweaks maximal     # sanity-check your tweak shows up
python flow.py check-requirements
```

There's no CI running actual tweak application yet (that needs a real Windows VM matrix across 7/8.1/10/11) — be honest in your PR description about what you did and didn't test live.

## Code style

- Every tweak is declarative (`Tweak` dataclass) — never a raw shell command string. The engine in `apply_tweak()`/`revert_entry()` is what actually runs things; adding a new *method* (not just a new tweak) means adding one branch each to `_step_capture()`, `_step_apply()`, and `_step_revert()`.
- Single-file architecture is intentional, not a placeholder — keep new code in `flow.py` unless there's a strong reason to split it out.
- Comments in this codebase often explain *why*, not *what* — match that style; a comment restating the code in English isn't useful, one explaining a non-obvious tradeoff or past bug is.

## Reporting a bad tweak

If a tweak breaks something or the description undersells the risk, open an issue with your OS build, hardware, and what happened. That's worth more than a PR — a wrong tweak description on a system tool has real consequences for someone who trusted it.
