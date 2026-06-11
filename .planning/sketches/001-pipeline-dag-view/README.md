---
sketch: 001
name: pipeline-dag-view
question: "How should the pipeline DAG be laid out so dependencies, parallelism, and per-stage trigger buttons are all legible?"
winner: "B"
tags: [layout, dashboard, dag, pipeline]
---

# Sketch 001: Pipeline DAG View

## Design Question
The pipeline is a DAG. How do we render it so an operator can (a) see the dependency order, (b) see which stages run in parallel, (c) see per-stage counts/progress, and (d) trigger each stage from its node — with buttons disabled when upstream deps aren't met or the agent is busy?

Dependency model from `35-STAGE-DEPENDENCIES.md`:
- Discovery → {Extract Metadata ∥ Fingerprint ∥ Analyze} (parallel, file-only inputs)
- Proposals joins on **Analyze + Metadata only** (not Fingerprint)
- Approve → Execute (terminal, human-gated)
- Live-set tracklist branch (Scan/Search → Scrape → Discogs) runs parallel to the file group

## How to View
open .planning/sketches/001-pipeline-dag-view/index.html

## Variants
- **A: Flow rail** — left→right tiers; the 3 file stages fan out as a stacked parallel group; SVG edges converge explicitly into Proposals; tracklist branch on a lower rail. Most "dashboard-like".
- **B: Graph canvas** — free-positioned nodes on an SVG canvas with curved edges; reads like a true DAG / graph editor; compact node chips. Most "graph-like".
- **C: Swimlanes** — two labelled lanes (File pipeline / Live-set tracklist); the parallel stages bracketed in a dashed "parallel · file-only" block so concurrency is unmistakable. Most "explainable".

Live-ish counts baked in to show real states: Discovery 11,428 done · Metadata 11,428 done · Analyze 27/11,428 (8 active) · Fingerprint 0 (agent busy → disabled) · Proposals 0 (waiting on Analyze → disabled) · Execute gated.

## What to Look For
- Is the **parallel group** obvious as parallel (vs. a misread sequential chain)?
- Is the **join into Proposals** (analyze + metadata, NOT fingerprint) visually honest?
- Do the **disabled-button reasons** ("Waiting on Analyze", "Agent busy", "Needs tracklist") read clearly?
- Does the tracklist branch feel like a parallel side-chain, not a continuation?
- Which scales better as more stages/agents are added?
