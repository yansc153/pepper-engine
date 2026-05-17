# S2 Handoff — Observer contract

**Files**: `src/observers/base.py` (210 lines), `src/observers/__init__.py` (empty),
`tests/unit/observers/test_base.py` (16 tests, all green), root `conftest.py` +
`pytest.ini` (importlib mode so `src/observers/` and the test dir can coexist).

**Spec deviation**: `tests/unit/observers/__init__.py` was deleted. With it in
place the bare name `observers` resolves to the empty test package and shadows
`src/observers/`, breaking imports. The empty `src/observers/__init__.py`
remains as required.

## Public surface

```python
AuthorTier   = Literal[0, 1, 2, 3]                       # 0 = news_flash (no learn)
SourceName   = Literal["x_list_finance", "x_list_general", "xueqiu",
                       "futu", "news_flash", "self_monitor"]
TopicLane    = Literal["pre_market", "intraday", "post_market", "overnight",
                       "general_tech_ai", "general_meme_career", "other"]
ContentMode  = Literal["insight", "meme", "emotional"]
OptimalLength = Literal["short", "medium", "long", "article"]

@dataclass(frozen=True, slots=True)
class Observation:
    source: SourceName; author_handle: str; author_tier: AuthorTier
    content: str; posted_at: datetime  # tz-aware UTC
    likes: int; retweets: int; replies: int; impressions: int | None
    has_image: bool; raw_url: str; topic_hint: TopicLane | None

@runtime_checkable
class SourceAdapter(Protocol):
    name: SourceName; cookie_env_key: str; rate_limit_per_hour: int
    async def fetch_latest(self, since: datetime) -> list[Observation]: ...
    async def health_check(self) -> bool: ...

class ObservationValidationError(ValueError): ...

def from_scrape_dict(d, source, tier) -> Observation   # twitter_bot dict → obs
def to_db_row(obs)   -> dict                          # for reaction_observations INSERT
def from_db_row(row) -> Observation                   # reverse
```

## Usage — S5 (adapter authors)

```python
from observers.base import Observation, SourceAdapter, from_scrape_dict

class XListFinanceAdapter:
    name = "x_list_finance"
    cookie_env_key = "TWITTER_COOKIE_FILE"
    rate_limit_per_hour = 12

    async def fetch_latest(self, since):
        raw = await twitter_bot.scrape_list_by_url(self.list_url, max_posts=30)
        return [from_scrape_dict(r, source=self.name, tier=1)
                for r in raw if _coerce_dt(r["created_at"]) > since]

    async def health_check(self) -> bool: ...
```

`from_scrape_dict` accepts these aliases per field: `author/handle/username/screen_name`,
`text/body`, `created_at/timestamp`, `favorite_count/like_count`,
`retweet_count/reposts`, `reply_count/comments`, `view_count/views`,
`has_media/with_image`, `url/tweet_url/link`.

## Usage — S6 (miner / DB)

```python
from observers.base import Observation, to_db_row, from_db_row

cur.execute(
    "INSERT INTO reaction_observations (source, author_handle, author_tier, "
    "content, posted_at, likes, retweets, replies, impressions, has_image, "
    "raw_url, topic_hint) VALUES (:source, :author_handle, :author_tier, "
    ":content, :posted_at, :likes, :retweets, :replies, :impressions, "
    ":has_image, :raw_url, :topic_hint)",
    to_db_row(obs),
)
# round-trip safe: from_db_row(to_db_row(obs)) == obs
```

`posted_at` is ISO-8601 UTC; `has_image` is 0/1. `topic_hint` round-trips as
`None`. `viral_score` / `is_viral` / `observed_at` / `distilled_at` are S6's
responsibility — `to_db_row` deliberately omits them.

## Validation rules (enforced in `__post_init__`)

- naive datetime → `ObservationValidationError`
- unknown `source` or `topic_hint` → `ObservationValidationError`
- tier outside `{0,1,2,3}` → `ObservationValidationError`
- handle leading `@` is stripped only inside `from_scrape_dict`
