//! State-of-knowledge (epistemic) uncertainty: distributions, keyed random
//! numbers, inverse-CDF sampling and summary statistics.
//!
//! Design decisions (see docs/quantification.md, "Uncertainty propagation"):
//!
//! - **Keyed, counter-based random numbers.** The uniform deviate for an
//!   uncertain quantity in Monte Carlo iteration `i` is a pure function of
//!   `(seed, key, i)`, where `key` is the quantity's stable model ID
//!   (`PAR-...`, `IE-...`, `BE-.../value`, `CCF-.../total_probability`).
//!   Consequences, all by construction:
//!     * state-of-knowledge correlation: every basic event referencing
//!       `PAR-X` sees the same sample of `PAR-X` in iteration `i`;
//!     * the same iteration draws the same values in every engine process,
//!       so per-event-tree runs can be summed iteration by iteration into a
//!       model-wide metric (ci/quantify.py runs one process per tree);
//!     * adding, removing or reordering an *unrelated* quantity changes no
//!       other quantity's samples (git-diff stability), and base and head of
//!       a pull request share random numbers for every unchanged quantity
//!       (common random numbers), so the paired change is not swamped by
//!       Monte Carlo noise;
//!     * results are bit-for-bit reproducible from (model tag, seed, N).
//! - **Inverse-CDF sampling from one uniform per quantity.** A changed
//!   distribution in a pull request is coupled comonotonically to the old
//!   one (same `u`, new quantile), and Latin hypercube sampling can later be
//!   added by changing only how `u` is produced.
//! - **Point value = distribution mean.** For lognormal the point value *is*
//!   the mean (NUREG/CR-6928 convention, as in RiskSpectrum and SAPHIRE);
//!   beta, gamma and uniform are fully specified by their own parameters, so
//!   their mean must agree with the point value (relative tolerance
//!   `MEAN_REL_TOL`) or sampling is refused.

use anyhow::{anyhow, bail, Result};
use serde::Deserialize;

/// Φ⁻¹(0.95): the lognormal error factor is EF = q95 / median = exp(Z95·σ).
pub const Z95: f64 = 1.6448536269514722;

/// Allowed relative mismatch between a point value and the mean of a
/// fully-specified distribution (beta, gamma, uniform). Wide enough for a
/// point value rounded to three significant figures, narrow enough to catch
/// a distribution that describes a different quantity.
pub const MEAN_REL_TOL: f64 = 1e-2;

// ---------------------------------------------------------------------------
// YAML mirror and resolved distributions
// ---------------------------------------------------------------------------

/// `uncertainty:` block as written in the model (schema `$defs/uncertainty`).
#[derive(Deserialize, Debug, Clone, PartialEq)]
#[serde(tag = "distribution", rename_all = "lowercase", deny_unknown_fields)]
pub enum UncertaintyDef {
    Lognormal { error_factor: f64 },
    Beta { alpha: f64, beta: f64 },
    Gamma { shape: f64, scale: f64 },
    Uniform { lower: f64, upper: f64 },
}

/// A resolved, samplable distribution.
#[derive(Debug, Clone, PartialEq)]
pub enum Dist {
    /// ln X ~ Normal(mu, sigma²)
    Lognormal { mu: f64, sigma: f64 },
    Beta { a: f64, b: f64 },
    /// shape k, scale theta
    Gamma { k: f64, theta: f64 },
    Uniform { lo: f64, hi: f64 },
}

impl Dist {
    /// Resolve a model distribution against the quantity's point value.
    pub fn from_def(def: &UncertaintyDef, point: f64, what: &str) -> Result<Dist> {
        let dist = match *def {
            UncertaintyDef::Lognormal { error_factor } => {
                if !(error_factor > 1.0) || !error_factor.is_finite() {
                    bail!("{what}: lognormal error_factor must be > 1, got {error_factor}");
                }
                if !(point > 0.0) || !point.is_finite() {
                    bail!("{what}: lognormal needs a positive point value (the \
                           mean), got {point}");
                }
                let sigma = error_factor.ln() / Z95;
                Dist::Lognormal { mu: point.ln() - 0.5 * sigma * sigma, sigma }
            }
            UncertaintyDef::Beta { alpha, beta } => {
                if !(alpha > 0.0 && beta > 0.0) {
                    bail!("{what}: beta needs alpha > 0 and beta > 0");
                }
                Dist::Beta { a: alpha, b: beta }
            }
            UncertaintyDef::Gamma { shape, scale } => {
                if !(shape > 0.0 && scale > 0.0) {
                    bail!("{what}: gamma needs shape > 0 and scale > 0");
                }
                Dist::Gamma { k: shape, theta: scale }
            }
            UncertaintyDef::Uniform { lower, upper } => {
                if !(lower >= 0.0 && upper > lower) {
                    bail!("{what}: uniform needs 0 <= lower < upper, got \
                           [{lower}, {upper}]");
                }
                Dist::Uniform { lo: lower, hi: upper }
            }
        };
        let m = dist.mean();
        if (m - point).abs() > MEAN_REL_TOL * point.abs().max(m.abs()) {
            bail!(
                "{what}: point value {point:e} is not the mean {m:e} of its \
                 {} distribution (relative tolerance {MEAN_REL_TOL}); the \
                 point value must be the distribution mean",
                dist.name()
            );
        }
        Ok(dist)
    }

    pub fn name(&self) -> &'static str {
        match self {
            Dist::Lognormal { .. } => "lognormal",
            Dist::Beta { .. } => "beta",
            Dist::Gamma { .. } => "gamma",
            Dist::Uniform { .. } => "uniform",
        }
    }

    pub fn mean(&self) -> f64 {
        match *self {
            Dist::Lognormal { mu, sigma } => (mu + 0.5 * sigma * sigma).exp(),
            Dist::Beta { a, b } => a / (a + b),
            Dist::Gamma { k, theta } => k * theta,
            Dist::Uniform { lo, hi } => 0.5 * (lo + hi),
        }
    }

    /// Inverse CDF at `u` in (0, 1).
    pub fn quantile(&self, u: f64) -> Result<f64> {
        debug_assert!(u > 0.0 && u < 1.0);
        Ok(match *self {
            Dist::Lognormal { mu, sigma } => (mu + sigma * normal_quantile(u)).exp(),
            Dist::Beta { a, b } => beta_quantile(a, b, u)?,
            Dist::Gamma { k, theta } => theta * gamma_quantile(k, u)?,
            Dist::Uniform { lo, hi } => lo + u * (hi - lo),
        })
    }
}

// ---------------------------------------------------------------------------
// Keyed counter-based uniforms
// ---------------------------------------------------------------------------

/// SplitMix64 finalizer (Steele, Lea, Flood 2014): a bijective 64-bit mixer
/// with full avalanche; used here as a hash, not as a sequential stream.
#[inline]
pub fn splitmix64(z: u64) -> u64 {
    let mut z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// FNV-1a 64-bit hash of a quantity key (a stable model ID).
pub fn key_hash(key: &str) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in key.bytes() {
        h ^= b as u64;
        h = h.wrapping_mul(0x0000_0100_0000_01b3);
    }
    h
}

/// Uniform deviate in the open interval (0, 1) for (seed, key, iteration).
/// For fixed (seed, key) the map iteration -> 64-bit state is a composition
/// of bijections, so distinct iterations never share a state.
#[inline]
pub fn keyed_uniform(seed: u64, key: u64, iter: u64) -> f64 {
    let stream = splitmix64(splitmix64(seed) ^ key);
    let x = splitmix64(stream ^ splitmix64(iter));
    // 53 random bits, centred: u in [2^-54, 1 - 2^-54], never 0 or 1.
    ((x >> 11) as f64 + 0.5) * (1.0 / 9_007_199_254_740_992.0)
}

/// How the uniform deviates of a Monte Carlo run are laid out.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sampling {
    /// Simple random sampling: `keyed_uniform(seed, key, i)`.
    Srs,
    /// Latin hypercube sampling over `n` iterations: iteration i of a
    /// quantity falls in stratum π(i) of [0, 1) cut into n equal strata,
    /// where π is a permutation keyed by (seed, key) alone, jittered
    /// uniformly inside the stratum. Each quantity visits every stratum
    /// exactly once; quantities are paired independently (random
    /// permutations), so correlation between quantities is only what the
    /// model's shared parameters create.
    Lhs { n: u64 },
}

impl Sampling {
    pub fn name(&self) -> &'static str {
        match self {
            Sampling::Srs => "srs",
            Sampling::Lhs { .. } => "lhs",
        }
    }
}

const PERM_SALT: u64 = 0x4C48_535F_5045_524D; // "LHS_PERM"

/// The stratum permutation of one quantity for LHS: a Fisher–Yates shuffle
/// of 0..n driven by keyed uniforms of (seed, key ⊕ salt, position), so it
/// depends on nothing but (seed, key, n) — the keyed-RNG properties
/// (reproducibility, additivity across processes, diff stability) carry
/// over from simple random sampling.
pub fn lhs_permutation(seed: u64, key: u64, n: u64) -> Vec<u32> {
    let mut perm: Vec<u32> = (0..n as u32).collect();
    for j in (1..n).rev() {
        let u = keyed_uniform(seed, key ^ PERM_SALT, j);
        let r = ((u * (j + 1) as f64) as u64).min(j);
        perm.swap(j as usize, r as usize);
    }
    perm
}

/// LHS deviate of iteration i: (π(i) + v)/n with v = keyed_uniform(seed,
/// key, i), kept strictly inside (0, 1) (for the top stratum the division
/// can round up to 1).
#[inline]
pub fn lhs_uniform(perm: &[u32], seed: u64, key: u64, iter: u64) -> f64 {
    let n = perm.len() as f64;
    let u = (perm[iter as usize] as f64 + keyed_uniform(seed, key, iter)) / n;
    u.min(1.0 - f64::EPSILON / 2.0)
}

// ---------------------------------------------------------------------------
// Special functions
// ---------------------------------------------------------------------------

/// Φ⁻¹(p): Wichura, "Algorithm AS 241: The percentage points of the normal
/// distribution", Applied Statistics 37 (1988) 477–484, PPND16 (about 16
/// significant digits over the whole range).
pub fn normal_quantile(p: f64) -> f64 {
    let q = p - 0.5;
    if q.abs() <= 0.425 {
        let r = 0.180625 - q * q;
        return q
            * (((((((2.509_080_928_730_122_7e3 * r + 3.343_057_558_358_813e4) * r
                + 6.726_577_092_700_87e4)
                * r
                + 4.592_195_393_154_987e4)
                * r
                + 1.373_169_376_550_946e4)
                * r
                + 1.971_590_950_306_551_3e3)
                * r
                + 1.331_416_678_917_843_8e2)
                * r
                + 3.387_132_872_796_366_5)
            / (((((((5.226_495_278_852_545e3 * r + 2.872_908_573_572_194_3e4) * r
                + 3.930_789_580_009_271e4)
                * r
                + 2.121_379_430_158_659_7e4)
                * r
                + 5.394_196_021_424_751e3)
                * r
                + 6.871_870_074_920_579e2)
                * r
                + 4.231_333_070_160_091e1)
                * r
                + 1.0);
    }
    let mut r = if q < 0.0 { p } else { 1.0 - p };
    r = (-r.ln()).sqrt();
    let val = if r <= 5.0 {
        let r = r - 1.6;
        (((((((7.745_450_142_783_414e-4 * r + 2.272_384_498_926_918_4e-2) * r
            + 2.417_807_251_774_506e-1)
            * r
            + 1.270_458_252_452_368_4)
            * r
            + 3.647_848_324_763_204_5)
            * r
            + 5.769_497_221_460_691)
            * r
            + 4.630_337_846_156_546)
            * r
            + 1.423_437_110_749_683_5)
            / (((((((1.050_750_071_644_416_8e-9 * r + 5.475_938_084_995_345e-4) * r
                + 1.519_866_656_361_645_7e-2)
                * r
                + 1.481_039_764_274_800_8e-1)
                * r
                + 6.897_673_349_851e-1)
                * r
                + 1.676_384_830_183_803_8)
                * r
                + 2.053_191_626_637_759)
                * r
                + 1.0)
    } else {
        let r = r - 5.0;
        (((((((2.010_334_399_292_288_1e-7 * r + 2.711_555_568_743_487_6e-5) * r
            + 1.242_660_947_388_078_4e-3)
            * r
            + 2.653_218_952_657_612_4e-2)
            * r
            + 2.965_605_718_285_048_7e-1)
            * r
            + 1.784_826_539_917_291_3)
            * r
            + 5.463_784_911_164_114)
            * r
            + 6.657_904_643_501_103)
            / (((((((2.044_263_103_389_939_7e-15 * r + 1.421_511_758_316_446e-7) * r
                + 1.846_318_317_510_054_8e-5)
                * r
                + 7.868_691_311_456_133e-4)
                * r
                + 1.487_536_129_085_061_5e-2)
                * r
                + 1.369_298_809_227_358e-1)
                * r
                + 5.998_322_065_558_88e-1)
                * r
                + 1.0)
    };
    if q < 0.0 { -val } else { val }
}

/// ln Γ(x) for x > 0: Lanczos approximation (g = 7, n = 9), with the
/// reflection formula below 0.5.
pub fn ln_gamma(x: f64) -> f64 {
    const G: f64 = 7.0;
    const C: [f64; 9] = [
        0.999_999_999_999_809_9,
        676.520_368_121_885_1,
        -1_259.139_216_722_402_8,
        771.323_428_777_653_1,
        -176.615_029_162_140_6,
        12.507_343_278_686_905,
        -0.138_571_095_265_720_12,
        9.984_369_578_019_572e-6,
        1.505_632_735_149_311_6e-7,
    ];
    if x < 0.5 {
        let pi = std::f64::consts::PI;
        return (pi / (pi * x).sin()).ln() - ln_gamma(1.0 - x);
    }
    let x = x - 1.0;
    let mut a = C[0];
    let t = x + G + 0.5;
    for (i, c) in C.iter().enumerate().skip(1) {
        a += c / (x + i as f64);
    }
    0.5 * (2.0 * std::f64::consts::PI).ln() + (x + 0.5) * t.ln() - t + a.ln()
}

const SF_EPS: f64 = 1e-16;
const SF_TINY: f64 = 1e-300;
const SF_MAX_ITER: usize = 100_000;

/// Regularized incomplete gamma, returning (P(a,x), Q(a,x)) with the smaller
/// of the two computed directly (series below a+1, Lentz continued fraction
/// above), so both tails keep relative accuracy.
pub fn gamma_pq(a: f64, x: f64) -> Result<(f64, f64)> {
    if x <= 0.0 {
        return Ok((0.0, 1.0));
    }
    let ln_front = a * x.ln() - x - ln_gamma(a);
    if x < a + 1.0 {
        let mut ap = a;
        let mut del = 1.0 / a;
        let mut sum = del;
        for _ in 0..SF_MAX_ITER {
            ap += 1.0;
            del *= x / ap;
            sum += del;
            if del.abs() < sum.abs() * SF_EPS {
                let p = sum * ln_front.exp();
                return Ok((p, 1.0 - p));
            }
        }
        bail!("incomplete gamma series did not converge (a={a}, x={x})")
    } else {
        let mut b = x + 1.0 - a;
        let mut c = 1.0 / SF_TINY;
        let mut d = 1.0 / b;
        let mut h = d;
        for i in 1..SF_MAX_ITER {
            let an = -(i as f64) * (i as f64 - a);
            b += 2.0;
            d = an * d + b;
            if d.abs() < SF_TINY {
                d = SF_TINY;
            }
            c = b + an / c;
            if c.abs() < SF_TINY {
                c = SF_TINY;
            }
            d = 1.0 / d;
            let del = d * c;
            h *= del;
            if (del - 1.0).abs() < SF_EPS {
                let q = ln_front.exp() * h;
                return Ok((1.0 - q, q));
            }
        }
        bail!("incomplete gamma continued fraction did not converge (a={a}, x={x})")
    }
}

/// Continued fraction for the incomplete beta function (modified Lentz).
fn beta_cf(a: f64, b: f64, x: f64) -> Result<f64> {
    let (qab, qap, qam) = (a + b, a + 1.0, a - 1.0);
    let mut c = 1.0;
    let mut d = 1.0 - qab * x / qap;
    if d.abs() < SF_TINY {
        d = SF_TINY;
    }
    d = 1.0 / d;
    let mut h = d;
    for m in 1..SF_MAX_ITER {
        let m = m as f64;
        let m2 = 2.0 * m;
        let aa = m * (b - m) * x / ((qam + m2) * (a + m2));
        d = 1.0 + aa * d;
        if d.abs() < SF_TINY {
            d = SF_TINY;
        }
        c = 1.0 + aa / c;
        if c.abs() < SF_TINY {
            c = SF_TINY;
        }
        d = 1.0 / d;
        h *= d * c;
        let aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2));
        d = 1.0 + aa * d;
        if d.abs() < SF_TINY {
            d = SF_TINY;
        }
        c = 1.0 + aa / c;
        if c.abs() < SF_TINY {
            c = SF_TINY;
        }
        d = 1.0 / d;
        let del = d * c;
        h *= del;
        if (del - 1.0).abs() < SF_EPS {
            return Ok(h);
        }
    }
    bail!("incomplete beta continued fraction did not converge (a={a}, b={b}, x={x})")
}

fn ln_beta(a: f64, b: f64) -> f64 {
    ln_gamma(a) + ln_gamma(b) - ln_gamma(a + b)
}

/// Regularized incomplete beta, returning (I_x(a,b), 1 - I_x(a,b)) with the
/// smaller of the two computed directly (continued fraction on the side
/// where it converges), so both tails keep relative accuracy.
pub fn beta_inc_pq(a: f64, b: f64, x: f64) -> Result<(f64, f64)> {
    if x <= 0.0 {
        return Ok((0.0, 1.0));
    }
    if x >= 1.0 {
        return Ok((1.0, 0.0));
    }
    let ln_front = a * x.ln() + b * (-x).ln_1p() - ln_beta(a, b);
    if x < (a + 1.0) / (a + b + 2.0) {
        let p = ln_front.exp() * beta_cf(a, b, x)? / a;
        Ok((p, 1.0 - p))
    } else {
        let q = ln_front.exp() * beta_cf(b, a, 1.0 - x)? / b;
        Ok((1.0 - q, q))
    }
}

/// Solve g(t) = 0 for t by safeguarded Newton: `f(t)` returns (g, dg/dt)
/// with g increasing and the bracket satisfying g(lo) < 0 < g(hi). Callers
/// pass g = ln F(e^t) - ln target (log residual, log argument): in the far
/// tails ln F is nearly linear in t, where Newton on F itself would creep.
/// A non-finite g (F underflowed to 0) or derivative falls back to bisection.
fn solve_log(
    mut lo: f64,
    mut hi: f64,
    t0: f64,
    what: &str,
    f: &dyn Fn(f64) -> Result<(f64, f64)>,
) -> Result<f64> {
    let mut t = if t0 > lo && t0 < hi { t0 } else { 0.5 * (lo + hi) };
    for _ in 0..400 {
        let (g, dg) = f(t)?;
        if g == 0.0 {
            return Ok(t);
        }
        if g.is_nan() {
            bail!("{what}: quantile residual is NaN at t = {t}");
        }
        if g < 0.0 {
            lo = t;
        } else {
            hi = t;
        }
        let mut next = if dg > 0.0 && dg.is_finite() { t - g / dg } else { f64::NAN };
        if !(next > lo && next < hi) {
            next = 0.5 * (lo + hi); // Newton left the bracket: bisect
        }
        if (next - t).abs() <= 4.0 * f64::EPSILON * t.abs().max(1e-300)
            || hi - lo <= 4.0 * f64::EPSILON * t.abs().max(1e-300)
        {
            return Ok(next);
        }
        t = next;
    }
    Err(anyhow!("{what}: quantile inversion did not converge"))
}

/// Quantile of the standard gamma distribution (scale 1) with shape `k`.
pub fn gamma_quantile(k: f64, u: f64) -> Result<f64> {
    let upper = u > 0.5; // solve on the smaller tail for relative accuracy
    let target = if upper { 1.0 - u } else { u };
    // Initial guess: Wilson–Hilferty, or the small-x series for small k.
    let z = normal_quantile(u);
    let wh = k * (1.0 - 1.0 / (9.0 * k) + z / (3.0 * k.sqrt())).powi(3);
    let x0 = if wh > 0.0 && k >= 1.0 {
        wh
    } else {
        ((u.ln() + ln_gamma(k + 1.0)) / k).exp()
    };
    let lg = ln_gamma(k);
    let lt = target.ln();
    let f = |t: f64| -> Result<(f64, f64)> {
        let x = t.exp();
        let (p, q) = gamma_pq(k, x)?;
        let dens_t = (k * t - x - lg).exp(); // x · pdf(x) = dP/dt = -dQ/dt
        Ok(if upper {
            (lt - q.ln(), dens_t / q)
        } else {
            (p.ln() - lt, dens_t / p)
        })
    };
    let mut hi = (k.max(1.0) * 4.0 + 50.0).ln();
    while f(hi)?.0 <= 0.0 {
        hi += 1.0;
    }
    Ok(solve_log(-745.0, hi, x0.ln(), "gamma", &f)?.exp())
}

/// Quantile of Beta(a, b).
///
/// Two independent choices keep full precision. The *variable* solved for
/// is whichever of x and 1 - x lies in (0, 1/2] (decided exactly by
/// comparing u with I_{1/2}(a,b)), so its logarithm is bounded away from 0
/// and a relative tolerance on it is attainable; by symmetry
/// I_x(a,b) = 1 - I_{1-x}(b,a) the 1 - x case is the same problem for
/// Beta(b, a) at 1 - u. The *residual* uses whichever tail probability is
/// the smaller, which is always the exactly known one: both targets u and
/// 1 - u are carried through the swap, because 1 - u rounds (to 1.0 for
/// u < 2^-53) whenever u < 1/2.
pub fn beta_quantile(a: f64, b: f64, u: f64) -> Result<f64> {
    let (p_half, _) = beta_inc_pq(a, b, 0.5)?;
    if u <= p_half {
        beta_quantile_half(a, b, u, 1.0 - u)
    } else {
        Ok(1.0 - beta_quantile_half(b, a, 1.0 - u, u)?)
    }
}

/// Solve I_x(a,b) = lo_t (equivalently 1 - I_x(a,b) = up_t, where
/// lo_t + up_t = 1 and the smaller of the two is exact), knowing x <= 1/2.
fn beta_quantile_half(a: f64, b: f64, lo_t: f64, up_t: f64) -> Result<f64> {
    let lb = ln_beta(a, b);
    let upper = up_t < lo_t;
    let lt = if upper { up_t.ln() } else { lo_t.ln() };
    // Initial guess: small-x series x ≈ (u · a · B(a,b))^(1/a), capped.
    let x0 = ((lo_t.ln() + a.ln() + lb) / a).exp().min(0.25);
    let f = |t: f64| -> Result<(f64, f64)> {
        let x = t.exp();
        let dens_t = (a * t + (b - 1.0) * (-x).ln_1p() - lb).exp(); // dI/dt
        let (p, q) = beta_inc_pq(a, b, x)?;
        Ok(if upper {
            (lt - q.ln(), dens_t / q)
        } else {
            (p.ln() - lt, dens_t / p)
        })
    };
    Ok(solve_log(-745.0, 0.5f64.ln(), x0.ln(), "beta", &f)?.exp())
}

// ---------------------------------------------------------------------------
// Summary statistics
// ---------------------------------------------------------------------------

/// Percentile with linear interpolation between order statistics
/// (Hyndman & Fan type 7, NumPy's default); `sorted` must be ascending.
pub fn percentile(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    if n == 0 {
        return f64::NAN;
    }
    let h = (n - 1) as f64 * q;
    let lo = h.floor() as usize;
    if lo + 1 >= n {
        return sorted[n - 1];
    }
    sorted[lo] + (h - lo as f64) * (sorted[lo + 1] - sorted[lo])
}

#[derive(Debug, Clone)]
pub struct Summary {
    pub n: usize,
    pub mean: f64,
    pub std: f64,
    pub p05: f64,
    pub p50: f64,
    pub p95: f64,
}

impl Summary {
    pub fn of(samples: &[f64]) -> Summary {
        let n = samples.len();
        let mean = samples.iter().sum::<f64>() / n as f64;
        let var = if n > 1 {
            samples.iter().map(|x| (x - mean) * (x - mean)).sum::<f64>()
                / (n - 1) as f64
        } else {
            0.0
        };
        let mut s = samples.to_vec();
        s.sort_by(|a, b| a.partial_cmp(b).expect("NaN sample"));
        Summary {
            n,
            mean,
            std: var.sqrt(),
            p05: percentile(&s, 0.05),
            p50: percentile(&s, 0.50),
            p95: percentile(&s, 0.95),
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "mean": self.mean,
            "std": self.std,
            "std_error_of_mean": self.std / (self.n as f64).sqrt(),
            "p05": self.p05,
            "p50": self.p50,
            "p95": self.p95,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rel(a: f64, b: f64) -> f64 {
        (a - b).abs() / b.abs().max(1e-300)
    }

    /// Reference values from SciPy 1.17.1 `scipy.special.ndtri` (Cephes, an
    /// implementation independent of AS 241).
    #[test]
    fn normal_quantile_reference_values() {
        let cases = [
            (0.5, 0.0),
            (0.95, 1.6448536269514722),
            (0.975, 1.959963984540054),
            (0.999, 3.090232306167813),
            (0.05, -1.6448536269514729),
            (1e-10, -6.361340902404056),
            (1e-300, -37.0470962993612),
            (0.3, -0.5244005127080409),
            (0.8, 0.8416212335729143),
        ];
        for (p, z) in cases {
            let got = normal_quantile(p);
            let ok = if z == 0.0 { got.abs() < 1e-16 } else { rel(got, z) < 1e-14 };
            assert!(ok, "Φ⁻¹({p}) = {got}, reference {z}");
        }
        assert!(rel(normal_quantile(0.95), Z95) < 1e-15);
    }

    /// Reference values from SciPy 1.17.1 `scipy.special.gammaln`.
    #[test]
    fn ln_gamma_reference_values() {
        let cases = [
            (0.5, 0.5723649429247),
            (1.0, 0.0),
            (2.0, 0.0),
            (0.1, 2.252712651734206),
            (3.7, 1.428072326665388),
            (150.25, 601.2615040324997),
            (1e-5, 11.512919692895826),
        ];
        for (x, want) in cases {
            let got = ln_gamma(x);
            assert!((got - want).abs() < 1e-13 * want.abs().max(1.0),
                    "lnΓ({x}) = {got}, reference {want}");
        }
    }

    /// Reference quantiles from SciPy 1.17.1 `gammaincinv` / `betaincinv`.
    #[test]
    fn gamma_and_beta_quantile_reference_values() {
        let g = [
            (0.5, 0.05, 0.001966070000009761),
            (0.5, 0.95, 1.920729410347062),
            (2.5, 0.5, 2.175730095547763),
            (1.0, 0.5, std::f64::consts::LN_2),
            (37.0, 0.05, 27.59461554097935),
            (0.1, 1e-6, 6.073048362408003e-61),
        ];
        for (k, u, want) in g {
            let got = gamma_quantile(k, u).unwrap();
            assert!(rel(got, want) < 1e-11, "gamma({k}) q({u}) = {got}, ref {want}");
        }
        let b = [
            (1.5, 248.5, 0.05, 0.0007069793888036134),
            (1.5, 248.5, 0.95, 0.015585302017797998),
            (0.5, 0.5, 0.3, 0.2061073738537634),
            (200.0, 1.0, 0.5, 0.9965402628278678),
            (0.5, 1.0e4, 1e-3, 7.854182098101979e-11),
        ];
        for (a, bb, u, want) in b {
            let got = beta_quantile(a, bb, u).unwrap();
            assert!(rel(got, want) < 1e-11, "beta({a},{bb}) q({u}) = {got}, ref {want}");
        }
    }

    /// Inversion round trip over a grid: F(F⁻¹(u)) = u.
    #[test]
    fn quantile_round_trips() {
        for &u in &[1e-12, 1e-6, 0.01, 0.2, 0.5, 0.77, 0.99, 1.0 - 1e-9] {
            for &k in &[0.05, 0.5, 1.0, 3.0, 400.0] {
                let x = gamma_quantile(k, u).unwrap();
                let (p, q) = gamma_pq(k, x).unwrap();
                let err = if u > 0.5 { rel(q, 1.0 - u) } else { rel(p, u) };
                assert!(err < 1e-10, "gamma k={k} u={u}: x={x} P={p}");
            }
            // (2.5, 2e4) at u > 0.5: small solution in the upper half of
            // the probability scale (found by the property harness).
            for &(a, b) in &[(0.5, 0.5), (1.5, 248.5), (20.0, 3.0), (0.2, 50.0),
                             (2.5, 2.0e4), (4.8, 3.3e4)] {
                let x = beta_quantile(a, b, u).unwrap();
                assert!((0.0..=1.0).contains(&x));
                if 1.0 - x < 1e-12 {
                    continue; // x within ~1e4 ulps of 1: not resolvable in f64
                }
                let (p, q) = beta_inc_pq(a, b, x).unwrap();
                let err = if u > 0.5 { rel(q, 1.0 - u) } else { rel(p, u) };
                assert!(err < 1e-9, "beta({a},{b}) u={u}: x={x} P={p} Q={q}");
            }
        }
    }

    /// Every inversion converges across the parameter families the model
    /// format admits for probabilities (regression for the property-harness
    /// finding: small solutions at u > 0.5 with large beta).
    #[test]
    fn inversions_converge_over_parameter_families() {
        for i in 0..100u64 {
            let a = 0.1 + 9.9 * keyed_uniform(1, 11, i);
            let m = 10f64.powf(-5.0 + 4.9 * keyed_uniform(1, 12, i));
            let b = a * (1.0 - m) / m;
            for j in 0..200u64 {
                let u = keyed_uniform(3, 99, 1000 * i + j);
                let x = beta_quantile(a, b, u).unwrap();
                let (p, q) = beta_inc_pq(a, b, x).unwrap();
                let err = if u > 0.5 { rel(q, 1.0 - u) } else { rel(p, u) };
                assert!(err < 1e-9 || 1.0 - x < 1e-12, "beta({a},{b}) u={u}");
                let g = gamma_quantile(a, u).unwrap();
                let (p, q) = gamma_pq(a, g).unwrap();
                let err = if u > 0.5 { rel(q, 1.0 - u) } else { rel(p, u) };
                assert!(err < 1e-9, "gamma({a}) u={u}");
            }
        }
    }

    /// Lognormal parameterization: point value is the mean, EF = q95/q50.
    #[test]
    fn lognormal_mean_and_error_factor() {
        let d = Dist::from_def(
            &UncertaintyDef::Lognormal { error_factor: 3.0 }, 3.0e-5, "t").unwrap();
        assert!(rel(d.mean(), 3.0e-5) < 1e-15);
        let (q50, q95) = (d.quantile(0.5).unwrap(), d.quantile(0.95).unwrap());
        assert!(rel(q95 / q50, 3.0) < 1e-14);
    }

    #[test]
    fn inconsistent_point_value_is_refused() {
        // Demo maintenance event: beta(1.5, 248.5) has mean 6.0e-3 exactly.
        assert!(Dist::from_def(&UncertaintyDef::Beta { alpha: 1.5, beta: 248.5 },
                               6.0e-3, "t").is_ok());
        assert!(Dist::from_def(&UncertaintyDef::Beta { alpha: 1.5, beta: 248.5 },
                               6.0e-2, "t").is_err());
        assert!(Dist::from_def(&UncertaintyDef::Lognormal { error_factor: 3.0 },
                               0.0, "t").is_err());
        assert!(Dist::from_def(&UncertaintyDef::Uniform { lower: 2.0, upper: 1.0 },
                               1.5, "t").is_err());
    }

    /// LHS: every quantity's stratum map is a permutation of 0..n, each
    /// deviate lies in its stratum and strictly inside (0, 1), and the
    /// permutation is a pure function of (seed, key, n).
    #[test]
    fn lhs_stratifies_every_quantity() {
        for &(seed, n) in &[(1u64, 1u64), (7, 2), (20260708, 1000), (99, 4097)] {
            for key in [key_hash("PAR-A"), key_hash("BE-X/rate"), key_hash("IE-T")] {
                let perm = lhs_permutation(seed, key, n);
                // a permutation of 0..n
                let mut seen = vec![false; n as usize];
                for &s in &perm {
                    assert!(!seen[s as usize], "stratum {s} used twice");
                    seen[s as usize] = true;
                }
                // each deviate inside its stratum, strictly inside (0, 1)
                for i in 0..n {
                    let u = lhs_uniform(&perm, seed, key, i);
                    let s = perm[i as usize] as f64;
                    assert!(u > 0.0 && u < 1.0);
                    assert!(u >= s / n as f64 - 1e-15 && u <= (s + 1.0) / n as f64 + 1e-15,
                            "u {u} outside stratum {s} of {n}");
                }
                // pure: recomputing gives the same permutation
                assert_eq!(perm, lhs_permutation(seed, key, n));
            }
        }
        // keyed: different keys or seeds give different permutations
        let a = lhs_permutation(5, key_hash("PAR-A"), 64);
        assert_ne!(a, lhs_permutation(5, key_hash("PAR-B"), 64));
        assert_ne!(a, lhs_permutation(6, key_hash("PAR-A"), 64));
        // the top stratum never yields u = 1 even for n = 2
        let p2 = lhs_permutation(3, 0, 2);
        for i in 0..2 {
            assert!(lhs_uniform(&p2, 3, 0, i) < 1.0);
        }
    }

    /// The deviate is a pure function of (seed, key, iteration): independent
    /// of call order and of which other keys exist.
    #[test]
    fn keyed_uniforms_are_pure_and_distinct() {
        let (s, a, b) = (20260708u64, key_hash("PAR-A"), key_hash("PAR-B"));
        let u1: Vec<f64> = (0..1000).map(|i| keyed_uniform(s, a, i)).collect();
        let _noise: Vec<f64> = (0..1000).map(|i| keyed_uniform(s, b, i)).collect();
        let u2: Vec<f64> = (0..1000).rev().map(|i| keyed_uniform(s, a, i)).collect();
        assert!(u1.iter().eq(u2.iter().rev()));
        assert!(u1.iter().all(|&u| u > 0.0 && u < 1.0));
        assert_ne!(keyed_uniform(s, a, 0), keyed_uniform(s, b, 0));
        assert_ne!(keyed_uniform(s, a, 0), keyed_uniform(s + 1, a, 0));
    }

    /// Crude statistical sanity of the keyed stream: equidistribution over
    /// 20 bins (chi-square, 19 dof; 1e-6 critical value ≈ 61.7) along the
    /// iteration axis and across keys, and near-zero lag-1 correlation.
    #[test]
    fn keyed_uniforms_look_uniform() {
        let n = 200_000u64;
        let seed = 7;
        let chi2 = |us: &[f64]| {
            let mut bins = [0usize; 20];
            for &u in us {
                bins[(u * 20.0) as usize] += 1;
            }
            let e = us.len() as f64 / 20.0;
            bins.iter().map(|&c| (c as f64 - e).powi(2) / e).sum::<f64>()
        };
        let along: Vec<f64> = (0..n).map(|i| keyed_uniform(seed, key_hash("PAR-X"), i)).collect();
        let across: Vec<f64> = (0..n)
            .map(|i| keyed_uniform(seed, key_hash(&format!("PAR-{i}")), 0))
            .collect();
        assert!(chi2(&along) < 61.7, "chi2 along = {}", chi2(&along));
        assert!(chi2(&across) < 61.7, "chi2 across = {}", chi2(&across));
        let m = along.iter().sum::<f64>() / n as f64;
        let cov = along.windows(2).map(|w| (w[0] - m) * (w[1] - m)).sum::<f64>()
            / (n - 1) as f64;
        assert!((cov / (1.0 / 12.0)).abs() < 0.015, "lag-1 corr {}", cov * 12.0);
    }

    #[test]
    fn percentile_type7() {
        let s = [1.0, 2.0, 3.0, 4.0];
        assert_eq!(percentile(&s, 0.0), 1.0);
        assert_eq!(percentile(&s, 1.0), 4.0);
        assert!((percentile(&s, 0.5) - 2.5).abs() < 1e-15);
        assert!((percentile(&s, 0.05) - 1.15).abs() < 1e-15);
    }

    #[test]
    fn internally_tagged_yaml_parses_and_rejects_unknown_fields() {
        let ok: UncertaintyDef =
            serde_yaml::from_str("distribution: beta\nalpha: 1.5\nbeta: 248.5\n").unwrap();
        assert_eq!(ok, UncertaintyDef::Beta { alpha: 1.5, beta: 248.5 });
        assert!(serde_yaml::from_str::<UncertaintyDef>(
            "distribution: lognormal\nerror_factor: 3\nmedian: 1\n").is_err());
        assert!(serde_yaml::from_str::<UncertaintyDef>(
            "distribution: normal\nmean: 1\n").is_err());
    }
}
