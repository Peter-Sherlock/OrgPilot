"""Environment-backed runtime configuration with explicit provider opt-in."""

import os
from dataclasses import dataclass, field
from pathlib import Path


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
    collaboration_adapter: str = "mock"
    demo_bootstrap: bool = False
    feishu_use_ws: bool = True
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = field(default=None, repr=False)
    feishu_verification_token: str | None = field(default=None, repr=False)
    feishu_project_id: str = "feishu-project"

    @classmethod
    def from_env(cls) -> "OrgPilotSettings":
        _load_dotenv_if_exists()
        use_ws_env = os.getenv("ORGPILOT_FEISHU_USE_WS", "true").strip().lower()
        demo_bootstrap_env = os.getenv("ORGPILOT_DEMO_BOOTSTRAP", "false").strip().lower()
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
            collaboration_adapter=os.getenv("ORGPILOT_COLLABORATION_ADAPTER", "mock")
            .strip()
            .lower(),
            demo_bootstrap=demo_bootstrap_env in {"1", "true", "yes", "on"},
            feishu_use_ws=use_ws_env in {"1", "true", "yes", "on"},
            feishu_app_id=_optional_env("FEISHU_APP_ID"),
            feishu_app_secret=_optional_env("FEISHU_APP_SECRET"),
            feishu_verification_token=_optional_env("FEISHU_VERIFICATION_TOKEN"),
            feishu_project_id=os.getenv("ORGPILOT_FEISHU_PROJECT_ID", "feishu-project"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.llm_provider not in {"mock", "aihubmix"}:
            raise ValueError("ORGPILOT_LLM_PROVIDER must be 'mock' or 'aihubmix'")
        if self.llm_provider == "aihubmix" and not self.aihubmix_api_key:
            raise ValueError("AIHUBMIX_API_KEY is required when ORGPILOT_LLM_PROVIDER=aihubmix")

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
