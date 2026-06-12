# Sketch Manifest

## Design Direction
A "Pipeline DAG" view for the phaze pipeline dashboard. The pipeline is a directed acyclic graph of SAQ stages; the UI should render the dependency graph as nodes with per-stage counts, progress bars, and trigger buttons (gated by upstream deps + agent-busy state). Dark dashboard aesthetic, Tailwind v4.3.0 (matching the app's vendored build), colored stage cards (Discovered=cyan, Metadata=sky, Fingerprint=teal, Analyze=amber, Proposals=purple, Approve/Execute=green/gray, Tracklist=rose). Grounded in the data-dependency research in Phase 35's `35-STAGE-DEPENDENCIES.md`.

## Reference Points
- Existing flat stage-cards row + "Processing" bar (current dashboard)
- DAG/graph editors (node-edge canvases) for Variant B

## Sketches

| # | Name | Design Question | Winner | Tags |
|---|------|----------------|--------|------|
| 001 | pipeline-dag-view | How to lay out the pipeline DAG so dependencies, parallelism, and per-stage trigger buttons are all legible? | **B — Graph canvas** (node-edge DAG, edges anchored to node positions) | layout, dashboard, dag, pipeline |
