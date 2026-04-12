# Project Structure

```
phaze/
├── src/phaze/                  # Application package
│   ├── config.py               # Pydantic settings (env vars)
│   ├── constants.py            # File categories, extension map, tuning constants
│   ├── database.py             # Async SQLAlchemy engine + session factory
│   ├── main.py                 # FastAPI app factory with lifespan
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── base.py             #   DeclarativeBase + TimestampMixin
│   │   ├── file.py             #   FileRecord + FileState enum
│   │   ├── scan_batch.py       #   ScanBatch progress tracking
│   │   ├── metadata.py         #   FileMetadata (audio tags)
│   │   ├── analysis.py         #   AnalysisResult (BPM, key, mood, style)
│   │   ├── fingerprint.py      #   FingerprintResult (per-engine)
│   │   ├── proposal.py         #   RenameProposal + ProposalStatus
│   │   ├── execution.py        #   ExecutionLog (audit trail)
│   │   ├── tracklist.py        #   Tracklist + TracklistVersion + TracklistTrack
│   │   └── file_companion.py   #   FileCompanion (companion-media join)
│   ├── routers/                # API + UI endpoints
│   │   ├── health.py           #   GET /health
│   │   ├── scan.py             #   File discovery scan
│   │   ├── pipeline.py         #   Pipeline dashboard + processing triggers
│   │   ├── proposals.py        #   Proposal review + approval UI
│   │   ├── execution.py        #   Batch execution + SSE progress
│   │   ├── preview.py          #   Directory tree preview
│   │   ├── duplicates.py       #   Duplicate resolution UI
│   │   ├── tracklists.py       #   Tracklist management UI
│   │   └── companion.py        #   Companion file association
│   ├── schemas/                # Pydantic request/response models
│   │   ├── scan.py             #   Scan API schemas
│   │   └── companion.py        #   Companion/duplicate schemas
│   ├── services/               # Business logic
│   │   ├── ingestion.py        #   File discovery, hashing, bulk upsert
│   │   ├── metadata.py         #   Tag extraction via mutagen
│   │   ├── analysis.py         #   BPM/key/mood via essentia
│   │   ├── fingerprint.py      #   Multi-engine fingerprint orchestrator
│   │   ├── proposal.py         #   LLM calling + context building
│   │   ├── proposal_queries.py #   Proposal queries + pagination
│   │   ├── execution.py        #   Copy-verify-delete with audit logging
│   │   ├── execution_queries.py#   Execution log queries + pagination
│   │   ├── companion.py        #   Companion file association
│   │   ├── dedup.py            #   Duplicate detection + resolution
│   │   ├── collision.py        #   Destination path collision detection
│   │   ├── pipeline.py         #   Pipeline stats + file state queries
│   │   ├── tracklist_scraper.py#   1001Tracklists web scraper
│   │   └── tracklist_matcher.py#   Fuzzy match tracklists to files
│   ├── tasks/                  # SAQ async background jobs
│   │   ├── worker.py           #   SAQ settings + startup/shutdown
│   │   ├── functions.py        #   process_file (full pipeline per file)
│   │   ├── metadata_extraction.py # extract_file_metadata
│   │   ├── fingerprint.py      #   fingerprint_file (multi-engine)
│   │   ├── proposal.py         #   generate_proposals (batch LLM)
│   │   ├── execution.py        #   execute_approved_batch
│   │   ├── scan.py             #   scan_live_set (fingerprint matching)
│   │   ├── tracklist.py        #   scrape/search/refresh tracklists
│   │   ├── pool.py             #   ProcessPoolExecutor for CPU work
│   │   └── session.py          #   Session utilities
│   ├── prompts/                # LLM prompt templates
│   └── templates/              # Jinja2 HTML templates (HTMX + Tailwind)
│       ├── pipeline/           #   Pipeline dashboard
│       ├── proposals/          #   Proposal approval UI
│       ├── execution/          #   Execution dashboard + audit log
│       ├── duplicates/         #   Duplicate resolution UI
│       ├── tracklists/         #   Tracklist management UI
│       └── preview/            #   Directory tree preview
├── services/                   # Fingerprint microservices
│   ├── audfprint/              #   Landmark-based fingerprinting
│   └── panako/                 #   Tempo-robust fingerprinting
├── tests/                      # Test suite (85%+ coverage)
│   ├── conftest.py             #   Fixtures + test DB setup
│   ├── test_models/            #   ORM model tests
│   ├── test_routers/           #   Endpoint integration tests
│   ├── test_services/          #   Business logic unit tests
│   └── test_tasks/             #   SAQ job tests
├── alembic/                    # Database migrations (async template)
│   └── versions/               #   Migration scripts (001-008)
├── .github/workflows/          # CI/CD pipelines
│   ├── ci.yml                  #   Main orchestrator
│   ├── code-quality.yml        #   Pre-commit hooks
│   ├── tests.yml               #   Pytest + Codecov
│   └── security.yml            #   pip-audit, bandit, Semgrep, Trivy
├── scripts/                    # Utility scripts
│   └── download-models.sh      #   Download essentia ML models
├── docker-compose.yml          # Service orchestration
├── docker-compose.override.yml # Local development overrides
├── Dockerfile                  # Multi-stage build (API + worker)
├── justfile                    # Developer commands
├── pyproject.toml              # Project config + tool settings
└── uv.lock                     # Frozen dependency versions
```
