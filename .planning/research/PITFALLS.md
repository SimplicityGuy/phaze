# Pitfalls Research

**Domain:** Music collection management, batch file processing, AI-powered file organization
**Researched:** 2026-03-27
**Confidence:** HIGH (domain well-documented via beets ecosystem, file management tools, PostgreSQL bulk loading)

## Critical Pitfalls

Mistakes that cause data loss, rewrites, or major operational failures with 200K irreplaceable files.

### Pitfall 1: Irreversible File Operations Without Transaction Log

**What goes wrong:**
A batch rename/move operation partially completes, then crashes (OOM, power loss, disk full). Some files are at their new locations with new names, others are at old locations. The database says one thing, the filesystem says another. With 200K irreplaceable files, there is no "undo" unless you built one. Cross-filesystem moves via `shutil.move()` are copy-then-delete -- the delete can happen before the copy is verified.

**Why it happens:**
Developers treat file moves as simple function calls without thinking about atomicity. Filesystem operations are not transactional -- there is no rollback. When processing thousands of files in a batch, any interruption leaves the system in a partial state that is extremely difficult to recover from manually.

**How to avoid:**
- Implement a file operations journal (WAL pattern) in PostgreSQL: write the intended operation (source, destination, hash) BEFORE executing it, mark COMPLETE after
- Always copy-verify-delete: verify destination by re-computing sha256 after copy, only delete source after verification passes
- Process one file at a time within batches, committing state after each individual move
- Store the complete inverse operation for every forward operation (undo log)
- Use `os.rename()` for same-filesystem moves (atomic on POSIX); detect cross-filesystem situations and use copy-verify-delete instead

**Warning signs:**
- No `operations` or `file_moves` table in the database schema
- Batch operations that process all files before committing any state
- No verification step after file moves
- Using `shutil.move()` without checking if source and destination are on the same filesystem

**Phase to address:**
Phase 1 (database schema design). The operations journal must be a foundational table, not bolted on later.

---

### Pitfall 2: Hash-Only Deduplication Deletes the Wrong File

**What goes wrong:**
SHA256 correctly identifies two files as identical byte-for-byte duplicates. The system auto-deletes one. But the file at the "kept" path has been corrupted since hashing, and the user loses their only good copy. Alternatively, developers conflate "same song" with "same file" and try to auto-deduplicate near-duplicates (same audio, different encoding) which are NOT byte-identical and may have different quality levels.

**Why it happens:**
Hashes computed at ingest time go stale if files are modified or corrupted after hashing. Music collections have many near-duplicates (320kbps vs 128kbps, different encodes of the same live set) that require human judgment to resolve, not automated deletion.

**How to avoid:**
- NEVER auto-delete duplicates -- present duplicates in the admin UI for human review
- Show file metadata alongside hashes: bitrate, duration, file size, format, source path
- Re-hash files before any destructive operation to confirm the hash is still valid
- Distinguish between exact duplicates (same hash) and near-duplicates (same audio content, different encoding) -- these are separate workflows
- Use `(sha256_hash, original_path)` as a natural key for dedup on ingestion; upsert on conflict

**Warning signs:**
- Any code path that deletes files without human approval
- Deduplication logic that only checks hashes without surfacing context
- No re-verification of hashes before destructive operations

**Phase to address:**
Phase 1 (deduplication design) and the admin approval workflow phase. The principle "nothing deletes without human approval" must be a system invariant from day one.

---

### Pitfall 3: Unicode and Non-ASCII Filename Encoding Corruption

**What goes wrong:**
Files with non-ASCII characters in their names (accented artists like "Beyonce" stored as "Beyonc\u00e9", Japanese/Korean characters, special symbols) get mangled during processing. The database stores one representation, the filesystem has another, and path lookups silently fail. On macOS, filenames are stored in NFD (decomposed) normalization form; on Linux (the Docker host), they use NFC (composed). A file ingested from a macOS-formatted drive will have different byte sequences for the same visual filename on the Linux Docker container.

**Why it happens:**
The beets project documented this extensively: "paths on Unix are fundamentally bytes" while "paths on Windows are fundamentally text." Even on Linux, filesystem encoding can vary. Python 3.13 handles this better than Python 2, but developers still hit issues when they store paths as strings in PostgreSQL, compare them byte-by-byte, or pass them between systems without normalization.

**How to avoid:**
- Normalize ALL paths to NFC (Unicode Normalization Form C) at the ingestion boundary before storing in PostgreSQL
- Store paths as TEXT in PostgreSQL (which is UTF-8), but always normalize before insert
- Use `unicodedata.normalize('NFC', path)` on every path at the point of ingestion
- Use `pathlib.Path` consistently (not `os.path` string manipulation)
- Test with filenames containing: accented characters, CJK characters, emoji, spaces, parentheses, ampersands, single quotes
- Keep an `original_path_bytes` column (hex-encoded) for forensic recovery if normalization ever loses information

**Warning signs:**
- Path columns in the database with no documented encoding strategy
- String comparison of paths without normalization
- Tests that only use ASCII filenames
- Files "missing" from the database that exist on disk (encoding mismatch)

**Phase to address:**
Phase 1 (file ingestion). The normalization strategy must be decided and implemented at the very first point files enter the system.

---

### Pitfall 4: Docker Volume Permissions Mismatch

**What goes wrong:**
The Docker container runs as root (or a different UID than the host). Files created or moved by the container are owned by root on the host filesystem. The user cannot access their own music files from the host without `sudo`. With 200K files, fixing permissions after the fact is a multi-hour operation.

**Why it happens:**
Docker bind-mounts pass through host UID/GID ownership directly. If the container process runs as UID 0 (root) and creates files, those files are owned by root on the host. This is the single most common Docker Compose complaint for file-processing workloads.

**How to avoid:**
- Run the container process as a non-root user whose UID/GID matches the host user: `user: "${UID}:${GID}"` in docker-compose.yml
- Set `umask` appropriately in the container entrypoint
- Mount music files as read-only (`:ro`) for all services except the file-mover service
- Test file ownership after the very first container run -- do not discover this problem after processing 200K files

**Warning signs:**
- `docker-compose.yml` with no `user:` directive on services that write files
- Music volume mounted as read-write on services that should only read
- Files on the host suddenly owned by `root:root` after container operations

**Phase to address:**
Phase 1 (Docker Compose setup). Must be validated with a small test batch before any large-scale processing.

---

### Pitfall 5: AI Rename Proposals Without Deterministic Reproducibility

**What goes wrong:**
The AI proposes filenames for 200K files. The user approves 500 renames over a week. Then the AI model changes (API update, temperature drift, prompt tweak), and the next batch of proposals is stylistically inconsistent with the already-approved renames. The collection ends up with two naming conventions. LLMs also hallucinate filenames with `/`, `\0`, or characters invalid on the target filesystem, or propose identical names for different files.

**Why it happens:**
LLM outputs are non-deterministic by nature. Even with temperature=0, different model versions produce different outputs. LLMs have no concept of filesystem constraints and optimize for "nice looking" names, not valid ones.

**How to avoid:**
- Store the COMPLETE proposal (proposed name, proposed path) as an immutable record in PostgreSQL at generation time
- Include model version, prompt version, and timestamp in the proposal record for auditability
- Design the naming format as a TEMPLATE with deterministic components (artist, title, BPM) filled by AI extraction, not freeform AI-generated filenames
- Validate every proposed filename with a strict regex; check for collisions in the proposal batch before presenting to user
- Version your prompts and treat prompt changes as migrations
- Never regenerate proposals for files that already have approved renames

**Warning signs:**
- Proposals stored only in memory or as a computed property
- No prompt versioning or model version tracking
- No Pydantic validation on LLM output before persisting
- Regeneration logic that overwrites existing proposals

**Phase to address:**
AI integration phase. But the database schema for proposals must be designed in Phase 1 to be immutable-friendly.

---

### Pitfall 6: Unbounded Memory During Bulk File Hashing

**What goes wrong:**
Hashing 200K files by reading each file entirely into memory causes the process to consume tens of gigabytes of RAM and get OOM-killed. Concert video files can be multiple gigabytes each.

**Why it happens:**
The naive approach is `hashlib.sha256(open(path, 'rb').read()).hexdigest()` which reads the entire file into memory. For a 4GB concert video, that is 4GB of RAM for a single hash operation. Developers test with small files and never notice.

**How to avoid:**
- ALWAYS use chunked reading for hashing: read in 64KB chunks, feed to `hashlib` incrementally
- Set a standard chunk size as a constant (e.g., `HASH_CHUNK_SIZE = 65536`)
- For the parallel processing pipeline, monitor per-worker memory usage
- Use a two-pass deduplication: first pass compares file size (instant, zero I/O for mismatches), second pass hashes only files with matching sizes

**Warning signs:**
- `file.read()` without a size argument anywhere in hashing code
- OOM kills in Docker container logs during batch processing
- Hashing all 200K files when only a subset have matching sizes

**Phase to address:**
Phase 1 (file ingestion). The hashing utility should be one of the first things built and tested with large files.

---

### Pitfall 7: Blocking the Async Event Loop with CPU-Bound Audio Analysis

**What goes wrong:**
Running librosa `beat_track()` or chromaprint fingerprinting inside an async function blocks the entire event loop, starving other coroutines. API becomes unresponsive. Other jobs stall. Worker appears hung.

**Why it happens:**
librosa and chromaprint are CPU-bound C extensions. `await` does not yield for CPU work. "Parallelizable" does not mean "run all at once." Developers confuse concurrency with parallelism and forget that disk I/O, database connections, and memory are shared resources.

**How to avoid:**
- Run CPU-bound work in a process pool via `asyncio.to_thread()` or `loop.run_in_executor(ProcessPoolExecutor(...))`
- Use a bounded worker pool: start with 4 workers, measure throughput, increase until I/O or CPU saturates
- Implement backpressure: use a bounded queue so producers wait when consumers are overwhelmed
- Separate CPU-bound analysis from I/O-bound database writes using a producer-consumer pattern
- Limit database connections with connection pooling (e.g., `psycopg_pool` or PgBouncer)

**Warning signs:**
- Audio processing called directly in async path without `run_in_executor`
- `max_workers` set to `os.cpu_count()` or higher without measuring
- All 200K files submitted to the executor at once (instead of batched)
- Event loop blocked warnings, API health checks timing out

**Phase to address:**
Analysis pipeline phase. Database connection pooling strategy should be established in Phase 1.

---

### Pitfall 8: PostgreSQL Bulk Insert Performance Cliff

**What goes wrong:**
Inserting 200K file records one-at-a-time takes hours instead of minutes. Queries during batch inserts become unusable (20+ second response times), making the admin UI unresponsive during ingestion.

**Why it happens:**
Individual INSERT statements have per-statement overhead (parse, plan, execute, WAL write, index update). PostgreSQL insert performance degrades from ~3000/s to ~500/s as table size grows. Triggers make operations ~3x slower.

**How to avoid:**
- Use `COPY` or batch INSERT (1000-5000 rows per transaction) -- 10x faster than individual INSERTs
- Drop non-essential indexes before bulk ingestion, recreate after (keep primary key)
- Separate the ingestion write path from the admin UI read path with connection pooling
- Run `ANALYZE` after bulk loads to update query planner statistics
- Consider `UNLOGGED` tables for staging data during initial ingestion

**Warning signs:**
- Individual INSERT statements in a loop
- Ingestion taking more than 10 minutes for 200K records (should be under 2 minutes with COPY)
- Admin UI becoming unresponsive during batch operations

**Phase to address:**
Phase 1 (database and ingestion pipeline). The bulk loading strategy must be designed and benchmarked early.

---

### Pitfall 9: litellm Supply Chain Risk

**What goes wrong:**
Malicious code executes in the environment via compromised litellm package. March 2026: versions 1.82.7 and 1.82.8 contained malicious code via compromised maintainer account.

**Why it happens:**
Popular PyPI packages are high-value targets. Worker containers with filesystem access to the entire music collection amplify the impact.

**How to avoid:**
- Pin exact version with verified hash in `uv.lock`
- Monitor litellm security advisories
- Consider running LLM calls in a network-isolated container separate from the file-access container
- Review `pip-audit` / `safety` output in CI

**Warning signs:**
- Unexpected network connections from worker containers
- Hash mismatch in lock file after `uv sync`

**Phase to address:**
Phase 1 (dependency setup and CI security scanning).

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Storing file paths without NFC normalization | Faster initial development | Path comparison bugs, encoding mismatches, broken lookups when mount points change | Never -- normalize from day one |
| Single PostgreSQL connection (no pooling) | Simpler setup | Ingestion blocks admin UI, parallel workers deadlock on connection | Only during initial prototyping with <100 files |
| Skipping the operations journal | Faster to ship file moves | No undo capability, no crash recovery, manual cleanup after failures | Never -- irreplaceable files demand this |
| Hardcoded Docker UID/GID | Works on your machine | Breaks on any other machine, files owned by wrong user | Never -- parameterize from the start |
| Storing AI proposals as ephemeral (not persisted) | Less database complexity | Lose approval history, cannot audit decisions, proposals regenerated inconsistently | Never -- proposals are the core workflow artifact |
| Alembic migrations skipped for "quick" schema changes | Faster iteration | Dev/prod schema drift, migration failures in production | Never -- always generate migrations from model changes |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Mutagen (metadata reading) | Using `ID3()` directly which throws `ID3NoHeaderError` on files without ID3 headers | Use `mutagen.File()` which auto-detects format, or catch `ID3NoHeaderError` and fall back gracefully |
| Mutagen (encoding) | Assuming all ID3 tags are UTF-8 | Handle ID3v2.3 (Latin-1/UTF-16) and ID3v2.4 (UTF-8) differences; normalize to UTF-8 on read |
| LLM API (rename proposals) | No timeout or retry logic; one API failure blocks the entire batch | Use per-request timeouts, exponential backoff with jitter, and process files independently so one failure does not block others |
| PostgreSQL COPY | Passing Python objects directly to COPY | Pre-serialize to TSV/CSV format with proper escaping; use `psycopg.copy` with proper type handling |
| Docker volume mounts | Mounting the entire music collection read-write to all services | Mount read-only everywhere except the dedicated file-mover service; principle of least privilege |
| librosa on Python 3.13 | Importing librosa and getting `ModuleNotFoundError` for `aifc`/`sunau` | Install `standard-aifc` and `standard-sunau` packages explicitly in pyproject.toml |
| Chromaprint/fpcalc | pyacoustid imports fine but fingerprinting fails at runtime | Install `chromaprint-tools` in Dockerfile; test fingerprinting in CI with a sample audio file |
| FFmpeg in Docker | librosa audio loading fails silently or with cryptic error | Install `ffmpeg` in Docker image; test with actual audio file decode in CI |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Hashing full files into memory | OOM kills, swap thrashing | Chunked reading (64KB blocks) | First file over 1GB |
| Individual INSERTs in a loop | Ingestion takes hours | Use COPY or batch INSERT (1000-5000 rows) | Over 10K files |
| Unbounded worker pool | Disk I/O saturation, connection exhaustion | Bounded pool (4-8 workers), connection pooling | Over 50 concurrent operations |
| Querying during bulk ingestion | Admin UI hangs, 20+ second queries | Separate read/write connection pools, or ingest during off-hours | Over 50K rows being inserted |
| Full table scan for dedup checks | Dedup phase takes hours | B-tree index on sha256 column; size-first pre-filter | Over 100K files |
| Loading all records into admin UI | Browser tab crashes, API timeout | Server-side pagination, lazy loading, virtual scrolling | Over 1K records in a single response |
| Enqueueing 200K jobs at once | Redis memory exhaustion | Enqueue in batches (1000 at a time); use job result TTL to auto-expire | Over 50K queued jobs |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Path traversal in file move operations | Filename writes outside the music directory | Validate ALL destination paths are within the configured music root; use `Path.resolve()` and check `.is_relative_to(root)` |
| Storing raw file paths in API responses | Leaks server filesystem structure | Return file IDs and relative paths only; never expose absolute host paths |
| Running Docker containers as root | Container escape gives host root access | Use non-root user in Dockerfile, `user:` in docker-compose.yml |
| No rate limiting on AI API calls | Runaway batch drains API budget in minutes | Set per-minute and per-day API call limits; alert on unusual spend |
| Unsanitized filenames in subprocess calls | Command injection via filenames with backticks, semicolons, or dollar-parens | Never use `shell=True` with user-derived filenames; use `pathlib` and list-form subprocess calls exclusively |
| Compromised PyPI packages | Credential theft, data exfiltration from containers with filesystem access | Pin exact versions with verified hashes; run pip-audit in CI; isolate LLM containers from file-access containers |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Showing all 200K files in the approval queue | Overwhelming, unusable interface | Group by directory, album, or AI confidence score; paginate with filters |
| No progress indication during batch operations | User thinks the system is frozen, restarts it, causes corruption | WebSocket or polling-based progress bar showing files processed / total |
| Binary approve/reject without edit | User sees a 90% correct name but cannot fix the 10% | Allow inline editing of proposed names before approval |
| No undo after approval | User approves a batch, realizes a naming pattern was wrong | Implement undo within a time window using the operations journal |
| Showing SHA256 hashes to the user | Meaningless to humans in the approval UI | Show human-readable duplicate indicators: "Same as: [other file path], identical content" |

## "Looks Done But Isn't" Checklist

- [ ] **File ingestion:** Often missing handling for symlinks -- verify symlinks are either resolved or explicitly skipped, not silently followed into infinite loops
- [ ] **Deduplication:** Often missing cross-format duplicates -- verify that same-content files in different formats (mp3 vs m4a) are flagged as near-duplicates, not only exact hash matches
- [ ] **Batch rename:** Often missing filename collision detection -- verify that two files proposed to have the same destination path are caught BEFORE execution, not during
- [ ] **Admin UI:** Often missing concurrent session protection -- verify that two browser tabs approving the same file do not cause double-moves (optimistic locking)
- [ ] **Docker setup:** Often missing health checks -- verify that the database is actually ready before the ingestion service starts (use `healthcheck` and `depends_on` with `condition: service_healthy`)
- [ ] **AI proposals:** Often missing handling for API failures mid-batch -- verify that a timeout on file 5,000 does not lose proposals for files 1-4,999
- [ ] **File moves:** Often missing disk space checks -- verify that destination volume has sufficient space BEFORE starting a batch move
- [ ] **Path handling:** Often missing maximum path length checks -- verify that proposed paths do not exceed filesystem limits (255 chars per component, ~4096 total on ext4)
- [ ] **Corrupted audio files:** Often missing graceful handling -- verify that one malformed file does not kill the entire worker pipeline; mark as FAILED and continue

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Partial batch move (crash mid-operation) | LOW if journal exists, HIGH if not | Replay operations journal: complete pending moves or roll back incomplete ones based on journal state |
| Permission mismatch (files owned by root) | MEDIUM | `chown -R` on the music directory, update docker-compose.yml with correct UID/GID, re-test |
| Encoding corruption (paths mangled) | HIGH | Use `original_path_bytes` column to reconstruct original paths; re-ingest affected files with correct normalization |
| Database out of sync with filesystem | MEDIUM | Full re-scan: walk filesystem, hash all files, reconcile with database, flag discrepancies for human review |
| AI proposals inconsistent across batches | LOW | Proposals are immutable records; inconsistency is cosmetic. Regenerate unapproved proposals with new prompt version, leave approved ones unchanged |
| OOM during bulk hashing | LOW | Restart with chunked hashing; the journal shows which files were already hashed, skip those |
| Alembic migration drift | MEDIUM | Run `alembic check` to detect drift; generate corrective migration; never manually edit production schema |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Irreversible file operations | Phase 1: Database schema | Operations journal table exists with source, destination, hash, status, timestamp columns |
| Hash-only dedup deletes wrong file | Phase 1: Dedup design + Approval UI phase | No auto-delete code path exists; all deletions route through admin approval |
| Unicode filename corruption | Phase 1: File ingestion | Integration tests with non-ASCII filenames pass; NFC normalization applied at ingestion boundary |
| Docker volume permissions | Phase 1: Docker Compose setup | Files created by container are owned by host user; verified with `ls -la` after first test run |
| AI rename inconsistency | AI integration phase | Proposals table has model_version, prompt_version columns; approved proposals are immutable |
| Unbounded memory during hashing | Phase 1: File ingestion | Hashing function uses chunked reads; tested with a 4GB+ file without exceeding 100MB RSS |
| Event loop blocking | Analysis pipeline phase | Audio processing wrapped in `run_in_executor`; event loop blocked warnings absent |
| PostgreSQL bulk insert slowdown | Phase 1: Ingestion pipeline | 200K records insert in under 5 minutes; benchmarked with COPY |
| Parallel analysis without backpressure | Analysis pipeline phase | Worker pool bounded; queue depth monitored; connection pool sized to match workers |
| Supply chain risk | Phase 1: CI setup | pip-audit runs in CI; exact versions pinned with hashes; security scanning active |
| Missing system deps in Docker | Phase 1: Docker image | CI tests audio processing with real files inside the Docker image |

## Sources

- [Beets: Filename Encoding Hell](https://beets.io/blog/paths.html) -- detailed post-mortem on path encoding in music library management
- [Beets: Managing Huge Music Libraries](https://discourse.beets.io/t/using-beets-to-manage-huge-music-libraries-best-practices-and-suggestions/2598) -- community discussion on large library pitfalls
- [Understanding Beets](https://somas.is/notes/understanding-beets/) -- analysis of why music library management is inherently complex
- [Mutagen ID3 Documentation](https://mutagen.readthedocs.io/en/latest/user/id3.html) -- encoding and version compatibility details
- [PostgreSQL Bulk Loading](https://www.cybertec-postgresql.com/en/postgresql-bulk-loading-huge-amounts-of-data/) -- COPY vs INSERT performance
- [13 Tips for PostgreSQL Insert Performance](https://www.tigerdata.com/blog/13-tips-to-improve-postgresql-insert-performance/) -- index and trigger impact on bulk loads
- [Multiprocessing Race Conditions in Python](https://superfastpython.com/multiprocessing-race-condition-python/) -- file corruption from concurrent access
- [Docker Compose Permissions](https://dev.to/visuellverstehen/docker-docker-compose-and-permissions-2fih) -- UID/GID mismatch patterns
- [SHA-256 Deduplication Considerations](https://transloadit.com/devtips/efficient-file-deduplication-with-sha-256-and-node-js/) -- performance and correctness considerations
- [Python Filesystem Encoding History](https://vstinner.github.io/painful-history-python-filesystem-encoding.html) -- cross-platform path encoding challenges
- [Chasing Ghosts: Decoding ID3 Tags](https://danielszpisjak.com/blog/chasing-ghosts-decoding-id3-tags/) -- ID3 encoding edge cases
- [librosa Python 3.13 issue #1883](https://github.com/librosa/librosa/issues/1883) -- compatibility workarounds

---
*Pitfalls research for: Music collection management and batch file processing*
*Researched: 2026-03-27*
