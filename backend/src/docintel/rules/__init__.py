"""Deterministic, configurable business rules (Module 10).

Rule *types* (evaluators) are code: reviewed, tested, each with a Pydantic model for its
parameters. Rule *instances* (code, parameters, severity, on/off) are data in `business_rules`,
editable by administrators and validated against the evaluator's model before they are saved.
"""
