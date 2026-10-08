"""Logs structurés : une ligne JSON par événement, sur stderr.

En transport stdio, stdout est le canal du protocole MCP : rien d'autre ne doit y être écrit.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER_NAME = "text_to_sql_mcp"

# Attributs présents sur tout LogRecord : tout le reste vient du paramètre `extra`.
_CHAMPS_STANDARD = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Sérialise chaque enregistrement en une ligne JSON.

    Ordre des clés : horodatage, niveau, client (qui appelle l'outil), événement, puis les
    champs passés via `extra`. Une exception éventuelle est ajoutée sous la clé `exception`.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Construit la ligne JSON d'un enregistrement.

        Args:
            record: Enregistrement produit par le module logging.

        Returns:
            Une ligne JSON, sans saut de ligne.
        """
        extras = {k: v for k, v in record.__dict__.items() if k not in _CHAMPS_STANDARD}
        ligne: dict[str, Any] = {
            "horodatage": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "niveau": record.levelname,
        }
        if "client" in extras:
            ligne["client"] = extras.pop("client")
        ligne["evenement"] = record.getMessage()
        ligne["logger"] = record.name
        ligne |= extras
        if record.exc_info:
            ligne["exception"] = self.formatException(record.exc_info)
        return json.dumps(ligne, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Envoie tous les logs du processus en JSON sur stderr.

    Le SDK MCP installe à la création du serveur un gestionnaire « Rich » (coloré, sur
    plusieurs lignes) sur le logger racine : on le remplace, pour que chaque ligne de stderr
    soit un objet JSON. Le logger racine reste au niveau WARNING (les erreurs du SDK sont
    gardées, ses messages INFO doublonneraient nos traces) ; le logger du package suit `level`.
    Idempotent : un second appel remplace le gestionnaire au lieu d'en ajouter un.

    Args:
        level: Niveau minimal des messages du package (DEBUG, INFO, WARNING, ERROR).
    """
    gestionnaire = logging.StreamHandler(sys.stderr)
    gestionnaire.setFormatter(JsonFormatter())
    racine = logging.getLogger()
    racine.handlers = [gestionnaire]
    racine.setLevel(logging.WARNING)
    logging.getLogger(LOGGER_NAME).setLevel(level)
