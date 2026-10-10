#!/usr/bin/env python3
"""Fetch and deterministically select a private AlpacaEval quality slice."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import urllib.request
from collections import Counter
from pathlib import Path

REVISION = "2edc6fad8be6b14ea7230aabfd08188da6b8b814"
SOURCE_URL = f"https://huggingface.co/datasets/tatsu-lab/alpaca_eval/resolve/{REVISION}/alpaca_eval.json"
ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "test-artifacts" / "uzu-quality-source"
SOURCE_PATH = SOURCE_DIR / "alpaca_eval.json"
OUTPUT_PATH = ROOT / "test-artifacts" / "uzu_quality_100.json"
PROVENANCE_PATH = ROOT / "experiments" / "uzu-rust" / "QUALITY_DATASET_PROVENANCE.json"
SEED = 20261010


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fetch() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    if SOURCE_PATH.exists():
        return
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "meecp-uzu-quality/1"})
    with urllib.request.urlopen(request, timeout=60) as response, SOURCE_PATH.open("xb") as out:
        total = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > 10 * 1024 * 1024:
                raise RuntimeError("source exceeds 10 MiB limit")
            out.write(block)


def load_rows() -> list[dict]:
    value = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("data", value.get("eval", []))
    if not isinstance(value, list):
        raise ValueError("dataset is not a JSON array")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="reuse the pinned source only")
    parser.add_argument("--dry-run", action="store_true", help="validate and report counts without writing outputs")
    args = parser.parse_args()
    if args.offline and not SOURCE_PATH.exists():
        raise SystemExit(f"missing offline source: {SOURCE_PATH}")
    if not args.offline:
        fetch()
    rows = load_rows()
    eligible, excluded = [], Counter()
    seen = set()
    source_counts = Counter()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            excluded["non_object"] += 1
            continue
        prompt = row.get("instruction", row.get("prompt"))
        if not isinstance(prompt, str) or not prompt:
            excluded["nonempty_string"] += 1
            continue
        encoded = prompt.encode("utf-8")
        if len(encoded) > 3000:
            excluded["utf8_bytes_gt_3000"] += 1
            continue
        if len(prompt) > 8192:
            excluded["chars_gt_8192"] += 1
            continue
        if prompt in seen:
            excluded["duplicate_prompt"] += 1
            continue
        seen.add(prompt)
        category = row.get("dataset", row.get("source", "unknown"))
        category = category if isinstance(category, str) and category else "unknown"
        source_counts[category] += 1
        eligible.append({"id": f"source-{index}", "prompt": prompt, "category": category, "row_index": index})
    if len(eligible) < 100:
        raise RuntimeError(f"only {len(eligible)} eligible rows")
    quotas = {key: (count * 100) // len(eligible) for key, count in source_counts.items()}
    remainder = 100 - sum(quotas.values())
    for key in sorted(source_counts, key=lambda k: (-(source_counts[k] * 100 % len(eligible)), k))[:remainder]:
        quotas[key] += 1
    rng = random.Random(SEED)
    selected = []
    for category in sorted(source_counts):
        group = [row for row in eligible if row["category"] == category]
        selected.extend(rng.sample(group, quotas[category]))
    rng.shuffle(selected)
    cases = [{k: row[k] for k in ("id", "prompt", "category")} for row in selected]
    metadata = {"dataset": "tatsu-lab/alpaca_eval", "revision": REVISION, "seed": SEED, "selection_size": 100}
    selected_category_counts = Counter(row["category"] for row in selected)
    normalized_bytes = (json.dumps({"metadata": metadata, "cases": cases}, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    provenance = {"source_url": SOURCE_URL, "source_sha256": sha256(SOURCE_PATH), "source_size_bytes": SOURCE_PATH.stat().st_size,
                  "license": "CC-BY-NC-4.0 (dataset; evaluator code is Apache-2.0)", "source_counts": dict(sorted(source_counts.items())),
                  "eligibility_exclusion_counts": dict(sorted(excluded.items())), "eligible_count": len(eligible),
                  "selected_category_counts": dict(sorted(selected_category_counts.items())),
                  "normalized_dataset_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
                  "selected": [{"id": x["id"], "category": x["category"], "prompt_sha256": hashlib.sha256(x["prompt"].encode()).hexdigest()} for x in selected], "selection_seed": SEED}
    if args.dry_run:
        print(json.dumps({"eligible_count": len(eligible), "source_counts": dict(source_counts), "exclusions": dict(excluded)}))
        return
    if OUTPUT_PATH.exists() or PROVENANCE_PATH.exists():
        raise SystemExit("refusing to overwrite existing output")
    OUTPUT_PATH.write_bytes(normalized_bytes)
    PROVENANCE_PATH.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
