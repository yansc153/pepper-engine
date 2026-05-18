"""
Central configuration for the finance-account automation system.
All constants, paths, and environment variable loading.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

if ENV_PATH.exists():
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name, default).strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

# ── Project root ──

# ── Directory paths ──
CONFIG_DIR = PROJECT_ROOT / "config"
VOICE_DIR = PROJECT_ROOT / "voice"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
OPS_DIR = PROJECT_ROOT / "ops"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
SRC_DIR = PROJECT_ROOT / "src"

# ── Key files ──
DB_PATH = DATA_DIR / "pepperbot.db"
MEMORY_PATH = PROJECT_ROOT / "MEMORY.md"
KOL_LIST_PATH = CONFIG_DIR / "kol_list.md"
PERSONA_PATH = CONFIG_DIR / "persona.md"
FILTER_RULES_PATH = CONFIG_DIR / "filter_rules.md"
AVOID_SLOP_PATH = VOICE_DIR / "avoid_slop.md"
VOICE_PROFILE_PATH = VOICE_DIR / "voice_profile.md"
VOICE_RULES_PATH = VOICE_DIR / "voice_rules.md"
MEMENG_PATH = VOICE_DIR / "memeng_techniques.md"
HOOKS_PATH = TEMPLATES_DIR / "hooks_ai.md"
TEMPLATE_AI_PATH = TEMPLATES_DIR / "template_ai.md"
SOURCE_PACK_STYLE_ANCHOR_PATH = VOICE_DIR / "source_pack_style_anchor.md"
PLAYWRIGHT_RULES_PATH = OPS_DIR / "playwright_rules.md"

# ── LLM (local Claude CLI) ──
CLAUDE_CLI_PATH = _env_str("PEPPER_CLAUDE_CLI_PATH", "claude")
CLAUDE_MODEL = _env_str("PEPPER_CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_MAX_TOKENS = _env_int("PEPPER_CLAUDE_MAX_TOKENS", 1200)
PUBLISH_BACKEND = _env_str("PEPPER_PUBLISH_BACKEND", "opencli")
PUBLISH_FALLBACK_BACKEND = _env_str("PEPPER_PUBLISH_FALLBACK_BACKEND", "playwright")

# ── Twitter account ──
ACCOUNT_LABEL = _env_str("PEPPER_ACCOUNT_LABEL", "main")
TWITTER_HANDLE = _env_str("PEPPER_TWITTER_HANDLE", "@off_thetarget")
TWITTER_URL = "https://x.com"
TWITTER_HOME = f"{TWITTER_URL}/home"
TWITTER_LOGIN = f"{TWITTER_URL}/login"
TWITTER_COMPOSE = f"{TWITTER_URL}/compose/post"

# ── Schedule (24h format, Asia/Shanghai) ──
SCHEDULE_TIMEZONE = "Asia/Shanghai"
MORNING_HOUR = 7
NOON_HOUR = 13
EVENING_HOUR = 19
REVIEW_HOUR = 23

# ── Content limits ──
MAX_TWEET_LENGTH = 280
MAX_POSTS_PER_DAY = 20  # hard cap (rate limit guardrail)
MIN_POSTS_PER_DAY = 10
MAX_KOL_COMMENTS_PER_DAY = 15
MIN_KOL_COMMENTS_PER_DAY = 10
MAX_LIKES_PER_DAY = 50
MAX_FOLLOWS_PER_DAY = 10
FILTER_PASS_THRESHOLD = 55
FILTER_REVIEW_THRESHOLD = 40

# ── Content mix weights (initial, learner.py adjusts these) ──
@dataclass
class ContentWeights:
    market_hot_take: float = 0.35      # 市场快评
    earnings_reaction: float = 0.25    # 财报/指引反应
    sector_rotation: float = 0.20      # 板块轮动
    trading_psychology: float = 0.10   # 仓位/情绪/纪律
    controversy: float = 0.10          # 争议观点

    def as_dict(self) -> dict[str, float]:
        return {
            "market_hot_take": self.market_hot_take,
            "earnings_reaction": self.earnings_reaction,
            "sector_rotation": self.sector_rotation,
            "trading_psychology": self.trading_psychology,
            "controversy": self.controversy,
        }

    def normalize(self) -> None:
        total = sum(self.as_dict().values())
        if total <= 0:
            return
        self.market_hot_take /= total
        self.earnings_reaction /= total
        self.sector_rotation /= total
        self.trading_psychology /= total
        self.controversy /= total

DEFAULT_WEIGHTS = ContentWeights()

# ── Self-learning ──
CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive 0-interaction posts → pause
VIRAL_THRESHOLD_LIKES = 50     # post considered "viral" if likes >= this
VIRAL_THRESHOLD_RETWEETS = 20
KOL_VIRAL_THRESHOLD_LIKES = 200  # KOL post considered "viral"

# ── Review/backtest windows ──
REVIEW_WINDOWS_HOURS = [24, 72]  # check post performance at 24h and 72h marks

# ── Browser (Chrome CDP) ──
# 当前目录默认接大号专用 Chrome profile
X_PROFILE_DIR = Path(
    _env_str(
        "PEPPER_X_PROFILE_DIR",
        str(Path.home() / ".config" / "pepperbot" / "x-main-profile"),
    )
).expanduser()
X_DEBUG_PORT = _env_int("PEPPER_X_DEBUG_PORT", 9224)
CHROME_CDP_URL = _env_str("PEPPER_CHROME_CDP_URL", f"http://localhost:{X_DEBUG_PORT}")
SCREENSHOT_DIR = PROJECT_ROOT / "tmp_screenshots"
SESSION_DIR = DATA_DIR / "browser_session"
X_COOKIE_PATH = Path(
    _env_str("PEPPER_X_COOKIE_PATH", str(SESSION_DIR / f"{ACCOUNT_LABEL}_x_cookies.json"))
).expanduser()

# ── Image download ──
IMAGE_CACHE_DIR = PROJECT_ROOT / "tmp_images"
MAX_IMAGE_SIZE_MB = 5  # skip images larger than this

# ── Twitter List for KOL monitoring ──
KOL_LIST_NAME = "Finance-KOL-Monitor"  # private list name
KOL_LIST_URL = "https://x.com/i/lists/2034170120671793445"  # 实际 KOL 监控列表

# ── KOL tiers ──
@dataclass
class KOLTier:
    name: str
    priority: int  # 1=highest
    daily_comment_quota: int
    handles: list[str] = field(default_factory=list)

KOL_TIERS = [
    KOLTier(name="tier1", priority=1, daily_comment_quota=5),
    KOLTier(name="tier2", priority=2, daily_comment_quota=3),
    KOLTier(name="tier3", priority=3, daily_comment_quota=2),
]

# ── Logging ──
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
