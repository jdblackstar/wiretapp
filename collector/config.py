from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    KAFKA_BOOTSTRAP_SERVERS: str = "kafka:9092"
    KAFKA_RAW_EVENTS_TOPIC: str = "wiretapp_events_raw"
    KAFKA_VALIDATED_EVENTS_TOPIC: str = "wiretapp_events_validated"

    # For FastAPI/Uvicorn if needed, though Dockerfile handles defaults
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

    class Config:
        # If you have a .env file, settings can be loaded from it
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
