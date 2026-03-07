"""
Shared constants used across the application.
"""

KLASS_DEFAULTS = {
    "identity": {"importance": 0.9, "halflife_days": 365.0},
    "relationship": {"importance": 0.9, "halflife_days": 365.0},
    "bond": {"importance": 0.85, "halflife_days": 365.0},
    "conflict": {"importance": 0.85, "halflife_days": 365.0},
    "fact": {"importance": 0.8, "halflife_days": 180.0},
    "preference": {"importance": 0.6, "halflife_days": 120.0},
    "health": {"importance": 0.8, "halflife_days": 120.0},
    "task": {"importance": 0.5, "halflife_days": 30.0},
    "ephemeral": {"importance": 0.3, "halflife_days": 7.0},
    "other": {"importance": 0.5, "halflife_days": 60.0},
}
