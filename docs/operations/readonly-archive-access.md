# Read-only access to archive audio

Date: 2026-11-11. Bead: phaze-luy62.

Consumers that need to read archive audio (for example to refine track offsets inside a long set)
use a **read-only view** of the file-server host's archive. They never get a writable path, and
they resolve stored phaze paths with the published per-agent path prefixes.

## 1. Resolve a stored path

Stored `FileRecord` paths are **container** paths: they start with one of the owning agent's
`scan_roots`, for example `<archive-mount-in-container>/...`. The mapping is published to the
read-only analytics role, not over HTTP: connect as `analytics_ro` and read
`analytics_agent_path_prefixes` (one row per agent and prefix: `agent_id`, `name`, `kind`, `active`,
`prefix_ordinal`, `path_prefix`). `analytics_files.agent_id` says which agent owns a file. Both are
documented, with the stability rule, in [Read-only analytics access](../analytics-read-only-access.md).

```sql
SELECT DISTINCT ON (f.file_id) f.file_id, f.current_path, p.path_prefix
FROM analytics_files f
JOIN analytics_agent_path_prefixes p ON p.agent_id = f.agent_id AND p.active
 AND starts_with(f.current_path, rtrim(p.path_prefix, '/') || '/')
ORDER BY f.file_id, p.prefix_ordinal;
```

A prefix matches only on a path boundary: `/data/music` contains `/data/music/set.mp3` but not
`/data/music2/set.mp3`, which is why the query appends `/` to the prefix instead of comparing the
bare strings. When more than one of the agent's prefixes contains a path, the first one
(lowest `prefix_ordinal`) wins, the same order phaze's own containment check walks
(`src/phaze/services/containment.py`).

To open a stored path, replace its leading prefix with the consumer's own mount point of the
read-only view (section 2) and read the result. A path that starts with none of its agent's prefixes
is not resolvable: skip it, do not guess.

## 2. The read-only view

One mechanism per consumer location; both give the same guarantee, that a write is refused by the
kernel, not by consumer code.

| Consumer runs | Mechanism | Where it is enforced |
|---|---|---|
| On `host-store` itself | Bind mount with `:ro`: `docker-compose.readonly-archive.example.yml` | Container mount flag |
| On another host | Read-only export of `<archive-mount>` from `host-store` (NFS `ro`, or an SMB share with write disabled), mounted read-only on the consumer | Export on the server **and** mount flag on the client |

### Operator steps (NOT performed by any code change; run by the operator on the real hosts)

These change a real host, so they are listed here and nowhere automated.

1. On `host-store`, add a read-only export of `<archive-mount>` limited to the consumer host or
   subnet. For NFS the option set is `ro,root_squash,no_subtree_check`; never `rw`, never
   `no_root_squash`.
2. On the consumer host, mount it read-only, for example `mount -t nfs -o ro,nosuid,nodev,noexec
   host-store:<archive-mount> <consumer-mount>` (make it persistent in the host's own mount config).
3. Start the consumer with `ARCHIVE_MOUNT=<consumer-mount>` through
   `docker-compose.readonly-archive.example.yml` (or mount `<consumer-mount>` with `:ro` in the
   consumer's own compose).
4. Run the write-refusal check below on the consumer side, against the mount point as the consumer
   sees it. Record its `PASS` line.

## 3. Check that writes are refused

```bash
python scripts/check_readonly_archive.py <consumer-mount>
```

It tries to create one empty, uniquely named probe file. Exit 0 and `PASS: ... write refused
(EROFS)` mean the write was refused. `FAIL: WRITE SUCCEEDED` (the probe file is removed again)
means the mount is writable: stop and fix the export or mount flag. Any other error also fails,
since an unrecognised error does not prove read-only. It never reads archive content.

Rehearse it locally against a throwaway directory, never a real host:

```bash
D=$(mktemp -d)
docker run --rm -v "$D:/data/music:ro" -v "$PWD/scripts/check_readonly_archive.py:/c.py:ro" \
  python:3-alpine python /c.py /data/music        # PASS ... (EROFS)
docker run --rm -v "$D:/data/music:rw" -v "$PWD/scripts/check_readonly_archive.py:/c.py:ro" \
  python:3-alpine python /c.py /data/music        # FAIL: WRITE SUCCEEDED (negative control)
```

`tests/agents/deployment/test_readonly_archive.py` pins the `:ro` mount in the example compose and the
probe's classification; `tests/integration/test_migrations/test_096_analytics_agent_path_prefixes.py` pins the view.
