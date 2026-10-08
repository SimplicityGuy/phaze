# phaze-h62gx — contains-style companion stem matching

Measured 2026-10-08 UTC against the shipped linking chain at `c3671920` and the
2026-10-07 survey snapshot. This is an implementer's **NO-GO recommendation for a
broad production rule**, pending review of the changed population. The checked
labels support the motivating shape, but do not establish precision across that
population. No production linking rule changes in this bead.

## Frozen candidate and baseline

The scratch candidate was fixed before its first replay: after junk/duplicate
veto, content references and exact own-folder stem matching, but before the
existing tracklist collection guard, try own-folder containment. Compute the
shipped `companion_match_key` of the companion stem and each media stem. A
nonempty companion key must be a contiguous substring of the media key; return
**all** matching own-folder media. There is no new minimum key length, date
guard, uniqueness condition, token boundary or cross-folder search. The shipped
key's case folding, NFC normalization, scene-index and copy-marker stripping,
and removal of non-alphanumeric characters remain unchanged.

The baseline calls the real `services/companion_linking.py::link_companion`.
The candidate copies that function into scratch and inserts only this step.
The non-tracklist collection tightening proposed in phaze-cdoft is absent from
both. Production re-derive ordering is preserved: companions with own-folder
media first, then the remaining companions, with identifier ordering within
each phase. Each view resets its linked-content set. The duplicate-veto ON view
derives that set from successful links; OFF isolates matching recall from the
policy of suppressing identical orphan copies.

## Inputs and reproducibility

All **224** operator-checked labels survive: 164 original and 60 addendum, with
their final truth maps, survey file metadata, extracted features and classification
inputs. The original implementation replay directory is missing; this is a
reconstruction using the shipped chain and surviving survey inputs, not a claim
to reproduce that lost script. Private copies and executable replay are under
`<scratch>/phaze-h62gx/`; the originals were read only. The release-note NOT-junk
label correction is retained. Label names below are synthetic positional labels.

The snapshot supplies **104,247 media** and **45,390 companions**. Nineteen
unreadable feature records are excluded, leaving **45,371** replayed companions.
References are reconstructed from the survey's CUE, M3U, PLS and text-token
fields with the shipped basename and media-extension handling. Junk and tracklist
flags are survey classification inputs, not fresh bytes or production stored
features. In particular, this does not incorporate a later classification fix
by pretending the survey was re-read. The truth maps contain **176 positive
companions**, **48 none labels** and **356 correct companion/media pairs**.

The replay emits aggregate results and a private pair-level prediction map. For
each label, correct pairs are the intersection of predicted and truth media;
precision is correct/predicted pairs, companion recall counts any truth hit,
and media-pair recall is correct/all truth pairs. The table combines the original
and addendum strata without changing their truth. Results were computed before
a private JSON serializer failed; its set serialization was corrected and the
same frozen candidate was rerun to completion. The completed run is the evidence.

## Checked-label results

| Duplicate veto | Method | Correct / predicted pairs | Companion recall | Media-pair recall | None kept unlinked |
| --- | --- | ---: | ---: | ---: | ---: |
| OFF | Baseline | 349 / 349 (100%) | 172 / 176 | 349 / 356 | 48 / 48 |
| OFF | Contains candidate | 351 / 351 (100%) | 173 / 176 | 351 / 356 | 48 / 48 |
| ON | Baseline | 275 / 275 (100%) | 98 / 176 | 275 / 356 | 48 / 48 |
| ON | Contains candidate | 277 / 277 (100%) | 99 / 176 | 277 / 356 | 48 / 48 |

Exactly **one** checked label changes in either view: `label-040` gains **two**
correct media pairs and loses none. Across all 224 labels the candidate introduces
**zero false pairs**, removes **zero correct pairs**, and makes **zero none labels**
linked. The original stratum changes OFF from 243/243 pairs and 114/118 companions
found to 245/245 and 115/118; ON from 204/204 and 75/118 to 206/206 and 76/118.
The addendum stays OFF 106/106 and 58/58, ON 71/71 and 23/58.

## Population effect and current dump-folder scope

| Duplicate veto | Baseline pairs | Candidate pairs | Gained | Lost | Changed companions |
| --- | ---: | ---: | ---: | ---: | ---: |
| OFF | 52,366 | 52,462 | 273 | 177 | 155 |
| ON | 49,805 | 49,901 | 273 | 177 | 155 |

In both duplicate modes the candidate wins the contains step for **1,477**
companions, and folder fallback winners fall from **7,447** to **6,104**.
The `unlinked` step outcomes fall OFF from **1,818** to **1,684**, and ON from
**1,734** to **1,600**. These exclude junk and duplicate-veto outcomes; they are
not the count of all companions with zero links. There are **660**
collection-folder companions in the replay. Of the **155** changed companions,
**154** have no checked label: the **273** gained and **177** lost population pairs
cannot be classified as correct or false from these labels. Net gain is **96**,
not proof that every gained pair is correct or every removed pair was wrong.

The reconstructed ON baseline is **49,805**, not the historical **49,827** in the
molecule's original replay. That discrepancy is a reconstruction limitation,
not a claimed historical reproduction or a measurement of production drift.

In the snapshot, the **18,537-media** largest flat folder receives **1** pair
under each baseline/candidate and duplicate-policy view. None of those pairs is
in the checked labels, so this run does not establish that pair's correctness.
The candidate introduces **0** additional pairs into that snapshot folder.

A separate scoped production SELECT, measured 2026-10-08 UTC, ran with
`default_transaction_read_only=on`, an explicit read-only transaction and a
60-second statement timeout. A server-side GROUP BY selected the largest flat
media folder and returned only counts: **18,537 media**, **0 companions** in that
same folder and agent. This is a **largest-flat-folder proxy**, not a verified
identity match to the checked dump folder. Because the frozen candidate only
selects own-folder media, it introduces **0 contains links** into that measured
folder: the eligible population is empty. It introduces no cross-folder links
by construction; later chain behavior still needs the complete replay.

The production schema is **077**, and `companion_content_features` is absent.
Thus the full current production chain and its false-link population were not
measured from stored features. Existing production pairs are not ground truth.
The snapshot's checked-label zero false links and the current proxy's zero
eligible companions are distinct evidence, with distinct populations.

## Recommendation and acceptance mapping

**NO-GO for adding this unrestricted rule now.** The measured checked-label
precision stays 100%, and one motivating positive is recovered. However, only
one of 155 changed companions is checked, and punctuation-free substring keys
can match partial numbers or short generic names without a date or ambiguity
guard. Those are untested risks, not counted false links. Review the 154 unchecked
changed companions before choosing an implementation criterion; any narrower
rule requires a separately frozen candidate and replay rather than tuning this
one against its single successful label. The operator owns that decision.

Acceptance 1 is exercised by the exact-224-label baseline/candidate replay,
including pair precision, companion and media-pair recall, duplicate-policy
views, checked false links and the scoped current largest-flat-folder query.
The current-folder identity and complete current production false-link count
remain explicitly outside what those measurements establish. Acceptance 2 is
this recommendation and the docs-only diff: no production rule change.
