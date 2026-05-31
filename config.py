from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_PATH: str = "vlr_scraper.db"
    RATE_LIMIT_RPS: float = 0.67  # 1 request per 1.5 seconds
    CONCURRENCY: int = 3
    RETRY_MAX: int = 5
    RETRY_BACKOFF_BASE: float = 2.0
    REQUEST_TIMEOUT: int = 30
    USER_AGENT: str = "vlr-scraper/1.0 (research)"
    BASE_URL: str = "https://www.vlr.gg"
    LOG_LEVEL: str = "INFO"
    ERRORS_LOG: str = "errors.log"
    CLOUDFLARE_COOLDOWN_MINUTES: int = 10
    CLOUDFLARE_COOLDOWN_MAX_MINUTES: int = 120
    CLOAKBROWSER_HEADLESS: bool = True
    CLOAKBROWSER_HUMANIZE: bool = False
    CLOAKBROWSER_WAIT_SECONDS: int = 30
    CLOAKBROWSER_SESSION_PATH: str = "browser_session.json"

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()

# Agent normalization
AGENT_ALIASES: dict[str, str] = {
    "KAYO": "KAY/O",
    "Kay/o": "KAY/O",
    "kayo": "KAY/O",
}

CANONICAL_AGENTS: list[str] = [
    "Astra",
    "Breach",
    "Brimstone",
    "Chamber",
    "Clove",
    "Cypher",
    "Deadlock",
    "Fade",
    "Gekko",
    "Harbor",
    "Iso",
    "Jett",
    "KAY/O",
    "Killjoy",
    "Neon",
    "Omen",
    "Phoenix",
    "Raze",
    "Reyna",
    "Sage",
    "Skye",
    "Sova",
    "Tejo",
    "Viper",
    "Vyse",
    "Waylay",
    "Yoru",
]

CANONICAL_MAPS: list[str] = [
    "Ascent",
    "Bind",
    "Breeze",
    "Fracture",
    "Haven",
    "Icebox",
    "Lotus",
    "Pearl",
    "Split",
    "Sunset",
    "Abyss",
    "Drift",
    "Corrode",
]

VLR_REGIONS: list[str] = ["na", "eu", "ap", "sa", "mn", "gc", "cn"]

RANKINGS_REGIONS: list[str] = [
    "north-america",
    "europe",
    "pacific",
    "latin-america",
    "mena",
    "china",
    "gc",
]

STATS_TIMESPANS: list[str] = ["30", "60", "90", "all"]
