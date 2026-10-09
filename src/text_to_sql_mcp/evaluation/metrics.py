"""Métriques : coût, intervalle de confiance, pass@k et pass^k."""

from math import comb, sqrt

# Tarifs de base en dollars par million de tokens (entrée, sortie), sans cache.
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Coût d'un appel de l'agent, en dollars.

    Args:
        model: Identifiant du modèle.
        input_tokens: Tokens d'entrée, tous tours confondus.
        output_tokens: Tokens de sortie (réflexion comprise), tous tours confondus.

    Returns:
        Le coût, ou `None` si le tarif du modèle est inconnu.
    """
    if model not in PRICES_USD_PER_MTOK:
        return None
    prix_entree, prix_sortie = PRICES_USD_PER_MTOK[model]
    return (input_tokens * prix_entree + output_tokens * prix_sortie) / 1_000_000


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Intervalle de confiance de Wilson d'une proportion (95 % par défaut).

    Plus fiable que « p ± 1,96 σ » sur de petits échantillons et près de 0 % ou 100 %.

    Args:
        successes: Nombre de réussites.
        total: Nombre d'essais.
        z: Quantile de la loi normale (1,96 pour 95 %).

    Returns:
        Les bornes basse et haute, entre 0 et 1.
    """
    if total == 0:
        return (0.0, 1.0)
    p = successes / total
    denominateur = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominateur
    demi_largeur = z * sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denominateur
    return (max(0.0, centre - demi_largeur), min(1.0, centre + demi_largeur))


def pass_at_k(n: int, c: int, k: int) -> float:
    """Probabilité qu'au moins un essai sur k réussisse (« sait le faire »).

    Estimateur sans biais à partir de n essais dont c réussis (Chen et al., 2021).

    Args:
        n: Nombre d'essais réalisés.
        c: Nombre d'essais réussis.
        k: Taille du tirage (k <= n).
    """
    _verifier(n, c, k)
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """Probabilité que k essais réussissent **tous** (« on peut compter dessus »).

    Estimateur sans biais à partir de n essais dont c réussis (τ-bench, Yao et al., 2024).

    Args:
        n: Nombre d'essais réalisés.
        c: Nombre d'essais réussis.
        k: Taille du tirage (k <= n).
    """
    _verifier(n, c, k)
    return comb(c, k) / comb(n, k)


def _verifier(n: int, c: int, k: int) -> None:
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError(f"Il faut 0 <= c <= n et 1 <= k <= n (n={n}, c={c}, k={k}).")
