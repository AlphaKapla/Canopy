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
