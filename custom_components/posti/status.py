"""Deliberately almost empty: the two sources do not share a status map.

The tracking route reports ``status.main`` plus a ``subStatus[]`` list; the
account route a single flat ``shipmentPhase``. ``DELIVERED`` happens to be
spelled the same in both — that is a coincidence, not a shared vocabulary, so
each source keeps its own map and its own one-shot warning bookkeeping in its
own module. Only the "where to report an unmapped value" link is shared.
"""
from __future__ import annotations

# Where users report a status/shape we do not handle yet. The ``?template=``
# parameter matters: without it the link opens a blank form, missing the
# version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-posti/issues/new"
    "?template=unrecognised_status.yml"
)
