//! Reduced Ordered Binary Decision Diagram engine for fault tree analysis.
//!
//! Memory layout notes (the reason this is in Rust):
//! - A node is 12 bytes: (var: u32, low: u32, high: u32), stored in one flat
//!   `Vec<Node>` arena. References between nodes are u32 indices, not
//!   pointers: half the size of a pointer on 64-bit, and the arena is
//!   contiguous so traversals are cache-friendly.
//! - Hash consing (the "unique table") guarantees structural sharing: an
//!   identical sub-function is stored exactly once, so `mk` gives canonical,
//!   maximally-shared DAGs and O(1) equality checks (index comparison).
//! - The apply cache memoizes (op, f, g) -> result, which is what turns the
//!   naive exponential Shannon expansion into the classic
//!   O(|f|·|g|) apply algorithm.

use std::collections::HashMap;

use crate::reorder::{SiftLimits, Sifter};
use crate::zbdd::{self, Zbdd};

pub const ZERO: u32 = 0;
pub const ONE: u32 = 1;
const TERMINAL_VAR: u32 = u32::MAX;
/// Remap-table entry of a collected node (see `Bdd::gc`).
const DEAD: u32 = u32::MAX;

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct Node {
    var: u32,
    low: u32,
    high: u32,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
enum Op {
    And,
    Or,
    Xor,
    Without,
}

pub struct Bdd {
    nodes: Vec<Node>,
    unique: HashMap<Node, u32>,
    apply_cache: HashMap<(Op, u32, u32), u32>,
    not_cache: HashMap<u32, u32>,
    minsol_cache: HashMap<u32, u32>,
}

impl Bdd {
    pub fn new() -> Self {
        let mut nodes = Vec::with_capacity(1 << 16);
        // Index 0 = terminal FALSE, index 1 = terminal TRUE.
        nodes.push(Node { var: TERMINAL_VAR, low: 0, high: 0 });
        nodes.push(Node { var: TERMINAL_VAR, low: 1, high: 1 });
        Bdd {
            nodes,
            unique: HashMap::new(),
            apply_cache: HashMap::new(),
            not_cache: HashMap::new(),
            minsol_cache: HashMap::new(),
        }
    }

    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    #[inline]
    fn var(&self, f: u32) -> u32 {
        self.nodes[f as usize].var
    }
    #[inline]
    fn low(&self, f: u32) -> u32 {
        self.nodes[f as usize].low
    }
    #[inline]
    fn high(&self, f: u32) -> u32 {
        self.nodes[f as usize].high
    }
    #[inline]
    fn is_terminal(f: u32) -> bool {
        f <= ONE
    }

    /// Canonical node constructor (hash consing + redundant-node elision).
    fn mk(&mut self, var: u32, low: u32, high: u32) -> u32 {
        if low == high {
            return low; // reduction rule: redundant test
        }
        let key = Node { var, low, high };
        if let Some(&idx) = self.unique.get(&key) {
            return idx; // reduction rule: structural sharing
        }
        let idx = self.nodes.len() as u32;
        self.nodes.push(key);
        self.unique.insert(key, idx);
        idx
    }

    /// "if var then high else low", where `var` precedes every variable of
    /// `low` and `high` in the order (checked; a violation would silently
    /// break canonicity).
    pub fn branch(&mut self, var: u32, low: u32, high: u32) -> u32 {
        assert!(var < self.var(low) && var < self.var(high),
                "BDD variable order violated");
        self.mk(var, low, high)
    }

    /// The BDD variable for basic event `var` (ordering = var index).
    pub fn variable(&mut self, var: u32) -> u32 {
        self.mk(var, ZERO, ONE)
    }

    pub fn and(&mut self, f: u32, g: u32) -> u32 {
        self.apply(Op::And, f, g)
    }
    pub fn or(&mut self, f: u32, g: u32) -> u32 {
        self.apply(Op::Or, f, g)
    }
    pub fn xor(&mut self, f: u32, g: u32) -> u32 {
        self.apply(Op::Xor, f, g)
    }

    pub fn not(&mut self, f: u32) -> u32 {
        if f == ZERO {
            return ONE;
        }
        if f == ONE {
            return ZERO;
        }
        if let Some(&r) = self.not_cache.get(&f) {
            return r;
        }
        let (v, lo, hi) = (self.var(f), self.low(f), self.high(f));
        let nl = self.not(lo);
        let nh = self.not(hi);
        let r = self.mk(v, nl, nh);
        self.not_cache.insert(f, r);
        r
    }

    /// k-of-n gate, built by dynamic programming over (index, still-needed).
    pub fn atleast(&mut self, k: usize, inputs: &[u32]) -> u32 {
        fn rec(bdd: &mut Bdd, inputs: &[u32], i: usize, k: usize) -> u32 {
            if k == 0 {
                return ONE;
            }
            if inputs.len() - i < k {
                return ZERO;
            }
            let with = rec(bdd, inputs, i + 1, k - 1);
            let without = rec(bdd, inputs, i + 1, k);
            let f = inputs[i];
            let a = bdd.and(f, with);
            let nf = bdd.not(f);
            let b = bdd.and(nf, without);
            bdd.or(a, b)
        }
        rec(self, inputs, 0, k)
    }

    /// Shannon-expansion apply with memoization.
    fn apply(&mut self, op: Op, f: u32, g: u32) -> u32 {
        // Terminal short-circuits.
        match op {
            Op::And => {
                if f == ZERO || g == ZERO {
                    return ZERO;
                }
                if f == ONE {
                    return g;
                }
                if g == ONE {
                    return f;
                }
                if f == g {
                    return f;
                }
            }
            Op::Or => {
                if f == ONE || g == ONE {
                    return ONE;
                }
                if f == ZERO {
                    return g;
                }
                if g == ZERO {
                    return f;
                }
                if f == g {
                    return f;
                }
            }
            Op::Xor => {
                if f == ZERO {
                    return g;
                }
                if g == ZERO {
                    return f;
                }
                if f == g {
                    return ZERO;
                }
                if f == ONE {
                    return self.not(g);
                }
                if g == ONE {
                    return self.not(f);
                }
            }
            Op::Without => unreachable!("without() has its own driver"),
        }
        // Commutative ops: normalize operand order for better cache hits.
        let (f, g) = if f <= g { (f, g) } else { (g, f) };
        if let Some(&r) = self.apply_cache.get(&(op, f, g)) {
            return r;
        }
        let (vf, vg) = (self.var(f), self.var(g));
        let v = vf.min(vg);
        let (f0, f1) = if vf == v {
            (self.low(f), self.high(f))
        } else {
            (f, f)
        };
        let (g0, g1) = if vg == v {
            (self.low(g), self.high(g))
        } else {
            (g, g)
        };
        let lo = self.apply(op, f0, g0);
        let hi = self.apply(op, f1, g1);
        let r = self.mk(v, lo, hi);
        self.apply_cache.insert((op, f, g), r);
        r
    }

    // ---------------------------------------------------------------------
    // Quantification
    // ---------------------------------------------------------------------

    /// Exact top-event probability. `p[i]` = probability of basic event i.
    /// P(f) = p(v)·P(high) + (1-p(v))·P(low), memoized over shared nodes,
    /// so it runs in O(|f|) — this exactness (no rare-event or min-cut-upper
    /// -bound approximation) is the whole reason BDDs are used in PSA.
    pub fn probability(&self, f: u32, p: &[f64]) -> f64 {
        let mut memo: HashMap<u32, f64> = HashMap::new();
        self.prob_rec(f, p, &mut memo)
    }

    fn prob_rec(&self, f: u32, p: &[f64], memo: &mut HashMap<u32, f64>) -> f64 {
        if f == ZERO {
            return 0.0;
        }
        if f == ONE {
            return 1.0;
        }
        if let Some(&v) = memo.get(&f) {
            return v;
        }
        let pv = p[self.var(f) as usize];
        let hi = self.prob_rec(self.high(f), p, memo);
        let lo = self.prob_rec(self.low(f), p, memo);
        let r = pv * hi + (1.0 - pv) * lo;
        memo.insert(f, r);
        r
    }

    /// Compile the probability pass of `f` into a flat evaluation plan for
    /// repeated evaluation (Monte Carlo): the reachable nodes in ascending
    /// arena order. Children are always created before their parents
    /// (`mk` pushes after both operands exist), so ascending index order is
    /// a topological order and one forward sweep evaluates the function.
    /// Each node applies the same expression as `prob_rec`, so
    /// `plan.eval(p)` equals `probability(f, p)` bit for bit.
    pub fn prob_plan(&self, f: u32) -> ProbPlan {
        // Nodes in post-order of a depth-first walk from the root, low child
        // before high: children before parents, and an order that depends
        // only on the function's structure, never on node numbers — so a
        // garbage collection (which renumbers nodes) cannot change the
        // summation order of `ProbPlan::all_cofactors` (FR-50).
        let mut reach: Vec<u32> = Vec::new();
        let mut seen: std::collections::HashSet<u32> = std::collections::HashSet::new();
        let mut stack: Vec<(u32, bool)> = vec![(f, false)];
        while let Some((g, expanded)) = stack.pop() {
            if Self::is_terminal(g) {
                continue;
            }
            if expanded {
                reach.push(g);
                continue;
            }
            if !seen.insert(g) {
                continue;
            }
            stack.push((g, true));
            stack.push((self.high(g), false));
            stack.push((self.low(g), false));
        }
        let mut slot: HashMap<u32, u32> = HashMap::with_capacity(reach.len());
        slot.insert(ZERO, 0);
        slot.insert(ONE, 1);
        let mut nodes = Vec::with_capacity(reach.len());
        for (j, &g) in reach.iter().enumerate() {
            let (lo, hi) = (slot[&self.low(g)], slot[&self.high(g)]);
            nodes.push((self.var(g), lo, hi));
            slot.insert(g, j as u32 + 2);
        }
        ProbPlan { nodes, root: slot[&f] }
    }

    /// Birnbaum importance of variable v: P(f | v=1) - P(f | v=0), through
    /// restricted BDDs. The engine computes Birnbaum from plan cofactors
    /// (`ProbPlan::eval_cofactor`, no arena growth); this reference path is
    /// kept for the unit test that cross-checks the two.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn birnbaum(&mut self, f: u32, v: u32, p: &[f64]) -> f64 {
        let f1 = self.restrict(f, v, true);
        let f0 = self.restrict(f, v, false);
        self.probability(f1, p) - self.probability(f0, p)
    }

    /// Cofactor f|_{v=val}, memoized over shared nodes: O(|f|). (Without
    /// the memo the recursion revisits every path of a shared DAG, which is
    /// exponential: V&V anomaly D-14.)
    pub fn restrict(&mut self, f: u32, v: u32, val: bool) -> u32 {
        let mut memo = HashMap::new();
        self.restrict_rec(f, v, val, &mut memo)
    }

    fn restrict_rec(&mut self, f: u32, v: u32, val: bool,
                    memo: &mut HashMap<u32, u32>) -> u32 {
        if Self::is_terminal(f) || self.var(f) > v {
            return f;
        }
        if self.var(f) == v {
            return if val { self.high(f) } else { self.low(f) };
        }
        if let Some(&r) = memo.get(&f) {
            return r;
        }
        let (fv, lo, hi) = (self.var(f), self.low(f), self.high(f));
        let l = self.restrict_rec(lo, v, val, memo);
        let h = self.restrict_rec(hi, v, val, memo);
        let r = self.mk(fv, l, h);
        memo.insert(f, r);
        r
    }

    // ---------------------------------------------------------------------
    // Minimal cut sets (Rauzy's minimal-solutions algorithm).
    // Valid for COHERENT functions (monotone: AND/OR/VOTE of positive
    // literals). The caller is responsible for checking coherence.
    // ---------------------------------------------------------------------

    /// BDD encoding exactly the minimal solutions of a monotone f.
    pub fn minsol(&mut self, f: u32) -> u32 {
        if Self::is_terminal(f) {
            return f;
        }
        if let Some(&r) = self.minsol_cache.get(&f) {
            return r;
        }
        let (v, lo, hi) = (self.var(f), self.low(f), self.high(f));
        let l = self.minsol(lo);
        let h = self.minsol(hi);
        // Solutions through the high branch are minimal only if not already
        // implied by a solution that doesn't need v at all.
        let h2 = self.without(h, l);
        let r = self.mk(v, l, h2);
        self.minsol_cache.insert(f, r);
        r
    }

    /// f ⊘ g: solutions of f that are not supersets of any solution of g.
    fn without(&mut self, f: u32, g: u32) -> u32 {
        if f == ZERO || g == ONE {
            return ZERO;
        }
        if g == ZERO || f == ONE {
            return f;
        }
        let key = (Op::Without, f, g); // NOT commutative: no normalization
        if let Some(&r) = self.apply_cache.get(&key) {
            return r;
        }
        let (vf, vg) = (self.var(f), self.var(g));
        let r = if vf < vg {
            let (lo, hi) = (self.low(f), self.high(f));
            let l = self.without(lo, g);
            let h = self.without(hi, g);
            self.mk(vf, l, h)
        } else if vf > vg {
            // g's solutions mentioning vg can't subsume sets without vg.
            let g0 = self.low(g);
            self.without(f, g0)
        } else {
            let (f0, f1) = (self.low(f), self.high(f));
            let (g0, g1) = (self.low(g), self.high(g));
            let l = self.without(f0, g0);
            // A set containing v is subsumed by g-solutions with v (g1)
            // or without v (g0): remove both.
            let t = self.without(f1, g1);
            let h = self.without(t, g0);
            self.mk(vf, l, h)
        };
        self.apply_cache.insert(key, r);
        r
    }

    // ---------------------------------------------------------------------
    // Prime implicants (non-coherent functions).
    // ---------------------------------------------------------------------

    /// Prime implicants of f, as a ZBDD over literals (variable v's positive
    /// literal is ZBDD variable 2v, its negation 2v + 1). For f = ite(x, f1,
    /// f0) and g = f0 ∧ f1 (Coudert–Madre, Morreale):
    ///     PI(f) = PI(g) ∪ x·(PI(f1) ∖ PI(g)) ∪ ¬x·(PI(f0) ∖ PI(g))
    /// — a prime of f either avoids x (then it is a prime of the consensus
    /// g), or is x·p with p a prime of f1 that does not imply f0 (a prime of
    /// f1 implying f0 implies g and is then itself a prime of g), and
    /// symmetrically for ¬x. For a coherent f this is exactly the set of
    /// minimal cut sets (`minsol`), all literals positive. Memoized on f.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn prime_implicants(&mut self, f: u32, z: &mut Zbdd) -> u32 {
        let mut memo = HashMap::new();
        self.pi_rec(f, z, &mut memo)
    }

    /// Prime implicants of f with at most `k` literals, built directly
    /// (never the full set): with PI_0(f) = {∅} if f ≡ 1 else ∅,
    ///     PI_k(f) = PI_k(g) ∪ x·(PI_{k-1}(f1) ∖ PI_{k-1}(g))
    ///                       ∪ ¬x·(PI_{k-1}(f0) ∖ PI_{k-1}(g)).
    /// Exact: whether a product of order ≤ k−1 is a prime of g is decided
    /// within PI_{k-1}(g). `None` = no limit (the full recursion).
    /// Memoized on (f, k).
    pub fn prime_implicants_upto(&mut self, f: u32, z: &mut Zbdd, k: Option<usize>) -> u32 {
        match k {
            None => {
                let mut memo = HashMap::new();
                self.pi_rec(f, z, &mut memo)
            }
            Some(k) => {
                let mut memo = HashMap::new();
                self.pi_k_rec(f, z, k, &mut memo)
            }
        }
    }

    fn pi_k_rec(&mut self, f: u32, z: &mut Zbdd, k: usize,
                memo: &mut HashMap<(u32, usize), u32>) -> u32 {
        if f == ZERO {
            return zbdd::EMPTY;
        }
        if f == ONE {
            return zbdd::BASE;
        }
        if k == 0 {
            return zbdd::EMPTY;
        }
        if let Some(&r) = memo.get(&(f, k)) {
            return r;
        }
        let (v, f0, f1) = (self.var(f), self.low(f), self.high(f));
        let g = self.and(f0, f1);
        let pg = self.pi_k_rec(g, z, k, memo);
        let pg1 = self.pi_k_rec(g, z, k - 1, memo);
        let p1 = self.pi_k_rec(f1, z, k - 1, memo);
        let p0 = self.pi_k_rec(f0, z, k - 1, memo);
        let with_x = z.diff(p1, pg1);
        let with_x = z.attach(2 * v, with_x);
        let with_not_x = z.diff(p0, pg1);
        let with_not_x = z.attach(2 * v + 1, with_not_x);
        let r = z.union(pg, with_x);
        let r = z.union(r, with_not_x);
        memo.insert((f, k), r);
        r
    }

    fn pi_rec(&mut self, f: u32, z: &mut Zbdd, memo: &mut HashMap<u32, u32>) -> u32 {
        if f == ZERO {
            return zbdd::EMPTY;
        }
        if f == ONE {
            return zbdd::BASE;
        }
        if let Some(&r) = memo.get(&f) {
            return r;
        }
        let (v, f0, f1) = (self.var(f), self.low(f), self.high(f));
        let g = self.and(f0, f1);
        let pg = self.pi_rec(g, z, memo);
        let p1 = self.pi_rec(f1, z, memo);
        let p0 = self.pi_rec(f0, z, memo);
        let with_x = z.diff(p1, pg);
        let with_x = z.attach(2 * v, with_x);
        let with_not_x = z.diff(p0, pg);
        let with_not_x = z.attach(2 * v + 1, with_not_x);
        let r = z.union(pg, with_x);
        let r = z.union(r, with_not_x);
        memo.insert(f, r);
        r
    }

    // ---------------------------------------------------------------------
    // Garbage collection (mark and compact).
    // ---------------------------------------------------------------------

    /// Collect every node not reachable from `roots`. Returns the remap
    /// table old index -> new index (`DEAD` for collected nodes); the caller
    /// must pass every handle it keeps through [`Bdd::remap`].
    ///
    /// Survivors keep their relative arena order, so a child still precedes
    /// its parents (the invariant `prob_plan` relies on) and the collected
    /// BDD is the same graph with renumbered nodes: every function, every
    /// probability pass and every path enumeration is unchanged, bit for
    /// bit. The unique table is rebuilt from the survivors; the memo caches
    /// (apply, not, minsol) are dropped, which only costs recomputation.
    pub fn gc(&mut self, roots: &[u32]) -> Vec<u32> {
        let n = self.nodes.len();
        let mut live = vec![false; n];
        live[ZERO as usize] = true;
        live[ONE as usize] = true;
        let mut stack: Vec<u32> = roots.to_vec();
        while let Some(f) = stack.pop() {
            let i = f as usize;
            if live[i] {
                continue;
            }
            live[i] = true;
            stack.push(self.nodes[i].low);
            stack.push(self.nodes[i].high);
        }
        let mut map = vec![DEAD; n];
        let mut next = 0u32;
        for i in 0..n {
            if live[i] {
                map[i] = next;
                next += 1;
            }
        }
        let mut nodes = Vec::with_capacity((next as usize).max(1 << 16));
        for i in 0..n {
            if live[i] {
                let nd = self.nodes[i];
                nodes.push(if i as u32 <= ONE {
                    nd
                } else {
                    Node { var: nd.var, low: map[nd.low as usize], high: map[nd.high as usize] }
                });
            }
        }
        let mut unique = HashMap::with_capacity(nodes.len());
        for (i, nd) in nodes.iter().enumerate().skip(2) {
            unique.insert(*nd, i as u32);
        }
        self.nodes = nodes;
        self.unique = unique;
        self.apply_cache = HashMap::new();
        self.not_cache = HashMap::new();
        self.minsol_cache = HashMap::new();
        map
    }

    // ---------------------------------------------------------------------
    // Dynamic variable reordering (sifting, see reorder.rs).
    // ---------------------------------------------------------------------

    /// Internal nodes reachable from `roots`.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn reachable_count(&self, roots: &[u32]) -> usize {
        let mut seen = vec![false; self.nodes.len()];
        let mut stack: Vec<u32> = roots.to_vec();
        let mut count = 0;
        while let Some(f) = stack.pop() {
            if Self::is_terminal(f) || seen[f as usize] {
                continue;
            }
            seen[f as usize] = true;
            count += 1;
            stack.push(self.low(f));
            stack.push(self.high(f));
        }
        count
    }

    /// Copy the functions `roots` into a sifting manager over `nvars`
    /// variables, variable v at level v (this manager's order); returns it
    /// with each root's handle there, each holding one reference.
    pub fn to_sifter(&self, roots: &[u32], nvars: usize) -> (Sifter, Vec<u32>) {
        let n = self.nodes.len();
        let mut live = vec![false; n];
        let mut stack: Vec<u32> = roots.to_vec();
        while let Some(f) = stack.pop() {
            if Self::is_terminal(f) || live[f as usize] {
                continue;
            }
            live[f as usize] = true;
            stack.push(self.low(f));
            stack.push(self.high(f));
        }
        let mut s = Sifter::new(nvars);
        let mut map = vec![u32::MAX; n];
        map[ZERO as usize] = 0;
        map[ONE as usize] = 1;
        // children precede parents in the arena, so ascending index order
        // builds every child before its parents
        let mut built = Vec::new();
        for i in 2..n {
            if live[i] {
                let nd = self.nodes[i];
                let h = s.mk(nd.var, map[nd.low as usize], map[nd.high as usize]);
                map[i] = h;
                built.push(h);
            }
        }
        let handles: Vec<u32> = roots.iter().map(|&r| map[r as usize]).collect();
        for &h in &handles {
            s.mk_hold(h);
        }
        // drop the construction holds, parents first: every built node is
        // reachable from a root, so none is freed
        for &h in built.iter().rev() {
            s.deref(h);
        }
        s.set_interaction(&handles);
        (s, handles)
    }

    /// Sift the variable order of the functions `roots` (all other nodes
    /// are dropped, as by [`Bdd::gc`]) and rebuild this manager with the
    /// new order: returns the remap table old index -> new index for the
    /// roots (`DEAD` elsewhere; use [`Bdd::remap`]) and the permutation
    /// `perm`, where old variable v is new variable `perm[v]` (its level).
    /// Nodes are numbered by a structural traversal (roots in the given
    /// order, low before high, children first), so the result does not
    /// depend on hashing, and children precede parents as `prob_plan`
    /// requires. The memo caches are dropped.
    pub fn reorder(&mut self, roots: &[u32], nvars: usize, limits: SiftLimits)
        -> (Vec<u32>, Vec<u32>)
    {
        let (mut s, handles) = self.to_sifter(roots, nvars);
        let old_len = self.nodes.len();
        // the sifter now holds every live function: release this arena and
        // its tables before sifting, so the two never coexist at full size
        *self = Bdd::new();
        s.sift(limits);
        let perm: Vec<u32> = (0..nvars as u32).map(|v| s.level_of(v)).collect();
        let mut fresh = Bdd::new();
        let mut memo: HashMap<u32, u32> = HashMap::new();
        memo.insert(0, ZERO);
        memo.insert(1, ONE);
        fn build(fresh: &mut Bdd, s: &Sifter, perm: &[u32], h: u32,
                 memo: &mut HashMap<u32, u32>) -> u32 {
            if let Some(&r) = memo.get(&h) {
                return r;
            }
            let (v, lo, hi) = s.parts(h);
            let l = build(fresh, s, perm, lo, memo);
            let g = build(fresh, s, perm, hi, memo);
            let r = fresh.mk(perm[v as usize], l, g);
            memo.insert(h, r);
            r
        }
        let mut map = vec![DEAD; old_len];
        map[ZERO as usize] = ZERO;
        map[ONE as usize] = ONE;
        for (i, &h) in handles.iter().enumerate() {
            let r = build(&mut fresh, &s, &perm, h, &mut memo);
            map[roots[i] as usize] = r;
        }
        *self = fresh;
        (map, perm)
    }

    /// The arena's nodes as (var, low, high), terminals included (tests).
    #[cfg(test)]
    pub fn debug_nodes(&self) -> Vec<(u32, u32, u32)> {
        self.nodes.iter().map(|n| (n.var, n.low, n.high)).collect()
    }

    /// Truth value of f under an assignment indexed by variable.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn eval(&self, mut f: u32, x: &[bool]) -> bool {
        while !Self::is_terminal(f) {
            f = if x[self.var(f) as usize] { self.high(f) } else { self.low(f) };
        }
        f == ONE
    }

    /// A handle after [`Bdd::gc`]; panics on a handle that was not a root
    /// (a dangling handle is a programming error, never silently wrong).
    pub fn remap(map: &[u32], f: u32) -> u32 {
        let g = map[f as usize];
        assert!(g != DEAD, "BDD handle {f} was collected: it was not passed as a root");
        g
    }

    /// Enumerate cut sets from a minsol BDD as sorted variable lists.
    /// `limit` caps enumeration for very large models (None = all).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn enumerate_paths(&self, f: u32, limit: Option<usize>) -> Vec<Vec<u32>> {
        self.enumerate_paths_upto(f, limit, None)
    }

    /// As `enumerate_paths`, keeping only paths with at most `order_limit`
    /// variables (cut sets of order ≤ K); the high branch is pruned once
    /// the path is full, so larger sets are never generated.
    pub fn enumerate_paths_upto(&self, f: u32, limit: Option<usize>,
                                order_limit: Option<usize>) -> Vec<Vec<u32>> {
        let mut out = Vec::new();
        let mut path = Vec::new();
        self.paths_rec(f, &mut path, &mut out, limit, order_limit);
        out
    }

    fn paths_rec(
        &self,
        f: u32,
        path: &mut Vec<u32>,
        out: &mut Vec<Vec<u32>>,
        limit: Option<usize>,
        order_limit: Option<usize>,
    ) {
        if let Some(l) = limit {
            if out.len() >= l {
                return;
            }
        }
        if f == ZERO {
            return;
        }
        if f == ONE {
            out.push(path.clone());
            return;
        }
        // High edge: variable is in the cut set.
        if order_limit.map_or(true, |k| path.len() < k) {
            path.push(self.var(f));
            self.paths_rec(self.high(f), path, out, limit, order_limit);
            path.pop();
        }
        // Low edge: variable absent.
        self.paths_rec(self.low(f), path, out, limit, order_limit);
    }
}

impl Default for Bdd {
    fn default() -> Self {
        Self::new()
    }
}

/// Flat probability-evaluation plan for one BDD root (see `Bdd::prob_plan`).
/// Slots 0 and 1 are the terminals; node j lives in slot j + 2.
pub struct ProbPlan {
    nodes: Vec<(u32, u32, u32)>,
    root: u32,
}

impl ProbPlan {
    /// Number of non-terminal nodes (the size of the BDD it evaluates).
    pub fn len(&self) -> usize {
        self.nodes.len()
    }

    /// Rename variables (e.g. from a compiler-local numbering to a global one).
    pub fn map_vars(&mut self, f: impl Fn(u32) -> u32) {
        for n in &mut self.nodes {
            n.0 = f(n.0);
        }
    }

    /// P(f) under variable probabilities `p`; `buf` is reusable scratch.
    pub fn eval(&self, p: &[f64], buf: &mut Vec<f64>) -> f64 {
        buf.clear();
        buf.push(0.0);
        buf.push(1.0);
        for &(var, lo, hi) in &self.nodes {
            let pv = p[var as usize];
            let r = pv * buf[hi as usize] + (1.0 - pv) * buf[lo as usize];
            buf.push(r);
        }
        buf[self.root as usize]
    }

    /// Variables the plan depends on, ascending and deduplicated.
    pub fn support(&self) -> Vec<u32> {
        let mut vs: Vec<u32> = self.nodes.iter().map(|n| n.0).collect();
        vs.sort_unstable();
        vs.dedup();
        vs
    }

    /// Every cofactor probability at once (FR-50): (v, P(f | v = 1),
    /// P(f | v = 0)) for each variable of the support, ascending, in
    /// O(|plan| log |support|) instead of two `eval_cofactor` passes per
    /// variable. Every root-to-terminal path meets the level of v once —
    /// at a node labelled v, or on one edge that skips the level — and the
    /// probability of reaching a node above that level, and of each node
    /// below it, do not involve v. So P(f | v = val) = Σ over v-nodes n of
    /// T(n) · P(child_val(n)) + Σ over level-skipping edges e of T(e) ·
    /// P(target(e)), with T the top-down reach probability. Sums and
    /// products of non-negative terms only (range sums by a segment tree,
    /// no prefix differences), so no subtraction: a tiny P(f | v = 0) keeps
    /// full relative precision, as in `eval_cofactor`. Requires the plan's
    /// variables to increase along every edge (the BDD order); None
    /// otherwise, for the caller to fall back to `eval_cofactor`.
    pub fn all_cofactors(&self, p: &[f64]) -> Option<Vec<(u32, f64, f64)>> {
        let support = self.support();
        if support.is_empty() {
            return Some(Vec::new());
        }
        let n = self.nodes.len() + 2;
        let var_of = |i: usize| self.nodes[i - 2].0;
        for (j, &(v, lo, hi)) in self.nodes.iter().enumerate() {
            for c in [lo, hi] {
                if c >= 2 && (c as usize >= j + 2 || var_of(c as usize) <= v) {
                    return None;
                }
            }
        }
        // bottom-up probabilities, exactly as `eval`
        let mut pr = vec![0.0; n];
        pr[1] = 1.0;
        for (j, &(var, lo, hi)) in self.nodes.iter().enumerate() {
            let pv = p[var as usize];
            pr[j + 2] = pv * pr[hi as usize] + (1.0 - pv) * pr[lo as usize];
        }
        // top-down reach probabilities (parents come after their children)
        let mut reach = vec![0.0; n];
        reach[self.root as usize] = 1.0;
        for j in (0..self.nodes.len()).rev() {
            let (var, lo, hi) = self.nodes[j];
            let t = reach[j + 2];
            if t == 0.0 {
                continue;
            }
            let pv = p[var as usize];
            reach[hi as usize] += t * pv;
            reach[lo as usize] += t * (1.0 - pv);
        }
        let m = support.len();
        let rank_of = |v: u32| support.binary_search(&v).unwrap();
        let rank_at = |i: u32| if i < 2 { m } else { rank_of(var_of(i as usize)) };
        // segment tree over support ranks: add non-negative values to
        // ranges, read each leaf as the sum along its root path
        let mut size = 1;
        while size < m {
            size *= 2;
        }
        let mut seg = vec![0.0f64; 2 * size];
        let add = |seg: &mut Vec<f64>, lo: usize, hi: usize, x: f64| {
            // [lo, hi) over ranks
            if lo >= hi || x == 0.0 {
                return;
            }
            let (mut l, mut r) = (lo + size, hi + size);
            while l < r {
                if l & 1 == 1 {
                    seg[l] += x;
                    l += 1;
                }
                if r & 1 == 1 {
                    r -= 1;
                    seg[r] += x;
                }
                l /= 2;
                r /= 2;
            }
        };
        let (mut s1, mut s0) = (vec![0.0; m], vec![0.0; m]);
        // (no level lies above the root: it carries the smallest variable
        // of its own support, so every path starts at a support level)
        for (j, &(var, lo, hi)) in self.nodes.iter().enumerate() {
            let t = reach[j + 2];
            let r = rank_of(var);
            s1[r] += t * pr[hi as usize];
            s0[r] += t * pr[lo as usize];
            let pv = p[var as usize];
            add(&mut seg, r + 1, rank_at(hi), t * pv * pr[hi as usize]);
            add(&mut seg, r + 1, rank_at(lo), t * (1.0 - pv) * pr[lo as usize]);
        }
        Some(support.iter().enumerate().map(|(r, &v)| {
            let mut skip = 0.0;
            let mut i = r + size;
            while i >= 1 {
                skip += seg[i];
                i /= 2;
            }
            (v, s1[r] + skip, s0[r] + skip)
        }).collect())
    }

    /// Cofactor probability P(f | v = val): nodes labelled `v` take their
    /// `val` child directly (the Shannon cofactor), every other node applies
    /// the `eval` expression. No subtraction is involved, so P(f | v = 0)
    /// keeps full relative precision even when it is tiny next to P(f).
    pub fn eval_cofactor(&self, p: &[f64], v: u32, val: bool,
                         buf: &mut Vec<f64>) -> f64 {
        buf.clear();
        buf.push(0.0);
        buf.push(1.0);
        for &(var, lo, hi) in &self.nodes {
            let r = if var == v {
                buf[if val { hi } else { lo } as usize]
            } else {
                let pv = p[var as usize];
                pv * buf[hi as usize] + (1.0 - pv) * buf[lo as usize]
            };
            buf.push(r);
        }
        buf[self.root as usize]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 2-train system: TOP = (A_fts OR A_ftr) AND (B_fts OR B_ftr)
    fn two_train(bdd: &mut Bdd) -> u32 {
        let a1 = bdd.variable(0);
        let a2 = bdd.variable(1);
        let b1 = bdd.variable(2);
        let b2 = bdd.variable(3);
        let ta = bdd.or(a1, a2);
        let tb = bdd.or(b1, b2);
        bdd.and(ta, tb)
    }

    #[test]
    fn probability_exact() {
        let mut bdd = Bdd::new();
        let top = two_train(&mut bdd);
        let p = vec![1e-3, 2e-3, 1e-3, 2e-3];
        // P(train) = 1-(1-1e-3)(1-2e-3); P(top) = P(train)^2 (independent)
        let ptrain = 1.0 - (1.0 - 1e-3) * (1.0 - 2e-3);
        let expect = ptrain * ptrain;
        let got = bdd.probability(top, &p);
        assert!((got - expect).abs() < 1e-15, "got {got}, expect {expect}");
    }

    #[test]
    fn mcs_two_train() {
        let mut bdd = Bdd::new();
        let top = two_train(&mut bdd);
        let ms = bdd.minsol(top);
        let mut cuts = bdd.enumerate_paths(ms, None);
        cuts.sort();
        // Exactly the 4 double cut sets {Ai, Bj}.
        assert_eq!(
            cuts,
            vec![vec![0, 2], vec![0, 3], vec![1, 2], vec![1, 3]]
        );
    }

    #[test]
    fn mcs_subsumption() {
        // TOP = A OR (A AND B): MCS must be just {A}.
        let mut bdd = Bdd::new();
        let a = bdd.variable(0);
        let b = bdd.variable(1);
        let ab = bdd.and(a, b);
        let top = bdd.or(a, ab);
        let ms = bdd.minsol(top);
        let cuts = bdd.enumerate_paths(ms, None);
        assert_eq!(cuts, vec![vec![0]]);
    }

    #[test]
    fn vote_gate() {
        // 2-of-3 with p=0.1 each: P = 3p^2(1-p) + p^3 = 0.028
        let mut bdd = Bdd::new();
        let vars: Vec<u32> = (0..3).map(|i| bdd.variable(i)).collect();
        let top = bdd.atleast(2, &vars);
        let p = vec![0.1; 3];
        let got = bdd.probability(top, &p);
        assert!((got - 0.028).abs() < 1e-12, "got {got}");
        let ms = bdd.minsol(top);
        let mut cuts = bdd.enumerate_paths(ms, None);
        cuts.sort();
        assert_eq!(cuts, vec![vec![0, 1], vec![0, 2], vec![1, 2]]);
    }

    #[test]
    fn negation_probability() {
        // Non-coherent probability still exact: P(A AND NOT B).
        let mut bdd = Bdd::new();
        let a = bdd.variable(0);
        let b = bdd.variable(1);
        let nb = bdd.not(b);
        let f = bdd.and(a, nb);
        let p = vec![0.3, 0.4];
        let got = bdd.probability(f, &p);
        assert!((got - 0.3 * 0.6).abs() < 1e-15);
    }

    /// FR-50: every cofactor from the one sweep equals the per-variable
    /// pass to 1e-12 relative on random logic (xor and not included); an
    /// exact zero stays exactly zero; a plan whose variables were renamed
    /// out of the BDD order is refused (None), not mis-evaluated.
    #[test]
    fn all_cofactors_match_per_variable_passes() {
        let mut state = 0x9E37_79B9_7F4A_7C15u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let mut compared = 0;
        for _case in 0..300 {
            let mut bdd = Bdd::new();
            let nv = 2 + (next() % 12) as u32;
            let mut pool: Vec<u32> = (0..nv).map(|v| bdd.variable(v)).collect();
            for _ in 0..(3 + next() % 20) {
                let a = pool[(next() % pool.len() as u64) as usize];
                let b = pool[(next() % pool.len() as u64) as usize];
                let g = match next() % 4 {
                    0 => bdd.and(a, b),
                    1 => bdd.or(a, b),
                    2 => bdd.xor(a, b),
                    _ => bdd.not(a),
                };
                pool.push(g);
            }
            let f = *pool.last().unwrap();
            let p: Vec<f64> = (0..nv)
                .map(|_| match next() % 3 {
                    0 => 1e-9 * (1 + next() % 1000) as f64,
                    _ => (next() % 1_000_000) as f64 / 1_000_001.0,
                })
                .collect();
            let plan = bdd.prob_plan(f);
            let all = plan.all_cofactors(&p).expect("a BDD's plan is ordered");
            assert_eq!(all.iter().map(|x| x.0).collect::<Vec<_>>(), plan.support());
            let mut buf = Vec::new();
            for &(v, c1, c0) in &all {
                let (e1, e0) = (plan.eval_cofactor(&p, v, true, &mut buf),
                                plan.eval_cofactor(&p, v, false, &mut buf));
                for (a, b) in [(c1, e1), (c0, e0)] {
                    assert!((a - b).abs() <= 1e-12 * a.abs().max(b.abs()),
                            "cofactor of {v}: sweep {a} vs pass {b}");
                    assert_eq!(a == 0.0, b == 0.0, "exact zero kept");
                }
                compared += 1;
            }
        }
        assert!(compared > 500, "only {compared} cofactors compared");
        // x ∧ g: P(f | x = 0) is exactly 0
        let mut bdd = Bdd::new();
        let (x, y, z) = (bdd.variable(0), bdd.variable(1), bdd.variable(2));
        let g = bdd.or(y, z);
        let f = bdd.and(x, g);
        let all = bdd.prob_plan(f).all_cofactors(&[0.3, 0.2, 0.1]).unwrap();
        assert_eq!(all[0].2, 0.0);
        assert!((all[0].1 - (1.0 - 0.8 * 0.9)).abs() < 1e-15);
        // variables renamed against the order: refused
        let mut plan = bdd.prob_plan(f);
        plan.map_vars(|v| 2 - v);
        assert!(plan.all_cofactors(&[0.1, 0.2, 0.3]).is_none());
        // V&V D-25: the same function built in a different order (other
        // node numbers, as after a collection) gives the same plan, hence
        // bit-identical cofactors — the plan's order is structural
        let build = |order: &[usize]| {
            let mut b = Bdd::new();
            let v: Vec<u32> = (0..6).map(|i| b.variable(i)).collect();
            let terms = [(0, 3), (1, 4), (2, 5), (0, 5), (1, 3)];
            let mut acc = ZERO;
            let mut junk = ZERO;
            for &i in order {
                let (x, y) = terms[i];
                junk = b.xor(junk, v[y]);   // unrelated nodes shift the numbering
                let t = b.and(v[x], v[y]);
                acc = b.or(acc, t);
            }
            let _ = junk;
            let plan = b.prob_plan(acc);
            (plan.nodes.clone(), plan.root,
             plan.all_cofactors(&[0.11, 0.23, 0.37, 0.41, 0.53, 0.67]).unwrap())
        };
        let (n1, r1, c1) = build(&[0, 1, 2, 3, 4]);
        let (n2, r2, c2) = build(&[4, 2, 0, 3, 1]);
        assert_eq!((n1, r1), (n2, r2), "structural plan order");
        assert!(c1.iter().zip(&c2).all(|(a, b)| a.0 == b.0 && a.1.to_bits() == b.1.to_bits()
                                        && a.2.to_bits() == b.2.to_bits()));
    }

    /// The flat plan is the recursive pass, bit for bit, on random logic.
    #[test]
    fn prob_plan_matches_recursive_pass_exactly() {
        let mut state = 0x2545_F491_4F6C_DD1Du64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _case in 0..200 {
            let mut bdd = Bdd::new();
            let nv = 2 + (next() % 9) as u32;
            let mut pool: Vec<u32> = (0..nv).map(|v| bdd.variable(v)).collect();
            for _ in 0..(3 + next() % 12) {
                let a = pool[(next() % pool.len() as u64) as usize];
                let b = pool[(next() % pool.len() as u64) as usize];
                let g = match next() % 4 {
                    0 => bdd.and(a, b),
                    1 => bdd.or(a, b),
                    2 => bdd.xor(a, b),
                    _ => bdd.not(a),
                };
                pool.push(g);
            }
            let f = *pool.last().unwrap();
            let p: Vec<f64> = (0..nv)
                .map(|_| (next() % 1_000_000) as f64 / 1_000_001.0)
                .collect();
            let plan = bdd.prob_plan(f);
            let mut buf = Vec::new();
            assert_eq!(plan.eval(&p, &mut buf).to_bits(),
                       bdd.probability(f, &p).to_bits());
        }
        // Terminal roots.
        let bdd = Bdd::new();
        let mut buf = Vec::new();
        assert_eq!(bdd.prob_plan(ZERO).eval(&[], &mut buf), 0.0);
        assert_eq!(bdd.prob_plan(ONE).eval(&[], &mut buf), 1.0);
    }

    /// Plan cofactors equal the probability of the restricted BDD (up to
    /// rounding: restrict removes nodes whose children coincide, where the
    /// plan computes p·x + (1−p)·x), satisfy the Shannon identity
    /// P = p·P1 + (1−p)·P0, and the support is exactly the variables with
    /// a node.
    #[test]
    fn plan_cofactors_match_restrict() {
        let mut state = 0x9E37_79B9_7F4A_7C15u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let close = |a: f64, b: f64| (a - b).abs() <= 1e-15 + 1e-13 * a.abs().max(b.abs());
        for _case in 0..200 {
            let mut bdd = Bdd::new();
            let nv = 2 + (next() % 9) as u32;
            let mut pool: Vec<u32> = (0..nv).map(|v| bdd.variable(v)).collect();
            for _ in 0..(3 + next() % 12) {
                let a = pool[(next() % pool.len() as u64) as usize];
                let b = pool[(next() % pool.len() as u64) as usize];
                let g = match next() % 4 {
                    0 => bdd.and(a, b),
                    1 => bdd.or(a, b),
                    2 => bdd.xor(a, b),
                    _ => bdd.not(a),
                };
                pool.push(g);
            }
            let f = *pool.last().unwrap();
            let p: Vec<f64> = (0..nv)
                .map(|_| (next() % 1_000_000) as f64 / 1_000_001.0)
                .collect();
            let plan = bdd.prob_plan(f);
            let mut buf = Vec::new();
            let pf = plan.eval(&p, &mut buf);
            let support = plan.support();
            for v in 0..nv {
                let p1 = plan.eval_cofactor(&p, v, true, &mut buf);
                let p0 = plan.eval_cofactor(&p, v, false, &mut buf);
                let r1 = bdd.restrict(f, v, true);
                let r0 = bdd.restrict(f, v, false);
                assert!(close(p1, bdd.probability(r1, &p)));
                assert!(close(p0, bdd.probability(r0, &p)));
                let pv = p[v as usize];
                assert!(close(pf, pv * p1 + (1.0 - pv) * p0));
                if !support.contains(&v) {
                    // Not in the plan: cofactors are the plan value itself.
                    assert_eq!(p1.to_bits(), pf.to_bits());
                    assert_eq!(p0.to_bits(), pf.to_bits());
                    assert_eq!(r1, f);
                }
            }
        }
    }

    /// Number of non-terminal nodes reachable from f: a property of the
    /// function (canonical ROBDD), independent of node numbering.
    fn reachable(bdd: &Bdd, f: u32) -> usize {
        let mut seen = std::collections::HashSet::new();
        let mut st = vec![f];
        while let Some(g) = st.pop() {
            if Bdd::is_terminal(g) || !seen.insert(g) {
                continue;
            }
            st.push(bdd.low(g));
            st.push(bdd.high(g));
        }
        seen.len()
    }

    /// GC is invisible: on 200 random operation sequences, a BDD that
    /// collects (random root subsets, several times) and a twin that never
    /// collects end with the same functions — identical probabilities bit
    /// for bit, identical reachable sizes and paths — and after each
    /// collection every child precedes its parent, the arena holds exactly
    /// the reachable nodes, and hash consing still finds existing nodes.
    #[test]
    fn gc_is_invisible() {
        let mut state = 0xD1B5_4A32_D192_ED03u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _case in 0..200 {
            let nv = 2 + (next() % 8) as u32;
            let mut a = Bdd::new();          // collects
            let mut b = Bdd::new();          // twin, never collects
            let mut pa: Vec<u32> = (0..nv).map(|v| a.variable(v)).collect();
            let mut pb: Vec<u32> = (0..nv).map(|v| b.variable(v)).collect();
            let p: Vec<f64> = (0..nv)
                .map(|_| (next() % 1_000_000) as f64 / 1_000_001.0)
                .collect();
            for step in 0..(10 + next() % 30) {
                let i = (next() % pa.len() as u64) as usize;
                let j = (next() % pa.len() as u64) as usize;
                let op = next() % 5;
                let (fa, fb) = match op {
                    0 => (a.and(pa[i], pa[j]), b.and(pb[i], pb[j])),
                    1 => (a.or(pa[i], pa[j]), b.or(pb[i], pb[j])),
                    2 => (a.xor(pa[i], pa[j]), b.xor(pb[i], pb[j])),
                    3 => (a.not(pa[i]), b.not(pb[i])),
                    _ => { let (x, y) = (a.minsol(pa[i]), b.minsol(pb[i])); (x, y) }
                };
                pa.push(fa);
                pb.push(fb);
                if step % 7 == 6 {
                    // Keep a random subset of the handles; drop the rest on
                    // both sides so the sequences stay aligned.
                    let keep: Vec<usize> = (0..pa.len())
                        .filter(|&k| k < nv as usize || next() % 3 != 0)
                        .collect();
                    pa = keep.iter().map(|&k| pa[k]).collect();
                    pb = keep.iter().map(|&k| pb[k]).collect();
                    let map = a.gc(&pa);
                    pa = pa.iter().map(|&h| Bdd::remap(&map, h)).collect();
                    for (k, nd) in a.nodes.iter().enumerate().skip(2) {
                        assert!((nd.low as usize) < k && (nd.high as usize) < k,
                                "child after parent at {k}");
                    }
                    let mut all = std::collections::HashSet::new();
                    for &h in &pa {
                        let mut st = vec![h];
                        while let Some(g) = st.pop() {
                            if Bdd::is_terminal(g) || !all.insert(g) { continue; }
                            st.push(a.low(g));
                            st.push(a.high(g));
                        }
                    }
                    assert_eq!(a.node_count(), all.len() + 2, "arena holds only live nodes");
                }
            }
            for (&ha, &hb) in pa.iter().zip(&pb) {
                assert_eq!(a.probability(ha, &p).to_bits(), b.probability(hb, &p).to_bits());
                assert_eq!(reachable(&a, ha), reachable(&b, hb));
                assert_eq!(a.enumerate_paths(ha, None), b.enumerate_paths(hb, None));
                let (pla, plb) = (a.prob_plan(ha), b.prob_plan(hb));
                let mut buf = Vec::new();
                assert_eq!(pla.eval(&p, &mut buf).to_bits(), plb.eval(&p, &mut buf).to_bits());
            }
            // Hash consing survives: rebuilding a kept function finds it.
            let v0 = a.variable(0);
            let v1 = a.variable(nv - 1);
            let g = a.or(v0, v1);
            let n_before = a.node_count();
            assert_eq!(a.or(v0, v1), g);
            assert_eq!(a.node_count(), n_before);
        }
    }

    #[test]
    #[should_panic(expected = "was collected")]
    fn remap_of_a_dropped_handle_panics() {
        let mut bdd = Bdd::new();
        let a = bdd.variable(0);
        let b = bdd.variable(1);
        let f = bdd.and(a, b);
        let map = bdd.gc(&[a]);
        Bdd::remap(&map, f);
    }

    fn pi_list(bdd: &mut Bdd, f: u32) -> Vec<(Vec<u32>, Vec<u32>)> {
        let mut z = Zbdd::new();
        let s = bdd.prime_implicants(f, &mut z);
        let mut v: Vec<(Vec<u32>, Vec<u32>)> = z.enumerate(s, None, None).into_iter()
            .map(|p| (p.pos, p.neg)).collect();
        v.sort();
        v
    }

    /// Hand-computed prime implicants: XOR, the consensus example
    /// x·y + ¬x·z (whose third prime y·z is the consensus term), a
    /// tautology (the empty product) and a contradiction (none).
    #[test]
    fn prime_implicants_hand_computed() {
        let mut bdd = Bdd::new();
        let (x, y, z) = (bdd.variable(0), bdd.variable(1), bdd.variable(2));
        let f = bdd.xor(x, y);
        assert_eq!(pi_list(&mut bdd, f), vec![(vec![0], vec![1]), (vec![1], vec![0])]);
        let xy = bdd.and(x, y);
        let nx = bdd.not(x);
        let nxz = bdd.and(nx, z);
        let g = bdd.or(xy, nxz);
        // x·y, y·z (consensus) and ¬x·z, sorted as (positives, negatives)
        assert_eq!(pi_list(&mut bdd, g), vec![(vec![0, 1], vec![]), (vec![1, 2], vec![]),
                                              (vec![2], vec![0])]);
        assert_eq!(pi_list(&mut bdd, ONE), vec![(vec![], vec![])]);
        assert!(pi_list(&mut bdd, ZERO).is_empty());
    }

    /// Brute force on random functions of up to 6 variables (AND/OR/XOR/
    /// NOT): the engine's primes are exactly the products (over all 3^n
    /// literal choices) that imply f and from which no literal can be
    /// dropped; on coherent functions (AND/OR only) they are exactly the
    /// minimal cut sets.
    #[test]
    fn prime_implicants_brute_force() {
        let mut state = 0x5851_F42D_4C95_7F2Du64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for case in 0..400 {
            let coherent_only = case % 2 == 0;
            let mut bdd = Bdd::new();
            let nv = 1 + (next() % 6) as u32;
            let mut pool: Vec<u32> = (0..nv).map(|v| bdd.variable(v)).collect();
            for _ in 0..(2 + next() % 10) {
                let a = pool[(next() % pool.len() as u64) as usize];
                let b = pool[(next() % pool.len() as u64) as usize];
                let g = match next() % if coherent_only { 2 } else { 4 } {
                    0 => bdd.and(a, b),
                    1 => bdd.or(a, b),
                    2 => bdd.xor(a, b),
                    _ => bdd.not(a),
                };
                pool.push(g);
            }
            let f = *pool.last().unwrap();
            let eval = |st: u32, bdd: &Bdd| -> bool {
                let mut g = f;
                while !Bdd::is_terminal(g) {
                    g = if st >> bdd.var(g) & 1 == 1 { bdd.high(g) } else { bdd.low(g) };
                }
                g == ONE
            };
            // product = (pos mask, neg mask), disjoint
            let implies = |pos: u32, neg: u32, bdd: &Bdd| -> bool {
                (0..1u32 << nv).filter(|st| st & pos == pos && st & neg == 0)
                    .all(|st| eval(st, bdd))
            };
            let mut want = Vec::new();
            for code in 0..3u32.pow(nv) {
                let (mut pos, mut neg, mut c) = (0u32, 0u32, code);
                for v in 0..nv {
                    match c % 3 { 1 => pos |= 1 << v, 2 => neg |= 1 << v, _ => {} }
                    c /= 3;
                }
                if !implies(pos, neg, &bdd) {
                    continue;
                }
                let prime = (0..nv).all(|v| {
                    let b = 1 << v;
                    (pos & b == 0 || !implies(pos & !b, neg, &bdd))
                        && (neg & b == 0 || !implies(pos, neg & !b, &bdd))
                });
                if prime {
                    let bits = |m: u32| (0..nv).filter(|v| m >> v & 1 == 1).collect::<Vec<_>>();
                    want.push((bits(pos), bits(neg)));
                }
            }
            want.sort();
            let got = pi_list(&mut bdd, f);
            assert_eq!(got, want, "case {case}");
            // the truncated construction gives exactly the order <= k primes
            for k in 0..4usize {
                let mut z = Zbdd::new();
                let s = bdd.prime_implicants_upto(f, &mut z, Some(k));
                let mut gk: Vec<(Vec<u32>, Vec<u32>)> = z.enumerate(s, None, None)
                    .into_iter().map(|p| (p.pos, p.neg)).collect();
                gk.sort();
                let wk: Vec<(Vec<u32>, Vec<u32>)> = want.iter()
                    .filter(|p| p.0.len() + p.1.len() <= k).cloned().collect();
                assert_eq!(gk, wk, "case {case}, order <= {k}");
            }
            if coherent_only {
                let ms = bdd.minsol(f);
                let mut mcs: Vec<(Vec<u32>, Vec<u32>)> = bdd.enumerate_paths(ms, None)
                    .into_iter().map(|c| (c, vec![])).collect();
                mcs.sort();
                assert_eq!(got, mcs, "coherent case {case}: primes = minimal cut sets");
            }
        }
    }

    /// D-14 regression: restrict on a maximally shared DAG (a 64-variable
    /// XOR chain: 2 nodes per level, 2^64 paths) must be linear in the BDD
    /// size. Unmemoized, this test would never finish.
    #[test]
    fn restrict_is_linear_on_shared_dags() {
        let mut bdd = Bdd::new();
        let mut f = ZERO;
        for v in (0..64).rev() {
            let x = bdd.variable(v);
            f = bdd.xor(x, f);
        }
        let t = std::time::Instant::now();
        let r = bdd.restrict(f, 63, true);
        let p = vec![0.3; 64];
        // XOR chain with x63 = 1: parity of the rest, negated.
        let rest = bdd.restrict(f, 63, false);
        assert_eq!(r, bdd.not(rest));
        assert!((bdd.probability(r, &p) + bdd.probability(rest, &p) - 1.0).abs() < 1e-15);
        assert!(t.elapsed().as_secs_f64() < 5.0, "restrict took {:?}", t.elapsed());
    }

    /// Birnbaum from plan cofactors (the engine's path) agrees with the
    /// restricted-BDD reference on random BDDs, and is exactly 0 for
    /// variables outside the support.
    #[test]
    fn birnbaum_from_cofactors_matches_restrict() {
        let mut state = 0x243F_6A88_85A3_08D3u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _case in 0..200 {
            let mut bdd = Bdd::new();
            let nv = 2 + (next() % 9) as u32;
            let mut pool: Vec<u32> = (0..nv).map(|v| bdd.variable(v)).collect();
            for _ in 0..(3 + next() % 12) {
                let a = pool[(next() % pool.len() as u64) as usize];
                let b = pool[(next() % pool.len() as u64) as usize];
                let g = match next() % 4 {
                    0 => bdd.and(a, b),
                    1 => bdd.or(a, b),
                    2 => bdd.xor(a, b),
                    _ => bdd.not(a),
                };
                pool.push(g);
            }
            let f = *pool.last().unwrap();
            let p: Vec<f64> = (0..nv).map(|_| (next() % 1_000_000) as f64 / 1_000_001.0).collect();
            let plan = bdd.prob_plan(f);
            let support = plan.support();
            let mut buf = Vec::new();
            for v in 0..nv {
                let b_plan = if support.contains(&v) {
                    plan.eval_cofactor(&p, v, true, &mut buf)
                        - plan.eval_cofactor(&p, v, false, &mut buf)
                } else {
                    0.0
                };
                let b_ref = bdd.birnbaum(f, v, &p);
                assert!((b_plan - b_ref).abs() <= 1e-15 + 1e-13 * b_ref.abs(),
                        "var {v}: plan {b_plan} restrict {b_ref}");
                if !support.contains(&v) {
                    assert_eq!(b_ref, 0.0);
                }
            }
        }
    }

    #[test]
    fn hash_consing_shares_structure() {
        let mut bdd = Bdd::new();
        let a = bdd.variable(0);
        let b = bdd.variable(1);
        let f1 = bdd.and(a, b);
        let f2 = bdd.and(a, b);
        assert_eq!(f1, f2); // identical index = perfect sharing
    }
}
