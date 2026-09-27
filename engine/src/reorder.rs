//! Dynamic variable reordering by sifting (Rudell 1993).
//!
//! The main BDD manager (`bdd.rs`) orders variables by index and relies on
//! it everywhere (apply, cofactor plans, path enumeration, the ZBDD literal
//! encoding). Rather than make every algorithm level-aware, reordering
//! happens in this separate manager: the live functions are copied in,
//! sifted, and rebuilt into a fresh main arena in which each variable's
//! new index is its new level (`Bdd::reorder`). The caller renumbers its
//! variable map with the returned permutation.
//!
//! Here a node is (var, lo, hi) with a reference count (parents plus
//! external holds); each variable has its own unique table, and levels
//! are a permutation of variables. `swap(i)` exchanges the variables at
//! levels i and i + 1 in place: a node f = (x, f0, f1) with a child
//! labelled y becomes (y, (x, f00, f10), (x, f01, f11)) — the same node,
//! so every reference to f still denotes the same function — and nodes
//! whose count drops to zero are freed at once, so the live count is
//! exact after every swap. Sifting moves each variable, largest level
//! first, to every position within a growth bound and keeps the smallest.
//!
//! Every decision depends only on the graph's structure (level sizes, the
//! live count), never on hash iteration order, and `export` numbers nodes
//! by a structural traversal: results are reproducible bit for bit.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

/// Multiplicative hasher for the unique tables' (u32, u32) keys: much
/// cheaper than SipHash, and deterministic (hash values never influence
/// results here anyway: see the module note).
#[derive(Default)]
pub struct PairHasher(u64);

impl Hasher for PairHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0.rotate_left(5) ^ b as u64).wrapping_mul(0x51_7cc1_b727_220a_95);
        }
    }
    fn write_u32(&mut self, x: u32) {
        self.0 = (self.0.rotate_left(32) ^ x as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
}

type Table = HashMap<(u32, u32), u32, BuildHasherDefault<PairHasher>>;

const TERMINAL: u32 = u32::MAX;
const FREED: u32 = u32::MAX - 1;

/// Limits of one sifting pass (deterministic: counts, never time).
#[derive(Clone, Copy, Debug)]
pub struct SiftLimits {
    /// Stop moving a variable in one direction once the live count
    /// exceeds this factor times the smallest count seen while moving it.
    pub max_growth: f64,
    /// Sift at most this many variables (the largest levels first).
    pub max_vars: usize,
    /// Stop the pass after this many swaps in total.
    pub max_swaps: usize,
}

impl Default for SiftLimits {
    fn default() -> Self {
        // CUDD's defaults for sifting.
        SiftLimits { max_growth: 1.2, max_vars: 1000, max_swaps: 2_000_000 }
    }
}

pub struct Sifter {
    var: Vec<u32>,
    lo: Vec<u32>,
    hi: Vec<u32>,
    refs: Vec<u32>,
    free: Vec<u32>,
    unique: Vec<Table>,
    /// Bit matrix: variables v and w interact when some root's support
    /// holds both (`set_interaction`). Swapping two variables that do not
    /// interact changes no node — no node of one can have the other as a
    /// child — so it is only a relabelling of levels. Empty: all interact.
    interact: Vec<Vec<u64>>,
    level_of: Vec<u32>,
    var_at: Vec<u32>,
    live: usize,
    /// Swaps performed (all passes).
    pub swaps: usize,
    /// Reused work stack of `deref`.
    scratch: Vec<u32>,
}

impl Sifter {
    /// An empty manager over `nvars` variables in the identity order.
    pub fn new(nvars: usize) -> Self {
        Sifter {
            var: vec![TERMINAL, TERMINAL],
            lo: vec![0, 1],
            hi: vec![0, 1],
            refs: vec![0, 0],
            free: Vec::new(),
            unique: (0..nvars).map(|_| Table::default()).collect(),
            interact: Vec::new(),
            level_of: (0..nvars as u32).collect(),
            var_at: (0..nvars as u32).collect(),
            live: 0,
            swaps: 0,
            scratch: Vec::new(),
        }
    }

    pub fn nvars(&self) -> usize {
        self.var_at.len()
    }

    /// Internal (non-terminal) live nodes.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn live(&self) -> usize {
        self.live
    }

    pub fn level_of(&self, v: u32) -> u32 {
        self.level_of[v as usize]
    }

    #[cfg_attr(not(test), allow(dead_code))]
    pub fn var_at(&self, level: u32) -> u32 {
        self.var_at[level as usize]
    }

    fn level(&self, f: u32) -> u32 {
        let v = self.var[f as usize];
        if v == TERMINAL { u32::MAX } else { self.level_of[v as usize] }
    }

    pub fn parts(&self, f: u32) -> (u32, u32, u32) {
        (self.var[f as usize], self.lo[f as usize], self.hi[f as usize])
    }

    /// Add one external reference (a root held by the caller).
    pub fn mk_hold(&mut self, f: u32) {
        self.inc(f);
    }

    fn inc(&mut self, f: u32) {
        if f > 1 {
            self.refs[f as usize] += 1;
        }
    }

    /// Drop one reference; free the node (and, recursively, children that
    /// lose their last reference) when none is left.
    pub fn deref(&mut self, f: u32) {
        if f <= 1 {
            return;
        }
        debug_assert!(self.refs[f as usize] > 0, "reference count underflow");
        if self.refs[f as usize] > 1 {
            self.refs[f as usize] -= 1; // common case: no allocation
            return;
        }
        let mut stack = std::mem::take(&mut self.scratch);
        stack.push(f);
        while let Some(g) = stack.pop() {
            if g <= 1 {
                continue;
            }
            let i = g as usize;
            debug_assert!(self.refs[i] > 0, "reference count underflow");
            self.refs[i] -= 1;
            if self.refs[i] == 0 {
                let (v, l, h) = (self.var[i], self.lo[i], self.hi[i]);
                self.unique[v as usize].remove(&(l, h));
                self.var[i] = FREED;
                self.free.push(g);
                self.live -= 1;
                stack.push(l);
                stack.push(h);
            }
        }
        self.scratch = stack;
    }

    /// The node (v, l, h), reduced and hash-consed, with one reference
    /// added for the caller. `v` must precede both children in the order.
    pub fn mk(&mut self, v: u32, l: u32, h: u32) -> u32 {
        if l == h {
            self.inc(l);
            return l;
        }
        debug_assert!(self.level_of[v as usize] < self.level(l)
                      && self.level_of[v as usize] < self.level(h),
                      "sifter variable order violated");
        if let Some(&n) = self.unique[v as usize].get(&(l, h)) {
            self.refs[n as usize] += 1;
            return n;
        }
        self.inc(l);
        self.inc(h);
        let n = match self.free.pop() {
            Some(n) => {
                let i = n as usize;
                self.var[i] = v;
                self.lo[i] = l;
                self.hi[i] = h;
                self.refs[i] = 1;
                n
            }
            None => {
                self.var.push(v);
                self.lo.push(l);
                self.hi.push(h);
                self.refs.push(1);
                (self.var.len() - 1) as u32
            }
        };
        self.unique[v as usize].insert((l, h), n);
        self.live += 1;
        n
    }

    /// Record which variables interact, from the supports of `roots` (the
    /// functions every live node belongs to). Call after import.
    pub fn set_interaction(&mut self, roots: &[u32]) {
        let n = self.nvars();
        let words = (n + 63) / 64;
        let mut m = vec![vec![0u64; words]; n];
        let mut seen = vec![false; self.var.len()];
        for &r in roots {
            // support of r as a bit set
            let mut sup = vec![0u64; words];
            let mut stack = vec![r];
            let mut mark: Vec<u32> = Vec::new();
            while let Some(f) = stack.pop() {
                if f <= 1 || seen[f as usize] {
                    continue;
                }
                seen[f as usize] = true;
                mark.push(f);
                let v = self.var[f as usize] as usize;
                sup[v / 64] |= 1 << (v % 64);
                stack.push(self.lo[f as usize]);
                stack.push(self.hi[f as usize]);
            }
            for f in mark {
                seen[f as usize] = false;
            }
            for v in 0..n {
                if sup[v / 64] >> (v % 64) & 1 == 1 {
                    for (w, word) in m[v].iter_mut().enumerate() {
                        *word |= sup[w];
                    }
                }
            }
        }
        self.interact = m;
    }

    fn interacts(&self, v: u32, w: u32) -> bool {
        self.interact.is_empty()
            || self.interact[v as usize][w as usize / 64] >> (w % 64) & 1 == 1
    }

    /// Exchange the variables at levels `i` and `i + 1`, in place.
    pub fn swap(&mut self, i: u32) {
        let x = self.var_at[i as usize];
        let y = self.var_at[i as usize + 1];
        self.swaps += 1;
        if !self.interacts(x, y) || self.unique[x as usize].is_empty()
            || self.unique[y as usize].is_empty()
        {
            // no x-node has a y-child: only the levels change
            self.var_at[i as usize] = y;
            self.var_at[i as usize + 1] = x;
            self.level_of[y as usize] = i;
            self.level_of[x as usize] = i + 1;
            return;
        }
        // Processing order only affects which free slots new nodes take,
        // never the graph (export numbers nodes structurally).
        let xs: Vec<u32> = self.unique[x as usize].values().copied().collect();
        self.unique[y as usize].reserve(xs.len());
        // The new x-nodes are created at level i + 1, below y at level i:
        // switch the levels first so `mk`'s order check sees the new order.
        self.var_at[i as usize] = y;
        self.var_at[i as usize + 1] = x;
        self.level_of[y as usize] = i;
        self.level_of[x as usize] = i + 1;
        for f in xs {
            let fi = f as usize;
            let (f0, f1) = (self.lo[fi], self.hi[fi]);
            let f0y = self.var[f0 as usize] == y;
            let f1y = self.var[f1 as usize] == y;
            if !f0y && !f1y {
                continue; // f does not depend on y: it simply moves down
            }
            let (f00, f01) = if f0y { (self.lo[f0 as usize], self.hi[f0 as usize]) } else { (f0, f0) };
            let (f10, f11) = if f1y { (self.lo[f1 as usize], self.hi[f1 as usize]) } else { (f1, f1) };
            let a = self.mk(x, f00, f10);
            let b = self.mk(x, f01, f11);
            self.unique[x as usize].remove(&(f0, f1));
            self.var[fi] = y;
            self.lo[fi] = a;
            self.hi[fi] = b;
            let clash = self.unique[y as usize].insert((a, b), f);
            debug_assert!(clash.is_none(), "swap produced a duplicate node");
            self.deref(f0);
            self.deref(f1);
        }
    }

    /// Move the variable at level `from` towards level `to` by adjacent
    /// swaps, recording the best (live count, level) seen; returns where it
    /// stopped. It stops early once the live count exceeds `growth` times
    /// the best count so far (the bound tightens as better positions
    /// appear, as in CUDD), or when the swap budget runs out.
    fn walk(&mut self, from: u32, to: u32, growth: f64, best: &mut (usize, u32),
            budget: &mut usize) -> u32 {
        let mut at = from;
        while at != to {
            if *budget == 0 {
                break;
            }
            *budget -= 1;
            if to > at {
                self.swap(at);
                at += 1;
            } else {
                self.swap(at - 1);
                at -= 1;
            }
            if self.live < best.0 || (self.live == best.0 && at < best.1) {
                *best = (self.live, at);
            }
            if self.live as f64 > growth * best.0 as f64 {
                break;
            }
        }
        at
    }

    /// One sifting pass: each variable (largest level first, ties by
    /// variable index), up to `limits.max_vars` of them, is moved towards
    /// the nearer end of the order and then to the other end, as far as
    /// the growth bound allows, and left at the position that gave the
    /// fewest live nodes (the highest such level on ties).
    pub fn sift(&mut self, limits: SiftLimits) {
        let n = self.nvars() as u32;
        if n < 2 {
            return;
        }
        let mut order: Vec<(usize, u32)> = (0..n)
            .map(|v| (self.unique[v as usize].len(), v))
            .filter(|&(size, _)| size > 0)
            .collect();
        order.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
        let mut budget = limits.max_swaps;
        for &(_, v) in order.iter().take(limits.max_vars) {
            if budget == 0 {
                break;
            }
            let start = self.level_of[v as usize];
            let mut best = (self.live, start);
            let (first, second) = if start >= n / 2 { (n - 1, 0) } else { (0, n - 1) };
            let at = self.walk(start, first, limits.max_growth, &mut best, &mut budget);
            let at = self.walk(at, second, limits.max_growth, &mut best, &mut budget);
            // back to the best position, unconditionally (outside the
            // budget: returning is what makes the pass monotone)
            let mut back_budget = usize::MAX;
            let mut ignore = best;
            self.walk(at, best.1, f64::INFINITY, &mut ignore, &mut back_budget);
            debug_assert_eq!(self.live, best.0, "sifting failed to return to its best size");
        }
    }

    /// Check every structural invariant (tests): reference counts equal
    /// parent references plus `holds`, unique tables hold exactly the live
    /// nodes, nodes are reduced and ordered, the live count is right.
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn check(&self, holds: &HashMap<u32, u32>) -> Result<(), String> {
        let mut count = vec![0u32; self.var.len()];
        let mut live = 0;
        for i in 2..self.var.len() {
            let v = self.var[i];
            if v == FREED {
                continue;
            }
            live += 1;
            let (l, h) = (self.lo[i], self.hi[i]);
            if l == h {
                return Err(format!("node {i} is redundant"));
            }
            let lv = self.level_of[v as usize];
            if !(lv < self.level(l) && lv < self.level(h)) {
                return Err(format!("node {i} violates the order"));
            }
            if self.unique[v as usize].get(&(l, h)) != Some(&(i as u32)) {
                return Err(format!("node {i} missing from its unique table"));
            }
            count[l as usize] += 1;
            count[h as usize] += 1;
        }
        for (&f, &k) in holds {
            count[f as usize] += k;
        }
        for i in 2..self.var.len() {
            if self.var[i] != FREED && count[i] != self.refs[i] {
                return Err(format!("node {i}: {} references counted, {} stored",
                                   count[i], self.refs[i]));
            }
            if self.var[i] != FREED && self.refs[i] == 0 {
                return Err(format!("node {i} is live with no reference"));
            }
        }
        let in_tables: usize = self.unique.iter().map(|t| t.len()).sum();
        if live != self.live || in_tables != live {
            return Err(format!("live count {} / tables {in_tables} / actual {live}", self.live));
        }
        for (lvl, &v) in self.var_at.iter().enumerate() {
            if self.level_of[v as usize] != lvl as u32 {
                return Err(format!("level maps disagree at level {lvl}"));
            }
        }
        Ok(())
    }

    /// Truth value of f under an assignment indexed by variable (tests).
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn eval(&self, mut f: u32, x: &[bool]) -> bool {
        while f > 1 {
            let i = f as usize;
            f = if x[self.var[i] as usize] { self.hi[i] } else { self.lo[i] };
        }
        f == 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::bdd::{Bdd, ONE, ZERO};

    fn rng(seed: u64) -> impl FnMut() -> u64 {
        let mut s = seed;
        move || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            s
        }
    }

    /// Random functions over n variables built in the main manager.
    fn random_roots(bdd: &mut Bdd, n: u32, k: usize, next: &mut impl FnMut() -> u64) -> Vec<u32> {
        let mut pool: Vec<u32> = (0..n).map(|v| bdd.variable(v)).collect();
        for _ in 0..(3 * n) {
            let a = pool[(next() % pool.len() as u64) as usize];
            let b = pool[(next() % pool.len() as u64) as usize];
            let r = match next() % 4 {
                0 => bdd.and(a, b),
                1 => bdd.or(a, b),
                2 => bdd.xor(a, b),
                _ => {
                    let na = bdd.not(a);
                    bdd.and(na, b)
                }
            };
            pool.push(r);
        }
        (0..k).map(|i| pool[pool.len() - 1 - i]).collect()
    }

    fn truth(bdd: &Bdd, f: u32, n: u32) -> Vec<bool> {
        (0..1u32 << n).map(|m| {
            let x: Vec<bool> = (0..n).map(|v| m >> v & 1 == 1).collect();
            bdd.eval(f, &x)
        }).collect()
    }

    #[test]
    fn swaps_preserve_functions_and_invariants() {
        let mut next = rng(0x5EED_0F_51F7);
        for case in 0..200 {
            let n = 2 + (next() % 7) as u32;
            let mut bdd = Bdd::new();
            let roots = random_roots(&mut bdd, n, 3, &mut next);
            let want: Vec<Vec<bool>> = roots.iter().map(|&r| truth(&bdd, r, n)).collect();
            let (mut s, handles) = bdd.to_sifter(&roots, n as usize);
            let mut holds = HashMap::new();
            for &h in &handles {
                *holds.entry(h).or_insert(0) += 1;
            }
            holds.retain(|&h, _| h > 1);
            s.check(&holds).unwrap_or_else(|e| panic!("case {case} import: {e}"));
            for step in 0..40 {
                let i = (next() % (n as u64 - 1)) as u32;
                s.swap(i);
                s.check(&holds).unwrap_or_else(|e| panic!("case {case} step {step}: {e}"));
                for (j, &h) in handles.iter().enumerate() {
                    for m in 0..1u32 << n {
                        let x: Vec<bool> = (0..n).map(|v| m >> v & 1 == 1).collect();
                        assert_eq!(s.eval(h, &x), want[j][m as usize],
                                   "case {case} step {step} root {j}");
                    }
                }
            }
        }
    }

    #[test]
    fn swap_twice_is_identity_in_size() {
        let mut next = rng(0xABCD_1234);
        for _ in 0..100 {
            let n = 3 + (next() % 6) as u32;
            let mut bdd = Bdd::new();
            let roots = random_roots(&mut bdd, n, 2, &mut next);
            let (mut s, _) = bdd.to_sifter(&roots, n as usize);
            let before = s.live();
            let i = (next() % (n as u64 - 1)) as u32;
            s.swap(i);
            s.swap(i);
            assert_eq!(s.live(), before);
            assert_eq!((s.var_at(i), s.var_at(i + 1)), (i, i + 1));
        }
    }

    /// The sifted, exported BDD is canonical for its order: rebuilding the
    /// functions from scratch in the new order gives exactly as many nodes,
    /// and the same probabilities; sifting never grows the BDD.
    #[test]
    fn sifting_is_canonical_and_never_grows() {
        let mut next = rng(0x0DD5_1F7E);
        for case in 0..150 {
            let n = 2 + (next() % 8) as u32;
            let mut bdd = Bdd::new();
            let roots = random_roots(&mut bdd, n, 3, &mut next);
            let p: Vec<f64> = (0..n).map(|_| (1 + next() % 999) as f64 / 1000.0).collect();
            let want: Vec<Vec<bool>> = roots.iter().map(|&r| truth(&bdd, r, n)).collect();
            let probs: Vec<f64> = roots.iter().map(|&r| bdd.probability(r, &p)).collect();
            let before = bdd.reachable_count(&roots);
            let (map, perm) = bdd.reorder(&roots, n as usize, SiftLimits::default());
            let new_roots: Vec<u32> = roots.iter().map(|&r| Bdd::remap(&map, r)).collect();
            let after = bdd.reachable_count(&new_roots);
            assert!(after <= before, "case {case}: sifting grew {before} -> {after}");
            // new variable perm[v] stands for old variable v
            let mut pn = vec![0.0; n as usize];
            for v in 0..n as usize {
                pn[perm[v] as usize] = p[v];
            }
            for (j, &r) in new_roots.iter().enumerate() {
                for m in 0..1u32 << n {
                    let mut x = vec![false; n as usize];
                    for v in 0..n as usize {
                        x[perm[v] as usize] = m >> v & 1 == 1;
                    }
                    assert_eq!(bdd.eval(r, &x), want[j][m as usize], "case {case} root {j}");
                }
                let pr = bdd.probability(r, &pn);
                assert!((pr - probs[j]).abs() <= 1e-12 * probs[j].max(1e-300),
                        "case {case}: probability {pr} vs {}", probs[j]);
            }
            // canonical: rebuild each function from its truth table in a
            // fresh manager with the new order (index = level)
            let mut fresh = Bdd::new();
            let rebuilt: Vec<u32> = (0..roots.len()).map(|j| {
                let mut f = ZERO;
                for m in 0..1u32 << n {
                    if want[j][m as usize] {
                        let mut term = ONE;
                        for v in 0..n {
                            let lit = fresh.variable(perm[v as usize]);
                            let lit = if m >> v & 1 == 1 { lit } else { fresh.not(lit) };
                            term = fresh.and(term, lit);
                        }
                        f = fresh.or(f, term);
                    }
                }
                f
            }).collect();
            assert_eq!(fresh.reachable_count(&rebuilt), after, "case {case}: not canonical");
        }
    }

    /// (x1 ∧ x2) ∨ (x3 ∧ x4) ∨ … under the order x1, x3, x5, …, x2, x4, …
    /// needs 2^(m+1) − 2 nodes; paired, 2m. Sifting reaches the paired size.
    #[test]
    fn sifting_repairs_the_classic_bad_order() {
        for m in 2..=6u32 {
            let mut bdd = Bdd::new();
            // variable index = level: pair i is (i, m + i)
            let mut f = ZERO;
            for i in 0..m {
                let a = bdd.variable(i);
                let b = bdd.variable(m + i);
                let t = bdd.and(a, b);
                f = bdd.or(f, t);
            }
            let before = bdd.reachable_count(&[f]);
            assert_eq!(before, (1usize << (m + 1)) - 2);
            let (map, _) = bdd.reorder(&[f], 2 * m as usize, SiftLimits::default());
            let g = Bdd::remap(&map, f);
            assert_eq!(bdd.reachable_count(&[g]), 2 * m as usize, "m = {m}");
        }
    }

    /// Hash tables are seeded per table and per process; sifting and
    /// export must not depend on it: the same input sifted in two separate
    /// managers gives the same permutation and the same arena, node for
    /// node.
    #[test]
    fn reordering_is_deterministic() {
        let mut next = rng(0xD37E_2A11);
        for case in 0..60 {
            let n = 3 + (next() % 8) as u32;
            let seed = next();
            let build = |seed: u64| {
                let mut bdd = Bdd::new();
                let mut r = rng(seed);
                let roots = random_roots(&mut bdd, n, 3, &mut r);
                let (map, perm) = bdd.reorder(&roots, n as usize, SiftLimits::default());
                let nr: Vec<u32> = roots.iter().map(|&x| Bdd::remap(&map, x)).collect();
                (perm, nr, bdd.debug_nodes())
            };
            assert_eq!(build(seed), build(seed), "case {case}");
        }
    }

    #[test]
    fn export_without_sifting_is_the_same_graph() {
        let mut next = rng(0xFEED_BEEF);
        for _ in 0..50 {
            let n = 2 + (next() % 7) as u32;
            let mut bdd = Bdd::new();
            let roots = random_roots(&mut bdd, n, 3, &mut next);
            let p: Vec<f64> = (0..n).map(|_| (1 + next() % 999) as f64 / 1000.0).collect();
            let probs: Vec<u64> = roots.iter().map(|&r| bdd.probability(r, &p).to_bits()).collect();
            let size = bdd.reachable_count(&roots);
            let limits = SiftLimits { max_vars: 0, ..SiftLimits::default() };
            let (map, perm) = bdd.reorder(&roots, n as usize, limits);
            assert!(perm.iter().enumerate().all(|(v, &w)| v as u32 == w));
            let nr: Vec<u32> = roots.iter().map(|&r| Bdd::remap(&map, r)).collect();
            assert_eq!(bdd.reachable_count(&nr), size);
            let got: Vec<u64> = nr.iter().map(|&r| bdd.probability(r, &p).to_bits()).collect();
            assert_eq!(got, probs, "identity reorder must be bit-identical");
        }
    }
}
