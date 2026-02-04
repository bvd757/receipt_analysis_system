from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True)

    DATABASE_URL: str = "sqlite:///./app/data/app.db"

    JWT_SECRET: str = "change-me"
    jwt_alg: str = "HS256"
    access_token_expire_minutes: int = 1440

    OPENAI_API_KEY: str = ""
    OPENAI_OCR_MODEL: str = "gpt-4o-mini"
    OPENAI_STRUCT_MODEL: str = "gpt-4o-mini"
    FX_EUR_TO_USD: float = 1.0
    FX_CHF_TO_USD: float = 1.0
    FX_RUB_TO_USD: float = 1.0



settings = Settings()
