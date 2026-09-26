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
        quantify_event_tree(&model_dir, model, &target, mcs_limit, json_out, mc)
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

        if mc.is_some() {
            let mut plan = c.bdd.prob_plan(conj);
            let mut buf = Vec::new();
            if plan.eval(&p, &mut buf).to_bits() != p_seq.to_bits() {
                bail!("internal: flat probability plan disagrees with the \
                       recursive pass for {seq_id}");
            }
            let local: Vec<u32> = c.be_of_var.iter().map(|id| {
                *global_idx.entry(id.clone()).or_insert_with(|| {
                    global_be.push(id.clone());
                    (global_be.len() - 1) as u32
                })
            }).collect();
            plan.map_vars(|v| local[v as usize]);
            plans.push(plan);
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
