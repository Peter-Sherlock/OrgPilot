"""Environment-backed runtime configuration with explicit provider opt-in."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv_if_exists(dotenv_path: str = ".env") -> None:
    path = Path(dotenv_path)
    if not path.exists():
        return
    try:
        content = path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'\"")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


@dataclass(frozen=True, slots=True)
class OrgPilotSettings:
    """Runtime settings. External providers are disabled unless explicitly selected."""

    database_url: str = "sqlite+aiosqlite:///./orgpilot.db"
    api_token: str | None = field(default=None, repr=False)
    llm_provider: str = "mock"
    aihubmix_api_key: str | None = field(default=None, repr=False)
    aihubmix_base_url: str = "https://aihubmix.com"
    aihubmix_model: str = "gpt-5.6-luna"
    llm_reasoning_effort: str | None = None
    collaboration_adapter: str = "mock"
    demo_bootstrap: bool = False
    feishu_use_ws: bool = True
    feishu_allow_writes: bool = False
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = field(default=None, repr=False)
    feishu_verification_token: str | None = field(default=None, repr=False)
    feishu_project_id: str = "feishu-project"
    reference_timezone: str = "Asia/Shanghai"
    directive_reminder_minutes: int = 60
    directive_escalation_minutes: int = 1440
    outbox_max_attempts: int = 3
    outbox_retry_seconds: int = 30
    outbox_sweep_seconds: int = 15

    @classmethod
    def from_env(cls) -> "OrgPilotSettings":
        _load_dotenv_if_exists()
        use_ws_env = os.getenv("ORGPILOT_FEISHU_USE_WS", "true").strip().lower()
        demo_bootstrap_env = os.getenv("ORGPILOT_DEMO_BOOTSTRAP", "false").strip().lower()
        allow_writes_env = os.getenv("ORGPILOT_FEISHU_ALLOW_WRITES", "false").strip().lower()

        def _int_env(name: str, default: int) -> int:
            raw = os.getenv(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        settings = cls(
            database_url=os.getenv(
                "ORGPILOT_DATABASE_URL",
                "sqlite+aiosqlite:///./orgpilot.db",
            ),
            api_token=_optional_env("ORGPILOT_API_TOKEN"),
            llm_provider=os.getenv("ORGPILOT_LLM_PROVIDER", "mock").strip().lower(),
            aihubmix_api_key=_optional_env("AIHUBMIX_API_KEY"),
            aihubmix_base_url=os.getenv("AIHUBMIX_BASE_URL", "https://aihubmix.com").rstrip("/"),
            aihubmix_model=os.getenv("AIHUBMIX_MODEL", "gpt-5.6-luna"),
            llm_reasoning_effort=_optional_env("ORGPILOT_LLM_REASONING_EFFORT"),
            collaboration_adapter=os.getenv("ORGPILOT_COLLABORATION_ADAPTER", "mock")
            .strip()
            .lower(),
            demo_bootstrap=demo_bootstrap_env in {"1", "true", "yes", "on"},
            feishu_use_ws=use_ws_env in {"1", "true", "yes", "on"},
            feishu_allow_writes=allow_writes_env in {"1", "true", "yes", "on"},
            feishu_app_id=_optional_env("FEISHU_APP_ID"),
            feishu_app_secret=_optional_env("FEISHU_APP_SECRET"),
            feishu_verification_token=_optional_env("FEISHU_VERIFICATION_TOKEN"),
            feishu_project_id=os.getenv("ORGPILOT_FEISHU_PROJECT_ID", "feishu-project"),
            reference_timezone=os.getenv("ORGPILOT_TIMEZONE", "Asia/Shanghai").strip(),
            directive_reminder_minutes=_int_env("ORGPILOT_DIRECTIVE_REMINDER_MINUTES", 60),
            directive_escalation_minutes=_int_env("ORGPILOT_DIRECTIVE_ESCALATION_MINUTES", 1440),
            outbox_max_attempts=_int_env("ORGPILOT_OUTBOX_MAX_ATTEMPTS", 3),
            outbox_retry_seconds=_int_env("ORGPILOT_OUTBOX_RETRY_SECONDS", 30),
            outbox_sweep_seconds=_int_env("ORGPILOT_OUTBOX_SWEEP_SECONDS", 15),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.llm_provider not in {"mock", "aihubmix"}:
            raise ValueError("ORGPILOT_LLM_PROVIDER must be 'mock' or 'aihubmix'")
        if self.llm_provider == "aihubmix" and not self.aihubmix_api_key:
            raise ValueError("AIHUBMIX_API_KEY is required when ORGPILOT_LLM_PROVIDER=aihubmix")
        try:
            ZoneInfo(self.reference_timezone)
        except Exception as exc:
            raise ValueError(
                f"ORGPILOT_TIMEZONE {self.reference_timezone!r} is not a valid IANA timezone"
            ) from exc

        if self.collaboration_adapter not in {"mock", "feishu"}:
            raise ValueError("ORGPILOT_COLLABORATION_ADAPTER must be 'mock' or 'feishu'")
        if self.collaboration_adapter == "feishu":
            required_items = [
                ("FEISHU_APP_ID", self.feishu_app_id),
                ("FEISHU_APP_SECRET", self.feishu_app_secret),
            ]
            if not self.feishu_use_ws:
                required_items.append(("FEISHU_VERIFICATION_TOKEN", self.feishu_verification_token))

            missing = [name for name, value in required_items if not value]
            if missing:
                raise ValueError("Missing required Feishu settings: " + ", ".join(missing))
