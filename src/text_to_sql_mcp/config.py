"""Configuration centrale de l'application, lue depuis l'environnement et le fichier .env."""

from functools import cache
from typing import Literal

from psycopg.conninfo import make_conninfo
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Réglages de l'application.

    Chaque champ est lu dans une variable d'environnement du même nom (insensible à la
    casse), puis dans le fichier .env du répertoire courant ; une variable d'environnement
    est prioritaire sur le .env. Le .env est partagé avec Docker Compose : les variables de
    l'administrateur PostgreSQL qu'il contient sont ignorées ici.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "chinook"
    db_schema: str = "public"
    agent_db_user: str = "agent_lecteur"
    agent_db_password: SecretStr
    connect_timeout: int = Field(default=5, ge=1, description="Délai de connexion, en secondes.")
    row_cap: int = Field(
        default=100, ge=1, le=1000, description="Nombre maximal de lignes renvoyées par run_query."
    )
    statement_timeout: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Durée maximale d'une requête, en secondes (le rôle PostgreSQL plafonne à 30).",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def conninfo(self) -> str:
        """Construit la chaîne de connexion libpq du rôle lecture seule.

        `make_conninfo` échappe correctement les valeurs (espaces, apostrophes…).
        La chaîne contient le mot de passe en clair : ne jamais l'afficher ni la logguer.

        Returns:
            La chaîne de connexion au format `clé=valeur`.
        """
        return make_conninfo(
            host=self.postgres_host,
            port=self.postgres_port,
            dbname=self.postgres_db,
            user=self.agent_db_user,
            password=self.agent_db_password.get_secret_value(),
            connect_timeout=self.connect_timeout,
            application_name="text-to-sql-mcp",
            # Réglage de session : prioritaire sur le statement_timeout du rôle (30 s).
            options=f"-c statement_timeout={self.statement_timeout}s",
        )


@cache
def get_settings() -> Settings:
    """Renvoie les réglages, lus une seule fois puis mis en cache.

    Returns:
        L'instance unique de `Settings`.
    """
    return Settings()
