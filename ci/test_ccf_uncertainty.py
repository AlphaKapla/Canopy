#!/usr/bin/env python3
"""Tests for uncertainty on CCF factors (FR-36): `factor_uncertainty`.

A group's alphas get a Dirichlet distribution with parameters
a_k = N · alpha_k (a beta-factor group: a Beta on β). The engine draws
alpha_k = G_k / Σ G_j from independent keyed gammas G_k ~ Gamma(a_k, 1).

Exact references, computed here independently of the engine: P(top) of a
small tree over one group is expanded symbolically into a polynomial in
the multiplicity coefficients c_k (Q_k = c_k · Q_t, Q_t fixed), so every
raw moment E[P^j] is a combination of Dirichlet moments E[Π c_k^m_k]:
  * staggered, c_k = alpha_k / C(n-1, k-1): Π C^-m · Π (a_k)_(m_k) / (A)_(M)
    (rising factorials);
  * non-staggered, c_k = k alpha_k / (alpha_t C(n-1, k-1)), alpha_t =
    Σ j alpha_j: with alpha = G / Σ G the normalisation cancels, and
    E[Π G_k^m_k / S^M] (S = Σ j G_j) = Π (a_k)_(m_k) / Γ(M) ·
    ∫_0^∞ t^(M-1) Π_j (1 + j t)^-(a_j + m_j) dt, integrated numerically
    (self-checked: with every weight 1 it must equal the closed form).
Checked: Monte Carlo mean within 6 standard errors of E[P], sample
variance within 6 standard errors of Var[P] (its standard error from the
exact fourth central moment), for staggered, non-staggered and
beta-factor groups; point results unchanged by adding the distribution;
the keyed factor draws visible in the output; malformed blocks refused.

Usage: python ci/test_ccf_uncertainty.py [--engine PATH]
"""
import argparse
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROV = {"source": "ci/test_ccf_uncertainty.py", "justification": "test fixture"}
N_SAMPLES = 20000


def rising(a, m):
    r = 1.0
    for i in range(m):
        r *= a + i
    return r


def weighted_moment(a, m, w):
    """E[Π_k G_k^m_k / S^M], S = Σ_k w_k G_k, G_k ~ Gamma(a_k, 1)
    independent (a_k = 0: G_k = 0, and then m_k must be 0)."""
    M = sum(m)
    pre = 1.0
    for ak, mk in zip(a, m):
        if ak == 0:
            if mk:
                return 0.0
            continue
        pre *= rising(ak, mk)
    if M == 0:
        return pre if pre == 1.0 else pre
    A = sum(a)
    # t = e^u; integrand in log space; trapezoid on a wide uniform grid
    # (exponentially convergent for this smooth, exponentially decaying
    # integrand: step 0.05 agrees with the closed form to ~1e-15 and with
    # step 0.001 to ~3e-14 on the cases measured when this was written)
    lo, hi, h = -45.0 / M - 5, 5 + 45.0 / max(A, 0.2), 0.05

    def log_f(u):
        return M * u - sum((ak + mk) * math.log1p(wk * math.exp(u))
                           for ak, mk, wk in zip(a, m, w) if ak > 0)
    n = int((hi - lo) / h)
    total = 0.0
    for i in range(n + 1):
        u = lo + i * h
        f = math.exp(log_f(u))
        total += f * (0.5 if i in (0, n) else 1.0)
    return pre * total * h / math.gamma(M)


def coeff_moment(a, m, scheme, memo):
    """E[Π c_k^m_k] for the coefficient vector c of an n-member group."""
    key = (tuple(m), scheme)
    if key in memo:
        return memo[key]
    n = len(a)
    binoms = [math.comb(n - 1, k - 1) for k in range(1, n + 1)]
    if scheme == "staggered":
        A, M = sum(a), sum(m)
        r = 1.0
        for ak, mk, c in zip(a, m, binoms):
            if ak == 0 and mk:
                r = 0.0
                break
            r *= rising(ak, mk) / c ** mk
        r = r / rising(A, M) if r else 0.0
    else:
        r = weighted_moment(a, m, list(range(1, n + 1)))
        for k, (mk, c) in enumerate(zip(m, binoms), start=1):
            r *= (k / c) ** mk
    memo[key] = r
    return r


def poly_all_fail(n, members_needed):
    """P(at least `members_needed` of the n members fail) as a polynomial
    {exponent tuple over (c_1..c_n): coefficient}, with Q_t factored out
    as a separate total degree: returns {(e_1..e_n): coef} where the term
    is coef · Π (c_k Q_t)^e_k. Events: every non-empty subset of members
    (singletons = independent parts, probability Q_1)."""
    subsets = [s for r in range(1, n + 1) for s in itertools.combinations(range(n), r)]
    poly = {}
    for bits in itertools.product([0, 1], repeat=len(subsets)):
        failed = set()
        for b, s in zip(bits, subsets):
            if b:
                failed |= set(s)
        if len(failed) < members_needed:
            continue
        # Π over events: Q_k if b else (1 - Q_k)
        terms = {tuple([0] * n): 1.0}
        for b, s in zip(bits, subsets):
            k = len(s)
            new = {}
            for e, c in terms.items():
                up = list(e)
                up[k - 1] += 1
                up = tuple(up)
                if b:
                    new[up] = new.get(up, 0.0) + c
                else:
                    new[e] = new.get(e, 0.0) + c
                    new[up] = new.get(up, 0.0) - c
            terms = new
        for e, c in terms.items():
            poly[e] = poly.get(e, 0.0) + c
    return {e: c for e, c in poly.items() if c != 0.0}


def poly_mul(p, q):
    out = {}
    for e1, c1 in p.items():
        for e2, c2 in q.items():
            e = tuple(x + y for x, y in zip(e1, e2))
            out[e] = out.get(e, 0.0) + c1 * c2
    return out


def raw_moments(poly, a, scheme, qt, jmax):
    memo = {}
    out, pj = [1.0], {tuple([0] * len(a)): 1.0}
    for _ in range(jmax):
        pj = poly_mul(pj, poly)
        out.append(sum(c * qt ** sum(e) * coeff_moment(a, e, scheme, memo)
                       for e, c in pj.items()))
    return out


def write_model(d, n, model_t, factors, testing, qt, concentration, need):
    os.makedirs(os.path.join(d, "basic-events"))
    os.makedirs(os.path.join(d, "fault-trees"))
    dump = lambda p, o: open(os.path.join(d, p), "w").write(yaml.safe_dump(o, sort_keys=True))
    members = [f"BE-M{i + 1}" for i in range(n)]
    dump("model.yaml", {
        "schema_version": "0.1.0",
        "model": {"id": "CCF-UNC-TEST", "name": "fixture", "risk_metrics": []},
        "includes": {"parameters": ["parameters.yaml"], "basic_events": ["basic-events/*.yaml"],
                     "fault_trees": ["fault-trees/*.yaml"], "house_events": ["house-events.yaml"],
                     "ccf_groups": ["ccf-groups.yaml"]}})
    dump("parameters.yaml", {"parameters": {}})
    dump("house-events.yaml", {"house_events": {}})
    dump("basic-events/be.yaml", {"basic_events": {
        m: {"label": m, "provenance": PROV,
            "failure_model": {"type": "probability",
                              "value": {"value": 1e-3, "unit": "per_demand"}}}
        for m in members}})
    top = ({"and": members} if need == n else {"atleast": {"k": need, "of": members}})
    dump("fault-trees/ft.yaml", {"fault_trees": {"FT-T": {
        "label": "t", "top_gate": "GT-TOP", "gates": {"GT-TOP": {"label": "t", "formula": top}}}}})
    g = {"label": "group", "model": model_t, "members": members,
         "total_probability": {"value": qt, "unit": "per_demand"},
         "factors": factors, "provenance": PROV}
    if model_t == "alpha-factor":
        g["testing"] = testing
    if concentration is not None:
        g["factor_uncertainty"] = {"distribution": "dirichlet", "concentration": concentration}
    dump("ccf-groups.yaml", {"ccf_groups": {"CCF-T": g}})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    # the quadrature against the closed form (every weight 1)
    for aa, mm in (([18.0, 1.4, 0.6], [2, 1, 1]), ([0.5, 2.0], [3, 0]), ([5.0, 0.2, 0.1, 0.05], [1, 1, 0, 2])):
        closed = 1.0
        for ak, mk in zip(aa, mm):
            closed *= rising(ak, mk)
        closed /= rising(sum(aa), sum(mm))
        quad = weighted_moment(aa, mm, [1.0] * len(aa))
        check(abs(quad - closed) <= 1e-9 * closed,
              f"quadrature self-check a={aa} m={mm}: {quad:.12e} vs {closed:.12e}")

    cases = [
        # (label, n, model, factors, testing, Qt, N, members needed)
        ("alpha 3, staggered, all fail", 3, "alpha-factor",
         {"alpha_1": 0.9, "alpha_2": 0.07, "alpha_3": 0.03}, "staggered", 0.05, 20.0, 3),
        ("alpha 3, non-staggered, 2 of 3", 3, "alpha-factor",
         {"alpha_1": 0.9, "alpha_2": 0.07, "alpha_3": 0.03}, "non-staggered", 0.05, 20.0, 2),
        ("alpha 4, staggered, 3 of 4, small N", 4, "alpha-factor",
         {"alpha_1": 0.85, "alpha_2": 0.08, "alpha_3": 0.05, "alpha_4": 0.02}, "staggered", 0.1, 4.0, 3),
        ("beta 2, all fail", 2, "beta-factor", {"beta": 0.1}, "staggered", 0.05, 10.0, 2),
    ]
    tmp = tempfile.mkdtemp(prefix="psa-ccfunc-")
    try:
        for label, n, model_t, factors, testing, qt, conc, need in cases:
            d0 = os.path.join(tmp, f"point-{len(os.listdir(tmp))}")
            write_model(d0, n, model_t, factors, testing, qt, None, need)
            d = os.path.join(tmp, f"unc-{len(os.listdir(tmp))}")
            write_model(d, n, model_t, factors, testing, qt, conc, need)
            v = subprocess.run([sys.executable, os.path.join(HERE, "validate.py"), d,
                                os.path.join(ROOT, "schema", "psa-model.schema.json")],
                               capture_output=True, text=True)
            check(v.returncode == 0, f"{label}: validates ({v.stdout.strip().splitlines()[-1:]})")
            p0 = subprocess.run([a.engine, d0, "FT-T", "--json"], capture_output=True, text=True)
            p1 = subprocess.run([a.engine, d, "FT-T", "--json"], capture_output=True, text=True)
            check(p0.returncode == 0 and p0.stdout == p1.stdout,
                  f"{label}: point results unchanged by factor_uncertainty")
            r = subprocess.run([a.engine, d, "FT-T", "--json", "--prob-only", "--samples",
                                str(N_SAMPLES), "--seed", "20260708", "--keep-samples"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                failures.append(f"{label}: engine failed: {r.stderr}")
                continue
            j = json.loads(r.stdout)
            u = j["uncertainty"]
            draws = u["draws"]
            keys = sorted(q["key"] for q in u.get("quantities", []))
            if model_t == "alpha-factor":
                want_keys = [f"CCF-T/alpha_{k}" for k in range(1, n + 1)]
            else:
                want_keys = ["CCF-T/alpha_1", f"CCF-T/alpha_{n}"]
            check(keys == sorted(want_keys), f"{label}: sampled quantities {keys}")
            # exact moments
            if model_t == "alpha-factor":
                al = [factors[f"alpha_{k}"] for k in range(1, n + 1)]
            else:
                al = [1 - factors["beta"]] + [0.0] * (n - 2) + [factors["beta"]]
            s = sum(al)
            aa = [conc * x / s for x in al]
            scheme = "staggered" if model_t == "beta-factor" else testing
            poly = poly_all_fail(n, need)
            mo = raw_moments(poly, aa, scheme, qt, 4)
            mean = mo[1]
            var = mo[2] - mean ** 2
            mu4 = mo[4] - 4 * mean * mo[3] + 6 * mean ** 2 * mo[2] - 3 * mean ** 4
            nn = len(draws)
            smean = math.fsum(draws) / nn
            svar = math.fsum((x - smean) ** 2 for x in draws) / (nn - 1)
            se_mean = math.sqrt(var / nn)
            se_var = math.sqrt(max(mu4 - var ** 2, 0.0) / nn)
            check(abs(smean - mean) <= 6 * se_mean,
                  f"{label}: MC mean {smean:.6e} vs exact {mean:.6e} "
                  f"({abs(smean - mean) / se_mean:.2f} SE)")
            check(abs(svar - var) <= 6 * se_var,
                  f"{label}: MC variance {svar:.6e} vs exact {var:.6e} "
                  f"({abs(svar - var) / se_var:.2f} SE)")
            check(abs(u["mean"] - smean) <= 1e-12 * smean, f"{label}: reported mean = draws' mean")

        # refusals (engine; the validator's own cases are in test_validate.py)
        d = os.path.join(tmp, "bad")
        write_model(d, 3, "alpha-factor", {"alpha_1": 0.9, "alpha_2": 0.07, "alpha_3": 0.03},
                    "staggered", 0.05, 20.0, 3)
        path = os.path.join(d, "ccf-groups.yaml")
        base = yaml.safe_load(open(path))
        for block, why in (({"distribution": "dirichlet", "concentration": 0}, "concentration 0"),
                           ({"distribution": "dirichlet", "concentration": -2}, "negative concentration"),
                           ({"distribution": "lognormal", "error_factor": 3}, "not a Dirichlet"),
                           ({"distribution": "dirichlet"}, "missing concentration")):
            c = json.loads(json.dumps(base))
            c["ccf_groups"]["CCF-T"]["factor_uncertainty"] = block
            open(path, "w").write(yaml.safe_dump(c))
            r = subprocess.run([a.engine, d, "FT-T", "--samples", "10"], capture_output=True, text=True)
            check(r.returncode != 0, f"engine refuses factor_uncertainty with {why}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("ccf factor uncertainty: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
