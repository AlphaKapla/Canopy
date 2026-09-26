//! Dense grid of the uncertainty module's special functions, for
//! cross-verification against an independent implementation
//! (ci/crosscheck_special_functions.py compares it with SciPy).
//!
//! Output lines (whitespace-separated, shortest round-trip floats):
//!   N u  Φ⁻¹(u)
//!   G k u  gamma_quantile(k, u)          (standard gamma, scale 1)
//!   B a b u  beta_quantile(a, b, u)
//!   L x  ln Γ(x)
use canopy::uncertainty::{beta_quantile, gamma_quantile, ln_gamma, normal_quantile};

fn main() {
    let mut us: Vec<f64> = (1..=300).map(|e| 10f64.powi(-e)).collect();
    us.extend((1..2000).map(|i| i as f64 / 2000.0));
    us.extend((1..=15).map(|e| 1.0 - 10f64.powi(-e)));
    for &u in &us {
        println!("N {u:e} {:e}", normal_quantile(u));
    }
    let tail = |u: &&f64| **u >= 1e-200;
    for &k in &[0.02, 0.1, 0.5, 0.9, 1.0, 1.7, 5.0, 30.0, 500.0, 5000.0] {
        for &u in us.iter().step_by(7).filter(tail) {
            println!("G {k:e} {u:e} {:e}", gamma_quantile(k, u).unwrap());
        }
    }
    for &(a, b) in &[(0.1, 0.1), (0.5, 0.5), (1.5, 248.5), (0.5, 1e4), (3.0, 3.0),
                     (200.0, 1.0), (1.0, 1.0), (12.0, 4000.0),
                     (2.5, 2.0e4), (4.8, 3.3e4)] {
        for &u in us.iter().step_by(7).filter(tail) {
            println!("B {a:e} {b:e} {u:e} {:e}", beta_quantile(a, b, u).unwrap());
        }
    }
    for &x in &[1e-8, 0.001, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 2.5, 7.3, 30.0, 171.5, 1000.0, 1e6] {
        println!("L {x:e} {:e}", ln_gamma(x));
    }
}
