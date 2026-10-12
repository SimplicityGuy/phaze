# Accepted quality increment

This increment (`phaze-zv309`) delivers eight already accepted children of the ongoing
quality implementation epic `phaze-75qpj`. Assembly and verification share batch
`accepted-increment`: `phaze-h8he1` and `phaze-ej427`.

## Frozen sources and provenance

- Main at assembly: `75d9e1bf00bede68def30089305ffafc73a48eed`.
- Accepted source: `c582a8fb99863bf5d30079db68e256653701faa2`.
- Accepted source tree: `d90aa95a72742c6dee0faa8a0652d7db0414bd7a`.
- Import uses one conventional `--no-ff` merge. Both source histories, original
  authors, and accepted-child merge bubbles remain reachable; no originals are rewritten.
- The only source-import conflict is `docs/README.md`: retain both main's partial-save
  and reviewed-revision guide links and the accepted source's coverage-generation link.
  Add this scope guide to the same index. No executable conflict resolution is required.
- The merge-aware hook flags existing literal `=======` docstring heading underlines in
  `tests/browser/conftest.py` and `tests/browser/axe.py`. Replace those two underlines
  with `-------`; executable ASTs remain identical with module docstrings omitted.
  Preserve the initial hook failure and rerun all hooks without exemptions.

| Included bead | Accepted scope |
| --- | --- |
| `phaze-k04p7` | Measurement baseline and evidence readback |
| `phaze-kl9i4` | Duplicate-contract coverage |
| `phaze-n78df` | Capture eligibility and SQL recovery coverage |
| `phaze-ola2z` | Configuration failure and cancellation cleanup coverage |
| `phaze-63cuy` | Pipeline/lane unknown-state rendering coverage |
| `phaze-ihvi5` | Branch diagnostics with existing policy preserved |
| `phaze-a393q` | Coverage-generation capability disposition: nonadoption |
| `phaze-b6csj` | Path fallback and root termination controls |

All eight were independently reviewed, validated on their exact source, submitted,
approved, integrated, and closed through Beadhive before the source freeze. Six canonical
repair PRs are already on main and are preserved by assembly. Their earlier gates do not
constitute validation of this assembled candidate.

## Validation and claim boundary

The exact assembled candidate requires independent assembly review and an authoritative
`just check-all` gate. Retain actual pytest populations, ordered disjoint node union,
raw JUnits, coverage contexts and denominator. Overall line coverage must be strictly
above 95%; existing branch safeguards and real-consumer assertions remain in force.
Candidate coverage claims must come from that candidate's retained evidence. This scope
document records no candidate coverage percentage before its gate completes.

Runtime reductions and accepted-bead velocity gains remain unproven. This increment adds
coverage, measurement, diagnostics, and explicit capability limits; it does not establish
an optimization gain or complete the original quality goal.

## Work still tracked separately

Excluded pending beads: `phaze-tmf6u`, `phaze-2jvjd`, `phaze-q8j9r`, `phaze-axfxb`,
`phaze-al4ct`, `phaze-mbtzg`, `phaze-l5uve`, and `phaze-x45ht`.
The original epic remains open. Final assembled verification `phaze-47gba` (G0) and
adoption review `phaze-8d5bs` (A0) remain mandatory as one final grouped unit. Landing this
increment closes only its delivery envelope and does not reparent or close unfinished work.
