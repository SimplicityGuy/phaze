# S6 — AGPL-3.0 vs phaze's MIT: compliance for clean-room and for sidecar

- **Bead:** `phaze-ytgo.5` (epic `phaze-ytgo` — AudioMuse-AI: clean-room vs sidecar, per purpose)
- **Date:** 2026-07-25
- **Tree:** `b051b3b`
- **Status:** investigation only. No product code, no `LICENSE` change, no `NOTICE` added, no
  `README` edit. Recommendations only.

> ## ⚠️ THIS IS NOT LEGAL ADVICE
>
> This is an **engineering compliance assessment** written by a software agent, not a lawyer. It
> reasons from the licence texts and from published interpretation guidance by the licences'
> own steward (the FSF). It is not an opinion of counsel, it creates no privilege, and it must
> not be relied on as a defence. Its purpose is to let `phaze-ytgo.7` (D1) **filter shapes on
> the record** rather than on intuition, and to identify the narrow set of questions that
> genuinely need a lawyer. Those questions are enumerated in
> [Needs counsel](#needs-counsel-do-not-resolve-these-in-a-bead) and **are not resolved here**.
>
> Where this document says "safe", read it as *"the licence text and the licensor's own
> published interpretation both point this way, and the alternative reading is not seriously
> argued"* — not as *"a court has held"*.

______________________________________________________________________

## Clean-room disclosure (read this before treating this doc as input)

**I did not read any AudioMuse-AI source code, and this document contains no AudioMuse
expression.** Specifically:

| Artifact | Read? | Why |
| -------- | ----- | --- |
| `AudioMuse-AI/LICENSE` | **yes** | The bead's subject matter. Reading a licence is not a clean-room breach. |
| `AudioMuse-AI/README.md` | **yes** — grepped for licence/registry strings only | To find the published image name. |
| `AudioMuse-AI/deployment/docker-compose.yaml` | **yes** — service/image lines only | To answer the "reference an upstream image" question concretely. |
| Repository **file listing** (names only) | yes | To confirm what kinds of artifact exist. |
| `docs/ARCHITECTURE.md`, `docs/ALGORITHM.md` | **no** | Not needed for this bead. |
| Any `.py` file | **no** | Not needed for this bead. |
| Any model binary | **no** | Not needed for this bead. |

The epic's design permits the sidecar spike to read AudioMuse source; **this spike did not need
to**, and deliberately did not, so that this document is unconditionally safe input for
`phaze-ytgo.2`, `.3`, `.6` and for any future implementation molecule. See
[V1](#v1--the-clean-room-seal-corrected-in-both-directions) for why that "safe as *input*"
property, not the reader's identity, is the thing the seal should actually be protecting.

______________________________________________________________________

## Question

phaze staying MIT is a **binding constraint** (Robert, 2026-07-24). This spike runs **before**
technical merit: a shape that does not survive the licence filter is unavailable for every
purpose regardless of how it benchmarks. Five questions:

1. **Clean-room.** What inputs keep an independent implementation clean? Does reading
   AudioMuse's published prose — which names algorithms, parameters and config variables —
   taint an implementation? **Confirm or correct** the seal this molecule already operates
   under.
2. **Sidecar.** Is AudioMuse-as-a-separate-networked-process mere aggregation or a derivative
   work? What does AGPL §13 require when the only user is the operator? And: does phaze's
   **public MIT repository** shipping a compose file / Helm reference / documentation that
   pulls an AGPL-3.0 image constitute a distribution trigger?
3. **Media-server shim.** If `phaze-ytgo.4` finds a Subsonic-compatible endpoint is needed,
   does code written *specifically to feed an AGPL application* carry greater derivative-work
   risk than a generic Subsonic endpoint?
4. **Model weights.** Licences travel with weights as well as code. What is the compliance
   position for models the clean-room path would need?
5. **Remedies.** What must phaze concretely do under each shape?

### The asymmetry, stated up front

Conflating the two directions is the single most common error in this area, so:

> **MIT code may be consumed *by* an AGPL project freely.** The FSF classifies the Expat (MIT)
> licence as "a lax, permissive non-copyleft free software license, **compatible with the GNU
> GPL**" ([E2](#e2--the-asymmetry-verified-against-the-fsfs-own-licence-list)). AudioMuse could
> vendor phaze's code tomorrow and owe phaze nothing but the copyright notice.
>
> **The constraint runs the other way.** AGPL §5(c) requires that a work based on the Program
> be licensed "as a whole, under this License … to the whole of the work, and all its parts,
> regardless of how they are packaged"
> ([E3](#e3--what-the-agpl-actually-triggers-on-verbatim)). There is no reciprocal path: phaze
> cannot absorb AGPL code and stay MIT.

Nothing below softens that. Everything below is about **where the boundary of "a work based on
the Program" actually falls**, which is a narrower question than "did we touch AGPL software".

______________________________________________________________________

## Method

Primary sources first, in this order of authority: **(1)** the licence texts themselves,
**(2)** statute, **(3)** the licence steward's published interpretation, **(4)** the state of
phaze's own tree. No forum opinion is cited or relied on anywhere in this document.

1. **Fetched the canonical AGPL-3.0 text** from the FSF and pinned it by hash:

   ```console
   $ curl -sS https://www.gnu.org/licenses/agpl-3.0.txt -o agpl-3.0.txt
   $ shasum -a 256 agpl-3.0.txt
   0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0  agpl-3.0.txt
   ```

   Every §-quote below is `sed`-extracted from that file, not recalled.

2. **Fetched AudioMuse-AI's `LICENSE` and diffed it byte-for-byte against the canonical text.**
   It is *not* a verbatim copy — the difference is material and is [E1](#e1--the-two-licences-verified).

3. **Extracted the FSF's own interpretation entries** from `gnu.org/licenses/gpl-faq.html` and
   `license-list.html` by anchor id, so each is quoted as published rather than paraphrased:
   `#MereAggregation`, `#AggregateContainers`, `#GPLPlugins`, `#AGPLv3InteractingRemotely`,
   `#UnreleasedModsAGPL`, `#InternalDistribution`, `#GPLOutput`, `#WhatCaseIsOutputGPL`,
   `#IfInterpreterIsGPL`, `#Expat`, `#apache2`, `#ms-pl`.

4. **Pulled 17 U.S.C. § 102(b) verbatim** for the clean-room question.

5. **Audited phaze's own tree for existing precedent** — and found a live one. phaze already
   ships an AGPL-3.0 engine as a sidecar ([E6](#e6--phaze-already-ships-an-agpl-30-engine-and-already-conveys-it)).

6. **Probed `ghcr.io` anonymously** to establish whether phaze's sidecar images are publicly
   distributed, rather than assuming from the workflow file.

7. **Resolved every model-weight licence by fetching the licensor's own metadata** — GitHub
   repository licence records, Hugging Face model-card `license` fields, and the MTG's own
   models page ([E7](#e7--model-weight-licences-resolved-at-source)).

### What could NOT be established, and why

- **No AudioMuse source was read** (by choice — see the disclosure above), so this document
  makes **no** claim about how AudioMuse is internally structured, and cannot assess whether
  any *specific* AudioMuse component is separable.
- **Which exact model binaries AudioMuse ships in a given release was not verified.** Its
  `LICENSE` addendum enumerates three model families; I did not download a release to confirm
  the enumeration is complete. Where that matters below it is flagged, not assumed.
- **No jurisdictional analysis outside the United States.** Section [E5](#e5--clean-room-doctrine-the-statute-not-folklore)
  notes the EU parallel because phaze's repository is world-visible, but the reasoning here is
  US-copyright-shaped. That limitation is real and is listed under
  [Needs counsel](#needs-counsel-do-not-resolve-these-in-a-bead).
- **No opinion on litigation risk, damages, or enforcement likelihood.** Out of scope for an
  engineering assessment and squarely a lawyer's question.

______________________________________________________________________

## Evidence

### E1 — The two licences, verified

**phaze** — `LICENSE` at the repository root is the MIT (Expat) licence, `Copyright (c) 2026
Robert Wlodarczyk`. The repository is **public**, confirmed against the GitHub API rather than
assumed:

```console
$ curl -sSL https://api.github.com/repos/SimplicityGuy/phaze | ...
private: False | visibility: public | license: MIT | fork: False
```

**AudioMuse-AI** — its `LICENSE` is the AGPL-3.0, but **not a verbatim copy**:

```console
$ diff <(head -620 agpl-3.0.txt) <(head -620 audiomuse-LICENSE.txt) && echo IDENTICAL
IDENTICAL
```

Lines 1–620 — the preamble and the **entire operative body through
`END OF TERMS AND CONDITIONS`** — are byte-identical to the FSF text. What differs is the tail:
AudioMuse **replaces** the non-operative "How to Apply These Terms to Your New Programs"
appendix with an addendum about bundled model weights. Quoted in full because it is the single
most load-bearing piece of evidence in this document:

```text
----------------------------------------------------------------------
ADDITIONAL NOTICE: INCLUDED MACHINE LEARNING MODELS
----------------------------------------------------------------------

This Program distributes pre-trained machine learning models which are
governed by their own respective licenses. These binary could be NOT
licensed under the GNU AGPLv3.

From AudioMuse-AI v0.9.0 the following models are included and you agree to
the following terms:

1. MUSICNN MODELS:
   - License: ISC
   - Official Project: https://github.com/jordipons/musicnn

2. LAION-CLAP MODEL (only Text Head):
   - License: CC0 1.0 (Public Domain)
   - Official Project: https://github.com/LAION-AI/CLAP

3. AudioMuse-AI-DCLAP MODELS (only Audio Head):
   - License: AGPLv3
   - Official Project: https://github.com/NeptuneHub/AudioMuse-AI-DCLAP

...

SUMMARY FOR USERS:
The software code is Open Source under AGPLv3. However, the complete
distribution (including Docker images or releases containing these files)
depends from the model weight used.
```

**Three findings follow immediately:**

1. **The operative terms are stock AGPL-3.0.** No additional permission under §7, no linking
   exception, no commercial dual-licence offer for the *code*. There is no negotiated escape
   hatch to lean on.
2. **The removed appendix is non-operative.** It sits after `END OF TERMS AND CONDITIONS` and
   is instructional. Removing it changes nothing about the grant.
3. **AudioMuse asserts AGPL-3.0 over a set of model weights** — the DCLAP audio head. This is
   the finding that bites `phaze-ytgo.3`, and it is handled at [E7](#e7--model-weight-licences-resolved-at-source)
   and [V4](#v4--model-weights-one-blocking-checkpoint-and-a-clean-substitute).

### E2 — The asymmetry, verified against the FSF's own licence list

From `gnu.org/licenses/license-list.html#Expat`:

> "This is a lax, permissive non-copyleft free software license, **compatible with the GNU
> GPL**. Some people call this license 'the MIT License' […]"

Compatibility here is **one-directional in effect**: MIT-licensed code can be combined into a
GPL/AGPL work (the result is licensed AGPL as a whole), but the reverse combination has no
permission. AGPL §5(c) is the operative bar, quoted next.

### E3 — What the AGPL actually triggers on, verbatim

Four passages do nearly all the work. Line numbers are into the pinned canonical text.

**§0, the definition of "convey" (lines 87–89)** — the trigger for every distribution
obligation:

> To "convey" a work means any kind of propagation that enables other parties to make or
> receive copies. **Mere interaction with a user through a computer network, with no transfer
> of a copy, is not conveying.**

**§2, on output (lines 146–149)** — the passage that decides whether AudioMuse's embeddings are
encumbered:

> This License explicitly affirms your unlimited permission to run the unmodified Program.
> **The output from running a covered work is covered by this License only if the output, given
> its content, constitutes a covered work.**

and (lines 152–153):

> **You may make, run and propagate covered works that you do not convey, without conditions**
> so long as your license otherwise remains in force.

**§5(c) and the aggregate paragraph (lines 210–231)** — the boundary of the copyleft:

> c) You must license the entire work, as a whole, under this License to anyone who comes into
> possession of a copy. This License will therefore apply, along with any applicable section 7
> additional terms, **to the whole of the work, and all its parts, regardless of how they are
> packaged.**
>
> […]
>
> A compilation of a covered work with **other separate and independent works, which are not by
> their nature extensions of the covered work, and which are not combined with it such as to
> form a larger program**, in or on a volume of a storage or distribution medium, is called an
> "aggregate" […] **Inclusion of a covered work in an aggregate does not cause this License to
> apply to the other parts of the aggregate.**

**§13, first paragraph (lines 540–551)** — and note the conditional opening, which is very
widely misquoted:

> Notwithstanding any other provision of this License, **if you modify the Program**, your
> modified version must prominently offer all users interacting with it remotely through a
> computer network (if your version supports such interaction) an opportunity to receive the
> Corresponding Source of your version […]

§13's obligation is **conditioned on modification**. Running a stock, unmodified AGPL program
as a network service does not, by the text, trigger the source-offer duty. The FSF states the
same, in the negative, at `#UnreleasedModsAGPL`:

> "The GNU Affero GPL requires that **modified versions** of the software offer all users
> interacting with it over a computer network an opportunity to receive the source."

### E4 — The FSF's own line between "one program" and "two programs"

`gpl-faq.html#MereAggregation` — the criterion, in the steward's words:

> "Where's the line between two separate programs, and one program with two parts? This is a
> legal question, which ultimately judges will decide. We believe that a proper criterion
> depends both on **the mechanism of communication** (exec, pipes, rpc, function calls within a
> shared address space, etc.) and **the semantics of the communication** (what kinds of
> information are interchanged). If the modules are included in the same executable file, they
> are definitely combined in one program. If modules are designed to run linked together in a
> shared address space, that almost surely means combining them into one program. **By
> contrast, pipes, sockets and command-line arguments are communication mechanisms normally
> used between two separate programs. So when they are used for communication, the modules
> normally are separate programs.** But if the semantics of the communication are intimate
> enough, exchanging complex internal data structures, that too could be a basis to consider
> the two parts as combined into a larger program."

Note honestly: the FSF opens by conceding this is undecided law. The criterion is the
licensor-side reading, not a holding.

`#AggregateContainers` — directly on the containerisation question:

> "When it comes to determining whether two pieces of software form a single work, does the
> fact that the code is in one or more containers have any effect? **No**, the analysis of
> whether they are a single work or an aggregate is unchanged by the involvement of
> containers."

`#GPLPlugins` — on fork/exec specifically:

> "**A main program that uses simple fork and exec to invoke plug-ins and does not establish
> intimate communication between them results in the plug-ins being a separate program.**"

`#AGPLv3InteractingRemotely` — what counts as remote interaction:

> "If the program is expressly designed to accept user requests and send responses over a
> network, then it meets these criteria. Common examples […] include web and mail servers,
> interactive web-based applications […]"

`#InternalDistribution` — on the single-operator case:

> "Is making and using multiple copies within one organization or company 'distribution'?
> **No**, in that case the organization is just making the copies for itself. […] However, when
> the organization transfers copies to other organizations or individuals, that is
> distribution."

`#GPLOutput` and `#WhatCaseIsOutputGPL` — on whether output is encumbered:

> "In general this is legally impossible; copyright law does not give you any say in the use of
> the output people make from their data using your program. […] **when a program translates
> its input into some other form, the copyright status of the output inherits that of the input
> it was generated from.**"
>
> "**The output of a program is not, in general, covered by the copyright on the code of the
> program.**"

### E5 — Clean-room doctrine: the statute, not folklore

17 U.S.C. § 102(b), fetched verbatim:

> "(b) **In no case does copyright protection for an original work of authorship extend to any
> idea, procedure, process, system, method of operation, concept, principle, or discovery,
> regardless of the form in which it is described, explained, illustrated, or embodied in such
> work.**"

The trailing clause is the whole answer to Q1. An algorithm is a *procedure*; a parameter
choice is a *fact about a procedure*; a technique is a *method of operation*. § 102(b) removes
all of them from the scope of copyright **"regardless of the form in which it is described"** —
including when the form is a `.py` file, and *a fortiori* when the form is an
`ARCHITECTURE.md`.

The EU rule is parallel (noted because phaze's repository is world-visible, not because this
document analyses EU law): Directive 2009/24/EC Art. 1(2) excludes from protection "ideas and
principles which underlie any element of a computer program, including those which underlie its
interfaces."

**What § 102(b) does *not* say** — and this is the part the seal should be built on:
§ 102(b) protects *ideas*, so **expression** remains protected. Identifiers, literal parameter
tables, comment text, and the selection-and-arrangement of a module structure are expression or
can contribute to an expression claim. § 102(b) is a shield for *knowing*, never for
*transcribing*.

### E6 — phaze already ships an AGPL-3.0 engine, and already conveys it

This molecule is not reasoning about a hypothetical. **The exact shape under evaluation is
already in the tree, unremediated.**

**The engine is AGPL-3.0** — resolved from the licensor's own repository record, not inferred:

```console
JorenSix/Panako       -> AGPL-3.0 | GNU Affero General Public License v3.0
JorenSix/TarsosDSP    -> GPL-3.0  | GNU General Public License v3.0     (Panako's DSP dependency)
dpwe/audfprint        -> MIT      | MIT License                          (no issue)
```

**phaze builds it from source in its public MIT repository** —
`services/panako/Dockerfile.panako:12-14`:

```dockerfile
ARG PANAKO_REF=e4b0e1dbb55e340bc66c90bac0ceb82b2cf84211
RUN git clone --filter=blob:none https://github.com/JorenSix/Panako.git /build/panako \
    && git -C /build/panako checkout --detach "${PANAKO_REF}"
```

**phaze's wrapper talks to it over fork/exec with a command line** —
`services/panako/app.py:34` and `:106,131,141`:

```python
JAVA_BASE_CMD = ["java", "--add-opens=java.base/java.nio=ALL-UNNAMED", "-jar", PANAKO_JAR]
...
result = subprocess.run(...)
```

**CI builds and pushes that image to a public registry** —
`.github/workflows/docker-publish.yml:41-45,131`:

```yaml
- name: panako
  dockerfile: services/panako/Dockerfile.panako
  image_suffix: "/panako"
...
push: ${{ github.event_name != 'pull_request' }}
```

**And the resulting image is anonymously pullable — verified, not assumed:**

```console
$ TK=$(curl -sSL "https://ghcr.io/token?scope=repository:simplicityguy/phaze/panako:pull&service=ghcr.io" | jq -r .token)
$ curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TK" \
    https://ghcr.io/v2/simplicityguy/phaze/panako/manifests/latest
200
```

An anonymous token suffices. `ghcr.io/simplicityguy/phaze/panako` is **published to the
public**, and it contains a compiled Panako JAR (AGPL-3.0) that bundles TarsosDSP (GPL-3.0).

**Finally, the attribution state of the repository:**

```console
$ ls NOTICE* THIRD*
(none)

$ grep -niE 'licen' README.md
12: ![License: MIT](https://img.shields.io/github/license/SimplicityGuy/phaze)
424: ## 📄 License
426: This project is licensed under the MIT License -- see the [LICENSE](LICENSE) file for details.
```

No `NOTICE`, no third-party licence section, no mention anywhere that a published phaze image
contains AGPL-3.0 and GPL-3.0 code.

**Two separable conclusions, and they point in opposite directions —**
see [V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations).
The *architecture* (separate process, fork/exec, CLI) is the cleanest possible shape. The
*packaging* (build-and-publish, no notices) carries real, present-tense obligations.

The compose reference itself is the contrasting shape — `docker-compose.agent.yml:176-181`:

```yaml
  panako:
    image: ghcr.io/simplicityguy/phaze/panako:${PHAZE_IMAGE_TAG:-latest}
    # build:
    #   dockerfile: services/panako/Dockerfile.panako
```

and AudioMuse publishes its own image the same way — `deployment/docker-compose.yaml:32`:

```yaml
    image: ghcr.io/neptunehub/audiomuse-ai:latest
```

So a phaze compose file referencing *that* string is a materially different act from phaze
rebuilding and republishing the software. [V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations)
turns on exactly that distinction.

### E7 — Model-weight licences, resolved at source

Every row resolved from the licensor's own metadata (GitHub licence record or Hugging Face
model-card `license` field), never from recall:

| Weights / project | Licence | Source | Usable in an MIT phaze? |
| ----------------- | ------- | ------ | ----------------------- |
| **AudioMuse-AI-DCLAP** (CLAP *audio* head) | **AGPL-3.0** | GitHub repo record + AudioMuse `LICENSE` addendum | **NO — blocking.** Copyleft weights. |
| LAION-AI/CLAP (code) | CC0-1.0 | GitHub repo record | Yes — public-domain dedication. |
| `laion/clap-htsat-unfused` (checkpoint) | **Apache-2.0** | HF model card | **Yes** — FSF-recognised free, GPL-3-compatible, no copyleft on phaze. |
| `laion/larger_clap_music_and_speech` | **Apache-2.0** | HF model card | **Yes.** |
| `microsoft/msclap` | **MS-PL** | HF model card | Usable, but see caveat below. |
| jordipons/musicnn (code) | ISC | GitHub repo record | Yes — permissive. |
| **essentia MTG models** (already in phaze) | **CC BY-NC-SA 4.0** | MTG models page | Conditional — see below. |

**On MS-PL**, `license-list.html#ms-pl`:

> "This is a free software license; it has a **copyleft that is not strong, but incompatible
> with the GNU GPL**. We urge you not to use the Ms-PL for this reason."

Weak file-level reciprocity. Not fatal, but it buys a reciprocal obligation that Apache-2.0
does not, for no capability gain. **Prefer the LAION Apache-2.0 checkpoints.**

**On Apache-2.0**, `license-list.html#apache2`:

> "This is a free software license, compatible with version 3 of the GNU GPL. […] The patent
> termination provision is a good thing, which is why we recommend the Apache 2.0 license for
> substantial programs over other lax permissive licenses."

**On the essentia models phaze already ships** — from the MTG's own models page:

> "**All the models created by the MTG are licensed under CC BY-NC-SA 4.0** and are also
> available under proprietary license upon request."

`discogs-effnet-bs64-1`, `msd-musicnn`, `mtt-musicnn` and the classifier heads are MTG-created
and downloaded from `essentia.upf.edu` — `src/phaze/scripts/download_models.py:82-83`. Their
`.json` metadata carries **no `license` field**, so the site statement is the only licence
notice, and nothing in the tree records it.

The **redistribution arm is not triggered**, and that matters: the weights are fetched at
**runtime** by each operator's own container (`src/phaze/tasks/_shared/model_bootstrap.py` —
"Auto-download essentia weights when `/models` is empty"), and the published `api` image copies
no model files:

```console
$ grep -nE '^COPY|^ADD' Dockerfile
33: COPY assets/ assets/          34: COPY src/phaze/templates/ ...
63: COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/
66: COPY pyproject.toml uv.lock ./     70: COPY src/ src/
71: COPY alembic/ alembic/             72: COPY alembic.ini ./
76: COPY --from=css-builder /build/src/phaze/static/css/app.css ...
```

**phaze does not redistribute the weights** — each operator receives them directly from the
licensor, under the licensor's own terms. That is the correct pattern and it should be the
template for every future model dependency ([R4](#r4--model-weights)). What *is* outstanding is
the **BY** (attribution) condition, and the **NC** condition as a ceiling on any future
commercial use.

### E8 — What could not be verified

| # | Unverified | Why it matters | Who can close it |
| - | ---------- | -------------- | ---------------- |
| U1 | Whether AudioMuse's `LICENSE` model addendum is a *complete* enumeration of the binaries in a given release. | If a release also bundles MTG-created essentia weights, the "MUSICNN MODELS: ISC" attribution would be incomplete — the ISC licence is jordipons' *code* licence, and MTG re-trained variants are CC BY-NC-SA 4.0. **This is upstream's compliance question, not phaze's**, and is raised only because it affects whether pulling their image is clean. | `phaze-ytgo.4` — inspect a release manifest. |
| U2 | Whether AudioMuse's image can be run with the AGPL DCLAP audio head disabled. | Decides whether the sidecar can be operated without an AGPL weights file present at all. | `phaze-ytgo.4`. |
| U3 | Whether phaze's `panako` image is currently referenced from any public-facing docs page that would need a notice. | Scopes [R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict). | A grep during the remediation bead. |

______________________________________________________________________

## Verdict

### V1 — The clean-room seal: corrected, in both directions

**Direct answer to the bead's question: reading AudioMuse's `ARCHITECTURE.md` / `ALGORITHM.md`
does NOT taint an independent implementation. Published prose is safe input.**

The basis is 17 U.S.C. § 102(b) ([E5](#e5--clean-room-doctrine-the-statute-not-folklore)):
ideas, procedures, processes and methods of operation are outside copyright **"regardless of
the form in which it is described, explained, illustrated, or embodied."** A document that says
"we use an IVF index with these parameters" conveys facts and procedures. Facts and procedures
are not the licensed subject matter. Knowing them cannot infringe.

So the seal as written is **too strict** in that respect — but the interesting finding is that
it is simultaneously **not strict enough**, in four specific ways. The molecule should not
simply relax it; it should replace it.

#### The seal is built on the wrong premise

The current seal reasons: *reading source contaminates the reader; therefore quarantine
readers.* That is the trade-secret / reverse-engineering model, where the defendant's problem is
that they had **access they were not entitled to**.

**That model does not fit here.** AudioMuse's source is *published*, under a licence that
grants everyone the right to read, run, modify and study it. There is no unauthorised access to
disprove. What a clean-room actually buys in this situation is **evidentiary**, not permissive:
if phaze's implementation is ever alleged to be a derivative of AudioMuse, being able to say
"the people who wrote this had never read your code" is a cheap and very strong rebuttal, and
its absence is expensive to argue around. A clean-room here is **prophylactic hygiene with a
real purpose, not a legal prerequisite.**

Naming the purpose correctly is what fixes the rule, because it relocates the constraint from
the **input** side to the **output** side:

> **The risk is not knowing what AudioMuse does. The risk is reproducing how AudioMuse
> expresses it.**

#### Four holes in the seal as written

**H1 — It seals *agents*, but the thing that propagates is *documents*.** The epic's `.4`
(sidecar) may read source. `.4` writes a spike doc. `phaze-ytgo.7` (D1) reads every spike doc.
The implementation molecule reads D1. **That is an unbroken path from AudioMuse source into the
implementation's inputs**, and the seal does not close it. The epic gestures at this — "S5's
doc must not quote AudioMuse source into any file that a future clean-room implementation would
read" — but states it as a footnote on one bead rather than as the rule itself. It is the rule
itself.

**H2 — It explicitly blesses the one category of prose that *is* literal expression:
configuration variable NAMES.** The epic permits reading "its configuration variable NAMES", and
the epic body already transcribes `IVF_METRIC`, `IVF_NLIST_MAX`, `IVF_NPROBE`. Individually,
short identifiers are very likely unprotectable (merger, *scènes à faire*, short-phrase
doctrine). But a **systematic** adoption of another project's naming scheme is precisely the
pattern that produces bad structure-sequence-and-organisation facts, and — decisively — **phaze
gains nothing from it.** phaze has its own settings conventions in `src/phaze/config.py`. Naming
a phaze setting `IVF_NPROBE` buys zero capability and donates a gratuitous similarity.

**H3 — It never says what the seal protects against, so nobody can tell when it is
satisfied.** "Do not open a `.py`" is checkable but not connected to any harm. An agent who
obeys it perfectly and then transcribes a parameter table out of `ALGORITHM.md` has satisfied
the seal and created the exact exposure the seal exists to prevent.

**H4 — The seal's own numbering is a live hazard.** The epic seals "S2, S3, S4" and unseals
"S5". The bead ids are `.2`, `.3`, `.6` (sealed) and `.4` (unsealed). **`S4` is bead `.6`, and
`S5` is bead `.4`.** An agent that reads the epic and matches the digit will unseal the wrong
bead. This is not a legal problem; it is a bookkeeping defect that can cause the legal problem.

#### The replacement rule (this is what D1 should record)

> **THE CLEAN-ROOM RULE — supersedes the epic's `S2/S3/S4`-vs-`S5` seal.**
>
> The constraint is on **what you write**, not on **what you read**. Two tiers, and they apply
> to *documents*, not to *agents*:
>
> **Tier 1 — INPUT (unrestricted).** Anyone in this molecule may read anything AudioMuse
> publishes: `README`, `ARCHITECTURE.md`, `ALGORITHM.md`, configuration documentation, issues,
> release notes, and — for the sidecar and deployment work — its source. Reading published
> material is expressly licensed and, per § 102(b), knowing a procedure cannot infringe.
> **The prohibition on reading prose is lifted.**
>
> **Tier 2 — OUTPUT (binding, and it is the whole seal).** Any document that a future phaze
> implementation could read — every spike doc, D1, every implementation bead — must contain
> **no AudioMuse expression**. Concretely, none of:
>
> - AudioMuse source code, in any quantity, including "illustrative" fragments;
> - AudioMuse identifiers — variable, function, class, table, column, config or endpoint names
>   — proposed for adoption in phaze. Naming one to *describe* AudioMuse is fine
>   ("AudioMuse exposes a setting controlling probe count"); proposing phaze adopt the spelling
>   is not;
> - literal parameter values transcribed from AudioMuse without an **independent citation**.
>   Every numeric constant that reaches a phaze design must cite a paper, a library's own
>   documentation, or a phaze measurement. "AudioMuse uses N" is not a design rationale — it is
>   a copied constant with no engineering justification, which is bad practice *and* bad
>   evidence;
> - AudioMuse's module decomposition proposed as phaze's module decomposition.
>
> **Practical consequence.** `phaze-ytgo.4` may read AudioMuse source freely — and its doc must
> be written as though a lawyer will read both. It should describe the **integration seam**
> (endpoints, payload shapes, deployment topology, resource envelope), which is interface fact,
> and not the **implementation**.
>
> **Enforcement.** Every spike and design doc in this line carries a **clean-room disclosure**:
> what it read, and an affirmative statement that it reproduces no AudioMuse expression. This
> document carries one at the top. That disclosure — a contemporaneous, version-controlled,
> signed-commit record — is worth considerably more than an unverifiable claim that nobody
> looked.
>
> **Bookkeeping.** Refer to beads by **bead id only** (`phaze-ytgo.4`), never by `S`-number. The
> `S`-numbers and the bead ids do not correspond.

**Net effect on the molecule:** the seal gets *looser* on input (`.2`, `.3`, `.6` may now read
`ARCHITECTURE.md` / `ALGORITHM.md`, which they were told they could anyway) and *tighter and
better-targeted* on output (nobody, including `.4`, may launder expression forward). No spike
needs to be redone; this document's own disclosure block is the template.

### V2 — The sidecar SURVIVES — and the *packaging*, not the architecture, is what carries obligations

**Verdict: running AudioMuse as a separate networked process is mere aggregation, not a
derivative work. The shape survives the licence filter.** Four independent grounds, and it does
not need all four:

1. **Mechanism of communication.** Separate OS process, separate container, HTTP over a socket.
   The FSF's own criterion: "pipes, sockets and command-line arguments are communication
   mechanisms normally used between two separate programs. So when they are used for
   communication, **the modules normally are separate programs**" (`#MereAggregation`). And
   containerisation changes nothing either way (`#AggregateContainers`).
2. **Semantics of communication.** The exchange is REST requests and result payloads across a
   documented HTTP surface — not shared address space, not exchange of internal data
   structures. This is the FSF's *separate programs* case, not its "intimate enough" exception.
3. **§5's aggregate paragraph.** AudioMuse and phaze are "separate and independent works, which
   are not by their nature extensions of" one another. "Inclusion of a covered work in an
   aggregate does not cause this License to apply to the other parts of the aggregate."
4. **§2's output clause — and this one is decisive for the data.** "The output from running a
   covered work is covered by this License **only if the output, given its content, constitutes
   a covered work**", reinforced by `#GPLOutput` / `#WhatCaseIsOutputGPL`. **An embedding vector
   computed from Robert's own audio file is not a copy of AudioMuse's code.** phaze may store
   AudioMuse-produced vectors, cluster labels and playlists in its own Postgres and serve them
   from an MIT application. *The output is not encumbered.* This is the finding that makes the
   sidecar genuinely useful rather than merely legal.

**And the in-tree precedent is exactly this shape.** `services/panako/app.py` fork/execs
`java -jar panako.jar` and parses its stdout ([E6](#e6--phaze-already-ships-an-agpl-30-engine-and-already-conveys-it)) —
the FSF's `#GPLPlugins` "simple fork and exec […] results in the plug-ins being a separate
program". **`app.py` staying MIT is defensible today, and an AudioMuse sidecar would be a
cleaner case still** (network boundary rather than a process boundary inside one image).

#### §13 when the only user is the operator

**No source-offer obligation attaches, for two independent reasons — but the second is
fragile.**

1. **The trigger is modification.** §13 opens "**if you modify the Program**"
   ([E3](#e3--what-the-agpl-actually-triggers-on-verbatim)). Running a stock upstream image is
   not modifying. FSF concurs: the duty falls on "**modified versions**" (`#UnreleasedModsAGPL`).
   Even without this, running a covered work you do not convey is permitted "**without
   conditions**" (§2). **Run stock and §13 never fires.**
2. **Even if modified, the obligation runs to "all users interacting with it remotely" —
   Robert, who already has the source.** §13 creates no duty to publish to the world; it
   creates a duty to the *interacting users*. When the set of interacting users is {the
   operator}, it is self-satisfying. Compare `#InternalDistribution`: copies made within one
   organisation for itself are not distribution.

**Why reason (2) is fragile, and must be recorded as a condition rather than a conclusion:** it
depends entirely on the user set staying at one. If phaze's UI is ever exposed to a second
person — a household member, a remote-access tunnel, a friend given a login — then AudioMuse is
being interacted with remotely by someone who is not the licensee, and if the deployment is
*also* modified, §13's duty is live and owed to that person. Reason (1) is the robust one.
**Therefore the operational rule is: run upstream images unmodified. If you must modify, publish
the modified source.** That is cheap, and it makes the fragile reason unnecessary.

Note also `#AGPLv3InteractingRemotely`: AudioMuse is "expressly designed to accept user requests
and send responses over a network", so it is squarely the *kind* of program §13 was written
for. Nothing about it being self-hosted or single-user takes it out of §13's subject matter —
only the modification condition does. **The "it's a private single-user tool so it doesn't
matter" intuition is right about the outcome and wrong about the reason**, and relying on the
wrong reason is what breaks the moment a second user appears.

#### The distribution question — the sharp one, and it is a real trigger

The bead asks whether phaze's **public MIT repository** shipping a compose file / Helm chart /
documentation that pulls an AGPL-3.0 image is itself a trigger. **The answer splits on one
distinction, and phaze is currently on both sides of it.**

**A REFERENCE is not conveying.** A compose file containing
`image: ghcr.io/neptunehub/audiomuse-ai:latest` transfers no copy of AudioMuse. The operator's
Docker daemon fetches the image from **NeptuneHub's** registry, under **NeptuneHub's** terms,
directly from NeptuneHub. Per §0, conveying is "propagation that **enables other parties to make
or receive copies**" — and the party enabling and making that copy is the upstream registry, not
phaze. phaze publishes a *string that names* a work. Naming is not copying; a URL is not a
distribution.

Two corollaries worth stating because they are the plausible objections:

- **It is not a §5 aggregate problem either.** A compose file is a deployment descriptor, not a
  compilation containing a covered work. The aggregate analysis is not even reached — there is
  no covered work in the repository to aggregate.
- **It imposes no "further restriction" under §10.** phaze's MIT licence covers phaze's files.
  It makes no claim over AudioMuse and removes no recipient's rights.

**BUILDING AND PUBLISHING an image IS conveying.** Take an AGPL work, compile it, bake it into
an artifact, push that artifact to a public registry — that is conveying a covered work in
non-source form, and §4/§6 obligations attach in full: appropriate legal notices, and
Corresponding Source made available.

**phaze is doing exactly this today, with Panako, and it is unremediated**
([E6](#e6--phaze-already-ships-an-agpl-30-engine-and-already-conveys-it)):
`ghcr.io/simplicityguy/phaze/panako:latest` is anonymously pullable (`HTTP 200`), contains a
compiled AGPL-3.0 Panako JAR bundling GPL-3.0 TarsosDSP, and the repository carries no `NOTICE`,
no third-party licence section, and a `README` that says "MIT" and nothing else.

**So the rule this molecule must carry forward is not "avoid AGPL" but:**

| Act | Conveying? | Obligation |
| --- | ---------- | ---------- |
| Compose / Helm / docs **referencing** an upstream-published AGPL image | **No** | None under the AGPL. (Courtesy notice recommended — [R2](#r2--sidecar-referencing-an-upstream-image-recommended-shape).) |
| Documenting how to run an AGPL program | **No** | None. Instructions are phaze's own expression. |
| **Building** AGPL source into an image and **pushing** it publicly | **YES** | §4 notices + §6 Corresponding Source, in full. |
| Vendoring / porting / translating AGPL source into phaze's tree | **YES**, and worse | §5(c) copyleft over "the whole of the work, and all its parts". **Irreconcilable with MIT.** |

That table, not a blanket prohibition, is the compliance position.

### V3 — The media-server shim: low risk, and intent is not the test

**Verdict: a Subsonic-compatible endpoint in phaze does NOT carry materially greater
derivative-work risk because it exists to feed an AGPL application.**

**Intent does not create derivative-work status.** AGPL §0 defines "modify" as "**to copy from
or adapt** all or part of the work in a fashion requiring copyright permission." The operative
verbs are *copy* and *adapt* — acts performed on the work. Writing original code that another
program happens to call is neither. There is no doctrine under which purpose alone converts an
independent work into a derivative one.

**And the Subsonic case is the safest available shape**, for a reason specific to it: **the
Subsonic API is a third party's published interface, not AudioMuse's.** phaze implementing it
is phaze implementing a documented protocol that many servers implement. Under § 102(b) an
interface is a "method of operation"; under Directive 2009/24/EC Art. 1(2) "ideas and principles
which underlie any element of a computer program, **including those which underlie its
interfaces**" are unprotected. And the seam is HTTP over sockets — the FSF's *separate programs*
mechanism. Every factor points the same way.

Risk ranking, lowest to highest, for D1 to use:

| Shape | Risk | Why |
| ----- | ---- | --- |
| Generic Subsonic endpoint, written from the **published Subsonic API spec** | **Lowest.** | Third-party interface; unprotected under § 102(b); many independent implementations exist. |
| Subsonic endpoint + AudioMuse-specific quirk workarounds derived from **observed behaviour** | **Low.** | Behavioural observation is fact-finding. Document *how* each quirk was discovered. |
| An **AudioMuse-private** RPC schema phaze implements to spec | **Low-moderate.** | Still socket-separated, but the interface is now AudioMuse's own expression; copying a schema wholesale is closer to the line than implementing a public standard. |
| A Subsonic adapter **written by reading AudioMuse's own client/adapter code** | **Highest — avoid.** | This is copying-risk, and it is exactly where [V1](#v1--the-clean-room-seal-corrected-in-both-directions)'s Tier-2 output rule bites. |

**The genuine aggravator is not purpose, it is proximity to their expression.** A shim is
low-risk *when written from a public spec*; the same shim written by cribbing their adapter is
the highest-risk artifact in the whole molecule. That is a `phaze-ytgo.4` instruction, not a
prohibition on the shim.

One clause deserves a caveat so nobody over-reads it: AGPL §1's Corresponding Source definition
mentions subprograms "**specifically designed to require**" the work, "such as by intimate data
communication or control flow". That clause governs *what Corresponding Source must include for
a work that is already covered* — it does not make phaze covered. Do not let it be quoted as
though it did.

### V4 — Model weights: one blocking checkpoint, and a clean substitute

**Verdict: model licensing eliminates one specific checkpoint and nothing else.**

**The blocking finding — for `phaze-ytgo.3`:**

> **`AudioMuse-AI-DCLAP` — the CLAP *audio* head — is licensed AGPL-3.0** by its own author, in
> AudioMuse's own `LICENSE` addendum and in the DCLAP repository's licence record
> ([E1](#e1--the-two-licences-verified), [E7](#e7--model-weight-licences-resolved-at-source)).
> **phaze must not ship, redistribute, or build a dependency on these weights.** The AGPL
> reciprocity that applies to code applies to any work the licensor licenses under it.

Note precisely what is and is not eliminated:

- **Eliminated: phaze redistributing DCLAP weights**, or baking them into a phaze-published
  image. That is conveying a covered work — unambiguous.
- **Eliminated as a dependency: phaze's own code loading DCLAP weights** as its CLAP audio head.
  Whether merely *loading* copyleft weights as data makes the loader a covered work is
  genuinely unsettled (`#IfInterpreterIsGPL` suggests data fed to a program is not linked to
  it, but weights are not obviously "data" in that sense, and there is no controlling
  authority). **When a boundary is unsettled and a permissive substitute exists at no
  capability cost, take the substitute.** Flagged for counsel below.
- **NOT eliminated: the sidecar running AudioMuse's stock image, which contains these weights.**
  Robert is a *recipient*, not a conveyor. Receiving and running is exactly what the licence
  grants ("unlimited permission to run the unmodified Program", §2).

**The substitute is clean and it is the better engineering choice anyway.** The clean-room path
does **not** need DCLAP:

| Component | Permissive option | Licence |
| --------- | ----------------- | ------- |
| CLAP checkpoints (audio + text) | `laion/clap-htsat-unfused`, `laion/larger_clap_music_and_speech` | **Apache-2.0** |
| CLAP reference implementation | `LAION-AI/CLAP` | **CC0-1.0** |
| musicnn | `jordipons/musicnn` | ISC |

**`phaze-ytgo.3` should price the LAION Apache-2.0 checkpoints and disregard DCLAP entirely.**
Apache-2.0 is FSF-recognised free, GPL-3-compatible, and imposes on phaze only notice
preservation — no copyleft, and a patent grant MIT lacks. `microsoft/msclap` (MS-PL) is
*usable* but buys weak copyleft for no gain; prefer LAION.

**The adjacent finding — phaze's existing essentia weights.** `discogs-effnet`, `msd-musicnn`
and the MTG classifier heads phaze already depends on are **CC BY-NC-SA 4.0**
([E7](#e7--model-weight-licences-resolved-at-source)). Three consequences, honestly separated:

- **ShareAlike / redistribution: not triggered.** phaze does not redistribute them. They are
  runtime-downloaded per operator from `essentia.upf.edu` and no `COPY` in the published
  `Dockerfile` bakes them in — verified, not assumed. **This is the correct pattern and should
  be the template for every future model dependency.**
- **Attribution (BY): outstanding today.** CC BY-NC-SA requires attribution. The repository has
  no `NOTICE` and no third-party section. Cheap to fix; fix it in the same bead as
  [R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict).
- **NonCommercial (NC): a ceiling, not a present breach.** A single-user personal archive is not
  commercial use. But **NC is a hard cap on phaze's commercial future** with these models —
  MTG offer a proprietary licence on request, so the escape hatch exists and is worth knowing
  about before it is urgent. Whether an *output* (a stored `style` value) is "Adapted Material"
  subject to ShareAlike is a real open question — flagged for counsel.

### Per-purpose impact (S1 rubric, `phaze-ytgo.1`)

Licence constraints bite at **shape** level, not purpose level — which is why the bead directs
this subsection to record surviving shapes. Both tables are given so D1's cross-tabulation stays
mechanical.

#### Which shapes survive the licence filter — the filter D1 applies *before* technical merit

| Shape | Licence verdict | Condition / cost |
| ----- | --------------- | ---------------- |
| **Clean-room native implementation in phaze** | **SURVIVES** | Under the corrected [V1](#v1--the-clean-room-seal-corrected-in-both-directions) rule. No DCLAP weights; permissive CLAP only. phaze stays MIT with zero AGPL exposure. |
| **Sidecar — operator pulls the upstream image; phaze *references* it** | **SURVIVES** | No AGPL obligation on phaze ([V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations)). Conditions: run **unmodified**; reference, never rebuild-and-publish; add a courtesy `NOTICE` + README line. |
| **Sidecar + a phaze-written Subsonic endpoint** | **SURVIVES** | Written from the **published Subsonic spec** ([V3](#v3--the-media-server-shim-low-risk-and-intent-is-not-the-test)). Never adapted from AudioMuse's adapters. |
| **Sidecar — phaze builds & publishes its own AudioMuse-derived image** | **SURVIVES ONLY WITH FULL AGPL COMPLIANCE** | §4 notices + §6 Corresponding Source. Legal, but it converts a zero-obligation shape into an ongoing one. **Recommend against** ([R3](#r3--sidecar-publishing-your-own-image-not-recommended)). |
| **Vendoring / porting / translating AudioMuse code into `src/phaze/`** | **ELIMINATED** | §5(c): the whole work, all parts, however packaged. Irreconcilable with the binding MIT constraint. |
| **Importing AudioMuse as a Python library in phaze's process** | **ELIMINATED** | Shared address space → "almost surely […] combining them into one program" (`#MereAggregation`). Same outcome as vendoring. |
| **Depending on `AudioMuse-AI-DCLAP` weights** | **ELIMINATED** | AGPL-3.0 weights ([V4](#v4--model-weights-one-blocking-checkpoint-and-a-clean-substitute)). Permissive substitute exists at no capability cost. |

#### Rubric block

| Purpose | Verdict | Granularity delivered | vs EFB | Evidence |
| ------- | ------- | --------------------- | ------ | -------- |
| P1 dedup + rename | **SERVES** | n-a (licence is granularity-neutral) | n-m | No licence constraint eliminates any P1 approach. Both surviving shapes are available. [V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations) |
| P2 discovery / playlists | **SERVES-WITH-CAVEAT** | n-a | n-m | Available under both shapes. **Caveat:** the clean-room text-search path must use LAION **Apache-2.0** CLAP, not DCLAP (AGPL-3.0). Substitute exists; no capability lost. [V4](#v4--model-weights-one-blocking-checkpoint-and-a-clean-substitute) |
| P3 set/tracklist | **SERVES** | n-a | n-m | No licence constraint. P3's blocker is structural (S1 `B3`), not legal — do not let a licence verdict be mistaken for a P3 verdict. |
| P4 archive QA | **SERVES** | n-a | n-m | No licence constraint. S1's `REDUNDANT`-vs-EFB question is unaffected by this spike. |

**Reading rule for D1:** this spike **eliminates no purpose**. It eliminates three *shapes*
(vendoring, in-process linking, DCLAP weights) and attaches conditions to two more. A cell that
this spike does not eliminate is still free to be "not worth it" on technical merit — the
licence filter is a gate, not an endorsement.

______________________________________________________________________

## Recommendation

### R1 — Clean-room shape

| Do | Don't |
| -- | ----- |
| Adopt the [V1](#v1--the-clean-room-seal-corrected-in-both-directions) replacement rule; record it in D1 as superseding the epic's seal. | Don't keep "may not open a `.py`" as the rule — it is unmoored from any harm and it leaves H1–H4 open. |
| Carry a **clean-room disclosure block** in every spike and design doc in this line (template: this document's). | Don't rely on an unverifiable after-the-fact claim that nobody looked. |
| Cite an **independent source** for every numeric parameter reaching a phaze design — paper, library docs, or a phaze measurement. | Don't transcribe a constant with "AudioMuse uses N" as its only justification. |
| Use phaze's own naming conventions (`src/phaze/config.py`). | Don't adopt AudioMuse's identifier spellings. Zero benefit, gratuitous similarity. |
| Refer to beads by **bead id**. | Don't use `S`-numbers — they do not correspond to bead ids (H4). |

**Licence outcome: no `NOTICE`, no `LICENSE` change, no README change.** A genuinely independent
implementation creates no attribution obligation. This shape is licence-free by construction —
which is a real and underrated advantage of the clean-room column that D1 should weigh
explicitly against its engineering cost.

### R2 — Sidecar, referencing an upstream image (**recommended shape**)

| Remedy | Required? | Concretely |
| ------ | --------- | ---------- |
| Reference the **upstream** image string | **Required** | `image: ghcr.io/neptunehub/audiomuse-ai:<pinned-tag>`. Pin a digest, consistent with how phaze already pins `PANAKO_REF` / `AUDFPRINT_SHA`. |
| **Never** rebuild-and-publish AudioMuse | **Required** | No `services/audiomuse/Dockerfile*`, no matrix entry in `docker-publish.yml`. This single rule keeps the shape at zero obligation. |
| Run **unmodified** | **Required (operational)** | Makes §13 unreachable via its own condition, and removes reliance on the fragile single-user argument. Config through env vars is not modification. |
| `NOTICE` file | **Recommended, not legally required** | One line: AudioMuse-AI, AGPL-3.0, upstream URL. Costs nothing; forecloses the argument entirely; and phaze needs the file anyway for [R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict). |
| README third-party section | **Recommended** | State that the optional sidecar is AGPL-3.0 and is pulled from upstream, not distributed by phaze. Removes any implication that phaze's MIT grant covers it. |
| `LICENSE` change | **NO** | phaze's own code is unaffected. Do not touch `LICENSE`. |
| Source offer | **NO** | phaze conveys nothing. |
| Keep the compose reference out of the repo | **NO — unnecessary** | This was the bead's open question; the answer is that a reference is not conveying ([V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations)). **The compose file can ship.** |

### R3 — Sidecar, publishing your own image (**not recommended**)

If phaze ever builds an AudioMuse-derived image and pushes it publicly, **all** of the following
become required, not optional:

1. §4 — retain all copyright/licence notices; the image must carry the AGPL text.
2. §6 — Corresponding Source available to every recipient, by one of §6(a)–(e).
3. §5(a) — if modified: prominent notice of modification, with a date.
4. §13 — if modified and network-interactive: a source offer to remote users.
5. Model weights bundled in that image inherit *their* licences too — including AGPL-3.0 DCLAP
   ([E1](#e1--the-two-licences-verified)).

**Recommendation: don't.** [R2](#r2--sidecar-referencing-an-upstream-image-recommended-shape)
achieves the same operator experience at zero obligation. The only reason to publish your own
image is to pin or patch — and pinning is achievable by digest.

### R4 — Model weights

| Rule | Rationale |
| ---- | --------- |
| **Never redistribute third-party weights.** Fetch at runtime, per operator, from the licensor. | Already phaze's pattern for essentia ([E7](#e7--model-weight-licences-resolved-at-source)) and it is why the CC BY-NC-SA ShareAlike arm is not triggered today. Make it an explicit rule rather than an accident. |
| **`phaze-ytgo.3`: price LAION Apache-2.0 CLAP checkpoints. Disregard DCLAP.** | DCLAP is AGPL-3.0. Permissive substitute at no capability cost. |
| **Resolve every new model's licence from the licensor's own metadata before adopting it.** | The essentia `.json` files carry **no** `license` field — the licence lives only on the MTG's website. A model that "just downloads" can still carry NC/SA/copyleft terms. |
| **Record every model licence in the `NOTICE` file** ([R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict)). | CC BY-NC-SA's BY condition is outstanding today. |
| **Treat NC as a strategic ceiling and surface it now.** | The MTG models cap any commercial future; MTG offer a proprietary licence on request. Cheaper to know now than at the moment it matters. |

### R5 — The Panako remediation (do this regardless of this molecule's verdict)

**This is the highest-priority action in this document and it is not about AudioMuse.**

phaze publicly conveys `ghcr.io/simplicityguy/phaze/panako`, containing AGPL-3.0 Panako and
GPL-3.0 TarsosDSP, with no notices and no source offer
([E6](#e6--phaze-already-ships-an-agpl-30-engine-and-already-conveys-it)). Verified by anonymous
registry pull, not inferred from a workflow file.

**File a separate P1 bead — outside this molecule, not blocked on D1** — to do all of:

1. **Add a `NOTICE` / `THIRD-PARTY-LICENSES.md`** naming Panako (AGPL-3.0, pinned commit
   `e4b0e1d`, upstream URL), TarsosDSP (GPL-3.0), audfprint (MIT, pinned `cb03ba9`), and the
   MTG essentia models (CC BY-NC-SA 4.0).
2. **Add a README third-party section** stating that phaze's *own code* is MIT while the
   `panako` sidecar image contains AGPL-3.0/GPL-3.0 software, with a pointer to the source.
   **Do not change `LICENSE`** — phaze's own code is genuinely MIT and the wrapper's
   fork/exec seam is defensible ([V2](#v2--the-sidecar-survives--and-the-packaging-not-the-architecture-is-what-carries-obligations)).
3. **Satisfy §6 Corresponding Source for the published image.** Options, cheapest first:
   - **(a)** Add an OCI label + in-image `README` giving the pinned upstream commit and a
     written offer. Whether §6(d)'s "equivalent access to the Corresponding Source in the same
     way through the same place" is satisfied by pointing at upstream GitHub **needs counsel**
     — it is the one genuinely uncertain point in this remediation.
   - **(b)** Publish the source tarball as a GitHub release asset alongside each image tag.
     Unambiguous, mechanical, cheap. **Preferred.**
   - **(c)** Stop publishing the image; have operators build locally by uncommenting the
     existing `build:` block in `docker-compose.agent.yml:178-181`. Eliminates the obligation
     entirely at a real operator-experience cost.
4. **Add a CI guard** that fails when a new entry appears in `docker-publish.yml`'s image matrix
   without a corresponding `NOTICE` entry. This is the defect class, not the defect: the
   Panako exposure arose because *build-and-publish* looks locally identical to *reference*, and
   nothing in the repo distinguishes them.

**Why this belongs in this document even though it is not about AudioMuse:** the bead asks what
obligations attach when a public MIT repo ships an AGPL reference. The answer was already
demonstrable in phaze's own tree, in the stronger build-and-publish form. An assessment that
answered the hypothetical while the live instance sat unremediated in the same repository would
be worthless.

### Needs counsel (do not resolve these in a bead)

Each of these is a boundary question where a confident guess is worse than an honest gap. **None
blocks the recommended shapes** — each is either a "which cheap remedy" question or a
future-facing one.

| # | Question | Why an engineer should not answer it | Blocks? |
| - | -------- | ------------------------------------ | ------- |
| **C1** | Does §6(d) ("equivalent access … in the same way through the same place") permit satisfying Corresponding Source for phaze's `panako` image by pointing at the pinned upstream GitHub commit, or must phaze host the tarball itself? | Turns on the construction of "the same place" and on ongoing-availability duties. Determines whether [R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict)(a) suffices or (b) is required. | No — (b) is cheap and unambiguous. Take (b) unless counsel blesses (a). |
| **C2** | Are ML model weights copyrightable subject matter, and if so can a copyleft licence over weights reach a program that merely loads them at runtime? | Genuinely unsettled, no controlling authority, and the analogy to `#IfInterpreterIsGPL` is imperfect. | No — a permissive substitute (LAION Apache-2.0) removes the need to answer. |
| **C3** | Does CC BY-NC-SA 4.0's ShareAlike condition reach the *outputs* of a model — phaze's stored `mood` / `style` / `danceability` values? | Turns on whether outputs are "Adapted Material" under CC 4.0. Affects data phaze already persists. | No — but it should be answered before any commercial use. |
| **C4** | If phaze's UI is ever exposed to a second person, does the §13 single-operator position survive? | Fact-dependent on deployment and on who counts as a "user". | No — mooted by running upstream **unmodified**, which is the recommendation anyway. |
| **C5** | Does any of this analysis change outside the United States? | This document reasons from US copyright plus licence text. phaze's repository is world-visible. | No — but it bounds every "safe" in this document. |
| **C6** | Is the fork/exec wrapper seam in `services/panako/app.py` sufficient to keep that file MIT? | The FSF's criterion is the *licensor's* reading, and the FSF concedes "judges will decide". | No — this is the most favourable fact pattern in the FSF's own guidance. Confirm at leisure. |

### What D1 (`phaze-ytgo.7`) should record from this spike

1. **The clean-room seal is corrected** — input unrestricted, output constrained. Record the
   [V1](#v1--the-clean-room-seal-corrected-in-both-directions) replacement rule verbatim; it
   supersedes the epic's `S2/S3/S4` seal. Note the `S`-number/bead-id mismatch as a hazard.
2. **Published prose is safe input.** 17 U.S.C. § 102(b), explicitly.
3. **Neither headline shape is eliminated.** Clean-room and referenced-sidecar both survive.
4. **Three shapes ARE eliminated:** vendoring/porting AudioMuse code, in-process linking, and
   any dependency on `AudioMuse-AI-DCLAP` (AGPL-3.0) weights.
5. **`phaze-ytgo.3` gets a concrete instruction:** price LAION **Apache-2.0** CLAP checkpoints;
   disregard DCLAP.
6. **The compose-reference question is answered `NOT A TRIGGER`** — a reference is not
   conveying; **building-and-publishing is.**
7. **[R5](#r5--the-panako-remediation-do-this-regardless-of-this-molecules-verdict) is filed as
   a separate P1 bead and is not blocked on this molecule.**
8. **The licence filter passes every purpose P1–P4.** A cell may still be "not worth it" on
   technical merit; this spike removes shapes, never purposes.

______________________________________________________________________

## Sources

All fetched 2026-07-25.

| # | Source | Used for |
| - | ------ | -------- |
| 1 | GNU AGPL-3.0, canonical text — `https://www.gnu.org/licenses/agpl-3.0.txt` (sha256 `0d96a4ff…9abcb0`) | §0, §2, §5, §10, §12, §13 quotations |
| 2 | `NeptuneHub/AudioMuse-AI` `LICENSE` — `https://raw.githubusercontent.com/NeptuneHub/AudioMuse-AI/main/LICENSE` | byte-diff vs canonical; model addendum |
| 3 | FSF GPL FAQ — `https://www.gnu.org/licenses/gpl-faq.html` (`#MereAggregation`, `#AggregateContainers`, `#GPLPlugins`, `#AGPLv3InteractingRemotely`, `#UnreleasedModsAGPL`, `#InternalDistribution`, `#GPLOutput`, `#WhatCaseIsOutputGPL`, `#IfInterpreterIsGPL`) | separate-programs criterion; §13 scope; output |
| 4 | FSF licence list — `https://www.gnu.org/licenses/license-list.html` (`#Expat`, `#apache2`, `#ms-pl`) | MIT↔GPL asymmetry; Apache-2.0; MS-PL |
| 5 | 17 U.S.C. § 102(b) | clean-room / idea-expression |
| 6 | Directive 2009/24/EC Art. 1(2) | EU parallel on interfaces (noted, not analysed) |
| 7 | GitHub API licence records — `JorenSix/Panako`, `JorenSix/TarsosDSP`, `dpwe/audfprint`, `NeptuneHub/AudioMuse-AI-DCLAP`, `LAION-AI/CLAP`, `jordipons/musicnn`, `SimplicityGuy/phaze` | licence + visibility facts |
| 8 | Hugging Face model cards — `laion/clap-htsat-unfused`, `laion/larger_clap_music_and_speech`, `microsoft/msclap` | checkpoint licences |
| 9 | MTG essentia models page — `https://essentia.upf.edu/models.html` | CC BY-NC-SA 4.0 statement |
| 10 | `ghcr.io` registry v2 API, anonymous token | public-distribution verification |
| 11 | phaze tree at `b051b3b` — `LICENSE`, `README.md`, `Dockerfile`, `services/panako/*`, `services/audfprint/*`, `docker-compose*.yml`, `.github/workflows/docker-publish.yml`, `src/phaze/scripts/download_models.py`, `src/phaze/tasks/_shared/model_bootstrap.py` | in-tree precedent and current state |
| 12 | `AudioMuse-AI/deployment/docker-compose.yaml` (service/image lines only) | upstream published image reference |
