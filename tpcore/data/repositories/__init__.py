"""Engine-facing read repositories — classification_id-keyed.

Every method takes a classification_id (or series_id for macro) and
returns Pydantic v2 row models. Engines never speak ticker internally;
ticker-↔-classification_id translation lives in
``tpcore.identity.dispatcher.IdentityDispatcher`` and only fires at
system edges (Alpaca submit, AAR write, dashboard render).

Pattern: thin wrappers around asyncpg + SQL against the
``platform.v_*`` views or directly against tables when the column
surface is stable. No ORM. No identity map. No unit-of-work — these
are read repositories; writes go through their own dedicated paths
(``AARWriter``, ``OrderManagement``, etc.).

Module shape mirrors ``tpcore/fundamentals/cache.py``: caller passes
an ``asyncpg.Pool`` which the repo uses-but-doesn't-own.
"""

from tpcore.data.repositories.universe import UniverseRepo, UniverseRow

__all__ = ["UniverseRepo", "UniverseRow"]
