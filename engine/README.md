# Canopy engine — BDD quantification core (Rust)

Quantifies fault trees from the git-native PSA YAML model format.

## What it does
- Loads the YAML model (fault trees, basic events, parameters, house events),
  resolves parameter references, computes point probabilities per failure model
  (probability / rate-mission / rate-repair).
- Compiles gate formulas (and/or/xor/not/atleast, cross-tree transfers,
  house-event folding) into a reduced ordered BDD with gate-cycle detection.
- Exact top-event probability (no rare-event approximation), O(|BDD|).
- Minimal cut sets via Rauzy's minimal-solutions algorithm (coherent trees),
  ranked by probability.
- Birnbaum importance per basic event.

## Engine design
- Node = 12 bytes (var, low, high) in a flat Vec arena; u32 indices.
- Hash consing (unique table) -> canonical DAGs, O(1) equivalence.
- Memoized apply (and/or/xor) and without; operand-order normalization
  for commutative ops.
- Variable order = DFS discovery order from the top gate.

## Usage
    cargo run --release -- <model-dir> <FT-ID|ET-ID> [--house HE-ID=true] [--mcs-limit N] [--json]
    cargo test                    # unit tests (known-answer checks)
    cargo run --release --example bench   # 30k-event synthetic scale test

### Try it on the demo model

The repo ships a small illustrative model at [`../model`](../model)
(`DEMO-PWR-L1-INTERNAL`, not a real plant). From this directory:

    # Quantify a fault tree, with minimal cut sets + Birnbaum importance
    cargo run --release -- ../model FT-RHR

    # Same, as JSON (e.g. for scripting)
    cargo run --release -- ../model FT-RHR --json

    # Quantify an event tree (sequence frequencies + risk metric)
    cargo run --release -- ../model ET-SLOCA --json

    # Override a house event (e.g. put a train out of service)
    cargo run --release -- ../model ET-SLOCA --house HE-ECC-TRAIN-A-OOS=true --json

Or build once and invoke the binary directly from the repo root:

    cargo build --release --manifest-path engine/Cargo.toml
    engine/target/release/canopy model FT-RHR
    engine/target/release/canopy model ET-SLOCA --json

Other fault trees in the demo model: `FT-RPS`, `FT-ECCS-INJECTION`.

## Known limitations (v0.1, deliberate)
- No garbage collection: dead intermediate nodes stay in the arena. Fine for
  batch quantification; long-lived services need mark-sweep GC.
- No dynamic variable reordering (sifting); DFS order only.
- MCS restricted to coherent trees (prime implicants for non-coherent logic
  need Coudert-Madre / meta-products).
- No complement edges (would roughly halve node count).
