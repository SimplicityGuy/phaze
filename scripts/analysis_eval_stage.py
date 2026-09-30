"""Stage the evaluation corpus from nox to vox without exposing archive paths.

This is an operator-run command for the future isolated measurement window. It
reads paths from Phaze in a read-only transaction, copies through nox's analysis
container to host scratch, streams the SHA-named copies to vox scratch, and
verifies every digest there. No path from the production DB is printed or saved.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import shutil
import subprocess
from typing import Any

from scripts.analysis_eval import load_manifest


_COPY_CODE = """import hashlib,json,pathlib,subprocess,sys
row=json.load(sys.stdin)
dest=pathlib.Path(row['directory']) / row['name']
dest.parent.mkdir(parents=True,exist_ok=True)
if not dest.exists():
 result=subprocess.run(['docker','cp','phaze-agent-worker-analyze:'+row['path'],str(dest)],capture_output=True)
 if result.returncode: raise SystemExit('docker copy failed')
with dest.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
if digest!=row['sha256']: raise SystemExit('staged digest mismatch')
print(row['sha256'])
"""

_VERIFY_CODE = """import hashlib,json,pathlib,sys
data=json.load(sys.stdin)
directory=pathlib.Path(data['directory'])
for item in data['items']:
 path=directory / (item['sha256']+'.'+item['format'])
 with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
 if digest!=item['sha256']: raise SystemExit('transferred digest mismatch')
print(len(data['items']))
"""


def _ssh(host: str, command: str, *, input_text: str | None = None) -> str:
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh executable is required")
    result = subprocess.run(  # noqa: S603 -- fixed hosts and quoted remote commands
        [ssh, "-o", "BatchMode=yes", f"datum@{host}", command], input=input_text, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"remote operation failed on {host}; inspect the host directly")
    return result.stdout.strip()


def _source_paths(items: list[dict[str, Any]]) -> dict[str, str]:
    identifiers = ",".join("'" + item["file_id"] + "'::uuid" for item in items)
    sql = f"BEGIN READ ONLY; SELECT json_agg(json_build_object('id',id,'path',current_path)) FROM files WHERE id IN ({identifiers}); COMMIT;"  # noqa: S608 -- manifest IDs passed through uuid.UUID in load_manifest
    output = _ssh("lux", "docker exec -i postgres psql -X -q -U phaze -d phaze -v ON_ERROR_STOP=1 -At", input_text=sql)
    rows = json.loads(output)
    if len(rows) != len(items):
        raise RuntimeError("not every manifest ID exists in Phaze")
    return {row["id"]: row["path"] for row in rows}


def _scratch_path(raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute() or len(path.parts) < 3 or path.parts[1] != "scratch" or ".." in path.parts:
        raise ValueError("staging directories must be absolute paths under /scratch")
    return str(path)


def stage(items: list[dict[str, Any]], nox_dir: str, vox_dir: str) -> None:
    paths = _source_paths(items)
    # datum can sudo without a password on these hosts, but /scratch itself is
    # root-owned. Create only the named, private benchmark directories.
    for host, directory in (("nox", nox_dir), ("vox", vox_dir)):
        _ssh(host, "sudo -n install -d -o datum -m 0700 -- " + shlex.quote(directory))
    for item in items:
        name = f"{item['sha256']}.{item['format']}"
        payload = {"directory": nox_dir, "name": name, "path": paths[item["file_id"]], "sha256": item["sha256"]}
        _ssh("nox", "python3 -c " + shlex.quote(_COPY_CODE), input_text=json.dumps(payload))
        print(f"staged {item['file_id']}")  # noqa: T201

    # Paths passed to tar are entirely constructed from validated digest/format fields.
    names = [f"{item['sha256']}.{item['format']}" for item in items]
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh executable is required")
    source = subprocess.Popen(  # noqa: S603 -- fixed source host and SHA-derived tar member names
        [ssh, "-o", "BatchMode=yes", "datum@nox", shlex.join(["tar", "-C", nox_dir, "-cf", "-", *names])], stdout=subprocess.PIPE
    )
    assert source.stdout is not None
    target = subprocess.run(  # noqa: S603 -- fixed target host; scratch path is validated
        [ssh, "-o", "BatchMode=yes", "datum@vox", shlex.join(["tar", "-C", vox_dir, "-xf", "-"])],
        stdin=source.stdout,
        capture_output=True,
        check=False,
    )
    source.stdout.close()
    source_exit = source.wait()
    if source_exit or target.returncode:
        raise RuntimeError("scratch transfer failed; inspect both hosts directly")
    verified = _ssh("vox", "python3 -c " + shlex.quote(_VERIFY_CODE), input_text=json.dumps({"directory": vox_dir, "items": items}))
    if int(verified) != len(items):
        raise RuntimeError("vox verification count mismatch")
    print(f"verified {verified} SHA-addressed files on vox")  # noqa: T201


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--nox-dir", required=True)
    parser.add_argument("--vox-dir", required=True)
    args = parser.parse_args(argv)
    items = load_manifest(args.manifest)
    stage(items, _scratch_path(args.nox_dir), _scratch_path(args.vox_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
