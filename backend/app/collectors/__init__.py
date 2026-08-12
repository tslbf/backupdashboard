from .base import Collector, close_orphaned_runs, run_collector
from .azure import AzureCollector
from .legacy_sql import LegacySqlCollector
from .nable import NableCollector
from .veeam import VeeamCollector

ALL_COLLECTORS: dict[str, Collector] = {
    c.source: c
    for c in [
        VeeamCollector(),
        NableCollector(),
        AzureCollector(),
        LegacySqlCollector(),
    ]
}

__all__ = ["Collector", "run_collector", "close_orphaned_runs", "ALL_COLLECTORS"]
