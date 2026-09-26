//! Zero-suppressed BDDs (Minato 1993) for sets of products of literals.
//!
//! Used to represent prime-implicant sets of non-coherent functions. A
//! ZBDD node (var, lo, hi) stands for {products without `var` from lo} ∪
//! {`var` · p : p ∈ hi}; the zero-suppression rule removes nodes whose hi
//! child is the empty set, so an absent variable simply means "literal not
//! in the product" — the natural reading for sparse product sets, unlike
//! an ordinary BDD where an absent variable means "either value".
//!
//! Literals of basic-event variable v are encoded as ZBDD variables
//! 2v (the event, positive literal) and 2v + 1 (its negation), so products
//! built top-down along the function BDD's variable order are always in
//! ascending ZBDD-variable order.

use std::collections::HashMap;

/// The empty set of products.
pub const EMPTY: u32 = 0;
/// The set holding only the empty product (the constant true).
pub const BASE: u32 = 1;
const TERMINAL_VAR: u32 = u32::MAX;

#[derive(Clone, Copy, PartialEq, Eq, Hash, Debug)]
struct Node {
    var: u32,
    lo: u32,
    hi: u32,
}

#[derive(Clone, Copy, PartialEq, Eq, Hash)]
enum Op {
    Union,
    Diff,
    Product,
    NonSup,
}

/// Per-ZBDD-variable weights (probabilities) with memoized per-node
/// statistics for `truncate`. A variable's weight is set once, when it is
/// pushed, and never changes, so the memo stays valid while the vector
/// grows with newly discovered variables (ZBDD nodes are never renumbered).
pub struct Weights {
    p: Vec<f64>,
    stats: HashMap<u32, Stats>,
}

#[derive(Clone, Copy)]
struct Stats {
    sum: f64,
    min_p: f64,
    max_p: f64,
    min_len: usize,
    max_len: usize,
}

impl Weights {
    pub fn new(p: Vec<f64>) -> Self {
        Weights { p, stats: HashMap::new() }
    }
    /// Append the weight of the next variable.
    pub fn push(&mut self, w: f64) {
        self.p.push(w);
    }
    pub fn p(&self) -> &[f64] {
        &self.p
    }
}

pub struct Zbdd {
    nodes: Vec<Node>,
    unique: HashMap<Node, u32>,
    cache: HashMap<(Op, u32, u32), u32>,
}

/// One product: positive literals (basic-event variables that fail) and
/// negated literals (variables that must not fail), both ascending.
#[derive(Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct Product {
    pub pos: Vec<u32>,
    pub neg: Vec<u32>,
}

impl Product {
    #[cfg_attr(not(test), allow(dead_code))]
    pub fn order(&self) -> usize {
        self.pos.len() + self.neg.len()
    }
}

impl Zbdd {
    pub fn new() -> Self {
        Zbdd {
            nodes: vec![
                Node { var: TERMINAL_VAR, lo: EMPTY, hi: EMPTY },
                Node { var: TERMINAL_VAR, lo: BASE, hi: BASE },
            ],
            unique: HashMap::new(),
            cache: HashMap::new(),
        }
    }

    #[cfg_attr(not(test), allow(dead_code))]
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    /// Forget the operation cache when it holds more than `limit` entries
    /// (results only; nodes are untouched, so this changes speed, never a
    /// result).
    pub fn trim_cache(&mut self, limit: usize) {
        if self.cache.len() > limit {
            self.cache = HashMap::new();
        }
    }

    fn var(&self, s: u32) -> u32 {
        self.nodes[s as usize].var
    }
    fn lo(&self, s: u32) -> u32 {
        self.nodes[s as usize].lo
    }
    fn hi(&self, s: u32) -> u32 {
        self.nodes[s as usize].hi
    }

    /// Canonical node: zero-suppression (hi = ∅ → lo) and hash consing.
    pub fn node(&mut self, var: u32, lo: u32, hi: u32) -> u32 {
        if hi == EMPTY {
            return lo;
        }
        debug_assert!(var < self.var(lo) && var < self.var(hi),
                      "ZBDD variable order violated");
        let key = Node { var, lo, hi };
        if let Some(&i) = self.unique.get(&key) {
            return i;
        }
        let i = self.nodes.len() as u32;
        self.nodes.push(key);
        self.unique.insert(key, i);
        i
    }

    /// {literal · p : p ∈ s}; `literal` must precede every variable of s.
    pub fn attach(&mut self, literal: u32, s: u32) -> u32 {
        self.node(literal, EMPTY, s)
    }

    pub fn union(&mut self, p: u32, q: u32) -> u32 {
        if p == EMPTY {
            return q;
        }
        if q == EMPTY || p == q {
            return p;
        }
        let (p, q) = if p <= q { (p, q) } else { (q, p) };
        if let Some(&r) = self.cache.get(&(Op::Union, p, q)) {
            return r;
        }
        let (vp, vq) = (self.var(p), self.var(q));
        let r = if vp < vq {
            let (lo, hi) = (self.lo(p), self.hi(p));
            let l = self.union(lo, q);
            self.node(vp, l, hi)
        } else if vp > vq {
            let (lo, hi) = (self.lo(q), self.hi(q));
            let l = self.union(p, lo);
            self.node(vq, l, hi)
        } else {
            let (pl, ph, ql, qh) = (self.lo(p), self.hi(p), self.lo(q), self.hi(q));
            let l = self.union(pl, ql);
            let h = self.union(ph, qh);
            self.node(vp, l, h)
        };
        self.cache.insert((Op::Union, p, q), r);
        r
    }

    /// p ∖ q (exact set difference, not subsumption).
    pub fn diff(&mut self, p: u32, q: u32) -> u32 {
        if p == EMPTY || p == q {
            return EMPTY;
        }
        if q == EMPTY {
            return p;
        }
        if let Some(&r) = self.cache.get(&(Op::Diff, p, q)) {
            return r;
        }
        let (vp, vq) = (self.var(p), self.var(q));
        let r = if vp < vq {
            let (lo, hi) = (self.lo(p), self.hi(p));
            let l = self.diff(lo, q);
            self.node(vp, l, hi)
        } else if vp > vq {
            let lo = self.lo(q);
            self.diff(p, lo)
        } else {
            let (pl, ph, ql, qh) = (self.lo(p), self.hi(p), self.lo(q), self.hi(q));
            let l = self.diff(pl, ql);
            let h = self.diff(ph, qh);
            self.node(vp, l, h)
        };
        self.cache.insert((Op::Diff, p, q), r);
        r
    }

    /// {p ∪ q : p ∈ a, q ∈ b} (Minato's product / join), not minimized.
    pub fn product(&mut self, a: u32, b: u32) -> u32 {
        if a == EMPTY || b == EMPTY {
            return EMPTY;
        }
        if a == BASE {
            return b;
        }
        if b == BASE {
            return a;
        }
        let (a, b) = if a <= b { (a, b) } else { (b, a) };
        if let Some(&r) = self.cache.get(&(Op::Product, a, b)) {
            return r;
        }
        let (va, vb) = (self.var(a), self.var(b));
        let r = if va < vb {
            let (lo, hi) = (self.lo(a), self.hi(a));
            let l = self.product(lo, b);
            let h = self.product(hi, b);
            self.node(va, l, h)
        } else if va > vb {
            let (lo, hi) = (self.lo(b), self.hi(b));
            let l = self.product(a, lo);
            let h = self.product(a, hi);
            self.node(vb, l, h)
        } else {
            let (a0, a1, b0, b1) = (self.lo(a), self.hi(a), self.lo(b), self.hi(b));
            let l = self.product(a0, b0);
            let h11 = self.product(a1, b1);
            let h10 = self.product(a1, b0);
            let h01 = self.product(a0, b1);
            let h = self.union(h11, h10);
            let h = self.union(h, h01);
            self.node(va, l, h)
        };
        self.cache.insert((Op::Product, a, b), r);
        r
    }

    /// Products of p that are not supersets of any product of q.
    pub fn nonsupersets(&mut self, p: u32, q: u32) -> u32 {
        if p == EMPTY || q == BASE || p == q {
            return EMPTY;
        }
        if q == EMPTY {
            return p;
        }
        if p == BASE {
            // ∅ is a superset only of ∅ (q ≠ BASE here, but q may contain ∅
            // deeper down its lo chain)
            return if self.contains_empty(q) { EMPTY } else { BASE };
        }
        if let Some(&r) = self.cache.get(&(Op::NonSup, p, q)) {
            return r;
        }
        let (vp, vq) = (self.var(p), self.var(q));
        let r = if vp < vq {
            let (lo, hi) = (self.lo(p), self.hi(p));
            let l = self.nonsupersets(lo, q);
            let h = self.nonsupersets(hi, q);
            self.node(vp, l, h)
        } else if vp > vq {
            // q's products containing vq cannot be subsets of p's products
            let lo = self.lo(q);
            self.nonsupersets(p, lo)
        } else {
            let (p0, p1, q0, q1) = (self.lo(p), self.hi(p), self.lo(q), self.hi(q));
            let l = self.nonsupersets(p0, q0);
            let h = self.nonsupersets(p1, q0);
            let h = self.nonsupersets(h, q1);
            self.node(vp, l, h)
        };
        self.cache.insert((Op::NonSup, p, q), r);
        r
    }

    fn contains_empty(&self, mut s: u32) -> bool {
        while s > BASE {
            s = self.lo(s);
        }
        s == BASE
    }

    /// The minimal products of s (no product a superset of another).
    pub fn minimize(&mut self, s: u32) -> u32 {
        let mut memo = HashMap::new();
        self.min_rec(s, &mut memo)
    }

    fn min_rec(&mut self, s: u32, memo: &mut HashMap<u32, u32>) -> u32 {
        if s <= BASE {
            return s;
        }
        if let Some(&r) = memo.get(&s) {
            return r;
        }
        let (v, lo, hi) = (self.var(s), self.lo(s), self.hi(s));
        let l = self.min_rec(lo, memo);
        let h = self.min_rec(hi, memo);
        let h = self.nonsupersets(h, l);
        let r = self.node(v, l, h);
        memo.insert(s, r);
        r
    }

    /// Keep the products whose probability is ≥ `cutoff` and whose size is
    /// ≤ `order` (None: unlimited); return (kept, dropped), the dropped
    /// products as a set. A product's probability is the left fold
    /// 1.0 · p[v1] · p[v2] · … in ascending ZBDD-variable order (the order
    /// `enumerate` lists it in), so the keep/drop decision is exact at the
    /// cut-off itself.
    ///
    /// Whole subsets are kept or dropped without descending when their
    /// memoized extremes (least and most probable product, shortest and
    /// longest) clear the thresholds by a margin far above floating-point
    /// reassociation error; otherwise the decision is made product by
    /// product along the fold.
    pub fn truncate(&mut self, s: u32, w: &mut Weights, cutoff: f64, order: Option<usize>)
        -> (u32, u32)
    {
        self.trunc_rec(s, w, 1.0, cutoff, order.unwrap_or(usize::MAX))
    }

    fn trunc_rec(&mut self, s: u32, w: &mut Weights, acc: f64, cutoff: f64, left: usize)
        -> (u32, u32)
    {
        const MARGIN: f64 = 1e-9;
        if s == EMPTY {
            return (EMPTY, EMPTY);
        }
        let st = self.stats(s, w);
        if acc * st.max_p < cutoff * (1.0 - MARGIN) || st.min_len > left || acc < cutoff {
            return (EMPTY, s);
        }
        if acc * st.min_p >= cutoff * (1.0 + MARGIN) && st.max_len <= left {
            return (s, EMPTY);
        }
        if s == BASE {
            return (BASE, EMPTY);
        }
        let (v, lo, hi) = (self.var(s), self.lo(s), self.hi(s));
        let (l, dl) = self.trunc_rec(lo, w, acc, cutoff, left);
        let (h, dh) = if left == 0 {
            (EMPTY, hi)
        } else {
            self.trunc_rec(hi, w, acc * w.p[v as usize], cutoff, left - 1)
        };
        let k = self.node(v, l, h);
        (k, self.node(v, dl, dh))
    }

    /// `truncate(product(a, b), …)`'s kept set without forming the full
    /// product: pairs are pruned while they are built, so memory follows
    /// the retained products rather than the untruncated product. Kept
    /// products are decided on the same ascending fold as `truncate`, so
    /// the kept sets are identical.
    ///
    /// The second result is a set of *covering terms*, not the dropped
    /// products themselves: every dropped product contains one of them.
    /// When a whole block of pairs x ∪ y (x ∈ a, y ∈ b) is dropped, the
    /// side with the smaller Σ P stands for it (each x ∪ y contains x and
    /// y). That is all the truncation error bound needs.
    pub fn product_truncated(&mut self, a: u32, b: u32, w: &mut Weights, cutoff: f64,
                             order: Option<usize>) -> (u32, u32) {
        let mut memo = HashMap::new();
        self.ptr(a, b, w, 1.0, cutoff, order.unwrap_or(usize::MAX), &mut memo)
    }

    /// The cheaper of two covering sides.
    fn smaller(&self, a: u32, b: u32, w: &mut Weights) -> u32 {
        if self.stats(a, w).sum <= self.stats(b, w).sum { a } else { b }
    }

    #[allow(clippy::too_many_arguments)]
    fn ptr(&mut self, a: u32, b: u32, w: &mut Weights, acc: f64, cutoff: f64, left: usize,
           memo: &mut HashMap<(u32, u32, u64, usize), (u32, u32)>) -> (u32, u32) {
        const MARGIN: f64 = 1e-9;
        if a == EMPTY || b == EMPTY {
            return (EMPTY, EMPTY);
        }
        if a == BASE {
            return self.trunc_rec(b, w, acc, cutoff, left);
        }
        if b == BASE {
            return self.trunc_rec(a, w, acc, cutoff, left);
        }
        let (a, b) = if a <= b { (a, b) } else { (b, a) };
        let key = (a, b, acc.to_bits(), left);
        if let Some(&r) = memo.get(&key) {
            return r;
        }
        let (sa, sb) = (self.stats(a, w), self.stats(b, w));
        let r = if acc * sa.max_p.min(sb.max_p) < cutoff * (1.0 - MARGIN)
            || sa.min_len.max(sb.min_len) > left
            || acc < cutoff
        {
            // P(x ∪ y) ≤ min(P(x), P(y)); |x ∪ y| ≥ max(|x|, |y|)
            (EMPTY, self.smaller(a, b, w))
        } else if acc * sa.min_p * sb.min_p >= cutoff * (1.0 + MARGIN)
            && sa.max_len.saturating_add(sb.max_len) <= left
        {
            // P(x ∪ y) ≥ P(x) · P(y); |x ∪ y| ≤ |x| + |y|
            (self.product(a, b), EMPTY)
        } else {
            let (va, vb) = (self.var(a), self.var(b));
            let v = va.min(vb);
            let pv = w.p[v as usize];
            let (l, dl, h, dh) = if va != vb {
                let (x, y) = if va < vb { (a, b) } else { (b, a) };
                let (x0, x1) = (self.lo(x), self.hi(x));
                let (l, dl) = self.ptr(x0, y, w, acc, cutoff, left, memo);
                let (h, dh) = if left == 0 {
                    // every product with v is too long
                    (EMPTY, self.smaller(x1, y, w))
                } else {
                    self.ptr(x1, y, w, acc * pv, cutoff, left - 1, memo)
                };
                (l, dl, h, dh)
            } else {
                let (a0, a1, b0, b1) = (self.lo(a), self.hi(a), self.lo(b), self.hi(b));
                let (l, dl) = self.ptr(a0, b0, w, acc, cutoff, left, memo);
                let (h, dh) = if left == 0 {
                    // pairs (a1, ·) contain an x ∈ a1, pairs (a0, b1) a y ∈ b1
                    (EMPTY, self.union(a1, b1))
                } else {
                    let (h11, d11) = self.ptr(a1, b1, w, acc * pv, cutoff, left - 1, memo);
                    let (h10, d10) = self.ptr(a1, b0, w, acc * pv, cutoff, left - 1, memo);
                    let (h01, d01) = self.ptr(a0, b1, w, acc * pv, cutoff, left - 1, memo);
                    let h = self.union(h11, h10);
                    let h = self.union(h, h01);
                    let d = self.union(d11, d10);
                    (h, self.union(d, d01))
                };
                (l, dl, h, dh)
            };
            let k = self.node(v, l, h);
            (k, self.node(v, dl, dh))
        };
        memo.insert(key, r);
        r
    }

    /// Memoized extremes of a product set under the weights.
    fn stats(&self, s: u32, w: &mut Weights) -> Stats {
        if s == EMPTY {
            return Stats { sum: 0.0, min_p: f64::INFINITY, max_p: 0.0,
                           min_len: usize::MAX, max_len: 0 };
        }
        if s == BASE {
            return Stats { sum: 1.0, min_p: 1.0, max_p: 1.0, min_len: 0, max_len: 0 };
        }
        if let Some(&r) = w.stats.get(&s) {
            return r;
        }
        let (v, lo, hi) = (self.var(s), self.lo(s), self.hi(s));
        let l = self.stats(lo, w);
        let h = self.stats(hi, w);
        let pv = w.p[v as usize];
        let r = Stats {
            sum: l.sum + pv * h.sum,
            min_p: l.min_p.min(pv * h.min_p),
            max_p: l.max_p.max(pv * h.max_p),
            min_len: l.min_len.min(h.min_len.saturating_add(1)),
            max_len: l.max_len.max(h.max_len + 1),
        };
        w.stats.insert(s, r);
        r
    }

    /// Σ over the products of s of Π p[v] (not a probability of a union:
    /// the union bound on it).
    pub fn sum_prob(&self, s: u32, p: &[f64]) -> f64 {
        fn rec(z: &Zbdd, s: u32, p: &[f64], memo: &mut HashMap<u32, f64>) -> f64 {
            if s == EMPTY {
                return 0.0;
            }
            if s == BASE {
                return 1.0;
            }
            if let Some(&r) = memo.get(&s) {
                return r;
            }
            let r = rec(z, z.lo(s), p, memo) + p[z.var(s) as usize] * rec(z, z.hi(s), p, memo);
            memo.insert(s, r);
            r
        }
        rec(self, s, p, &mut HashMap::new())
    }

    /// Number of products in s.
    pub fn count(&self, s: u32) -> f64 {
        fn rec(z: &Zbdd, s: u32, memo: &mut HashMap<u32, f64>) -> f64 {
            if s <= BASE {
                return s as f64;
            }
            if let Some(&r) = memo.get(&s) {
                return r;
            }
            let r = rec(z, z.lo(s), memo) + rec(z, z.hi(s), memo);
            memo.insert(s, r);
            r
        }
        rec(self, s, &mut HashMap::new())
    }

    /// (var, lo, hi) of a non-terminal node (for conversions).
    pub fn parts(&self, s: u32) -> (u32, u32, u32) {
        (self.var(s), self.lo(s), self.hi(s))
    }

    /// The products of s with at most `order_limit` literals (all when
    /// None), at most `limit` of them (all when None), in a fixed order
    /// (depth first, literal included before excluded). ZBDD variable 2v is
    /// basic-event variable v, 2v + 1 its negation.
    pub fn enumerate(&self, s: u32, limit: Option<usize>, order_limit: Option<usize>)
        -> Vec<Product>
    {
        let mut out = Vec::new();
        let mut path: Vec<u32> = Vec::new();
        self.enum_rec(s, &mut path, &mut out, limit, order_limit);
        out
    }

    fn enum_rec(&self, s: u32, path: &mut Vec<u32>, out: &mut Vec<Product>,
                limit: Option<usize>, order_limit: Option<usize>) {
        if limit.map_or(false, |l| out.len() >= l) || s == EMPTY {
            return;
        }
        if s == BASE {
            let mut p = Product { pos: Vec::new(), neg: Vec::new() };
            for &z in path.iter() {
                if z % 2 == 0 { p.pos.push(z / 2) } else { p.neg.push(z / 2) }
            }
            out.push(p);
            return;
        }
        if order_limit.map_or(true, |k| path.len() < k) {
            path.push(self.var(s));
            self.enum_rec(self.hi(s), path, out, limit, order_limit);
            path.pop();
        }
        self.enum_rec(self.lo(s), path, out, limit, order_limit);
    }
}

impl Default for Zbdd {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn set(z: &mut Zbdd, products: &[&[u32]]) -> u32 {
        let mut s = EMPTY;
        for p in products {
            let mut q = BASE;
            let mut vs: Vec<u32> = p.to_vec();
            vs.sort_unstable();
            for &v in vs.iter().rev() {
                q = z.attach(v, q);
            }
            s = z.union(s, q);
        }
        s
    }

    fn refs(f: &[Vec<u32>]) -> Vec<&[u32]> {
        f.iter().map(|p| p.as_slice()).collect()
    }

    fn list(z: &Zbdd, s: u32) -> Vec<Vec<u32>> {
        let mut v: Vec<Vec<u32>> = z.enumerate(s, None, None).into_iter()
            .map(|p| {
                let mut lits: Vec<u32> = p.pos.iter().map(|x| 2 * x)
                    .chain(p.neg.iter().map(|x| 2 * x + 1)).collect();
                lits.sort_unstable();
                lits
            })
            .collect();
        v.sort();
        v
    }

    /// Union and difference agree with Rust set operations on random
    /// families of products; canonical form (equal sets, equal indices).
    #[test]
    fn set_algebra_matches_reference() {
        let mut state = 0x1234_5678_9ABC_DEF1u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _ in 0..300 {
            let mut z = Zbdd::new();
            let fam = |n: usize, next: &mut dyn FnMut() -> u64| -> Vec<Vec<u32>> {
                (0..n).map(|_| {
                    let mut p: Vec<u32> = (0..8u32).filter(|_| next() % 3 == 0).collect();
                    p.dedup();
                    p
                }).collect()
            };
            let a = fam((next() % 6) as usize, &mut next);
            let b = fam((next() % 6) as usize, &mut next);
            let (sa, sb) = (set(&mut z, &refs(&a)), set(&mut z, &refs(&b)));
            let ra: std::collections::BTreeSet<Vec<u32>> = a.iter().cloned().collect();
            let rb: std::collections::BTreeSet<Vec<u32>> = b.iter().cloned().collect();
            let u = z.union(sa, sb);
            let d = z.diff(sa, sb);
            assert_eq!(list(&z, u), ra.union(&rb).cloned().collect::<Vec<_>>());
            assert_eq!(list(&z, d), ra.difference(&rb).cloned().collect::<Vec<_>>());
            // canonical: rebuilding the union in the other order gives the same node
            assert_eq!(z.union(sb, sa), u);
            let again = set(&mut z, &refs(&ra.union(&rb).cloned().collect::<Vec<_>>()));
            assert_eq!(again, u);
        }
    }

    /// Product, minimization, non-supersets and truncation against their
    /// set-theoretic definitions on 300 random families (up to 8 variables,
    /// random probabilities, random cut-off and order limit).
    #[test]
    fn product_minimize_truncate_match_reference() {
        use std::collections::BTreeSet;
        let mut state = 0x9E6C_63D0_676A_9A99u64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        for _ in 0..300 {
            let mut z = Zbdd::new();
            let fam = |n: u64, next: &mut dyn FnMut() -> u64| -> Vec<Vec<u32>> {
                (0..n).map(|_| (0..8u32).filter(|_| next() % 3 == 0).collect()).collect()
            };
            let a = fam(next() % 6, &mut next);
            let b = fam(next() % 6, &mut next);
            let (sa, sb) = (set(&mut z, &refs(&a)), set(&mut z, &refs(&b)));
            let ra: BTreeSet<Vec<u32>> = a.iter().cloned().collect();
            let rb: BTreeSet<Vec<u32>> = b.iter().cloned().collect();
            // product
            let mut rp = BTreeSet::new();
            for x in &ra {
                for y in &rb {
                    let mut u: Vec<u32> = x.iter().chain(y.iter()).cloned().collect();
                    u.sort_unstable();
                    u.dedup();
                    rp.insert(u);
                }
            }
            let pr = z.product(sa, sb);
            assert_eq!(list(&z, pr), rp.iter().cloned().collect::<Vec<_>>());
            // minimize
            let sub = |x: &Vec<u32>, y: &Vec<u32>| x.iter().all(|v| y.contains(v));
            let rm: Vec<Vec<u32>> = rp.iter()
                .filter(|y| !rp.iter().any(|x| x != *y && sub(x, y))).cloned().collect();
            let mn = z.minimize(pr);
            assert_eq!(list(&z, mn), rm);
            // non-supersets
            let rn: Vec<Vec<u32>> = ra.iter()
                .filter(|y| !rb.iter().any(|x| sub(x, y))).cloned().collect();
            let ns = z.nonsupersets(sa, sb);
            assert_eq!(list(&z, ns), rn);
            // truncation (ZBDD variables are the literals here)
            let p: Vec<f64> = (0..8).map(|_| (1 + next() % 999) as f64 / 1000.0).collect();
            let cutoff = (next() % 1000) as f64 / 1e4;
            let order = if next() % 2 == 0 { Some((next() % 4) as usize) } else { None };
            let prob = |x: &Vec<u32>| x.iter().map(|&v| p[v as usize]).product::<f64>();
            let keep = |x: &Vec<u32>| prob(x) >= cutoff && order.map_or(true, |k| x.len() <= k);
            let (kept, dropped) = z.truncate(mn, &mut Weights::new(p.clone()), cutoff, order);
            let rk: Vec<Vec<u32>> = rm.iter().filter(|x| keep(x)).cloned().collect();
            let rd: Vec<Vec<u32>> = rm.iter().filter(|x| !keep(x)).cloned().collect();
            assert_eq!(list(&z, kept), rk);
            assert_eq!(list(&z, dropped), rd);
            let total: f64 = rm.iter().map(prob).sum();
            assert!((z.sum_prob(mn, &p) - total).abs() <= 1e-12 * total.max(1.0));
        }
    }

    #[test]
    fn truncated_product_keeps_the_same_set_and_bounds_the_loss() {
        let mut state = 0x51F1_5EED_0BAD_CAFEu64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            state
        };
        let n = 8u32;
        for case in 0..400 {
            let mut z = Zbdd::new();
            let fam = |k: u64, next: &mut dyn FnMut() -> u64| -> Vec<Vec<u32>> {
                (0..k).map(|_| (0..n).filter(|_| next() % 3 == 0).collect()).collect()
            };
            let a = fam(next() % 7, &mut next);
            let b = fam(next() % 7, &mut next);
            let (sa, sb) = (set(&mut z, &refs(&a)), set(&mut z, &refs(&b)));
            let p: Vec<f64> = (0..n).map(|_| (1 + next() % 999) as f64 / 1000.0).collect();
            let cutoff = if case % 5 == 0 { 0.0 } else { (next() % 1000) as f64 / 1e4 };
            let order = if next() % 2 == 0 { Some((next() % 4) as usize) } else { None };
            let full = z.product(sa, sb);
            let (want, _) = z.truncate(full, &mut Weights::new(p.clone()), cutoff, order);
            let (got, lost) = z.product_truncated(sa, sb, &mut Weights::new(p.clone()),
                                                  cutoff, order);
            assert_eq!(list(&z, got), list(&z, want), "case {case}");
            // every product of the full product is kept or contains a
            // covering term
            let (kl, ll) = (list(&z, got), list(&z, lost));
            let sub = |x: &Vec<u32>, y: &Vec<u32>| x.iter().all(|v| y.contains(v));
            for y in list(&z, full) {
                assert!(kl.contains(&y) || ll.iter().any(|t| sub(t, &y)),
                        "case {case}: {y:?} neither kept nor covered");
            }
            let d = z.sum_prob(lost, &p);
            // P(∪ all products) ≤ P(∪ kept) + Σ P(terms), by enumeration
            let union_p = |fam: &[Vec<u32>]| -> f64 {
                (0..1u32 << n).filter(|m| fam.iter().any(|x| x.iter().all(|&v| m >> v & 1 == 1)))
                    .map(|m| (0..n).map(|v| if m >> v & 1 == 1 { p[v as usize] }
                                             else { 1.0 - p[v as usize] }).product::<f64>())
                    .sum()
            };
            let (pf, pk) = (union_p(&list(&z, full)), union_p(&list(&z, got)));
            assert!(pf <= pk + d + 1e-12, "case {case}: {pf} > {pk} + {d}");
            if cutoff == 0.0 && order.is_none() {
                assert_eq!((got, lost), (full, EMPTY));
            }
        }
    }

    #[test]
    fn truncation_is_exact_at_the_cutoff() {
        // a product exactly at the cut-off is kept, one just below dropped;
        // the probability is the ascending fold, as `enumerate` lists it
        let mut z = Zbdd::new();
        let s = set(&mut z, &[&[0, 1], &[2], &[3, 4, 5]]);
        let p = vec![0.1, 0.3, 0.05, 0.5, 0.2, 0.2];
        let fold = |xs: &[u32]| xs.iter().fold(1.0, |a, &v| a * p[v as usize]);
        let c = fold(&[0, 1]);
        let (k, d) = z.truncate(s, &mut Weights::new(p.clone()), c, None);
        assert_eq!(list(&z, k), vec![vec![0, 1], vec![2]]);
        assert_eq!(list(&z, d), vec![vec![3, 4, 5]]);
        let above = f64::from_bits(c.to_bits() + 1);
        let (k, d) = z.truncate(s, &mut Weights::new(p.clone()), above, None);
        assert_eq!(list(&z, k), vec![vec![2]]);
        assert_eq!(list(&z, d), vec![vec![0, 1], vec![3, 4, 5]]);
        let _ = fold;
        // order limit: the size-3 product goes, whatever its probability
        let (k, _) = z.truncate(s, &mut Weights::new(p.clone()), 0.0, Some(2));
        assert_eq!(list(&z, k), vec![vec![0, 1], vec![2]]);
        let (k, d) = z.truncate(s, &mut Weights::new(p), 0.0, Some(0));
        assert_eq!((k, d), (EMPTY, s));
        // BASE (the empty product, probability 1) survives any cut-off < 1
        let (k, d) = z.truncate(BASE, &mut Weights::new(vec![]), 0.999, Some(0));
        assert_eq!((k, d), (BASE, EMPTY));
    }

    #[test]
    fn enumeration_limits() {
        let mut z = Zbdd::new();
        let s = set(&mut z, &[&[0], &[2, 4], &[1, 3, 5]]);
        assert_eq!(z.enumerate(s, None, None).len(), 3);
        assert_eq!(z.enumerate(s, Some(2), None).len(), 2);
        let le2 = z.enumerate(s, None, Some(2));
        assert_eq!(le2.len(), 2);
        assert!(le2.iter().all(|p| p.order() <= 2));
        // BASE is the single empty product, EMPTY has none
        assert_eq!(z.enumerate(BASE, None, None), vec![Product { pos: vec![], neg: vec![] }]);
        assert!(z.enumerate(EMPTY, None, None).is_empty());
    }
}
