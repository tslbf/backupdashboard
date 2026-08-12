from .base import Collector, run_collector
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

__all__ = ["Collector", "run_collector", "ALL_COLLECTORS"]
