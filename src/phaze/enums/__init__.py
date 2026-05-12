"""DB-free enum definitions shared between SQLAlchemy models and Pydantic schemas.

The agent worker (Phase 26 D-03) is forbidden from importing ``phaze.database`` /
``phaze.models``. ``phaze.schemas.agent_*`` modules therefore source their shared
``Literal``-style enums from this package, and ``phaze.models.*`` re-imports the
same definitions so the canonical column type is consistent across the boundary.
"""
