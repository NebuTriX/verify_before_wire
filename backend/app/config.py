"""Runtime configuration. Every value can be overridden with an environment variable
of the same name in upper case (e.g. GEMINI_MODEL)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent  # backend/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Google Cloud -----------------------------------------------------
    google_cloud_project: str = ""
    # Gemini 3 preview models are served from the global endpoint only.
    gemini_location: str = "global"
    gemini_model: str = "gemini-3-flash-preview"
    gemini_thinking_level: str = "low"          # low | high ; "" to leave default
    gemini_timeout_s: float = 45.0
    # None = model default. Google recommends leaving Gemini 3 at its default (1.0);
    # reproducibility comes from deterministic checks + JSON schemas, not temperature.
    gemini_temperature: float | None = None
    gemini_seed: int | None = 7

    # Storage: "firestore" in Cloud Run, "memory" for local dev and tests.
    store_backend: str = "firestore"
    firestore_database: str = "(default)"
    uploads_bucket: str = ""                    # empty = keep uploads in the case store only

    # --- Live external data sources --------------------------------------
    rdap_base: str = "https://rdap.org"
    fx_api_base: str = "https://api.frankfurter.app"
    opensanctions_api_key: str = ""              # empty -> falls back to the OFAC SDN list
    opensanctions_base: str = "https://api.opensanctions.org"
    ofac_sdn_url: str = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    http_timeout_s: float = 6.0

    # --- Business rules ---------------------------------------------------
    company_name: str = "Acme Holdings Pte Ltd"
    company_domains: str = "acme-holdings.com"   # comma separated, used for lookalike checks
    base_currency: str = "USD"
    business_timezone: str = "Asia/Singapore"
    new_domain_days_high: int = 90
    new_domain_days_medium: int = 365
    amount_ratio_high: float = 3.0
    amount_ratio_medium: float = 1.8

    # --- App ----------------------------------------------------------------
    max_upload_mb: int = 15
    max_files: int = 6
    demo_dir: Path = ROOT.parent / "demo"
    static_dir: Path = ROOT / "static"
    seed_file: Path = ROOT / "scripts" / "seed_data.json"
    lease_seconds: int = 150
    log_level: str = "INFO"

    @property
    def company_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.company_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
