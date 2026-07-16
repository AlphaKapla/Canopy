//! Loader for the git-native PSA YAML model format (schema v0.1).
//! Parses fault trees, basic events, parameters and house events, resolves
//! parameter references, and computes point probabilities per basic event.

use anyhow::{anyhow, bail, Context, Result};
use serde::Deserialize;
use std::collections::HashMap;
use std::fs;
use std::path::Path;

// ---------- serde mirror of the YAML schema (subset needed to quantify) ----

#[derive(Deserialize, Debug)]
struct FaultTreesFile {
    fault_trees: HashMap<String, FaultTreeDef>,
}

#[derive(Deserialize, Debug)]
pub struct FaultTreeDef {
    #[allow(dead_code)]
    pub label: String,
    pub top_gate: String,
    pub gates: HashMap<String, GateDef>,
}

#[derive(Deserialize, Debug)]
pub struct GateDef {
    #[allow(dead_code)]
    pub label: String,
    pub formula: Formula,
}

#[derive(Deserialize, Debug, Clone)]
#[serde(untagged)]
pub enum Formula {
    Ref(String),
    Op(FormulaOp),
}

#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields, rename_all = "lowercase")]
pub enum FormulaOp {
    And(Vec<Formula>),
    Or(Vec<Formula>),
    Xor(Vec<Formula>),
    Not(Box<Formula>),
    Atleast { k: usize, of: Vec<Formula> },
}

#[derive(Deserialize, Debug)]
struct BasicEventsFile {
    basic_events: HashMap<String, BasicEventDef>,
}

#[derive(Deserialize, Debug)]
struct BasicEventDef {
    #[allow(dead_code)]
    label: String,
    failure_model: FailureModel,
}

#[derive(Deserialize, Debug)]
#[serde(tag = "type")]
enum FailureModel {
    #[serde(rename = "probability")]
    Probability { value: QuantityOrRef },
    #[serde(rename = "rate-mission")]
    RateMission {
        rate: QuantityOrRef,
        mission_time: QuantityOrRef,
    },
    #[serde(rename = "rate-repair")]
    RateRepair { rate: QuantityOrRef, mttr: QuantityOrRef },
    #[serde(rename = "rate-periodic-test")]
    RatePeriodicTest { rate: QuantityOrRef, test_interval: QuantityOrRef },
    #[serde(rename = "frequency")]
    Frequency { value: QuantityOrRef },
}

#[derive(Deserialize, Debug)]
#[serde(untagged)]
enum QuantityOrRef {
    Ref { param: String },
    Quantity { value: f64, unit: Option<String> },
}

#[derive(Deserialize, Debug)]
struct ParametersFile {
    parameters: HashMap<String, ParameterDef>,
}

#[derive(Deserialize, Debug)]
struct ParameterDef {
    value: f64,
    #[allow(dead_code)]
    unit: Option<String>,
}

#[derive(Deserialize, Debug)]
struct HouseEventsFile {
    house_events: HashMap<String, HouseEventDef>,
}

#[derive(Deserialize, Debug)]
struct HouseEventDef {
    default: bool,
}

// ---------- assembled, resolved model --------------------------------------

pub struct Model {
    pub fault_trees: HashMap<String, FaultTreeDef>,
    /// All gates across all trees, merged into one namespace (transfers are
    /// plain cross-references, so gate IDs are global).
    pub gates: HashMap<String, Formula>,
    /// Basic event ID -> resolved point probability.
    pub be_prob: HashMap<String, f64>,
    /// House event ID -> boolean state (defaults; configurations override).
    pub house: HashMap<String, bool>,
}

fn load_yaml<T: for<'de> Deserialize<'de>>(path: &Path) -> Result<T> {
    let text = fs::read_to_string(path)
        .with_context(|| format!("reading {}", path.display()))?;
    serde_yaml::from_str(&text)
        .with_context(|| format!("parsing {}", path.display()))
}

fn glob_dir(dir: &Path) -> Result<Vec<std::path::PathBuf>> {
    let mut v: Vec<_> = fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map(|x| x == "yaml").unwrap_or(false))
        .collect();
    v.sort();
    Ok(v)
}

/// Convert one basic event's failure model to a point probability, given a
/// parameter resolver. Standalone (not a `Model` method) so unit tests can
/// exercise the real conversion arithmetic without a full YAML fixture.
fn failure_model_prob(
    id: &str,
    fm: &FailureModel,
    resolve: &dyn Fn(&QuantityOrRef, &str) -> Result<f64>,
) -> Result<f64> {
    Ok(match fm {
        FailureModel::Probability { value } => resolve(value, id)?,
        FailureModel::RateMission { rate, mission_time } => {
            let r = resolve(rate, id)?;
            let t = resolve(mission_time, id)?;
            1.0 - (-r * t).exp()
        }
        FailureModel::RateRepair { rate, mttr } => {
            let r = resolve(rate, id)?;
            let m = resolve(mttr, id)?;
            (r * m) / (1.0 + r * m)
        }
        FailureModel::RatePeriodicTest { rate, test_interval } => {
            let r = resolve(rate, id)?;
            let t = resolve(test_interval, id)?;
            // Time-averaged standby unavailability between idealized
            // (instantaneous, perfect) periodic tests:
            //   Q_avg = (1/T) integral_0^T (1 - e^-rt) dt
            //         = 1 - (1 - e^-rT) / (rT).
            // Exact, not the small-rT linear approximation rT/2; the two
            // agree to float precision for any realistic rate*interval
            // product (subnormal-rT cancellation is not a concern in that
            // domain).
            let x = r * t;
            if x == 0.0 { 0.0 } else { 1.0 - (1.0 - (-x).exp()) / x }
        }
        FailureModel::Frequency { .. } => bail!(
            "{id}: frequency-type events are initiators, \
             not fault-tree basic events"
        ),
    })
}

impl Model {
    pub fn load(model_dir: &Path) -> Result<Model> {
        // Parameters first (basic events reference them).
        let params: ParametersFile = load_yaml(&model_dir.join("parameters.yaml"))?;
        let resolve = |q: &QuantityOrRef, what: &str| -> Result<f64> {
            match q {
                QuantityOrRef::Quantity { value, .. } => Ok(*value),
                QuantityOrRef::Ref { param } => params
                    .parameters
                    .get(param)
                    .map(|p| p.value)
                    .ok_or_else(|| anyhow!("{what}: unresolved parameter {param}")),
            }
        };

        // Basic events from every file in basic-events/.
        let mut be_prob = HashMap::new();
        for path in glob_dir(&model_dir.join("basic-events"))? {
            let file: BasicEventsFile = load_yaml(&path)?;
            for (id, be) in file.basic_events {
                let p = failure_model_prob(&id, &be.failure_model, &resolve)?;
                if !(0.0..=1.0).contains(&p) {
                    bail!("{id}: resolved probability {p} outside [0,1]");
                }
                if be_prob.insert(id.clone(), p).is_some() {
                    bail!("duplicate basic event ID across files: {id}");
                }
            }
        }

        // House events (defaults).
        let house_file: HouseEventsFile =
            load_yaml(&model_dir.join("house-events.yaml"))?;
        let house = house_file
            .house_events
            .into_iter()
            .map(|(id, h)| (id, h.default))
            .collect();

        // Fault trees: merge every file's trees and gates into one namespace.
        let mut fault_trees = HashMap::new();
        let mut gates: HashMap<String, Formula> = HashMap::new();
        for path in glob_dir(&model_dir.join("fault-trees"))? {
            let file: FaultTreesFile = load_yaml(&path)?;
            for (ft_id, ft) in file.fault_trees {
                for (gid, g) in &ft.gates {
                    if gates.insert(gid.clone(), g.formula.clone()).is_some() {
                        bail!("duplicate gate ID across files: {gid}");
                    }
                }
                if fault_trees.insert(ft_id.clone(), ft).is_some() {
                    bail!("duplicate fault tree ID: {ft_id}");
                }
            }
        }

        // Common-cause expansion (if the file exists).
        let ccf_path = model_dir.join("ccf-groups.yaml");
        if ccf_path.exists() {
            let file: CcfGroupsFile = load_yaml(&ccf_path)?;
            let resolver = |p: &str| -> Result<f64> {
                params.parameters.get(p).map(|d| d.value).ok_or_else(
                    || anyhow!("CCF: unresolved parameter {p}"))
            };
            expand_ccf(&file.ccf_groups, &mut be_prob, &mut gates, &resolver)?;
        }

        Ok(Model { fault_trees, gates, be_prob, house })
    }

    pub fn set_house(&mut self, id: &str, value: bool) -> Result<()> {
        match self.house.get_mut(id) {
            Some(v) => {
                *v = value;
                Ok(())
            }
            None => bail!("unknown house event {id}"),
        }
    }
}

// ---------- event trees & manifest ------------------------------------------

#[derive(Deserialize, Debug)]
struct EventTreeFile {
    event_tree: EventTreeDef,
}

#[derive(Deserialize, Debug)]
pub struct EventTreeDef {
    pub id: String,
    #[allow(dead_code)]
    pub label: String,
    pub initiating_event: InitiatingEventDef,
    pub functional_events: HashMap<String, FunctionalEventDef>,
    pub sequences: HashMap<String, SequenceDef>,
}

#[derive(Deserialize, Debug)]
pub struct InitiatingEventDef {
    pub id: String,
    #[allow(dead_code)]
    pub label: String,
    pub frequency: FrequencyDef,
}

#[derive(Deserialize, Debug)]
pub struct FrequencyDef {
    pub value: f64,
    pub unit: String,
}

#[derive(Deserialize, Debug)]
pub struct FunctionalEventDef {
    pub label: String,
    pub top_gate: String,
}

#[derive(Deserialize, Debug, Clone, Copy, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Outcome {
    Success,
    Failure,
    Bypassed,
}

#[derive(Deserialize, Debug)]
pub struct SequenceDef {
    pub path: HashMap<String, Outcome>,
    pub end_state: String,
    pub transfer: Option<String>,
    #[serde(default)]
    pub house_events: HashMap<String, bool>,
}

#[derive(Deserialize, Debug)]
struct ManifestFile {
    model: ManifestModel,
}

#[derive(Deserialize, Debug)]
struct ManifestModel {
    #[serde(default)]
    risk_metrics: Vec<RiskMetricDef>,
}

#[derive(Deserialize, Debug)]
pub struct RiskMetricDef {
    pub id: String,
    pub label: String,
    pub end_states: Vec<String>,
}

impl Model {
    /// Load event trees and risk metrics (call after `load`).
    pub fn load_event_trees(
        model_dir: &Path,
    ) -> Result<(HashMap<String, EventTreeDef>, Vec<RiskMetricDef>)> {
        let mut trees = HashMap::new();
        let dir = model_dir.join("event-trees");
        if dir.is_dir() {
            for path in glob_dir(&dir)? {
                let file: EventTreeFile = load_yaml(&path)?;
                let et = file.event_tree;
                if et.initiating_event.frequency.unit != "per_year" {
                    bail!(
                        "{}: initiating-event frequency must be per_year, got {}",
                        et.id,
                        et.initiating_event.frequency.unit
                    );
                }
                for (seq_id, seq) in &et.sequences {
                    for fe in seq.path.keys() {
                        if !et.functional_events.contains_key(fe) {
                            bail!("{seq_id}: path references undefined {fe}");
                        }
                    }
                    if seq.path.len() != et.functional_events.len() {
                        bail!(
                            "{seq_id}: path must resolve every functional event"
                        );
                    }
                }
                if trees.insert(et.id.clone(), et).is_some() {
                    bail!("duplicate event tree ID");
                }
            }
        }
        let manifest: ManifestFile = load_yaml(&model_dir.join("model.yaml"))?;
        Ok((trees, manifest.model.risk_metrics))
    }
}

// ---------- common-cause failure expansion ----------------------------------

#[derive(Deserialize, Debug)]
struct CcfGroupsFile {
    ccf_groups: HashMap<String, CcfGroupDef>,
}

#[derive(Deserialize, Debug)]
pub struct CcfGroupDef {
    #[allow(dead_code)]
    pub label: String,
    pub model: String, // alpha-factor | beta-factor  (mgl: not yet supported)
    pub members: Vec<String>,
    pub total_probability: QuantityOrRef2,
    #[serde(default)]
    pub factors: HashMap<String, f64>,
    /// staggered (default) | non-staggered
    #[serde(default = "default_testing")]
    pub testing: String,
}
fn default_testing() -> String { "staggered".into() }

// serde alias of QuantityOrRef usable from the CCF path (same shape).
#[derive(Deserialize, Debug)]
#[serde(untagged)]
pub enum QuantityOrRef2 {
    Ref { param: String },
    Quantity { value: f64 },
}

fn binom(n: u64, k: u64) -> f64 {
    let mut r = 1.0;
    for i in 0..k {
        r = r * (n - i) as f64 / (i + 1) as f64;
    }
    r
}

/// Substitute basic-event references per `map` (member -> CCF event IDs):
/// Ref(m) becomes Or([Ref(m), Ref(ccf...)]) so the member keeps its
/// (rescaled) independent contribution plus every CCF event containing it.
fn subst(f: &Formula, map: &HashMap<String, Vec<String>>) -> Formula {
    match f {
        Formula::Ref(id) => match map.get(id) {
            Some(ccfs) => {
                let mut ops = vec![Formula::Ref(id.clone())];
                ops.extend(ccfs.iter().cloned().map(Formula::Ref));
                Formula::Op(FormulaOp::Or(ops))
            }
            None => f.clone(),
        },
        Formula::Op(op) => Formula::Op(match op {
            FormulaOp::And(xs) =>
                FormulaOp::And(xs.iter().map(|x| subst(x, map)).collect()),
            FormulaOp::Or(xs) =>
                FormulaOp::Or(xs.iter().map(|x| subst(x, map)).collect()),
            FormulaOp::Xor(xs) =>
                FormulaOp::Xor(xs.iter().map(|x| subst(x, map)).collect()),
            FormulaOp::Not(x) =>
                FormulaOp::Not(Box::new(subst(x, map))),
            FormulaOp::Atleast { k, of } => FormulaOp::Atleast {
                k: *k,
                of: of.iter().map(|x| subst(x, map)).collect(),
            },
        }),
    }
}

/// Expand CCF groups: rescale member independent probabilities, create the
/// combination basic events, and rewrite every gate formula.
///
/// Alpha-factor model, per NUREG/CR-5485:
///   staggered:      Q_k = alpha_k / C(n-1, k-1) * Qt
///   non-staggered:  Q_k = k * alpha_k / (alpha_t * C(n-1, k-1)) * Qt,
///                   alpha_t = sum(k * alpha_k)
/// Beta-factor: Q_1 = (1-beta) Qt, Q_n = beta Qt, intermediates zero.
pub fn expand_ccf(
    groups: &HashMap<String, CcfGroupDef>,
    be_prob: &mut HashMap<String, f64>,
    gates: &mut HashMap<String, Formula>,
    resolve_param: &dyn Fn(&str) -> Result<f64>,
) -> Result<()> {
    for (gid, g) in groups {
        let n = g.members.len();
        if !(2..=8).contains(&n) {
            bail!("{gid}: CCF group size {n} unsupported (2..=8)");
        }
        for m in &g.members {
            if !be_prob.contains_key(m) {
                bail!("{gid}: member {m} is not a defined basic event");
            }
        }
        let qt = match &g.total_probability {
            QuantityOrRef2::Quantity { value } => *value,
            QuantityOrRef2::Ref { param } => resolve_param(param)?,
        };

        // Per-multiplicity alphas.
        let alphas: Vec<f64> = match g.model.as_str() {
            "alpha-factor" => (1..=n)
                .map(|k| {
                    g.factors.get(&format!("alpha_{k}")).copied().ok_or_else(
                        || anyhow!("{gid}: missing factor alpha_{k}"))
                })
                .collect::<Result<_>>()?,
            "beta-factor" => {
                let beta = *g.factors.get("beta").ok_or_else(
                    || anyhow!("{gid}: beta-factor needs factor beta"))?;
                let mut a = vec![0.0; n];
                a[0] = 1.0 - beta;
                a[n - 1] = beta;
                a
            }
            "mgl" => bail!("{gid}: MGL not yet supported; convert to \
                            alpha factors"),
            other => bail!("{gid}: unknown CCF model {other}"),
        };
        let asum: f64 = alphas.iter().sum();
        if (asum - 1.0).abs() > 1e-3 {
            bail!("{gid}: alpha factors sum to {asum}, expected 1.0");
        }

        let alpha_t: f64 = alphas
            .iter()
            .enumerate()
            .map(|(i, a)| (i as f64 + 1.0) * a)
            .sum();
        let qk: Vec<f64> = (1..=n)
            .map(|k| {
                let c = binom((n - 1) as u64, (k - 1) as u64);
                match g.testing.as_str() {
                    "staggered" => alphas[k - 1] / c * qt,
                    "non-staggered" =>
                        (k as f64) * alphas[k - 1] / (alpha_t * c) * qt,
                    other => panic!("unknown testing scheme {other}"),
                }
            })
            .collect();

        // Rescale members to their independent contribution Q_1.
        for m in &g.members {
            be_prob.insert(m.clone(), qk[0]);
        }
        // Combination events for every subset of size >= 2.
        let mut map: HashMap<String, Vec<String>> = HashMap::new();
        for mask in 1u32..(1 << n) {
            let k = mask.count_ones() as usize;
            if k < 2 {
                continue;
            }
            let idxs: Vec<usize> =
                (0..n).filter(|i| mask & (1 << i) != 0).collect();
            let id = format!(
                "BE-{gid}-{}",
                idxs.iter().map(|i| (i + 1).to_string())
                    .collect::<Vec<_>>().join("-")
            );
            be_prob.insert(id.clone(), qk[k - 1]);
            for &i in &idxs {
                map.entry(g.members[i].clone()).or_default().push(id.clone());
            }
        }
        // Rewrite all gate formulas.
        for f in gates.values_mut() {
            *f = subst(f, &map);
        }
    }
    Ok(())
}

#[cfg(test)]
mod ccf_tests {
    use super::*;

    /// Hand-computed reference: 2 pumps in parallel (AND of failures),
    /// alpha-factor, staggered, Qt = 1e-3, a1 = 0.95, a2 = 0.05.
    /// Q1 = 0.95e-3, Q2 = 0.05e-3 (C(1,0)=1).
    /// TOP = C or (A_i and B_i)  =>  P = Q2 + Q1^2 - Q2*Q1^2.
    #[test]
    fn alpha_factor_two_pump_and() {
        let mut be = HashMap::new();
        be.insert("BE-A".to_string(), 1.0e-3);
        be.insert("BE-B".to_string(), 1.0e-3);
        let mut gates = HashMap::new();
        gates.insert(
            "GT-TOP".to_string(),
            Formula::Op(FormulaOp::And(vec![
                Formula::Ref("BE-A".into()),
                Formula::Ref("BE-B".into()),
            ])),
        );
        let mut groups = HashMap::new();
        groups.insert("CCF-P".to_string(), CcfGroupDef {
            label: "pumps".into(),
            model: "alpha-factor".into(),
            members: vec!["BE-A".into(), "BE-B".into()],
            total_probability: QuantityOrRef2::Quantity { value: 1.0e-3 },
            factors: HashMap::from([
                ("alpha_1".to_string(), 0.95),
                ("alpha_2".to_string(), 0.05),
            ]),
            testing: "staggered".into(),
        });
        expand_ccf(&groups, &mut be, &mut gates, &|_| unreachable!())
            .unwrap();

        let (q1, q2) = (0.95e-3, 0.05e-3);
        assert!((be["BE-A"] - q1).abs() < 1e-15);
        assert!((be["BE-CCF-P-1-2"] - q2).abs() < 1e-15);

        // Evaluate P(top) by direct enumeration of the 3 rewritten events.
        let f = &gates["GT-TOP"];
        let mut p_top = 0.0;
        for m in 0u32..8 {
            let st = |id: &str| match id {
                "BE-A" => m & 1 != 0,
                "BE-B" => m & 2 != 0,
                _ => m & 4 != 0,
            };
            fn ev(f: &Formula, st: &dyn Fn(&str) -> bool) -> bool {
                match f {
                    Formula::Ref(id) => st(id),
                    Formula::Op(FormulaOp::And(xs)) =>
                        xs.iter().all(|x| ev(x, st)),
                    Formula::Op(FormulaOp::Or(xs)) =>
                        xs.iter().any(|x| ev(x, st)),
                    _ => unreachable!(),
                }
            }
            if ev(f, &st) {
                let w = |b: bool, p: f64| if b { p } else { 1.0 - p };
                p_top += w(m & 1 != 0, q1) * w(m & 2 != 0, q1)
                    * w(m & 4 != 0, q2);
            }
        }
        let expect = q2 + q1 * q1 - q2 * q1 * q1;
        assert!((p_top - expect).abs() < 1e-15,
                "got {p_top}, expect {expect}");
    }

    /// Group size 8 is the new upper bound (was 6). Beta-factor model keeps
    /// the arithmetic hand-checkable at this size: only Q_1 and Q_n are
    /// nonzero, intermediates are exactly zero, and the combination-event
    /// count follows 2^n - n - 1 exactly.
    #[test]
    fn group_size_eight_beta_factor() {
        let members: Vec<String> =
            (1..=8).map(|i| format!("BE-{i}")).collect();
        let mut be = HashMap::new();
        for m in &members {
            be.insert(m.clone(), 1.0e-3);
        }
        let mut gates = HashMap::new();
        let mut groups = HashMap::new();
        groups.insert("CCF-8".to_string(), CcfGroupDef {
            label: "octet".into(),
            model: "beta-factor".into(),
            members: members.clone(),
            total_probability: QuantityOrRef2::Quantity { value: 1.0e-3 },
            factors: HashMap::from([("beta".to_string(), 0.1)]),
            testing: "staggered".into(),
        });
        expand_ccf(&groups, &mut be, &mut gates, &|_| unreachable!())
            .unwrap();

        let (q1, q8) = (0.9e-3, 0.1e-3);
        for m in &members {
            assert!((be[m] - q1).abs() < 1e-15);
        }
        let full_id = "BE-CCF-8-1-2-3-4-5-6-7-8";
        assert!((be[full_id] - q8).abs() < 1e-15);

        let combo_count = be.len() - members.len();
        assert_eq!(combo_count, (1usize << 8) - 8 - 1); // 247

        let intermediate_zero = be.iter()
            .filter(|(id, _)| id.starts_with("BE-CCF-8-") && *id != full_id)
            .all(|(_, p)| *p == 0.0);
        assert!(intermediate_zero);
    }

    #[test]
    fn group_size_nine_rejected() {
        let members: Vec<String> =
            (1..=9).map(|i| format!("BE-{i}")).collect();
        let mut be = HashMap::new();
        for m in &members {
            be.insert(m.clone(), 1.0e-3);
        }
        let mut gates = HashMap::new();
        let mut groups = HashMap::new();
        groups.insert("CCF-9".to_string(), CcfGroupDef {
            label: "nonet".into(),
            model: "beta-factor".into(),
            members,
            total_probability: QuantityOrRef2::Quantity { value: 1.0e-3 },
            factors: HashMap::from([("beta".to_string(), 0.1)]),
            testing: "staggered".into(),
        });
        assert!(
            expand_ccf(&groups, &mut be, &mut gates, &|_| unreachable!())
                .is_err()
        );
    }
}

#[cfg(test)]
mod failure_model_tests {
    use super::*;

    /// Hand-computed reference: rate = 1e-3 /hr, test_interval = 100 hr,
    /// so rT = 0.1. Q_avg = 1 - (1 - e^-0.1) / 0.1.
    /// e^-0.1 = 0.90483741803595957316...
    /// (1 - e^-0.1) / 0.1 = 0.951625819640404...
    /// Q_avg = 0.048374180359596 (matches the small-rT approx rT/2 = 0.05
    /// to within the expected second-order correction -rT^2/6 ≈ -0.00167).
    #[test]
    fn periodic_test_unavailability() {
        let fm = FailureModel::RatePeriodicTest {
            rate: QuantityOrRef::Quantity { value: 1.0e-3, unit: None },
            test_interval: QuantityOrRef::Quantity { value: 100.0, unit: None },
        };
        let resolve = |q: &QuantityOrRef, _what: &str| -> Result<f64> {
            match q {
                QuantityOrRef::Quantity { value, .. } => Ok(*value),
                QuantityOrRef::Ref { .. } => unreachable!(),
            }
        };
        let p = failure_model_prob("BE-TEST", &fm, &resolve).unwrap();
        assert!((p - 0.048374180359596).abs() < 1e-12, "got {p}");
    }

    /// Zero rate (or zero test interval) is the exact limit Q_avg -> 0,
    /// not the 0/0 the closed form would hit undivided.
    #[test]
    fn periodic_test_zero_rate_is_exact_zero() {
        let fm = FailureModel::RatePeriodicTest {
            rate: QuantityOrRef::Quantity { value: 0.0, unit: None },
            test_interval: QuantityOrRef::Quantity { value: 100.0, unit: None },
        };
        let resolve = |q: &QuantityOrRef, _what: &str| -> Result<f64> {
            match q {
                QuantityOrRef::Quantity { value, .. } => Ok(*value),
                QuantityOrRef::Ref { .. } => unreachable!(),
            }
        };
        let p = failure_model_prob("BE-TEST", &fm, &resolve).unwrap();
        assert_eq!(p, 0.0);
    }
}
