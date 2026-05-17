# S3 — LLM Adapter HANDOFF

## 签名

```python
from llm import call_llm, LLMError

text: str = call_llm(
    prompt: str,
    *,
    model: str | None = None,             # None → backend 默认模型
    backend: str | None = None,           # None → env LLM_BACKEND → "claude_cli"
    response_format: Literal["text","json"] = "text",
    timeout: int = 90,                    # 秒
    max_retries: int = 1,                 # 失败后重试次数（共 max_retries+1 次）
)
```

合法 `backend` ∈ `{"claude_cli", "moonshot"}`。默认模型：
- claude_cli → `claude-sonnet-4-6`（env `CLAUDE_CLI_PATH` 指定二进制）
- moonshot → `kimi-k2-0905-preview`（env `MOONSHOT_API_KEY` 必填，`MOONSHOT_BASE_URL` 默认 `https://api.moonshot.cn/v1`）

返回值始终为 `str`。`response_format="json"` 时保证可 `json.loads()`。

## 错误语义

**任何失败 → `LLMError`（RuntimeError 子类），无静默 fallback。** 触发场景：
- backend 不在白名单
- `MOONSHOT_API_KEY` 缺失（moonshot 后端）
- claude CLI 二进制不存在 / exec 失败 / 非零退出 / 空输出
- subprocess 或 HTTP 超时
- HTTP ≥ 400
- moonshot 响应结构异常 / content 为空
- JSON 模式：剥 ``` 包裹后仍不能 `json.loads`，重试 `max_retries` 次后抛

## 使用示例（给 S6 / S9 / S14）

```python
# S6 writer 草稿（文本）
draft = call_llm(prompt, timeout=120)

# S9 reviewer 评分（JSON）
raw = call_llm(score_prompt, response_format="json", max_retries=2)
score = json.loads(raw)

# S14 learner 周复盘（强制走 prod kimi）
report = call_llm(retro_prompt, backend="moonshot", timeout=180)

# 通用错误捕获
try:
    out = call_llm(p)
except LLMError as e:
    logger.warning("llm down: %s", e)
    # 调用方自己决定 retry / skip / 阻断
```

## 交付

- `src/llm.py`（≤ 200 行，单函数 ≤ 50 行，httpx 同步 + subprocess.run）
- `tests/unit/test_llm.py`（14 用例 全绿）
- `pytest tests/unit/test_llm.py`：14 passed
- `mypy --strict src/llm.py`：0 errors
