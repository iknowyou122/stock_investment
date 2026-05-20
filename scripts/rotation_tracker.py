"""Sector Rotation Radar — reads last N heat snapshots, classifies sector states,
and scores downstream rotation candidates using config/rotation_map.json.

Output: data/market_heat/rotation_signal.json

Usage:
    python scripts/rotation_tracker.py
    python scripts/rotation_tracker.py --date 2026-05-20
    make rotation
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

_ROOT = Path(__file__).resolve().parents[1]
_HEAT_DIR = _ROOT / "data" / "market_heat"
_MAP_PATH = _ROOT / "config" / "rotation_map.json"

# Number of recent snapshots to analyse for trend
_WINDOW = 10

SectorState = Literal["HOT", "COOLING", "EMERGING", "COLD", "STABLE"]


@dataclass
class NodeSnapshot:
    key: str
    label: str
    node_type: str          # "industry" | "concept"
    rank_pcts: list[float]  # newest first, up to _WINDOW entries
    ret_5d_pcts: list[float]
    acceleration_pcts: list[float]

    @property
    def latest_rank_pct(self) -> float:
        return self.rank_pcts[0] if self.rank_pcts else 50.0

    @property
    def rank_delta(self) -> float:
        """Change in rank percentile vs WINDOW sessions ago."""
        if len(self.rank_pcts) < 2:
            return 0.0
        return self.rank_pcts[0] - self.rank_pcts[-1]

    @property
    def short_rank_delta(self) -> float:
        """Change in rank percentile vs 2 sessions ago (fast signal)."""
        if len(self.rank_pcts) < 2:
            return 0.0
        return self.rank_pcts[0] - self.rank_pcts[min(2, len(self.rank_pcts) - 1)]

    @property
    def state(self) -> SectorState:
        rp = self.latest_rank_pct
        d = self.rank_delta
        sd = self.short_rank_delta
        if rp >= 65 and d >= -5:
            return "HOT"
        if rp >= 65 and d < -5:
            return "COOLING"
        if rp < 40 and sd >= 8:
            return "EMERGING"
        if rp < 35 and d <= 0:
            return "COLD"
        return "STABLE"


@dataclass
class RotationCandidate:
    key: str
    label: str
    node_type: str
    current_state: SectorState
    trigger_nodes: list[str]     # upstream HOT/COOLING nodes pointing here
    trigger_labels: list[str]    # human-readable trigger labels
    score: float                 # weighted sum of edge conf * state bonus
    avg_lag_weeks: float
    note: str                    # combined notes from edges
    current_rank_pct: float
    rank_delta: float


def _load_rotation_map() -> tuple[dict, list[dict]]:
    if not _MAP_PATH.exists():
        return {}, []
    with open(_MAP_PATH, encoding="utf-8") as f:
        rm = json.load(f)
    return rm.get("nodes", {}), rm.get("edges", [])


def _load_snapshots(heat_dir: Path, n: int = _WINDOW) -> list[dict]:
    """Load up to n most-recent heat_*.json files, newest first."""
    files = sorted(heat_dir.glob("heat_*.json"))[-n:]
    snaps = []
    for fp in reversed(files):
        try:
            with open(fp, encoding="utf-8") as f:
                snaps.append(json.load(f))
        except Exception:
            pass
    return snaps


def _load_concept_snapshots(heat_dir: Path, n: int = _WINDOW) -> list[dict]:
    files = sorted(heat_dir.glob("concept_heat_*.json"))[-n:]
    snaps = []
    for fp in reversed(files):
        try:
            with open(fp, encoding="utf-8") as f:
                snaps.append(json.load(f))
        except Exception:
            pass
    return snaps


def build_node_snapshots(
    heat_snaps: list[dict],
    concept_snaps: list[dict],
    nodes: dict,
) -> dict[str, NodeSnapshot]:
    """Build NodeSnapshot for every node using historical data."""
    ns: dict[str, NodeSnapshot] = {}

    # Industry nodes
    for key, meta in nodes.items():
        if meta["type"] != "industry":
            continue
        label = meta["label"]
        rank_pcts, ret5s, accels = [], [], []
        for snap in heat_snaps:
            ind = snap.get("industries", {}).get(key)
            if ind:
                rank_pcts.append(float(ind.get("rank_pct", 50)))
                ret5s.append(float(ind.get("ret_5d_pct", 0)))
                accels.append(float(ind.get("acceleration_pct", 0)))
        ns[key] = NodeSnapshot(key, label, "industry", rank_pcts, ret5s, accels)

    # Concept nodes
    for key, meta in nodes.items():
        if meta["type"] != "concept":
            continue
        label = meta["label"]
        rank_pcts, ret5s, accels = [], [], []
        for snap in concept_snaps:
            c = snap.get("concepts", {}).get(key)
            if c:
                rank_pcts.append(float(c.get("rank_pct", 50)))
                ret5s.append(float(c.get("ret_5d_pct", 0)))
                accels.append(float(c.get("acceleration_pct", 0)))
        ns[key] = NodeSnapshot(key, label, "concept", rank_pcts, ret5s, accels)

    return ns


def score_rotation_candidates(
    node_snaps: dict[str, NodeSnapshot],
    edges: list[dict],
) -> list[RotationCandidate]:
    """Score each target node based on HOT/COOLING upstream nodes."""

    # Aggregate upstream signals for each target
    target_signals: dict[str, list[dict]] = {}
    for e in edges:
        src = e["from"]
        tgt = e["to"]
        if src not in node_snaps or tgt not in node_snaps:
            continue
        src_snap = node_snaps[src]
        state = src_snap.state
        # Only HOT or COOLING upstream nodes are meaningful triggers
        if state not in ("HOT", "COOLING"):
            continue
        conf = float(e.get("conf", 0.5))
        lag = int(e.get("lag_weeks", 2))
        note = e.get("note", "")
        state_bonus = 1.0 if state == "HOT" else 0.6
        target_signals.setdefault(tgt, []).append({
            "src": src,
            "src_label": node_snaps[src].label,
            "conf": conf,
            "lag": lag,
            "note": note,
            "state_bonus": state_bonus,
            "weighted_score": conf * state_bonus,
        })

    candidates: list[RotationCandidate] = []
    for tgt_key, signals in target_signals.items():
        if tgt_key not in node_snaps:
            continue
        tgt_snap = node_snaps[tgt_key]
        tgt_state = tgt_snap.state

        # Skip targets already at peak (HOT + positive delta = no upside)
        if tgt_state == "HOT" and tgt_snap.rank_delta >= 5:
            continue

        total_score = sum(s["weighted_score"] for s in signals)
        avg_lag = sum(s["lag"] for s in signals) / len(signals)
        triggers = [s["src"] for s in signals]
        trigger_labels = [s["src_label"] for s in signals]
        notes = list({s["note"] for s in signals if s["note"]})

        # Bonus if target itself is EMERGING
        if tgt_state == "EMERGING":
            total_score *= 1.3

        candidates.append(RotationCandidate(
            key=tgt_key,
            label=tgt_snap.label,
            node_type=tgt_snap.node_type,
            current_state=tgt_state,
            trigger_nodes=triggers,
            trigger_labels=trigger_labels,
            score=round(total_score, 3),
            avg_lag_weeks=round(avg_lag, 1),
            note="；".join(notes[:3]),
            current_rank_pct=round(tgt_snap.latest_rank_pct, 1),
            rank_delta=round(tgt_snap.rank_delta, 1),
        ))

    candidates.sort(key=lambda c: -c.score)
    return candidates


def build_rotation_signal(
    snapshot_date: date,
    node_snaps: dict[str, NodeSnapshot],
    candidates: list[RotationCandidate],
    top_n: int = 8,
) -> dict:
    """Serialise rotation signal to dict for JSON output."""
    hot = [k for k, n in node_snaps.items() if n.state == "HOT"]
    cooling = [k for k, n in node_snaps.items() if n.state == "COOLING"]
    emerging = [k for k, n in node_snaps.items() if n.state == "EMERGING"]

    def _node_info(key: str) -> dict:
        n = node_snaps[key]
        return {
            "key": key,
            "label": n.label,
            "type": n.node_type,
            "state": n.state,
            "rank_pct": round(n.latest_rank_pct, 1),
            "rank_delta": round(n.rank_delta, 1),
        }

    top_candidates = [
        {
            "key": c.key,
            "label": c.label,
            "type": c.node_type,
            "state": c.current_state,
            "score": c.score,
            "avg_lag_weeks": c.avg_lag_weeks,
            "trigger_labels": c.trigger_labels,
            "note": c.note,
            "rank_pct": c.current_rank_pct,
            "rank_delta": c.rank_delta,
        }
        for c in candidates[:top_n]
    ]

    return {
        "signal_date": str(snapshot_date),
        "hot_nodes": [_node_info(k) for k in hot],
        "cooling_nodes": [_node_info(k) for k in cooling],
        "emerging_nodes": [_node_info(k) for k in emerging],
        "rotation_candidates": top_candidates,
        "analysis_window_snapshots": len(list(_HEAT_DIR.glob("heat_*.json"))),
    }


def run(target_date: date | None = None, quiet: bool = False) -> dict:
    def _log(msg: str) -> None:
        if not quiet:
            print(msg)

    if not _HEAT_DIR.exists():
        _log("No heat snapshots found — run make heat-update first")
        return {}

    nodes, edges = _load_rotation_map()
    if not nodes:
        _log(f"Rotation map not found at {_MAP_PATH}")
        return {}

    heat_snaps = _load_snapshots(_HEAT_DIR)
    concept_snaps = _load_concept_snapshots(_HEAT_DIR)
    if not heat_snaps:
        _log("No heat snapshots available")
        return {}

    snap_date = target_date or date.fromisoformat(heat_snaps[0].get("snapshot_date", str(date.today())))
    _log(f"  Rotation tracker — {len(heat_snaps)} heat + {len(concept_snaps)} concept snapshots")

    node_snaps = build_node_snapshots(heat_snaps, concept_snaps, nodes)
    candidates = score_rotation_candidates(node_snaps, edges)
    signal = build_rotation_signal(snap_date, node_snaps, candidates)

    out_path = _HEAT_DIR / f"rotation_signal.json"
    _HEAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    _log(f"  Saved → {out_path}")

    # Print summary
    hot_labels = [node_snaps[k].label for k in [n["key"] for n in signal["hot_nodes"]]]
    cand_labels = [f'{c["label"]}({c["score"]:.2f})' for c in signal["rotation_candidates"][:5]]
    _log(f"  🔥 HOT ({len(signal['hot_nodes'])}): {', '.join(hot_labels[:6])}")
    _log(f"  📡 降溫中 ({len(signal['cooling_nodes'])}): " +
         ", ".join(node_snaps[n["key"]].label for n in signal["cooling_nodes"][:4]))
    _log(f"  ➡️  輪動候選: {', '.join(cand_labels)}")

    return signal


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description="Sector Rotation Tracker")
    p.add_argument("--date", default=None, help="Target date YYYY-MM-DD (default: latest snapshot)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()
    target = date.fromisoformat(args.date) if args.date else None
    signal = run(target, quiet=args.quiet)
    return 0 if signal else 1


if __name__ == "__main__":
    sys.exit(main())
