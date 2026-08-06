"""Channel discovery: find candidate signal channels, gate them by trust,
and promote them to real trading only after they prove out in paper.
"""

# Candidate lifecycle statuses.
DISCOVERED = "discovered"   # found by search, awaiting user approval
OBSERVING = "observing"     # approved+joined, trades PAPER-only until proven
ACTIVE = "active"           # proven track record, eligible for real orders
REJECTED = "rejected"       # user declined; never surfaced again

INGESTING_STATUSES = frozenset({OBSERVING, ACTIVE})
