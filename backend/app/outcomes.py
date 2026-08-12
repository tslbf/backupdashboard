"""Normalizing three vendors' result vocabularies into one.

Veeam says "Warning", Cove says status code 8 ("CompletedWithErrors"), Azure
says "CompletedWithWarnings". They mean the same thing, and the dashboard has to
count them together or every number is per-vendor trivia.

The raw vendor string is always kept alongside (`result_raw`) — normalization is
for counting and coloring, never a reason to lose what the console actually said.
"""
from __future__ import annotations

SUCCESS = "success"
WARNING = "warning"
FAILED = "failed"
RUNNING = "running"
MISSED = "missed"  # synthetic: no run at all on a night one was expected
UNKNOWN = "unknown"

# Order matters: worst first. Used to roll several jobs for one server up to a
# single verdict for the night — a server with one good and one failed job needs
# to surface as failed, not be hidden behind the success.
SEVERITY = [FAILED, MISSED, WARNING, RUNNING, UNKNOWN, SUCCESS]
_RANK = {outcome: i for i, outcome in enumerate(SEVERITY)}

# Outcomes that mean "somebody has to look at this tonight".
PROBLEM_OUTCOMES = (FAILED, MISSED, WARNING)
# Outcomes that mean restorable data was produced.
PROTECTED_OUTCOMES = (SUCCESS, WARNING)

ALL_OUTCOMES = [SUCCESS, WARNING, FAILED, MISSED, RUNNING, UNKNOWN]

OUTCOME_LABELS = {
    SUCCESS: "Success",
    WARNING: "Warning",
    FAILED: "Failed",
    MISSED: "No backup",
    RUNNING: "Running",
    UNKNOWN: "Unknown",
}


def worst(outcomes) -> str:
    """The outcome a set of jobs rolls up to. Empty set -> unknown."""
    ranked = sorted(outcomes, key=lambda o: _RANK.get(o, _RANK[UNKNOWN]))
    return ranked[0] if ranked else UNKNOWN


def severity(outcome: str) -> int:
    return _RANK.get(outcome, _RANK[UNKNOWN])


# --- Veeam Backup & Replication -------------------------------------------
# session.result.result on /api/v1/sessions. "None" appears on sessions that
# ended without a verdict, which in practice means still finalizing.
VEEAM_RESULTS = {
    "success": SUCCESS,
    "warning": WARNING,
    "failed": FAILED,
    "failure": FAILED,
    "none": RUNNING,
    "working": RUNNING,
    "inprogress": RUNNING,
    "pending": RUNNING,
    "idle": RUNNING,
    "canceled": FAILED,
    "cancelled": FAILED,
    "stopped": FAILED,
}

# --- N-able Cove (Backup Manager) ------------------------------------------
# Numeric status from column D9F17 on EnumerateAccountStatistics. Same table the
# PowerShell script carried, mapped through to canonical outcomes here.
NABLE_STATUS_CODES = {
    1: ("InProcess", RUNNING),
    2: ("Failed", FAILED),
    3: ("Aborted", FAILED),
    5: ("Completed", SUCCESS),
    6: ("Interrupted", FAILED),
    7: ("NotStarted", MISSED),
    8: ("CompletedWithErrors", WARNING),
    9: ("InProgressWithFaults", RUNNING),
    10: ("OverQuota", FAILED),
    11: ("NoSelection", WARNING),  # nothing selected to protect — a real gap
    12: ("Restarted", RUNNING),
}

NABLE_RESULTS = {name.lower(): outcome for name, outcome in NABLE_STATUS_CODES.values()}

# --- Azure Backup (Recovery Services vaults) -------------------------------
# properties.status on backupJobs.
AZURE_RESULTS = {
    "completed": SUCCESS,
    "completedwithwarnings": WARNING,
    "completedwithinformation": SUCCESS,
    "failed": FAILED,
    "cancelled": FAILED,
    "canceled": FAILED,
    "cancelling": FAILED,
    "inprogress": RUNNING,
    "notstarted": MISSED,
}

_BY_SOURCE = {
    "veeam": VEEAM_RESULTS,
    "nable": NABLE_RESULTS,
    "azure": AZURE_RESULTS,
}

# Last-resort substring rules, applied when a vendor emits a value none of the
# tables above knows. Ordered: the first hit wins, and "warning" is checked
# before "success" so "CompletedWithWarnings"-shaped novelties don't read clean.
_FALLBACK_PATTERNS = [
    ("fail", FAILED),
    ("error", WARNING),
    ("abort", FAILED),
    ("cancel", FAILED),
    ("interrupt", FAILED),
    ("quota", FAILED),
    ("warn", WARNING),
    ("progress", RUNNING),
    ("running", RUNNING),
    ("start", RUNNING),
    ("success", SUCCESS),
    ("complete", SUCCESS),
    ("ok", SUCCESS),
]


def normalize(source: str, raw: str | int | None) -> str:
    """Vendor result -> canonical outcome. Never raises; unknown stays visible."""
    if raw is None:
        return UNKNOWN
    if isinstance(raw, int) or (isinstance(raw, str) and raw.strip().isdigit()):
        code = int(raw)
        if source == "nable" and code in NABLE_STATUS_CODES:
            return NABLE_STATUS_CODES[code][1]
        return UNKNOWN

    text = str(raw).strip().lower()
    if not text:
        return UNKNOWN

    table = _BY_SOURCE.get(source, {})
    squashed = text.replace(" ", "").replace("_", "").replace("-", "")
    if squashed in table:
        return table[squashed]
    # A vendor table miss still deserves the cross-vendor tables before regex.
    for other in _BY_SOURCE.values():
        if squashed in other:
            return other[squashed]
    for needle, outcome in _FALLBACK_PATTERNS:
        if needle in squashed:
            return outcome
    return UNKNOWN


def nable_status_name(code: int | None) -> str | None:
    entry = NABLE_STATUS_CODES.get(code) if code is not None else None
    return entry[0] if entry else (f"Code{code}" if code is not None else None)
