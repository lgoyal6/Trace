"""Validate the checked-in results used by the public evaluation page."""
from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
r=json.loads((ROOT/'backend/measurement/results/retrieval_eval.json').read_text())
g=json.loads((ROOT/'backend/measurement/results/grounding_eval.json').read_text())
assert r['test']['queries_scored']==28
assert r['test']['documents']==165
assert r['comparison']['ordering_delta_ndcg_at_10_rarity_minus_lexical']==-0.0701
assert r['comparison']['ordering_delta_ndcg_at_10_rarity_minus_lexical_rare_queries_only']==0.0613
assert g['test']['cases']==1624
assert g['test']['at_dev_best_threshold']['false_positive']==1
assert g['test']['at_dev_best_threshold']['false_negative']==3
print('public proof valid: 28 held-out queries, 1624 grounding cases, negative result preserved')
