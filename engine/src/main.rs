//! canopy: quantify fault trees and event trees from a git-native PSA
//! YAML model.
//!
//! Usage:
//!   canopy <model-dir> <FT-ID|ET-ID> [--house HE-ID=true ...]
//!           [--mcs-limit N] [--prob-only] [--json]
//!           [--samples N [--seed S] [--keep-samples]]
//!
//! `--samples N` adds a Monte Carlo propagation of the model's
//! state-of-knowledge uncertainty (docs/quantification.md): N iterations of
//! the exact BDD probability pass with every uncertain quantity sampled once
//! per iteration from a random number keyed by (seed, quantity ID,
//! iteration). Point results are computed and printed exactly as without
//! the flag.

mod bdd;
mod model;
mod uncertainty;

use anyhow::{anyhow, bail, Result};
use bdd::{Bdd, ProbPlan};
use model::{Formula, FormulaOp, Model, Outcome, Sampler};
use uncertainty::Summary;
use serde_json::json;
use std::collections::HashMap;
use std::path::PathBuf;

struct Compiler<'m> {
    model: &'m Model,
    bdd: Bdd,
    var_of_be: HashMap<String, u32>,
    be_of_var: Vec<String>,
    gate_cache: HashMap<String, u32>,
    in_progress: Vec<String>,
    coherent: bool,
}

impl<'m> Compiler<'m> {
    fn new(model: &'m Model) -> Self {
        Compiler {
            model,
            bdd: Bdd::new(),
            var_of_be: HashMap::new(),
            be_of_var: Vec::new(),
            gate_cache: HashMap::new(),
            in_progress: Vec::new(),
            coherent: true,
        }
    }

    fn be_var(&mut self, id: &str) -> u32 {
        if let Some(&v) = self.var_of_be.get(id) {
            return v;
        }
        let v = self.be_of_var.len() as u32;
        self.var_of_be.insert(id.to_string(), v);
        self.be_of_var.push(id.to_string());
        v
    }

    fn compile_ref(&mut self, id: &str) -> Result<u32> {
        if id.starts_with("BE-") {
            if !self.model.be_prob.contains_key(id) {
                bail!("dangling basic event reference: {id}");
            }
            let v = self.be_var(id);
            return Ok(self.bdd.variable(v));
        }
        if id.starts_with("HE-") {
            let val = self
                .model
                .house
                .get(id)
                .ok_or_else(|| anyhow!("dangling house event reference: {id}"))?;
            return Ok(if *val { bdd::ONE } else { bdd::ZERO });
        }
        if id.starts_with("GT-") {
            if let Some(&g) = self.gate_cache.get(id) {
                return Ok(g);
            }
            if self.in_progress.iter().any(|g| g == id) {
                bail!(
                    "cycle through gates: {} -> {id}",
                    self.in_progress.join(" -> ")
                );
            }
            let formula = self
                .model
                .gates
                .get(id)
                .ok_or_else(|| anyhow!("dangling gate reference: {id}"))?
                .clone();
            self.in_progress.push(id.to_string());
            let f = self.compile(&formula)?;
            self.in_progress.pop();
            self.gate_cache.insert(id.to_string(), f);
            return Ok(f);
        }
        bail!("reference with unknown prefix: {id}")
    }

    fn compile(&mut self, formula: &Formula) -> Result<u32> {
        Ok(match formula {
            Formula::Ref(id) => self.compile_ref(id)?,
            Formula::Op(op) => match op {
                FormulaOp::And(xs) => {
                    let mut acc = bdd::ONE;
                    for x in xs {
                        let f = self.compile(x)?;
                        acc = self.bdd.and(acc, f);
                    }
                    acc
                }
                FormulaOp::Or(xs) => {
                    let mut acc = bdd::ZERO;
                    for x in xs {
                        let f = self.compile(x)?;
                        acc = self.bdd.or(acc, f);
                    }
                    acc
                }
                FormulaOp::Xor(xs) => {
                    self.coherent = false;
                    let mut acc = bdd::ZERO;
                    for x in xs {
                        let f = self.compile(x)?;
                        acc = self.bdd.xor(acc, f);
                    }
                    acc
                }
                FormulaOp::Not(x) => {
                    self.coherent = false;
                    let f = self.compile(x)?;
                    self.bdd.not(f)
                }
                FormulaOp::Atleast { k, of } => {
                    let inputs: Result<Vec<u32>> =
                        of.iter().map(|x| self.compile(x)).collect();
                    self.bdd.atleast(*k, &inputs?)
                }
            },
        })
    }
}

/// Monte Carlo options (`--samples`, `--seed`, `--keep-samples`).
#[derive(Clone, Copy)]
struct McOpts {
    samples: usize,
    seed: u64,
    keep: bool,
}

/// Default seed when `--seed` is not given (the repository's house seed);
/// always echoed in the output so a run is reproducible from it.
const DEFAULT_SEED: u64 = 20260708;

const SAMPLING_NOTE: &str = "simple random sampling; inverse CDF of one \
    uniform per quantity per iteration, keyed by (seed, quantity ID, \
    iteration); one sample per quantity per iteration shared by every \
    event that uses it (state-of-knowledge correlation)";

// ---- Consequence-level importance (docs/quantification.md) --------------
//
// A sequence probability is multilinear in every basic-event probability,
// so P(seq) = p_x·P(seq|x=1) + (1−p_x)·P(seq|x=0) exactly, success branches
// (negated tops) included. A group of sequences (an end state, a metric) is
// a sum of sequence frequencies, hence
//     F(x=v) = Σ_j f_IE · P_j(x=v)
// exactly, and every standard importance measure follows from F, F(x=1)
// and F(x=0). A sequence whose BDD does not depend on x contributes its
// frequency unchanged to both. Sums across event trees are exact for the
// same reason (ci/importance.py).

/// Cofactor probabilities (BE id, P(seq | x=1), P(seq | x=0)) of one
/// sequence, for every basic event its BDD depends on.
fn sequence_cofactors(plan: &ProbPlan, p: &[f64], be_of_var: &[String])
    -> Vec<(String, f64, f64)>
{
    let mut buf = Vec::new();
    plan.support().into_iter().map(|v| (
        be_of_var[v as usize].clone(),
        plan.eval_cofactor(p, v, true, &mut buf),
        plan.eval_cofactor(p, v, false, &mut buf),
    )).collect()
}

/// One basic event's conditional frequencies for a group of sequences.
struct ImpRow {
    event: String,
    f_true: f64,
    f_false: f64,
}

/// Exact group frequency F and, per basic event any member sequence
/// depends on, (F(x=1), F(x=0)). `seqs` holds (frequency, cofactors) of
/// the member sequences in the order the point total sums them, so F is
/// bit-identical to the reported metric value. Rows are sorted by event.
fn group_importance(ie_freq: f64, seqs: &[(f64, &[(String, f64, f64)])])
    -> (f64, Vec<ImpRow>)
{
    let f: f64 = seqs.iter().map(|s| s.0).sum();
    let events: std::collections::BTreeSet<&String> = seqs.iter()
        .flat_map(|s| s.1.iter().map(|c| &c.0)).collect();
    let lookup: Vec<HashMap<&str, (f64, f64)>> = seqs.iter()
        .map(|s| s.1.iter().map(|c| (c.0.as_str(), (c.1, c.2))).collect())
        .collect();
    let rows = events.into_iter().map(|x| {
        let (mut f1, mut f0) = (0.0, 0.0);
        for ((freq, _), cof) in seqs.iter().zip(&lookup) {
            match cof.get(x.as_str()) {
                Some(&(p1, p0)) => {
                    f1 += ie_freq * p1;
                    f0 += ie_freq * p0;
                }
                None => {
                    f1 += freq;
                    f0 += freq;
                }
            }
        }
        ImpRow { event: x.clone(), f_true: f1, f_false: f0 }
    }).collect();
    (f, rows)
}

/// (Birnbaum F1 − F0, Fussell–Vesely (F − F0)/F, RAW F1/F, RRW F/F0);
/// a ratio is None where its denominator is zero (RRW: F0 = 0 < F means
/// the group cannot occur without the event, i.e. RRW is infinite).
fn measures(f: f64, f1: f64, f0: f64) -> (f64, Option<f64>, Option<f64>, Option<f64>) {
    let fv = (f > 0.0).then(|| (f - f0) / f);
    let raw = (f > 0.0).then(|| f1 / f);
    let rrw = (f0 > 0.0).then(|| f / f0);
    (f1 - f0, fv, raw, rrw)
}

/// Rows ranked by Fussell–Vesely (undefined last), ties by event ID.
fn rank_by_fv(f: f64, rows: &[ImpRow]) -> Vec<&ImpRow> {
    let mut r: Vec<&ImpRow> = rows.iter().collect();
    r.sort_by(|a, b| {
        let (fa, fb) = (measures(f, a.f_true, a.f_false).1,
                        measures(f, b.f_true, b.f_false).1);
        match (fa, fb) {
            (Some(x), Some(y)) => y.partial_cmp(&x).unwrap(),
            (Some(_), None) => std::cmp::Ordering::Less,
            (None, Some(_)) => std::cmp::Ordering::Greater,
            (None, None) => std::cmp::Ordering::Equal,
        }.then_with(|| a.event.cmp(&b.event))
    });
    r
}

fn importance_json(f: f64, rows: &[ImpRow], be_prob: &HashMap<String, f64>)
    -> serde_json::Value
{
    json!(rank_by_fv(f, rows).into_iter().map(|r| {
        let (b, fv, raw, rrw) = measures(f, r.f_true, r.f_false);
        json!({
            "event": r.event,
            "probability": be_prob[&r.event],
            "frequency_if_true_per_year": r.f_true,
            "frequency_if_false_per_year": r.f_false,
            "birnbaum_per_year": b,
            "fussell_vesely": fv,
            "raw": raw,
            "rrw": rrw,
        })
    }).collect::<Vec<_>>())
}

fn opt_fmt(x: Option<f64>, w: usize) -> String {
    match x {
        Some(v) => format!("{v:>w$.4e}"),
        None => format!("{:>w$}", "inf/undef"),
    }
}

fn quantities_json(s: &Sampler) -> serde_json::Value {
    json!(s.quantities().iter().map(|q| json!({
        "key": q.key,
        "distribution": q.dist.name(),
        "point": q.point,
    })).collect::<Vec<_>>())
}

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    let usage = "usage: canopy <model-dir> <FT-ID|ET-ID> \
                 [--house HE-ID=bool] [--mcs-limit N] [--prob-only] [--json] \
                 [--samples N [--seed S] [--keep-samples]]";
    let model_dir = PathBuf::from(args.next().ok_or_else(|| anyhow!(usage))?);
    let target = args.next().ok_or_else(|| anyhow!(usage))?;

    let mut mcs_limit: Option<usize> = Some(1000);
    let mut json_out = false;
    let mut prob_only = false;
    let mut house_overrides: Vec<(String, bool)> = Vec::new();
    let mut samples: Option<usize> = None;
    let mut seed: Option<u64> = None;
    let mut keep = false;
    while let Some(a) = args.next() {
        match a.as_str() {
            "--house" => {
                let kv = args.next().ok_or_else(|| anyhow!("--house HE-ID=bool"))?;
                let (k, v) = kv
                    .split_once('=')
                    .ok_or_else(|| anyhow!("--house HE-ID=bool"))?;
                house_overrides.push((k.to_string(), v.parse()?));
            }
            "--mcs-limit" => {
                mcs_limit = Some(args.next().unwrap_or_default().parse()?);
            }
            "--json" => json_out = true,
            "--prob-only" => prob_only = true,
            "--samples" => {
                let n: usize = args.next().unwrap_or_default().parse()
                    .map_err(|_| anyhow!("--samples needs a positive integer"))?;
                if n == 0 {
                    bail!("--samples needs a positive integer");
                }
                samples = Some(n);
            }
            "--seed" => {
                seed = Some(args.next().unwrap_or_default().parse()
                    .map_err(|_| anyhow!("--seed needs an unsigned 64-bit integer"))?);
            }
            "--keep-samples" => keep = true,
            other => bail!("unknown argument {other}"),
        }
    }
    if samples.is_none() && (seed.is_some() || keep) {
        bail!("--seed and --keep-samples only apply with --samples N");
    }
    let mc = samples.map(|n| McOpts {
        samples: n,
        seed: seed.unwrap_or(DEFAULT_SEED),
        keep,
    });

    let mut model = Model::load(&model_dir)?;
    for (k, v) in house_overrides {
        model.set_house(&k, v)?;
    }

    if prob_only {
        mcs_limit = Some(0);
    }
    if target.starts_with("ET-") {
        quantify_event_tree(&model_dir, model, &target, mcs_limit, json_out,
                            prob_only, mc)
    } else {
        quantify_fault_tree(model, &target, mcs_limit, json_out, prob_only, mc)
    }
}

fn quantify_fault_tree(
    model: Model,
    ft_id: &str,
    mcs_limit: Option<usize>,
    json_out: bool,
    prob_only: bool,
    mc: Option<McOpts>,
) -> Result<()> {
    let ft = model
        .fault_trees
        .get(ft_id)
        .ok_or_else(|| anyhow!("fault tree {ft_id} not found"))?;
    let top_gate = ft.top_gate.clone();

    let mut c = Compiler::new(&model);
    let top = c.compile_ref(&top_gate)?;
    let p: Vec<f64> = c.be_of_var.iter().map(|id| model.be_prob[id]).collect();
    let ptop = c.bdd.probability(top, &p);

    // Monte Carlo over the same BDD (before minsol/restrict grow the arena).
    let mut unc_json = serde_json::Value::Null;
    let mut unc_summary: Option<(Summary, u64, usize)> = None;
    if let Some(mc) = mc {
        let plan = c.bdd.prob_plan(top);
        let mut buf = Vec::new();
        if plan.eval(&p, &mut buf).to_bits() != ptop.to_bits() {
            bail!("internal: flat probability plan disagrees with the \
                   recursive pass at the point values");
        }
        let (mut sampler, _) = Sampler::new(&model, &c.be_of_var, &[], mc.seed)?;
        let mut probs = vec![0.0; c.be_of_var.len()];
        let mut draws = Vec::with_capacity(mc.samples);
        for i in 0..mc.samples as u64 {
            sampler.draw(i, &mut probs)?;
            draws.push(plan.eval(&probs, &mut buf));
        }
        let sm = Summary::of(&draws);
        let mut j = sm.to_json();
        j["samples"] = json!(mc.samples);
        j["seed"] = json!(mc.seed);
        j["sampling"] = json!(SAMPLING_NOTE);
        j["clamped_probabilities"] = json!(sampler.clamped);
        j["quantities"] = quantities_json(&sampler);
        if mc.keep {
            j["draws"] = json!(draws);
        }
        unc_summary = Some((sm, sampler.clamped, sampler.quantities().len()));
        unc_json = j;
    }

    let mut cuts_out: Vec<(f64, Vec<String>)> = Vec::new();
    if c.coherent && mcs_limit != Some(0) {
        let ms = c.bdd.minsol(top);
        let cuts = c.bdd.enumerate_paths(ms, mcs_limit);
        for cut in cuts {
            let cp: f64 = cut.iter().map(|&v| p[v as usize]).product();
            let names = cut
                .iter()
                .map(|&v| c.be_of_var[v as usize].clone())
                .collect();
            cuts_out.push((cp, names));
        }
        cuts_out.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
    }

    let mut imp: Vec<(String, f64)> = if prob_only {
        Vec::new()
    } else {
        (0..c.be_of_var.len() as u32)
            .map(|v| (c.be_of_var[v as usize].clone(),
                      c.bdd.birnbaum(top, v, &p)))
            .collect()
    };
    imp.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap());

    if json_out {
        let mut out = json!({
            "type": "fault_tree",
            "id": ft_id,
            "top_gate": top_gate,
            "coherent": c.coherent,
            "probability": ptop,
            "bdd_nodes": c.bdd.node_count(),
            "minimal_cut_sets": cuts_out.iter().map(|(cp, names)| json!({
                "probability": cp, "events": names })).collect::<Vec<_>>(),
            "birnbaum": imp.iter().map(|(id, b)| json!({
                "event": id, "importance": b })).collect::<Vec<_>>(),
        });
        if mc.is_some() {
            out["uncertainty"] = unc_json;
        }
        println!("{}", serde_json::to_string_pretty(&out)?);
        return Ok(());
    }

    println!("fault tree      : {ft_id} (top gate {top_gate})");
    println!("basic events    : {}", c.be_of_var.len());
    println!("BDD nodes       : {}", c.bdd.node_count());
    println!("P(top) exact    : {ptop:.6e}");
    if let (Some(mc), Some((sm, clamped, nq))) = (mc, &unc_summary) {
        println!(
            "uncertainty     : mean {:.4e}  5% {:.4e}  median {:.4e}  95% {:.4e}",
            sm.mean, sm.p05, sm.p50, sm.p95
        );
        println!(
            "                  {} samples, seed {}, {nq} uncertain quantities, \
             {clamped} clamped",
            mc.samples, mc.seed
        );
    }
    if c.coherent {
        println!("minimal cut sets: {}", cuts_out.len());
        for (cp, names) in &cuts_out {
            println!("  {:>12.4e}  {{{}}}", cp, names.join(", "));
        }
    } else {
        println!("minimal cut sets: skipped (non-coherent tree)");
    }
    println!("Birnbaum importance:");
    for (id, b) in imp {
        println!("  {:>12.4e}  {id}", b);
    }
    Ok(())
}

fn quantify_event_tree(
    model_dir: &std::path::Path,
    mut model: Model,
    et_id: &str,
    mcs_limit: Option<usize>,
    json_out: bool,
    prob_only: bool,
    mc: Option<McOpts>,
) -> Result<()> {
    let (trees, metrics) = Model::load_event_trees(model_dir)?;
    let et = trees
        .get(et_id)
        .ok_or_else(|| anyhow!("event tree {et_id} not found"))?;
    let ie_freq = et.initiating_event.frequency.value;

    let mut fe_ids: Vec<&String> = et.functional_events.keys().collect();
    fe_ids.sort();
    let mut seq_ids: Vec<&String> = et.sequences.keys().collect();
    seq_ids.sort();

    struct SeqResult {
        id: String,
        freq: f64,
        end_state: String,
        transfer: Option<String>,
        cut_sets: Vec<(f64, Vec<String>)>,
        cofactors: Vec<(String, f64, f64)>,
    }
    let mut results: Vec<SeqResult> = Vec::new();

    // Monte Carlo: one flat plan per sequence over a global variable list.
    let mut plans: Vec<ProbPlan> = Vec::new();
    let mut global_be: Vec<String> = Vec::new();
    let mut global_idx: HashMap<String, u32> = HashMap::new();

    for seq_id in &seq_ids {
        let seq = &et.sequences[*seq_id];
        let saved: Vec<(String, bool)> = seq
            .house_events
            .keys()
            .map(|k| (k.clone(), model.house[k]))
            .collect();
        for (k, v) in &seq.house_events {
            model.set_house(k, *v)?;
        }

        let mut c = Compiler::new(&model);
        let mut conj = bdd::ONE;
        let mut fail_only = bdd::ONE;
        for fe in &fe_ids {
            let top_gate = et.functional_events[*fe].top_gate.clone();
            match seq.path[*fe] {
                Outcome::Bypassed => {}
                Outcome::Failure => {
                    let f = c.compile_ref(&top_gate)?;
                    conj = c.bdd.and(conj, f);
                    fail_only = c.bdd.and(fail_only, f);
                }
                Outcome::Success => {
                    let f = c.compile_ref(&top_gate)?;
                    let nf = c.bdd.not(f);
                    conj = c.bdd.and(conj, nf);
                }
            }
        }
        let p: Vec<f64> = c.be_of_var.iter().map(|id| model.be_prob[id]).collect();
        let p_seq = c.bdd.probability(conj, &p);
        let freq = ie_freq * p_seq;

        let mut cofactors = Vec::new();
        if mc.is_some() || !prob_only {
            let mut plan = c.bdd.prob_plan(conj);
            let mut buf = Vec::new();
            if plan.eval(&p, &mut buf).to_bits() != p_seq.to_bits() {
                bail!("internal: flat probability plan disagrees with the \
                       recursive pass for {seq_id}");
            }
            // Importance: exact cofactors of this sequence (local numbering).
            if !prob_only {
                cofactors = sequence_cofactors(&plan, &p, &c.be_of_var);
            }
            if mc.is_some() {
                let local: Vec<u32> = c.be_of_var.iter().map(|id| {
                    *global_idx.entry(id.clone()).or_insert_with(|| {
                        global_be.push(id.clone());
                        (global_be.len() - 1) as u32
                    })
                }).collect();
                plan.map_vars(|v| local[v as usize]);
                plans.push(plan);
            }
        }

        let mut cut_sets: Vec<(f64, Vec<String>)> = Vec::new();
        // Cut sets only for coherent sequence logic: minsol is invalid in
        // the presence of NOT/XOR (prime implicants would be required).
        // A tautological failure logic (e.g. a house event pinning a
        // functional event failed) yields the EMPTY cut set, consistent
        // with the fault-tree path: the sequence needs no component
        // failures. Suppressing it would leave a dominant sequence
        // unexplained.
        if seq.end_state != "OK" && c.coherent && mcs_limit != Some(0) {
            let ms = c.bdd.minsol(fail_only);
            for cut in c.bdd.enumerate_paths(ms, mcs_limit) {
                let cp: f64 = cut.iter().map(|&v| p[v as usize]).product();
                let names = cut
                    .iter()
                    .map(|&v| c.be_of_var[v as usize].clone())
                    .collect();
                cut_sets.push((ie_freq * cp, names));
            }
            cut_sets.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap());
        }

        results.push(SeqResult {
            id: (*seq_id).clone(),
            freq,
            end_state: seq.end_state.clone(),
            transfer: seq.transfer.clone(),
            cut_sets,
            cofactors,
        });
        for (k, v) in saved {
            model.set_house(&k, v)?;
        }
    }

    let metric_totals: Vec<(String, String, f64)> = metrics
        .iter()
        .map(|m| {
            let total: f64 = results
                .iter()
                .filter(|r| m.end_states.contains(&r.end_state))
                .map(|r| r.freq)
                .sum();
            (m.id.clone(), m.label.clone(), total)
        })
        .collect();

    // ---- Consequence-level importance -----------------------------------
    // Groups use the same membership and order as the point totals, so a
    // metric's F equals its reported value bit for bit (checked).
    let group = |pred: &dyn Fn(&str) -> bool| {
        let seqs: Vec<(f64, &[(String, f64, f64)])> = results.iter()
            .filter(|r| pred(&r.end_state))
            .map(|r| (r.freq, r.cofactors.as_slice()))
            .collect();
        group_importance(ie_freq, &seqs)
    };
    let mut metric_imp: Vec<(f64, Vec<ImpRow>)> = Vec::new();
    let mut end_state_imp: Vec<(String, f64, Vec<ImpRow>)> = Vec::new();
    if !prob_only {
        for (m, (id, _, total)) in metrics.iter().zip(&metric_totals) {
            let g = group(&|es| m.end_states.iter().any(|e| e == es));
            if g.0.to_bits() != total.to_bits() {
                bail!("internal: importance total for {id} disagrees with \
                       the metric value");
            }
            metric_imp.push(g);
        }
        let states: std::collections::BTreeSet<&String> =
            results.iter().map(|r| &r.end_state).collect();
        for es in states {
            let (f, rows) = group(&|e| e == es);
            end_state_imp.push((es.clone(), f, rows));
        }
    }

    // ---- Monte Carlo propagation ----------------------------------------
    // Sequence draws seq_draws[j][i] = f_IE(i) * P_j(i); metric draws sum
    // the qualifying sequences in the same order as the point totals.
    let ie = &et.initiating_event;
    let mut seq_draws: Vec<Vec<f64>> = Vec::new();
    let mut metric_draws: Vec<Vec<f64>> = Vec::new();
    let mut ie_draws: Vec<f64> = Vec::new();
    let mut sampler_opt: Option<Sampler> = None;
    if let Some(mc) = mc {
        let extra = [(ie.id.clone(), ie_freq, ie.frequency.uncertainty.clone())];
        let (mut sampler, extra_idx) =
            Sampler::new(&model, &global_be, &extra, mc.seed)?;
        let ie_q = extra_idx[0];
        let mut probs = vec![0.0; global_be.len()];
        let mut buf = Vec::new();
        seq_draws = vec![Vec::with_capacity(mc.samples); plans.len()];
        for i in 0..mc.samples as u64 {
            sampler.draw(i, &mut probs)?;
            let f_ie = match ie_q {
                Some(q) => sampler.value(q),
                None => ie_freq,
            };
            ie_draws.push(f_ie);
            for (j, plan) in plans.iter().enumerate() {
                seq_draws[j].push(f_ie * plan.eval(&probs, &mut buf));
            }
        }
        for m in &metrics {
            let members: Vec<usize> = results
                .iter()
                .enumerate()
                .filter(|(_, r)| m.end_states.contains(&r.end_state))
                .map(|(j, _)| j)
                .collect();
            metric_draws.push((0..mc.samples)
                .map(|i| members.iter().map(|&j| seq_draws[j][i]).sum())
                .collect());
        }
        sampler_opt = Some(sampler);
    }

    if json_out {
        let mut out = json!({
            "type": "event_tree",
            "id": et_id,
            "initiating_event": {
                "id": et.initiating_event.id,
                "frequency_per_year": ie_freq,
            },
            "sequences": results.iter().map(|r| json!({
                "id": r.id,
                "frequency_per_year": r.freq,
                "end_state": r.end_state,
                "transfer": r.transfer,
                "cut_sets": r.cut_sets.iter().map(|(f, names)| json!({
                    "frequency_per_year": f, "events": names,
                })).collect::<Vec<_>>(),
            })).collect::<Vec<_>>(),
            "metrics": metric_totals.iter().map(|(id, label, v)| json!({
                "id": id, "label": label, "value_per_year": v,
            })).collect::<Vec<_>>(),
        });
        if !prob_only {
            for (m, (f, rows)) in out["metrics"].as_array_mut().unwrap()
                .iter_mut().zip(&metric_imp)
            {
                m["importance"] = importance_json(*f, rows, &model.be_prob);
            }
            out["end_states"] = json!(end_state_imp.iter().map(|(es, f, rows)| json!({
                "id": es,
                "frequency_per_year": f,
                "importance": importance_json(*f, rows, &model.be_prob),
            })).collect::<Vec<_>>());
        }
        if let (Some(mc), Some(sampler)) = (mc, &sampler_opt) {
            for (j, seq) in out["sequences"].as_array_mut().unwrap()
                .iter_mut().enumerate()
            {
                let mut u = Summary::of(&seq_draws[j]).to_json();
                if mc.keep {
                    u["draws"] = json!(seq_draws[j]);
                }
                seq["uncertainty"] = u;
            }
            // Metric draws are always emitted: summing them iteration by
            // iteration across event-tree runs (same seed and N) gives the
            // model-wide metric distribution (ci/quantify.py).
            for (k, m) in out["metrics"].as_array_mut().unwrap()
                .iter_mut().enumerate()
            {
                let mut u = Summary::of(&metric_draws[k]).to_json();
                u["draws"] = json!(metric_draws[k]);
                m["uncertainty"] = u;
            }
            let mut u = json!({
                "samples": mc.samples,
                "seed": mc.seed,
                "sampling": SAMPLING_NOTE,
                "clamped_probabilities": sampler.clamped,
                "quantities": quantities_json(sampler),
            });
            if mc.keep {
                u["initiating_event_draws"] = json!(ie_draws);
            }
            out["uncertainty"] = u;
        }
        println!("{}", serde_json::to_string_pretty(&out)?);
        return Ok(());
    }

    println!(
        "event tree      : {et_id}  (IE {} @ {:.3e} /yr)",
        et.initiating_event.id, ie_freq
    );
    for r in &results {
        if !r.cut_sets.is_empty() {
            println!("  {} dominant cut sets (failure logic):", r.id);
            for (f, names) in r.cut_sets.iter().take(5) {
                println!("      {:>10.3e} /yr  {{{}}}", f, names.join(", "));
            }
        }
    }
    println!("sequences:");
    for r in &results {
        let note = if r.transfer.is_some() { "  [transfer]" } else { "" };
        println!(
            "  {:<14} {:>12.4e} /yr  -> {}{note}",
            r.id, r.freq, r.end_state
        );
    }
    for (k, (id, label, v)) in metric_totals.iter().enumerate() {
        println!("{id} ({label}) : {v:.4e} /yr");
        if let Some(d) = metric_draws.get(k) {
            let sm = Summary::of(d);
            println!(
                "    uncertainty: mean {:.4e}  5% {:.4e}  median {:.4e}  95% {:.4e} /yr",
                sm.mean, sm.p05, sm.p50, sm.p95
            );
        }
        if let Some((f, rows)) = metric_imp.get(k) {
            const TOP: usize = 10;
            println!("    importance (BDD-exact over this tree's sequences, \
                      top {} of {} by Fussell-Vesely):", TOP.min(rows.len()),
                     rows.len());
            println!("      {:>10} {:>10} {:>10} {:>12}  event",
                     "FV", "RAW", "RRW", "Birnbaum/yr");
            for r in rank_by_fv(*f, rows).into_iter().take(TOP) {
                let (b, fv, raw, rrw) = measures(*f, r.f_true, r.f_false);
                println!("      {} {} {} {:>12.4e}  {}", opt_fmt(fv, 10),
                         opt_fmt(raw, 10), opt_fmt(rrw, 10), b, r.event);
            }
        }
    }
    if let (Some(mc), Some(sampler)) = (mc, &sampler_opt) {
        println!(
            "uncertainty: {} samples, seed {}, {} uncertain quantities, {} clamped \
             (metrics exclude transfers, as the point values do)",
            mc.samples, mc.seed, sampler.quantities().len(), sampler.clamped
        );
    }
    let n_xfer = results.iter().filter(|r| r.transfer.is_some()).count();
    if n_xfer > 0 {
        println!(
            "note: {n_xfer} sequence(s) transfer to other event trees and \
             are not included in the metrics above"
        );
    }
    Ok(())
}

#[cfg(test)]
mod importance_tests {
    use super::*;

    /// Hand-computed consequence importance with a shared event and a
    /// success branch. FE1 top = A, FE2 top = A OR B; IE 1e-3 /yr;
    /// P(A) = 0.1, P(B) = 0.2.
    ///   S1: FE1 success, FE2 failure -> ¬A ∧ (A ∨ B) = ¬A ∧ B    (CD)
    ///   S2: FE1 failure, FE2 bypassed -> A                       (CD)
    /// F(CD) = f·(a + (1−a)b) = 2.8e-4; F(A=1) = f = 1e-3,
    /// F(A=0) = f·b = 2e-4; F(B=1) = f·(a + 1 − a) = 1e-3, F(B=0) = f·a =
    /// 1e-4. S2 does not depend on B, so it enters F(B=·) unchanged.
    /// The minimal-cut-set FV of A would be 0.1/0.28 = 0.357 (cut sets {B}
    /// from S1, {A} from S2); the exact value is 1 − 0.2/0.28 = 0.2857,
    /// because the success branch ¬A in S1 is accounted for.
    #[test]
    fn consequence_importance_hand_computed() {
        let mut bdd = Bdd::new();
        let a = bdd.variable(0);
        let b = bdd.variable(1);
        let fe2 = bdd.or(a, b);
        let na = bdd.not(a);
        let s1 = bdd.and(na, fe2);
        let s2 = a;
        let names = vec!["BE-A".to_string(), "BE-B".to_string()];
        let p = vec![0.1, 0.2];
        let ie = 1e-3;
        let cof = |root: u32| sequence_cofactors(&bdd.prob_plan(root), &p, &names);
        let (c1, c2) = (cof(s1), cof(s2));
        assert_eq!(c2.len(), 1, "S2 depends on A only");
        let f1 = ie * bdd.probability(s1, &p);
        let f2 = ie * bdd.probability(s2, &p);
        let (f, rows) = group_importance(ie, &[(f1, &c1), (f2, &c2)]);
        let close = |x: f64, y: f64| (x - y).abs() <= 1e-15 * y.abs();
        assert!(close(f, 2.8e-4));
        assert_eq!(rows.len(), 2);
        let (ra, rb) = (&rows[0], &rows[1]);
        assert_eq!((ra.event.as_str(), rb.event.as_str()), ("BE-A", "BE-B"));
        assert!(close(ra.f_true, 1e-3) && close(ra.f_false, 2e-4));
        assert!(close(rb.f_true, 1e-3) && close(rb.f_false, 1e-4));

        let (bi, fv, raw, rrw) = measures(f, ra.f_true, ra.f_false);
        assert!(close(bi, 8e-4));
        assert!(close(fv.unwrap(), 1.0 - 0.2 / 0.28));
        assert!(close(raw.unwrap(), 1.0 / 0.28));
        assert!(close(rrw.unwrap(), 1.4));
        let (bi, fv, _, rrw) = measures(f, rb.f_true, rb.f_false);
        assert!(close(bi, 9e-4));
        assert!(close(fv.unwrap(), 1.0 - 0.1 / 0.28));
        assert!(close(rrw.unwrap(), 2.8));
        // Ranking: B (FV 0.643) before A (FV 0.286).
        let ranked: Vec<&str> = rank_by_fv(f, &rows).iter()
            .map(|r| r.event.as_str()).collect();
        assert_eq!(ranked, ["BE-B", "BE-A"]);
    }

    /// Degenerate denominators are reported as undefined, never as a
    /// number: F = 0 (FV, RAW undefined) and F(x=0) = 0 (RRW infinite).
    #[test]
    fn importance_undefined_ratios() {
        assert_eq!(measures(0.0, 1e-3, 0.0), (1e-3, None, None, None));
        let (_, fv, raw, rrw) = measures(1e-4, 1e-3, 0.0);
        assert_eq!((fv, rrw), (Some(1.0), None));
        assert!((raw.unwrap() - 10.0).abs() < 1e-14);
    }
}
