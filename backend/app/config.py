import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.app.order_limits import EntryBudgetLimits
from backend.app.provider_settings import normalize_openai_compatible_endpoint


class RuntimeRole(StrEnum):
    SUBMISSION = "SUBMISSION"
    DEVELOPMENT = "DEVELOPMENT"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_account_role: RuntimeRole
    app_autonomous_enabled: bool = False
    app_submission_opportunity_enabled: bool = False
    app_policy_hash: SecretStr
    app_calibration_hash: SecretStr
    app_calibration_decision_boundary: datetime
    app_calibration_sealed_at: datetime
    app_entry_equity_floor: SecretStr
    app_maximum_lifetime_entries: SecretStr
    app_maximum_lifetime_risk: SecretStr
    app_maximum_position_loss: SecretStr
    app_maximum_entry_quantity: SecretStr
    app_opportunity_key: SecretStr | None = None
    app_opportunity_plan_version: SecretStr | None = None
    app_halt_maximum_trade_age_seconds: SecretStr | None = None
    alpaca_api_endpoint: str
    alpaca_api_key: SecretStr
    alpaca_secret_key: SecretStr
    alpaca_paper_trade: bool = True
    database_url: SecretStr
    gemini_api_key: SecretStr
    app_owner_access_code: SecretStr = Field(min_length=16, max_length=256)
    app_session_secret: SecretStr = Field(min_length=32)
    app_provider_settings_secret: SecretStr = Field(min_length=32)
    app_openai_compatible_origins: str = ""
    app_allowed_origin: str
    scheduler_token: SecretStr = Field(min_length=32)

    @model_validator(mode="after")
    def paper_only(self) -> "Settings":
        if any(
            not value.get_secret_value().strip()
            for value in (self.alpaca_api_key, self.alpaca_secret_key, self.gemini_api_key)
        ):
            raise ValueError("provider credentials are required")
        if self.alpaca_api_endpoint != "https://paper-api.alpaca.markets":
            raise ValueError("only Alpaca paper endpoint is allowed")
        if not self.alpaca_paper_trade:
            raise ValueError("paper mode is required")
        if any(
            not _is_hash(value.get_secret_value())
            for value in (self.app_policy_hash, self.app_calibration_hash)
        ):
            raise ValueError("runtime authority hashes must be lowercase SHA-256")
        if (
            self.app_calibration_decision_boundary.utcoffset() != timedelta(0)
            or self.app_calibration_sealed_at.utcoffset() != timedelta(0)
            or self.app_calibration_sealed_at < self.app_calibration_decision_boundary
        ):
            raise ValueError("calibration timestamps must be ordered UTC values")
        self.entry_budget_limits()
        self.opportunity_authority()
        self.openai_compatible_origins()
        origin = urlsplit(self.app_allowed_origin)
        try:
            port = origin.port
        except ValueError as error:
            raise ValueError("allowed origin must be an HTTPS origin") from error
        if (
            origin.scheme != "https"
            or not origin.hostname
            or port is not None
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("allowed origin must be an HTTPS origin")
        return self

    def opportunity_authority(self) -> tuple[str, int, timedelta] | None:
        supplied = (
            self.app_opportunity_key,
            self.app_opportunity_plan_version,
            self.app_halt_maximum_trade_age_seconds,
        )
        if all(value is None for value in supplied):
            return None
        if any(value is None for value in supplied):
            raise ValueError("opportunity authority is incomplete")
        key = self.app_opportunity_key
        version_authority = self.app_opportunity_plan_version
        age_authority = self.app_halt_maximum_trade_age_seconds
        if key is None or version_authority is None or age_authority is None:
            raise ValueError("opportunity authority is incomplete")
        value = key.get_secret_value().strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", value) is None:
            raise ValueError("opportunity key is invalid")
        try:
            version = int(version_authority.get_secret_value())
            maximum_trade_age = int(age_authority.get_secret_value())
        except ValueError as error:
            raise ValueError("opportunity authority is invalid") from error
        if not 1 <= version <= 2_147_483_647 or not 1 <= maximum_trade_age <= 120:
            raise ValueError("opportunity authority is invalid")
        if (
            self.app_account_role is RuntimeRole.SUBMISSION
            and not self.app_submission_opportunity_enabled
        ):
            return None
        return value, version, timedelta(seconds=maximum_trade_age)

    def openai_compatible_origins(self) -> tuple[str, ...]:
        if not self.app_openai_compatible_origins:
            return ()
        supplied = tuple(value.strip() for value in self.app_openai_compatible_origins.split(","))
        if len(supplied) > 4 or any(not value for value in supplied):
            raise ValueError("OpenAI-compatible origins are invalid")
        try:
            origins = tuple(
                normalize_openai_compatible_endpoint(
                    f"{origin.rstrip('/')}/v1",
                    allowed_origins=(origin,),
                ).removesuffix("/v1")
                for origin in supplied
            )
        except ValueError:
            raise ValueError("OpenAI-compatible origins are invalid") from None
        if len(set(origins)) != len(origins):
            raise ValueError("OpenAI-compatible origins are invalid")
        return origins

    def entry_budget_limits(self) -> EntryBudgetLimits:
        try:
            return EntryBudgetLimits(
                policy_hash=self.app_policy_hash.get_secret_value(),
                equity_floor=Decimal(self.app_entry_equity_floor.get_secret_value()),
                maximum_lifetime_entries=int(self.app_maximum_lifetime_entries.get_secret_value()),
                maximum_lifetime_risk=Decimal(self.app_maximum_lifetime_risk.get_secret_value()),
                maximum_position_loss=Decimal(self.app_maximum_position_loss.get_secret_value()),
                maximum_entry_quantity=int(self.app_maximum_entry_quantity.get_secret_value()),
            )
        except (InvalidOperation, ValueError) as error:
            raise ValueError("entry budget limits are invalid") from error


def _is_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
