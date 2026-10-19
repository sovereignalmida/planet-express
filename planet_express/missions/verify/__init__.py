"""Verification (v4 slice 8, contract 10): Core's own pre-agreed checks plus a fresh scan decide completion.

Nothing is imported here on purpose: `state.py` reads `plan.VerificationResult` to gate `completed`, and the heavier
modules (`checks`, `runner`, `baseline`) must not be pulled in by that import.
"""
