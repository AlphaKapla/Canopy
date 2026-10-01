#!/usr/bin/env python3
"""Tests for ci/import_mef.py beyond fault trees (FR-15): CCF groups, event
trees, untyped references, and every refusal rule — on small hand-written
MEF files with hand-computed results, through the validator and the
engine.

Usage: python ci/test_import_mef.py [--engine PATH]
"""
import argparse
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
SCHEMA = os.path.join(ROOT, "schema", "psa-model.schema.json")

# Two trains, each valve OR pump; untyped <event> references to gates and
# basic events; beta-factor group on the pumps (bare <factor>), alpha-factor
# group on the valves (<factors>). Non-staggered (MEF convention):
#   pumps  Qt 0.1, beta 0.2      -> Q1 = 0.08, Q2 = 0.02
#   valves Qt 0.05, alpha 0.9/0.1 -> alpha_t = 1.1,
#          Q1 = 0.9/1.1 * 0.05, Q2 = 2 * 0.1/1.1 * 0.05
TRAINS = """<?xml version="1.0"?>
<opsa-mef>
  <define-fault-tree name="Trains">
    <define-gate name="Top"><and><event name="TrainA"/><event name="TrainB"/></and></define-gate>
    <define-gate name="TrainA"><or><event name="ValveA"/><event name="PumpA"/></or></define-gate>
    <define-gate name="TrainB"><or><event name="ValveB"/><event name="PumpB"/></or></define-gate>
  </define-fault-tree>
  <define-CCF-group name="Pumps" model="beta-factor">
    <members><basic-event name="PumpA"/><basic-event name="PumpB"/></members>
    <distribution><float value="0.1"/></distribution>
    <factor level="2"><float value="0.2"/></factor>
  </define-CCF-group>
  <define-CCF-group name="Valves" model="alpha-factor">
    <members><basic-event name="ValveA"/><basic-event name="ValveB"/></members>
    <distribution><float value="0.05"/></distribution>
    <factors>
      <factor level="1"><float value="0.9"/></factor>
      <factor level="2"><float value="0.1"/></factor>
    </factors>
  </define-CCF-group>
</opsa-mef>
"""


def trains_p_top():
    q1p, q2p = 0.8 * 0.1, 0.2 * 0.1
    at = 0.9 + 2 * 0.1
    q1v, q2v = 0.9 / at * 0.05, 2 * 0.1 / at * 0.05
    p_common = 1 - (1 - q2p) * (1 - q2v)
    p_train = 1 - (1 - q1p) * (1 - q1v)
    return p_common + (1 - p_common) * p_train * p_train


# Event tree: initiator frequency 1e-2 /yr; F1 collects gate G1 = a or b,
# F2 collects the basic event c directly (-> pass-through gate), and the
# end state "Damage" is reached by two paths. P(a) = 0.1, P(b) = 0.2,
# P(c) = 0.3. G1 = 1 - 0.9*0.8 = 0.28.
#   F1 fails                     -> Damage  0.28          (F2 bypassed)
#   F1 works, F2 fails           -> Damage  0.72 * 0.3
#   F1 works, F2 works           -> Safe    0.72 * 0.7
ET = """<?xml version="1.0"?>
<opsa-mef>
  <define-initiating-event name="Init" event-tree="Tree"><float value="0.01"/></define-initiating-event>
  <define-event-tree name="Tree">
    <define-functional-event name="F1"/>
    <define-functional-event name="F2"/>
    <define-sequence name="Safe"/>
    <define-sequence name="Damage"/>
    <initial-state>
      <fork functional-event="F1">
        <path state="works">
          <collect-formula><not><gate name="G1"/></not></collect-formula>
          <fork functional-event="F2">
            <path state="works">
              <collect-formula><not><basic-event name="c"/></not></collect-formula>
              <sequence name="Safe"/>
            </path>
            <path state="fails">
              <collect-formula><basic-event name="c"/></collect-formula>
              <sequence name="Damage"/>
            </path>
          </fork>
        </path>
        <path state="fails">
          <collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>
        </path>
      </fork>
    </initial-state>
  </define-event-tree>
  <define-fault-tree name="Support">
    <define-gate name="G1"><or><basic-event name="a"/><basic-event name="b"/></or></define-gate>
  </define-fault-tree>
  <model-data>
    <define-basic-event name="a"><float value="0.1"/></define-basic-event>
    <define-basic-event name="b"><float value="0.2"/></define-basic-event>
    <define-basic-event name="c"><float value="0.3"/></define-basic-event>
  </model-data>
</opsa-mef>
"""


# FR-51. Split fractions, a named branch used twice, parameters and
# arithmetic, an initiator frequency given as an expression; no fault tree
# at all (D-27: that crashed the importer). Alarm works 0.8 / fails 0.2;
# Detect yes 0.9 / no 1 - 0.9; Suppress 0.75 / 0.25 in the named branch,
# 0.4 / 0.6 after a missed detection (a second Canopy functional event).
# The named branch lists its failure path first: state names, not order,
# pick the failure fraction.
#   works, yes  -> branch: Contained 0.8*0.9*0.75, Damage 0.8*0.9*0.25
#   works, no   -> Contained 0.8*0.1*0.4, Damage 0.8*0.1*0.6
#   fails       -> branch: Contained 0.2*0.75, Damage 0.2*0.25
# Contained 0.722, Damage 0.278; initiator 0.02 * 0.5 = 0.01 /yr.
FIRE = """<?xml version="1.0"?>
<opsa-mef>
  <define-initiating-event name="Fire" event-tree="FireTree">
    <mul><parameter name="fire-rate"/><float value="0.5"/></mul>
  </define-initiating-event>
  <define-event-tree name="FireTree">
    <define-functional-event name="Alarm"/>
    <define-functional-event name="Detect"/>
    <define-functional-event name="Suppress"/>
    <define-sequence name="Contained"/>
    <define-sequence name="Damage"/>
    <define-branch name="try-suppress">
      <fork functional-event="Suppress">
        <path state="failure"><collect-expression><float value="0.25"/></collect-expression><sequence name="Damage"/></path>
        <path state="success"><collect-expression><float value="0.75"/></collect-expression><sequence name="Contained"/></path>
      </fork>
    </define-branch>
    <initial-state>
      <fork functional-event="Alarm">
        <path state="works">
          <collect-expression><float value="0.8"/></collect-expression>
          <fork functional-event="Detect">
            <path state="yes">
              <collect-expression><parameter name="p-detect"/></collect-expression>
              <branch name="try-suppress"/>
            </path>
            <path state="no">
              <collect-expression><sub><float value="1"/><parameter name="p-detect"/></sub></collect-expression>
              <fork functional-event="Suppress">
                <path state="success"><collect-expression><float value="0.4"/></collect-expression><sequence name="Contained"/></path>
                <path state="failure"><collect-expression><float value="0.6"/></collect-expression><sequence name="Damage"/></path>
              </fork>
            </path>
          </fork>
        </path>
        <path state="fails">
          <collect-expression><float value="0.2"/></collect-expression>
          <branch name="try-suppress"/>
        </path>
      </fork>
    </initial-state>
  </define-event-tree>
  <model-data>
    <define-parameter name="fire-rate"><float value="0.02"/></define-parameter>
    <define-parameter name="p-detect"><div><float value="9"/><float value="10"/></div></define-parameter>
  </model-data>
</opsa-mef>
"""

# FR-51. Two files, one model: private elements and scoped references
# (PumpTrain's own private gate and event; the whole ValveTrain tree
# private; a dotted reference across trees; public model-data events
# found from inside a tree), a house event without <constant> (the MEF
# default, false: D-27 crashed on it), and an event-tree link. "motor"
# is both PumpTrain's private event (0.1, found first from inside
# PumpTrain) and a public one (0.5, what ValveTrain sees). power =
# grid & diesel = 0.15 is shared by both functional events:
#   P(pump fails) = 1 - 0.9*0.85                          = 0.235  Melt
#   pump ok, valve ok:    0.9*0.85*0.8*0.5 (power false)   = 0.306  Safe
#   pump ok, valve fails: 0.9*0.85*(1 - 0.8*0.5)           = 0.459  LateMelt
# (a product of marginals would give 0.765*0.34 for Safe), x 1e-3 /yr.
PLANT_FT = """<?xml version="1.0"?>
<opsa-mef>
  <define-fault-tree name="PumpTrain">
    <define-gate name="top" role="private"><or><basic-event name="motor"/><gate name="power"/></or></define-gate>
    <define-gate name="power" role="private"><and><basic-event name="grid"/><basic-event name="diesel"/></and></define-gate>
    <define-basic-event name="motor" role="private"><float value="0.1"/></define-basic-event>
  </define-fault-tree>
  <define-fault-tree name="ValveTrain" role="private">
    <define-gate name="top"><or><basic-event name="stem"/><gate name="PumpTrain.power"/><basic-event name="motor"/><house-event name="maintenance"/></or></define-gate>
    <define-basic-event name="stem"><float value="0.2"/></define-basic-event>
  </define-fault-tree>
  <model-data>
    <define-basic-event name="grid"><float value="0.3"/></define-basic-event>
    <define-basic-event name="diesel"><float value="0.5"/></define-basic-event>
    <define-basic-event name="motor"><float value="0.5"/></define-basic-event>
    <define-house-event name="maintenance"/>
  </model-data>
</opsa-mef>
"""
PLANT_ET = """<?xml version="1.0"?>
<opsa-mef>
  <define-initiating-event name="LOCA" event-tree="Injection"><float value="0.001"/></define-initiating-event>
  <define-event-tree name="Injection">
    <define-functional-event name="Pump"/>
    <define-sequence name="Cooled"><event-tree name="Recirculation"/></define-sequence>
    <define-sequence name="Melt"/>
    <initial-state>
      <fork functional-event="Pump">
        <path state="ok"><collect-formula><not><gate name="PumpTrain.top"/></not></collect-formula><sequence name="Cooled"/></path>
        <path state="failed"><collect-formula><gate name="PumpTrain.top"/></collect-formula><sequence name="Melt"/></path>
      </fork>
    </initial-state>
  </define-event-tree>
  <define-event-tree name="Recirculation">
    <define-functional-event name="Valve"/>
    <define-sequence name="Safe"/>
    <define-sequence name="LateMelt"/>
    <initial-state>
      <fork functional-event="Valve">
        <path state="ok"><collect-formula><not><gate name="ValveTrain.top"/></not></collect-formula><sequence name="Safe"/></path>
        <path state="failed"><collect-formula><gate name="ValveTrain.top"/></collect-formula><sequence name="LateMelt"/></path>
      </fork>
    </initial-state>
  </define-event-tree>
</opsa-mef>
"""

# D-28: a functional event of the same name in two trees, each collecting
# a basic event directly, shared one pass-through gate (T1 got T2's
# formula). T1: Bad = P(c) = 0.1; T2: Bad = P(d) = 0.3. T3 (FR-51): B
# collects b after A works and c after A fails (two functional events):
#   ~a ~b OK 0.72, ~a b Bad 0.18, a ~c OK 0.09, a c Bad 0.01.
SAME_NAMES = """<?xml version="1.0"?>
<opsa-mef>
  <define-initiating-event name="I1" event-tree="T1"><float value="1"/></define-initiating-event>
  <define-initiating-event name="I2" event-tree="T2"><float value="1"/></define-initiating-event>
  <define-initiating-event name="I3" event-tree="T3"><float value="1"/></define-initiating-event>
  <define-event-tree name="T1">
    <define-functional-event name="F"/>
    <define-sequence name="OK"/><define-sequence name="Bad"/>
    <initial-state>
      <fork functional-event="F">
        <path state="works"><collect-formula><not><basic-event name="c"/></not></collect-formula><sequence name="OK"/></path>
        <path state="fails"><collect-formula><basic-event name="c"/></collect-formula><sequence name="Bad"/></path>
      </fork>
    </initial-state>
  </define-event-tree>
  <define-event-tree name="T2">
    <define-functional-event name="F"/>
    <initial-state>
      <fork functional-event="F">
        <path state="works"><collect-formula><not><basic-event name="d"/></not></collect-formula><sequence name="OK"/></path>
        <path state="fails"><collect-formula><basic-event name="d"/></collect-formula><sequence name="Bad"/></path>
      </fork>
    </initial-state>
  </define-event-tree>
  <define-event-tree name="T3">
    <define-functional-event name="A"/>
    <define-functional-event name="B"/>
    <initial-state>
      <fork functional-event="A">
        <path state="works"><collect-formula><not><basic-event name="a"/></not></collect-formula>
          <fork functional-event="B">
            <path state="works"><collect-formula><not><basic-event name="b"/></not></collect-formula><sequence name="OK"/></path>
            <path state="fails"><collect-formula><basic-event name="b"/></collect-formula><sequence name="Bad"/></path>
          </fork>
        </path>
        <path state="fails"><collect-formula><basic-event name="a"/></collect-formula>
          <fork functional-event="B">
            <path state="works"><collect-formula><not><basic-event name="c"/></not></collect-formula><sequence name="OK"/></path>
            <path state="fails"><collect-formula><basic-event name="c"/></collect-formula><sequence name="Bad"/></path>
          </fork>
        </path>
      </fork>
    </initial-state>
  </define-event-tree>
  <model-data>
    <define-basic-event name="a"><float value="0.1"/></define-basic-event>
    <define-basic-event name="b"><float value="0.2"/></define-basic-event>
    <define-basic-event name="c"><float value="0.1"/></define-basic-event>
    <define-basic-event name="d"><float value="0.3"/></define-basic-event>
  </model-data>
</opsa-mef>
"""


# FR-52. Distributions and exponential failure models, mission time 100 h.
# The pumps share one lognormal rate (a parameter: one sample per trial for
# both); pump B runs for twice the mission time; the valve's probability
# is a beta parameter, the sensor's an inline gamma. Point values are the
# means: P(pump A) = 1 - exp(-1e-4*100), P(pump B) = 1 - exp(-1e-4*200),
# P(valve) = 1/(1+99), P(sensor) = 2*0.005;
# top = both pumps | valve | sensor.
DISTS = """<?xml version="1.0"?>
<opsa-mef>
  <define-fault-tree name="Pumps">
    <define-gate name="top"><or><gate name="both"/><basic-event name="valve"/><basic-event name="sensor"/></or></define-gate>
    <define-gate name="both"><and><basic-event name="pumpA"/><basic-event name="pumpB"/></and></define-gate>
    <define-basic-event name="pumpA"><exponential><parameter name="lambda-pump"/><system-mission-time/></exponential></define-basic-event>
    <define-basic-event name="pumpB"><exponential><parameter name="lambda-pump"/><mul><int value="2"/><system-mission-time/></mul></exponential></define-basic-event>
    <define-basic-event name="valve"><parameter name="p-valve"/></define-basic-event>
    <define-basic-event name="sensor"><gamma-deviate><float value="2"/><float value="0.005"/></gamma-deviate></define-basic-event>
    <define-parameter name="lambda-pump" unit="hours-1"><lognormal-deviate><float value="1e-4"/><float value="3"/><float value="0.95"/></lognormal-deviate></define-parameter>
    <define-parameter name="p-valve"><beta-deviate><float value="1"/><float value="99"/></beta-deviate></define-parameter>
  </define-fault-tree>
</opsa-mef>
"""


def dists_p_top():
    pa, pb = 1 - math.exp(-1e-4 * 100), 1 - math.exp(-1e-4 * 200)
    return 1 - (1 - pa * pb) * (1 - 0.01) * (1 - 0.01)


# FR-52. Reparameterized lognormals and a uniform, as basic-event
# probabilities: (mu, sigma) -> mean exp(mu + sigma^2/2), error factor
# exp(z95 sigma); an error factor 2 at level 0.9 -> 2^(z95/z90) at 0.95.
SHAPES = """<?xml version="1.0"?>
<opsa-mef>
  <define-fault-tree name="Shapes">
    <define-gate name="top"><or><basic-event name="a"/><basic-event name="b"/><basic-event name="c"/></or></define-gate>
    <define-basic-event name="a"><lognormal-deviate><float value="-7"/><float value="0.5"/></lognormal-deviate></define-basic-event>
    <define-basic-event name="b"><lognormal-deviate><float value="1e-3"/><float value="2"/><float value="0.9"/></lognormal-deviate></define-basic-event>
    <define-basic-event name="c"><uniform-deviate><float value="0.01"/><float value="0.03"/></uniform-deviate></define-basic-event>
  </define-fault-tree>
</opsa-mef>
"""


def variant(text, old, new):
    assert old in text, old
    return text.replace(old, new, 1)


def et_variant(old, new):
    assert old in ET, old
    return ET.replace(old, new, 1)


REFUSALS = [
    ("fork with one path", et_variant(
        """        <path state="fails">
          <collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>
        </path>
      </fork>
    </initial-state>""", """      </fork>
    </initial-state>"""), "has 1 paths"),
    ("paths not complementary", et_variant(
        """<collect-formula><gate name="G1"/></collect-formula>
          <sequence name="Damage"/>""",
        """<collect-formula><basic-event name="a"/></collect-formula>
          <sequence name="Damage"/>"""), "does not collect a formula and its negation"),
    ("set-house-event instruction", et_variant(
        """<sequence name="Safe"/>
            </path>""", """<set-house-event name="h"><constant value="true"/></set-house-event>
              <sequence name="Safe"/>
            </path>"""), "<set-house-event> has no Canopy equivalent"),
    ("formula collected outside a fork", et_variant(
        """    <initial-state>
      <fork""", """    <initial-state>
      <collect-formula><gate name="G1"/></collect-formula>
      <fork"""), "formula collected outside a fork path"),
    ("MGL group", TRAINS.replace('model="alpha-factor"', 'model="MGL"'),
     "model 'MGL' not supported"),
    ("an event defined as a gate and as a basic event", TRAINS.replace(
        '<define-gate name="TrainB">', '<define-gate name="PumpA"><or><event name="ValveA"/><event name="ValveB"/></or></define-gate>\n    <define-gate name="TrainB">'),
     "event PumpA defined twice (as gate and as basic event)"),
    ("ambiguous untyped reference in a tree's own scope", variant(
        TRAINS, '<define-gate name="TrainB">',
        '<define-basic-event name="TrainB" role="private"><float value="0.1"/></define-basic-event>\n    <define-gate name="TrainB">'),
     "cannot resolve an untyped reference"),
    # FR-51 refusals
    ("cyclic named branches", variant(
        FIRE, '<collect-expression><float value="0.25"/></collect-expression><sequence name="Damage"/>',
        '<collect-expression><float value="0.25"/></collect-expression><branch name="try-suppress"/>'),
     "cyclic named branches: try-suppress -> try-suppress"),
    ("undefined named branch", variant(
        FIRE, '<branch name="try-suppress"/>', '<branch name="nowhere"/>'),
     "branch nowhere is not defined in this tree"),
    ("collect-formula and collect-expression mixed", variant(variant(
        FIRE, '<collect-expression><float value="0.2"/></collect-expression>',
        '<collect-formula><basic-event name="x"/></collect-formula>'),
        '<model-data>', '<model-data><define-basic-event name="x"><float value="0.2"/></define-basic-event>'),
     "mixes collect-formula and collect-expression"),
    ("fractions not summing to 1", variant(
        FIRE, '<float value="0.25"/>', '<float value="0.3"/>'),
     "sum to 1.05, not 1"),
    ("fraction outside [0,1]", variant(variant(
        FIRE, '<float value="0.25"/>', '<float value="-0.25"/>'),
        '<float value="0.75"/>', '<float value="1.25"/>'),
     "fraction -0.25 on the fork on Suppress is outside [0,1]"),
    ("expression collected outside a fork", variant(
        FIRE, '<initial-state>\n      <fork functional-event="Alarm">',
        '<initial-state>\n      <collect-expression><float value="0.5"/></collect-expression>\n      <fork functional-event="Alarm">'),
     "expression collected outside a fork path"),
    ("fork path collecting nothing", variant(
        FIRE, '<collect-expression><float value="0.8"/></collect-expression>', ''),
     "must collect exactly one formula (or one expression)"),
    ("parameter cycle", variant(
        FIRE, '<define-parameter name="fire-rate"><float value="0.02"/></define-parameter>',
        '<define-parameter name="fire-rate"><parameter name="p-detect"/></define-parameter>').replace(
        '<div><float value="9"/><float value="10"/></div>', '<parameter name="fire-rate"/>'),
     "parameter cycle: "),
    ("division by zero", variant(
        FIRE, '<float value="10"/>', '<float value="0"/>'),
     "division by zero"),
    ("non-constant expression", variant(
        TRAINS, '<distribution><float value="0.1"/></distribution>',
        '<distribution><exponential><float value="1e-3"/><float value="24"/></exponential></distribution>'),
     "unsupported expression <exponential>"),
    ("functional events forked out of declaration order", variant(
        SAME_NAMES, '<define-functional-event name="A"/>\n    <define-functional-event name="B"/>',
        '<define-functional-event name="B"/>\n    <define-functional-event name="A"/>'),
     "fork on B after A: functional events must be forked in their declaration order"),
    ("both paths of a fork in the same state", variant(
        SAME_NAMES, '<path state="fails"><collect-formula><basic-event name="c"/></collect-formula><sequence name="Bad"/>',
        '<path state="works"><collect-formula><basic-event name="c"/></collect-formula><sequence name="Bad"/>'),
     "both paths of the fork on F have state 'works'"),
    ("undefined sequence", variant(
        SAME_NAMES, '<sequence name="Bad"/>', '<sequence name="Nowhere"/>'),
     "sequence Nowhere is not defined"),
    ("two initiating events on one tree", variant(
        SAME_NAMES, '<define-initiating-event name="I2" event-tree="T2">',
        '<define-initiating-event name="I2" event-tree="T1">'),
     "event tree T1: two initiating events (I1, I2)"),
    ("private element referenced from outside by its bare name", variant(
        PLANT_FT + "\x00" + PLANT_ET, '<gate name="PumpTrain.top"/></collect-formula><sequence name="Melt"/>',
        '<gate name="top"/></collect-formula><sequence name="Melt"/>'),
     "gate top referenced but never defined"),
    ("instruction in a sequence definition", variant(
        PLANT_FT + "\x00" + PLANT_ET, '<define-sequence name="Melt"/>',
        '<define-sequence name="Melt"><collect-expression><float value="0.5"/></collect-expression></define-sequence>'),
     "<collect-expression> in a sequence definition has no Canopy equivalent"),
    ("link to an undefined event tree", variant(
        PLANT_FT + "\x00" + PLANT_ET, '<event-tree name="Recirculation"/>', '<event-tree name="Nowhere"/>'),
     "links to undefined event tree Nowhere"),
    ("cyclic event-tree links", variant(
        PLANT_FT + "\x00" + PLANT_ET, '<define-sequence name="Safe"/>',
        '<define-sequence name="Safe"><event-tree name="Injection"/></define-sequence>'),
     "cyclic event-tree links: Injection -> Recirculation -> Injection"),
    ("private element at model scope", variant(
        PLANT_FT, '<define-basic-event name="grid">', '<define-basic-event name="grid" role="private">'),
     "basic event grid: private at model scope"),
    ("CCF total probability outside [0,1]", variant(
        TRAINS, '<distribution><float value="0.1"/></distribution>', '<distribution><float value="1.5"/></distribution>'),
     "CCF group Pumps: total probability 1.5 outside [0,1]"),
    ("negative CCF factor", variant(
        TRAINS, '<factor level="2"><float value="0.2"/></factor>', '<factor level="2"><float value="-0.2"/></factor>'),
     "CCF group Pumps: factor -0.2 outside [0,1]"),
    ("beta factor at the wrong level", variant(
        TRAINS, '<factor level="2"><float value="0.2"/></factor>', '<factor level="1"><float value="0.2"/></factor>'),
     "the beta factor's level must be the number of members (2), got 1"),
    ("component", variant(
        TRAINS, '</define-fault-tree>', '<define-component name="C"/></define-fault-tree>'),
     "components not supported"),
    ("rule", variant(
        TRAINS, '</opsa-mef>', '<define-rule name="R"/></opsa-mef>'),
     "<define-rule> not supported by the importer"),
    # FR-52 refusals
    ("mission time used but not given", DISTS, "give --mission-time HOURS", ()),
    ("normal distribution", variant(
        DISTS, '<gamma-deviate><float value="2"/><float value="0.005"/></gamma-deviate>',
        '<normal-deviate><float value="0.01"/><float value="0.001"/></normal-deviate>'),
     "<normal-deviate> has no Canopy equivalent"),
    ("distribution inside arithmetic", variant(
        DISTS, '<gamma-deviate><float value="2"/><float value="0.005"/></gamma-deviate>',
        '<mul><float value="0.5"/><gamma-deviate><float value="2"/><float value="0.005"/></gamma-deviate></mul>'),
     "the distribution <gamma-deviate> is used as a constant"),
    ("lognormal confidence level below 0.5", variant(
        DISTS, '<float value="3"/><float value="0.95"/>', '<float value="3"/><float value="0.3"/>'),
     "needs a confidence level in (0.5, 1)"),
    ("rate parameter in another unit", variant(
        DISTS, 'name="lambda-pump" unit="hours-1"', 'name="lambda-pump" unit="years-1"'),
     "has unit 'years-1'; only hours-1 imports"),
    ("one distribution as a rate and as a probability", variant(
        DISTS, '<parameter name="p-valve"/></define-basic-event>',
        '<parameter name="lambda-pump"/></define-basic-event>'),
     "parameter Pumps.lambda-pump is used as per_demand here and as per_hour elsewhere"),
    ("exponential with three arguments", variant(
        DISTS, '<system-mission-time/></exponential></define-basic-event>\n    <define-basic-event name="pumpB">',
        '<system-mission-time/><float value="1"/></exponential></define-basic-event>\n    <define-basic-event name="pumpB">'),
     "<exponential> takes (rate, time), got 3 arguments"),
    ("cyclic parameter aliases (D-31: looped forever)", variant(
        DISTS, '<define-parameter name="p-valve"><beta-deviate><float value="1"/><float value="99"/></beta-deviate></define-parameter>',
        '<define-parameter name="p-valve"><parameter name="p-valve-2"/></define-parameter>'
        '<define-parameter name="p-valve-2"><parameter name="p-valve"/></define-parameter>'),
     "parameter cycle: Pumps.p-valve -> Pumps.p-valve-2 -> Pumps.p-valve"),
]


def run(args, **kw):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=300, **kw)
    except subprocess.TimeoutExpired:           # a hang fails the check (D-31)
        return subprocess.CompletedProcess(args, 124, "", "TIMEOUT")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default=os.environ.get(
        "CANOPY_BIN", os.path.join(ROOT, "engine/target/release/canopy")))
    a = ap.parse_args()
    failures = []

    def check(cond, msg):
        (print(f"  ok  {msg}") if cond else failures.append(msg))

    def close(x, y):
        return abs(x - y) <= 1e-14 * max(abs(x), abs(y))

    tmp = tempfile.mkdtemp(prefix="psa-mefimp-")
    try:
        def imp(name, text, *extra):
            """Import `text` (several files when separated by NUL)."""
            xmls = []
            for i, part in enumerate(text.split("\x00"), start=1):
                xmls.append(os.path.join(tmp, f"{name}-{i}.xml"))
                open(xmls[-1], "w").write(part)
            out = os.path.join(tmp, name)
            shutil.rmtree(out, ignore_errors=True)
            return run([sys.executable, os.path.join(HERE, "import_mef.py"), *xmls, out,
                        *extra]), out

        def quantify(out):
            """{tree id: ({end state: frequency}, partition sum)} via quantify.py"""
            res = out + ".json"
            q = run([sys.executable, os.path.join(HERE, "quantify.py"), out, res],
                    env={**os.environ, "CANOPY_BIN": a.engine})
            if q.returncode != 0:
                failures.append(f"quantify {out}: {q.stderr[-300:]}")
                return {}
            return {t: ({e["id"]: e["frequency_per_year"] for e in r["end_states"]},
                        r["partition"]["sum_probability"])
                    for t, r in json.load(open(res)).items()}

        def validates(out, what):
            v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
            check(v.returncode == 0 and "0 error(s), 0 warning(s)" in v.stdout,
                  f"{what} validates without warnings: {v.stdout.strip()[-200:]}")

        # trains: untyped refs + both CCF encodings
        r, out = imp("trains", TRAINS)
        check(r.returncode == 0, f"trains import: {r.stderr}")
        v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
        check(v.returncode == 0, f"trains validates: {v.stdout}")
        e = run([a.engine, out, "FT-MAIN", "--json", "--prob-only"])
        if e.returncode == 0:
            p = json.loads(e.stdout)["probability"]
            check(close(p, trains_p_top()),
                  f"trains P(top) {p} = hand-computed non-staggered {trains_p_top()}")
        else:
            failures.append(f"trains engine: {e.stderr}")
        ccf = open(os.path.join(out, "ccf-groups.yaml")).read()
        check("testing: non-staggered" in ccf and "beta: 0.2" in ccf
              and "alpha_2: 0.1" in ccf, "CCF groups imported non-staggered with factors")

        # event tree
        r, out = imp("tree", ET)
        check(r.returncode == 0 and "note:" not in r.stderr.replace(
            "note: event tree Tree", ""), f"tree import: {r.stderr}")
        v = run([sys.executable, os.path.join(HERE, "validate.py"), out, SCHEMA])
        check(v.returncode == 0 and "0 error(s)" in v.stdout, f"tree validates: {v.stdout}")
        e = run([a.engine, out, "ET-TREE", "--json"])
        if e.returncode == 0:
            j = json.loads(e.stdout)
            rows = {(s["end_state"], tuple(sorted(s["cut_sets"][0]["events"]))
                     if s["cut_sets"] else ()): s["frequency_per_year"]
                    for s in j["sequences"]}
            freq = {s["id"]: (s["end_state"], s["frequency_per_year"]) for s in j["sequences"]}
            check(len(freq) == 3, f"three rows (one per path): {sorted(freq)}")
            dmg = sorted(f for es, f in freq.values() if es == "Damage")
            check(len(dmg) == 2 and close(dmg[0], 0.01 * 0.72 * 0.3)
                  and close(dmg[1], 0.01 * 0.28), f"Damage rows {dmg}")
            metrics = {m["id"]: m["value_per_year"] for m in j["metrics"]}
            check(close(metrics["Damage"], 0.01 * (0.28 + 0.72 * 0.3))
                  and close(metrics["Safe"], 0.01 * 0.72 * 0.7),
                  f"one metric per end state, summing its paths: {metrics}")
            check(close(j["initiating_event"]["frequency_per_year"], 0.01),
                  "initiator frequency taken from the file")
            check(close(j["partition"]["sum_probability"], 1.0), "partition = 1")
        else:
            failures.append(f"tree engine: {e.stderr}")
        et_yaml = open(os.path.join(out, "event-trees", "et-tree.yaml")).read()
        check("GT-FE-F2" in et_yaml, "F2 (collects a basic event) gets a pass-through gate")
        check("bypassed" in et_yaml, "F2 is bypassed on the path where F1 fails")

        # FR-51: split fractions, named branch, parameters; no fault tree (D-27)
        r, out = imp("fire", FIRE)
        check(r.returncode == 0, f"fire import (no fault tree in the file): {r.stderr}")
        if r.returncode == 0:
            validates(out, "fire")
            res = quantify(out).get("ET-FIRETREE")
            if res:
                es, part = res
                check(close(es.get("Contained", math.nan), 0.01 * 0.722) and close(es.get("Damage", math.nan), 0.01 * 0.278),
                      f"fire end states = hand-computed 0.01 x (0.722, 0.278): {es}")
                check(close(part, 1.0), "fire partition = 1")
            tree = yaml.safe_load(open(os.path.join(out, "event-trees", "et-firetree.yaml")))["event_tree"]
            check(list(tree["functional_events"]) ==
                  ["FE-ALARM", "FE-DETECT", "FE-SUPPRESS", "FE-SUPPRESS-2"],
                  f"Suppress, collecting two different fractions, is two functional "
                  f"events: {list(tree['functional_events'])}")
            check(len(tree["sequences"]) == 6, "six rows: the named branch expanded at both uses")
            fr = yaml.safe_load(open(os.path.join(out, "basic-events", "split-fractions.yaml")))["basic_events"]
            probs = {b: e["failure_model"]["value"]["value"] for b, e in fr.items()}
            check(probs == {"BE-SF-FIRETREE-ALARM": 0.2, "BE-SF-FIRETREE-DETECT": 1 - 0.9,
                            "BE-SF-FIRETREE-SUPPRESS": 0.25, "BE-SF-FIRETREE-SUPPRESS-2": 0.6},
                  f"each fork's failure fraction is a basic event (state names decide): {probs}")
            ie = tree["initiating_event"]
            check(close(ie["frequency"]["value"], 0.01)
                  and "evaluated from a MEF expression" in ie["provenance"]["justification"],
                  f"initiator frequency evaluated from <mul> over a parameter, and says so: {ie}")
            check("imported as 2 functional events FE-SUPPRESS, FE-SUPPRESS-2" in r.stderr,
                  "the conversion notes the split functional event")

        # FR-51: two files, private names, house-event default (D-27), a link
        r, out = imp("plant", PLANT_FT + "\x00" + PLANT_ET)
        check(r.returncode == 0, f"plant import (two files): {r.stderr}")
        if r.returncode == 0:
            validates(out, "plant")
            res = quantify(out)
            check(set(res) == {"ET-INJECTION"}, f"the linked-only tree is not quantified "
                  f"standalone: {sorted(res)}")
            if "ET-INJECTION" in res:
                es, part = res["ET-INJECTION"]
                check(close(es.get("Melt", math.nan), 0.235e-3) and close(es.get("Safe", math.nan), 0.306e-3)
                      and close(es.get("LateMelt", math.nan), 0.459e-3),
                      f"plant end states = hand-computed (shared power, link "
                      f"followed on one BDD): {es}")
                check(close(part, 1.0), "plant partition = 1")
            gates = yaml.safe_load(open(os.path.join(out, "fault-trees", "imported.yaml")))[
                "fault_trees"]["FT-MAIN"]["gates"]
            check(gates.get("GT-VALVETRAIN-TOP", {}).get("formula") ==
                  {"or": ["BE-VALVETRAIN-STEM", "GT-PUMPTRAIN-POWER", "BE-MOTOR", "HE-MAINTENANCE"]}
                  and gates.get("GT-PUMPTRAIN-TOP", {}).get("formula") ==
                  {"or": ["BE-PUMPTRAIN-MOTOR", "GT-PUMPTRAIN-POWER"]},
                  "private elements map from their full paths; local, dotted and "
                  "public references resolve as SCRAM does")
            he = yaml.safe_load(open(os.path.join(out, "house-events.yaml")))["house_events"]
            check(he.get("HE-MAINTENANCE", {}).get("default") is False,
                  "a house event without <constant> imports as false (MEF default)")
            e = run([a.engine, out, "ET-INJECTION", "--json", "--house", "HE-MAINTENANCE=true"])
            if e.returncode == 0:
                m = {x["id"]: x["value_per_year"] for x in json.loads(e.stdout)["metrics"]}
                check(close(m.get("LateMelt", math.nan), 0.765e-3) and m.get("Safe") == 0.0,
                      f"the house event reaches the linked tree: {m}")
            else:
                failures.append(f"plant engine --house: {e.stderr}")
            manifest = yaml.safe_load(open(os.path.join(out, "model.yaml")))
            check(sorted(x["id"] for x in manifest["model"]["risk_metrics"]) ==
                  ["LateMelt", "Melt", "Safe"],
                  "metrics for every end state except the link row's own")
            inj = yaml.safe_load(open(os.path.join(out, "event-trees", "et-injection.yaml")))["event_tree"]
            check([s.get("transfer") for s in inj["sequences"].values()] ==
                  ["ET-RECIRCULATION", None], "the linked sequence is a transfer")

        # D-28 + FR-51: same functional-event name in several trees; variants
        r, out = imp("same", SAME_NAMES)
        check(r.returncode == 0, f"same-names import: {r.stderr}")
        if r.returncode == 0:
            validates(out, "same-names")
            res = quantify(out)
            got = {t: res.get(t, ({}, 0))[0].get("Bad") for t in ("ET-T1", "ET-T2", "ET-T3")}
            check(got["ET-T1"] is not None and close(got["ET-T1"], 0.1)
                  and got["ET-T2"] is not None and close(got["ET-T2"], 0.3),
                  f"each tree keeps its own formula (D-28): Bad {got}")
            check(got["ET-T3"] is not None and close(got["ET-T3"], 0.19)
                  and close(res.get("ET-T3", ({}, 0))[0].get("OK", math.nan), 0.81),
                  f"B collecting b or c by branch: Bad 0.19, OK 0.81: {res.get('ET-T3')}")
            t3 = yaml.safe_load(open(os.path.join(out, "event-trees", "et-t3.yaml")))["event_tree"]
            check(list(t3["functional_events"]) == ["FE-A", "FE-B", "FE-B-2"],
                  f"T3 functional events {list(t3['functional_events'])}")

        # FR-52: distributions and exponential failure models
        r, out = imp("dists", DISTS, "--mission-time", "100")
        check(r.returncode == 0, f"distributions import: {r.stderr}")
        if r.returncode == 0:
            validates(out, "distributions")
            e = run([a.engine, out, "FT-MAIN", "--json", "--prob-only"])
            check(e.returncode == 0 and close(json.loads(e.stdout)["probability"], dists_p_top()),
                  f"P(top) at the means = hand-computed {dists_p_top()}")
            pars = yaml.safe_load(open(os.path.join(out, "parameters.yaml")))["parameters"]
            check({k: (v["value"], v["unit"], v["uncertainty"]) for k, v in pars.items()} == {
                "PAR-LAMBDA-PUMP": (1e-4, "per_hour",
                                          {"distribution": "lognormal", "error_factor": 3.0}),
                "PAR-P-VALVE": (0.01, "per_demand",
                                      {"distribution": "beta", "alpha": 1.0, "beta": 99.0})},
                  f"distribution parameters become Canopy parameters at their means: {pars}")
            bes = yaml.safe_load(open(os.path.join(out, "basic-events", "imported.yaml")))["basic_events"]
            fa, fb = bes["BE-PUMPA"]["failure_model"], bes["BE-PUMPB"]["failure_model"]
            check(fa == {"type": "rate-mission", "rate": {"param": "PAR-LAMBDA-PUMP"},
                         "mission_time": {"value": 100.0, "unit": "hour"}}
                  and fb["rate"] == {"param": "PAR-LAMBDA-PUMP"}
                  and fb["mission_time"] == {"value": 200.0, "unit": "hour"},
                  f"exponentials are rate-mission sharing one rate parameter: {fa} {fb}")
            check(bes["BE-SENSOR"]["failure_model"]["value"] ==
                  {"value": 0.01, "unit": "per_demand",
                   "uncertainty": {"distribution": "gamma", "shape": 2.0, "scale": 0.005}},
                  "an inline gamma is the probability's distribution, at its mean")
            check("distribution" in bes["BE-SENSOR"]["provenance"]["justification"]
                  and "rate-mission" in bes["BE-PUMPA"]["provenance"]["justification"],
                  "the provenance says how each value was obtained")
            e = run([a.engine, out, "FT-MAIN", "--json", "--prob-only", "--samples", "2000",
                     "--seed", "1"])
            q = sorted(x["key"] if isinstance(x, dict) else x
                       for x in json.loads(e.stdout)["uncertainty"]["quantities"]) if e.returncode == 0 else []
            check(e.returncode == 0 and len(q) == 3,
                  f"Monte Carlo samples three quantities (the shared rate once): {q}")
        r, out = imp("shapes", SHAPES)
        check(r.returncode == 0, f"lognormal forms and uniform import: {r.stderr}")
        if r.returncode == 0:
            validates(out, "shapes")
            bes = yaml.safe_load(open(os.path.join(out, "basic-events", "imported.yaml")))["basic_events"]
            va = bes["BE-A"]["failure_model"]["value"]
            vb = bes["BE-B"]["failure_model"]["value"]
            vc = bes["BE-C"]["failure_model"]["value"]
            z95 = 1.6448536269514722
            z90 = 1.2815515655446004
            check(close(va["value"], math.exp(-7 + 0.125))
                  and close(va["uncertainty"]["error_factor"], math.exp(z95 * 0.5)),
                  f"lognormal (mu, sigma) as mean and error factor: {va}")
            check(vb["value"] == 1e-3 and close(vb["uncertainty"]["error_factor"], 2 ** (z95 / z90)),
                  f"error factor at level 0.9 converted to 0.95: {vb}")
            check(close(vc["value"], 0.02) and vc["uncertainty"] ==
                  {"distribution": "uniform", "lower": 0.01, "upper": 0.03},
                  f"uniform at its mean: {vc}")

        # SCRAM trims attribute values
        r, out = imp("spaces", variant(TRAINS, '<event name="TrainA"/>', '<event name="  TrainA  "/>'))
        e = run([a.engine, out, "FT-MAIN", "--json", "--prob-only"])
        check(r.returncode == 0 and e.returncode == 0
              and close(json.loads(e.stdout)["probability"], trains_p_top()),
              "names padded with spaces resolve (SCRAM trims attribute values)")

        for name, text, frag, *extra in REFUSALS:
            # every case imports with a mission time unless it says otherwise
            r, _ = imp("refused", text, *(extra[0] if extra else ("--mission-time", "100")))
            check(r.returncode != 0 and frag in r.stderr,
                  f"refused: {name} ({r.stderr.strip()[:90]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        for f in failures:
            print("FAIL:", f)
        return 1
    print("import_mef: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
