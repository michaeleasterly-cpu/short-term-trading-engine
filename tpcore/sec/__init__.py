"""SEC EDGAR adapters.

Public EDGAR endpoints (no API key required, ``User-Agent`` mandatory).
Today this package exposes a single adapter that pulls Form 4 (insider
transactions) and 8-K (material events) filings via the ``data.sec.gov``
submissions API + the ``www.sec.gov/Archives`` filing-detail URLs.

Reference implementation for the 5-stage data adapter pipeline — see
``docs/superpowers/pipelines/data_adapter_pipeline.md``.
"""

from .edgar_adapter import SECEdgarAdapter

__all__ = ["SECEdgarAdapter"]
