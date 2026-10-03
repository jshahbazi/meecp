"""Publish numeric grades and provenance hashes, never runtime text."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics

COHORTS = (
    'decisions-final', 'refinement', 'holdout', 'atomic', 'atomic-2048',
    'code-final-14', 'user-prompt-final', 'user-prompt-seed7',
    'user-prompt-seed19', 'user-prompt-seed43',
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, default=Path('.build/bonsai-onboarding'))
    parser.add_argument('--output', type=Path, default=Path(__file__).with_name('results_summary.json'))
    args = parser.parse_args()
    cohorts = []
    for name in COHORTS:
        path = args.input / (name + '.json')
        data = path.read_bytes()
        original = json.loads(data)
        records = original['records']
        # All retained fields are hashes, numeric counters, booleans, or fixed IDs.
        rows = []
        for record in records:
            grade = record['grade']
            rows.append({
                key: record[key] for key in (
                    'kind', 'case_id', 'arm', 'seed', 'temperature', 'runtime_ok',
                    'prompt_sha256', 'output_sha256', 'budget_exhausted',
                    'wall_seconds', 'prefill_tokens', 'decode_steps',
                    'max_rss_bytes', 'peak_physical_footprint_bytes',
                )
            } | {'grade': {
                key: grade[key] for key in (
                    'json_valid', 'schema_valid', 'semantic_pass',
                    'critical_false_readiness', 'failure_fields',
                    'pass', 'failure', 'passed', 'total', 'failed_tests',
                ) if key in grade
            }})
        cohorts.append({
            'id': name,
            'numeric_artifact_sha256': hashlib.sha256(data).hexdigest(),
            'provenance': {key: value for key, value in original.items()
                           if key.endswith('_sha256') or key in ('runtime', 'context_capacity')},
            'count': len(records),
            'pass_count': sum(record['grade'].get('semantic_pass', record['grade'].get('pass', False))
                              for record in records),
            'runtime_success_count': sum(record['runtime_ok'] for record in records),
            'critical_false_readiness_count': sum(record['grade'].get('critical_false_readiness', False)
                                                  for record in records),
            'wall_mean_seconds': statistics.mean(record['wall_seconds'] for record in records),
            'max_rss_bytes': max(record['max_rss_bytes'] for record in records),
            'max_physical_footprint_bytes': max(record['peak_physical_footprint_bytes'] for record in records),
            'records': rows,
        })
    args.output.write_text(json.dumps({'cohorts': cohorts}, indent=2, sort_keys=True, allow_nan=False) + '\n')
    print(json.dumps({'cohorts': len(cohorts), 'records': sum(cohort['count'] for cohort in cohorts)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
