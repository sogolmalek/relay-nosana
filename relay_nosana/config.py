from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalizeConfig(BaseModel):
    # PENDING (not in M0): needs per-instance base-commit trees wired into
    # LocalizeTask and its silent HEAD ("default") fallback removed first.
    enabled: bool = False
    retrieval: Literal["plain", "rrf"] = "plain"  # RRF kept but off: -19pt on dev
    exclude_globs: list[str] = ["**/test*/**", "**/*_test.*", "docs/**", "*.md"]
    candidates_k: int = 30


class DuplicateConfig(BaseModel):
    candidates_k: int = 5


class CalibrationConfig(BaseModel):
    n_per_task: int = 100
    bootstrap: int = 1000


class RoutingConfig(BaseModel):
    threshold_ratio: float = 0.9
    abs_floor: float | None = None


class CompilerConfig(BaseModel):
    summary: str = "extractive"  # extractive | open_model


class NosanaModelConfig(BaseModel):
    name: str
    hf_id: str
    market: str
    max_model_len: int = 8192
    quant: str | None = None
    price_per_hour_usd: float = 0.0


class NosanaConfig(BaseModel):
    # Source: learn.nosana.com/api/intro.html — "The current production API base URL: https://api.nosana.com"
    # The spec originally listed https://dashboard.k8s.prd.nos.ci/api (older internal URL).
    # Keep as a config value; the first real `relay nosana balance` call will settle it.
    base_url: str = "https://api.nosana.com"
    max_credits_per_run: float = 10.0  # USD; M0 cap
    concurrency: int = 16
    ready_timeout_min: int = 30
    deployment_timeout_min: int = 120
    models: list[NosanaModelConfig] = Field(default_factory=list)


class FrontierConfig(BaseModel):
    model: str = "claude-sonnet-4-6"
    price_in_per_mtok: float = 3.0
    price_out_per_mtok: float = 15.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Secrets from env vars only — never logged
    nosana_api_key: str = Field(default="", alias="NOSANA_API_KEY")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    relay_core_path: str | None = Field(default=None, alias="RELAY_CORE_PATH")

    # These are populated from yaml
    repo: str = "OWNER/REPO"
    seed: int = 42
    label_map: dict[str, str] = Field(default_factory=dict)
    localize: LocalizeConfig = Field(default_factory=LocalizeConfig)
    duplicate: DuplicateConfig = Field(default_factory=DuplicateConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    compiler: CompilerConfig = Field(default_factory=CompilerConfig)
    nosana: NosanaConfig = Field(default_factory=NosanaConfig)
    frontier: FrontierConfig = Field(default_factory=FrontierConfig)

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "Settings":
        search = [
            path,
            Path("relay.nosana.yaml"),
            Path(__file__).parent.parent / "relay.nosana.yaml",
        ]
        yaml_data: dict[str, Any] = {}
        for p in search:
            if p and Path(p).exists():
                with open(p) as f:
                    yaml_data = yaml.safe_load(f) or {}
                break

        # Merge yaml values with env; env takes precedence for secrets
        env_vars = {
            "NOSANA_API_KEY": os.environ.get("NOSANA_API_KEY", ""),
            "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN", ""),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "RELAY_CORE_PATH": os.environ.get("RELAY_CORE_PATH"),
        }
        merged = {**yaml_data, **{k: v for k, v in env_vars.items() if v is not None}}
        return cls.model_validate(merged)


_settings: Settings | None = None


def get_settings(path: str | Path | None = None) -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_yaml(path)
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
