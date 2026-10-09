"""Évaluation de l'agent : gold set, notation (execution accuracy, abstention) et métriques."""

from text_to_sql_mcp.evaluation.comparison import (
    ABSTENTION_MARKER,
    Verdict,
    compare_results,
    grade,
    is_abstention,
    normalize_value,
)
from text_to_sql_mcp.evaluation.gold import GoldQuestion, load_gold_set
from text_to_sql_mcp.evaluation.metrics import (
    PRICES_USD_PER_MTOK,
    cost_usd,
    pass_at_k,
    pass_hat_k,
    wilson_interval,
)
from text_to_sql_mcp.evaluation.runner import (
    EvalRecord,
    compute_references,
    evaluate,
    load_records,
    new_run_id,
    regrade,
    save_records,
)
from text_to_sql_mcp.evaluation.simulation import OracleAnthropic

__all__ = [
    "ABSTENTION_MARKER",
    "PRICES_USD_PER_MTOK",
    "EvalRecord",
    "GoldQuestion",
    "OracleAnthropic",
    "Verdict",
    "compare_results",
    "compute_references",
    "cost_usd",
    "evaluate",
    "grade",
    "is_abstention",
    "load_gold_set",
    "load_records",
    "new_run_id",
    "normalize_value",
    "pass_at_k",
    "pass_hat_k",
    "regrade",
    "save_records",
    "wilson_interval",
]
