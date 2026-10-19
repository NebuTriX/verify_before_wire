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
    # Gemini connection. GEMINI_MODE=vertex: the customer's own Google Cloud project (works from
    # AWS/Azure/on-prem via Workload Identity Federation; point GOOGLE_APPLICATION_CREDENTIALS at
    # the federation config). GEMINI_MODE=api_key: Gemini API key (e.g. a vendor-provided account).
    gemini_mode: str = "vertex"
    gemini_project: str = ""                     # defaults to google_cloud_project
    gemini_api_key: str = ""
    gemini_redact: bool = True                   # mask accounts/emails/phones for language + narrative
    # Gemini 3 preview models are served from the global endpoint only.
    gemini_location: str = "global"
    gemini_model: str = "gemini-3-flash-preview"
    gemini_thinking_level: str = "low"          # low | high ; "" to leave default
    gemini_timeout_s: float = 45.0
    # None = model default. Google recommends leaving Gemini 3 at its default (1.0);
    # reproducibility comes from deterministic checks + JSON schemas, not temperature.
    gemini_temperature: float | None = None
    gemini_seed: int | None = 7

    # Storage. STORE_BACKEND: firestore (GCP) | sql (PostgreSQL on any cloud / on-prem) | memory (tests)
    store_backend: str = "firestore"
    firestore_database: str = "(default)"
    database_url: str = ""                       # e.g. postgresql+asyncpg://user@host/db (sql backend)
    # BLOB_BACKEND: gcs | s3 | azure | file | memory  (where uploaded files are kept)
    blob_backend: str = "gcs"
    uploads_bucket: str = ""                     # bucket / container name
    blob_dir: str = "/var/lib/vbw/blobs"         # file backend
    s3_kms_key_id: str = ""                      # optional customer-managed key on AWS
    azure_blob_account_url: str = ""             # https://<account>.blob.core.windows.net
    demo_enabled: bool = False                   # bundled sample scenarios + replay; off in production

    # --- Live external data sources --------------------------------------
    rdap_base: str = "https://rdap.org"
    fx_api_base: str = "https://api.frankfurter.app"
    opensanctions_api_key: str = ""              # empty -> falls back to the OFAC SDN list
    opensanctions_base: str = "https://api.opensanctions.org"
    ofac_sdn_url: str = "https://www.treasury.gov/ofac/downloads/sdn.csv"
    un_sanctions_url: str = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
    eu_sanctions_url: str = ("https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/"
                             "content?token=dG9rZW4tMjAxNw")   # the EU's public download token
    uk_sanctions_url: str = "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv"
    sanctions_lists: str = "ofac,un,eu,uk"        # which official lists to screen against
    sanctions_refresh_hours: float = 12.0
    sanctions_cache_dir: Path = ROOT / "data" / "sanctions"   # snapshots survive restarts; "" disables
    gleif_base: str = "https://api.gleif.org/api/v1"          # Global LEI Foundation: legal entities + BICs
    vies_base: str = "https://ec.europa.eu/taxation_customs/vies/rest-api"   # EU VAT number validation
    ifsc_api_base: str = "https://ifsc.razorpay.com"
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

    # --- Auth ---------------------------------------------------------------
    auth_mode: str = "firebase"                  # firebase | dev (local only)
    allow_dev_auth: bool = False
    firebase_project_id: str = ""                # defaults to google_cloud_project
    firebase_api_key: str = ""                   # public web config, served to the browser
    firebase_auth_domain: str = ""                  # defaults to <project>.firebaseapp.com
    firebase_app_id: str = ""                    # web app id from Firebase Console (optional)
    allow_unverified_email: bool = False
    invite_days: int = 7
    rate_limit_per_minute: int = 60              # per tenant, case creation

    # --- App ----------------------------------------------------------------
    max_upload_mb: int = 20                      # per file; Gemini takes media inline up to ~20 MB
    max_files: int = 6
    demo_dir: Path = ROOT.parent / "demo"
    static_dir: Path = ROOT / "static"
    seed_file: Path = ROOT / "scripts" / "seed_data.json"
    prompt_version: str = "2026-10-13"
    lease_seconds: int = 150
    metrics_enabled: bool = True                 # Prometheus /metrics (counts and latencies only; restrict at ingress)
    # Real phone calls for AI verification calls (optional; without them the call runs in the browser)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""                 # E.164, e.g. +6531234567
    public_base_url: str = ""                    # https URL Twilio can reach, e.g. https://vbw.example.com
    media_ai_review: bool = True                 # ask Gemini for an indicative deepfake review of voice/video
    worker_enabled: bool = True                  # run the background worker inside the web process
    worker_concurrency: int = 4                  # investigations at once, per worker process
    worker_poll_seconds: float = 1.0
    worker_grace_seconds: float = 8.0            # browser-submitted cases: time for the live view to claim them first
    webhook_allow_insecure: bool = False         # allow http:// and private addresses (local development only)
    log_level: str = "INFO"

    @property
    def company_domain_list(self) -> list[str]:
        return [d.strip().lower() for d in self.company_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
