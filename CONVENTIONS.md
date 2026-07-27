# Conventions

## No local identifiers in tracked files

phaze is developed against a real personal music archive on real hardware. Investigation output —
spike docs, planning notes, debug write-ups, benchmark records — is where traces of that archive
accumulate, because the honest way to report a measurement is to say what it was measured on.

**Do not commit them.** Scrub as you write, not afterwards.

### Never commit

- **Filenames, directory names or absolute paths from the real archive.** This is the category that
  matters most. A release name in a log excerpt, a staging-mount path in a traceback, a directory
  name in a table of sampled files.
- **Content digests and file UUIDs taken from live data** — fingerprint hashes, `sha256` values,
  database row ids pasted from a real query.

### Acceptable

- **Invented example filenames** that illustrate a naming format — `Artist - Event - Title
  (2024).mp3`. These teach the format without evidencing what is in the collection.
- **Synthetic test fixtures** — `song.mp3`, `dup.mp3`, `reference.wav`.
- **Host and account names in local instruction material.** Committed source, scripts and published
  docs should refer to hosts by role instead.

### Use these placeholders

Follow the vocabulary already established in `docs/spikes/`, so scrubbed documents read
consistently:

| For | Use |
| --- | --- |
| Individual tracks | `<track-01>`, `<track-02>`, … |
| Concert sets / long recordings | `<set-01>`, `<set-02>`, … |
| Archive mount, host side | `<archive-mount>` |
| Archive mount, in-container | `<archive-mount-in-container>` |
| Local scratch directories | `<scratch>/…` |
| Fingerprint digests | `fp_<hash-1>`, `fp_<hash-2>`, … |
| File UUIDs | `<uuid-1>`, `<uuid-2>`, … |
| Hosts, where a role name will not do | `host-prod`, `host-store` |

### Replace identifiers, never quantities

This is the rule that gets broken when scrubbing is rushed, and it destroys the value of the
document it was meant to protect.

Every measured value stays exact — row counts, durations, latencies, sample sizes, percentages. The
point of an investigation record is that its conclusions are checkable.

> **Good:** "36 files totalling 42.34 h, stratified across the duration distribution"
> **Bad:** "a few dozen files" — scrubbed, but now worthless as evidence

If a scrub changes a number, it is a bug in the scrub. A useful check after any pass: diff the
numeric tokens of the before and after, and confirm the only digits lost were part of a removed
identifier.

### Scope

Any tracked file: spike and design docs, `.planning/**`, source comments, scripts, SQL. **Also
commit messages and PR bodies** — they are just as permanent and just as public as the files.

### The history caveat

Scrubbing a file does not scrub git history. Once an identifier is committed, removing it from the
working tree leaves it fully readable via `git show <old-sha>`, and removing it from history means
a rewrite and a force-push — which is disruptive, and on a shared branch may not be possible at all.

**Prefer never committing the identifier over fixing it later.** When writing up a measurement, use
the placeholder in the first draft rather than the real name you intend to replace before pushing.
