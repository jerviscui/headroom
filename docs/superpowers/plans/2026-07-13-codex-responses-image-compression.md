# Codex Responses 图片输入优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 OpenAI Responses 的 HTTP 与已确认 WebSocket 请求在不修改图片来源或未变更字节的前提下，对安全的图片输入设置 `detail="low"`。

**Architecture:** 新增一个独立的 Responses 图片适配器，负责提取、策略决策、确定性缓存和原位写回；它通过既有的图片路由器做分类，但绝不调用会重编码图片的 `ImageCompressor.compress()`。HTTP 与 WebSocket 调用同一个适配器，并把图片 mutation 与现有文本压缩的 mutation tracker 合并，以保留既有原始字节/frame 透传语义。

**Tech Stack:** Python 3.10+、FastAPI、websockets、pytest、现有 ONNX `OnnxTechniqueRouter` / `TrainedRouter`、既有 `BodyMutationTracker`。

## Global Constraints

- `--image-optimize` / `config.image_optimize` 是图片总开关；不能被 `config.optimize`（文本压缩）隐式控制。
- 第一版仅允许把有效 `input_image.detail` 写成 `low`；不得下载、缩放、裁剪、转码、OCR 或改变 `image_url`、`file_id`、part 顺序和未知字段。
- 未修改 HTTP 请求必须转发 `original_body_bytes`；未修改 WS 客户端 frame 必须转发原始字符串。
- 只支持已由脱敏 golden fixture 确认的 Responses payload 和 WS 包装层；未知 frame 原样透传。
- 分类异常、超时、缺少依赖、无查询和无效结构均安全保留图片，并且不能阻断文本压缩或请求转发。
- 决策缓存只保存不可逆哈希、动作、置信度分档和原因类别；日志不得包含 base64、完整 URL/查询、file_id 或完整用户查询。
- 图片视觉 token 仅能作为 `estimated` 独立指标，绝不能加入既有精确 `tokens_saved`。

---

## File Structure

- Create: `headroom/proxy/responses_image_optimizer.py` — 纯同步的提取、策略、LRU 决策缓存、原位写回和估算结果值对象。
- Modify: `headroom/image/compressor.py` — 公开只读路由适配 API，复用既有路由器而不进入图片重编码管线。
- Modify: `headroom/proxy/server.py` — 在 `ProxyConfig` 中增加 `responses_image_mode` 及受限解析。
- Modify: `headroom/proxy/handlers/openai.py` — 在 HTTP、WS 首帧和后续 `response.create` 路径调用同一适配器，合并 mutation 与标签。
- Modify: `headroom/proxy/metrics.py`（或实际 metrics façade）— 图片独立计数和估算值记录入口。
- Create: `tests/fixtures/openai_responses_images/` — Codex CLI / ChatGPT App 的脱敏协议形状与受控小图片 fixture。
- Create: `tests/test_responses_image_optimizer.py` — 适配器、缓存、策略、privacy 与估算单元测试。
- Modify: `tests/test_proxy_byte_faithful_forwarding.py` — HTTP 未修改/修改字节路径回归。
- Modify: `tests/test_openai_codex_ws_lifecycle.py` — 已确认 WS 包装层的原始 frame / 重写回归。
- Modify: `tests/test_handler_outcome_tag_invariant.py` — 防止在 handler 中重新引入图片开关内联判断。

## Interfaces

```python
ImageSourceKind = Literal["data_url", "url", "file_id"]
ResponsesImageMode = Literal["off", "safe", "balanced", "force-low"]

@dataclass(frozen=True, slots=True)
class ResponsesImageOptimizationResult:
    payload: dict[str, Any]
    modified: bool
    candidates: int
    low: int
    preserved: int
    already_low: int
    skip_reasons: Counter[str]
    estimated_tokens_before: int | None
    estimated_tokens_after: int | None

class ResponsesImageOptimizer:
    def optimize(
        self,
        payload: dict[str, Any],
        *,
        model: str,
        mode: ResponsesImageMode,
        transport: Literal["http", "ws"],
    ) -> ResponsesImageOptimizationResult: ...
```

`OpenAIHandlerMixin` owns one `ResponsesImageOptimizer`; its cache lives for the proxy lifetime. The image optimizer returns the same payload object when it has no mutation, so handlers can use `result.modified` as the sole serialization gate.

### Task 1: 固化真实客户端边界与配置契约

**Files:**
- Create: `tests/fixtures/openai_responses_images/codex-cli-0.144.1-http.json`
- Create: `tests/fixtures/openai_responses_images/chatgpt-app-26.707.31428-http.json`
- Create: `tests/fixtures/openai_responses_images/chatgpt-app-26.707.31428-ws-response-create.json`
- Modify: `headroom/proxy/server.py`
- Test: `tests/test_responses_image_optimizer.py`

**Consumes:** 设计文档的已确认 payload 形状，以及真实客户端脱敏采集。

**Produces:** `ProxyConfig.responses_image_mode: Literal["off", "safe", "balanced", "force-low"]`，默认 `"safe"`；可被后续 adapter 直接读取。

- [ ] **Step 1: 采集并审查 fixture，再写会失败的配置与 fixture 加载测试**

在真实 Codex CLI 0.144.1、ChatGPT App 26.707.31428 的图片请求中采集 HTTP/WS 协议形状。删除 `authorization`、账号标识、URL 凭据、真实 file ID、完整图片正文；仅使用仓库内受控 1x1 PNG data URL。测试必须证明三份 fixture 可解析、分别包含 `input_image`，且 WS fixture 的图片只位于 `response.create.response.input`。

```python
@pytest.mark.parametrize("name", ["codex-cli-0.144.1-http", "chatgpt-app-26.707.31428-http"])
def test_captured_http_fixtures_have_only_sanitized_image_sources(name: str) -> None:
    payload = _load_fixture(name)
    assert _input_images(payload)
    assert "Authorization" not in json.dumps(payload)

def test_responses_image_mode_defaults_to_safe() -> None:
    assert ProxyConfig().responses_image_mode == "safe"
```

- [ ] **Step 2: 运行测试确认它因 fixture/config 尚不存在而失败**

Run: `uv run pytest tests/test_responses_image_optimizer.py -q`

Expected: FAIL，指出缺少 fixture 或 `ProxyConfig.responses_image_mode`。

- [ ] **Step 3: 添加受限配置字段和经审查的 fixture**

在 `ProxyConfig` 使用字面量或等价验证器，并拒绝未知 mode。fixture 只保留结构所需字段：`model`、`input`、`input_text`、`input_image`、`detail` 和无害未知字段；WS 使用 `{ "type": "response.create", "response": { ... } }`。

```python
responses_image_mode: Literal["off", "safe", "balanced", "force-low"] = "safe"
```

- [ ] **Step 4: 运行 fixture/config 测试确认通过**

Run: `uv run pytest tests/test_responses_image_optimizer.py -q`

Expected: PASS。

- [ ] **Step 5: 提交协议和配置契约**

```text
git add headroom/proxy/server.py tests/fixtures/openai_responses_images tests/test_responses_image_optimizer.py
git commit -m "test: capture Responses image request shapes"
```

### Task 2: 创建 Responses 图片提取器、写回器与确定性缓存

**Files:**
- Create: `headroom/proxy/responses_image_optimizer.py`
- Test: `tests/test_responses_image_optimizer.py`

**Consumes:** Task 1 fixture，`hashlib`、`OrderedDict` 和 `input[].content[]` 的官方协议形状。

**Produces:** `ResponsesImageOptimizer.optimize()` 的结构校验、slot 定位和 `ResponsesImageDecisionCache`；尚不调用 ML。

- [ ] **Step 1: 写提取/写回/cache 的失败测试**

覆盖 data URL、HTTPS URL、`file_id`、同一消息多图、多个用户 message、未知 part、`type` 缺省的用户 message、`detail` 缺省与已有 `low`。断言唯一允许变更是目标 part 的 `detail`，且 data URL 字符串、未知字段、part 顺序与数量逐字相同。

```python
def test_rewriter_only_changes_target_detail() -> None:
    original = _load_fixture("codex-cli-0.144.1-http")
    result = _optimizer_with_decision("low").optimize(
        deepcopy(original), model="gpt-5", mode="safe", transport="http"
    )
    assert result.payload["input"][0]["content"][1]["detail"] == "low"
    assert _without_detail(result.payload) == _without_detail(original)

def test_cache_key_uses_each_images_own_message_query() -> None:
    assert _cache_key(image="a", query="first") != _cache_key(image="a", query="second")
```

- [ ] **Step 2: 运行测试确认提取器尚未定义**

Run: `uv run pytest tests/test_responses_image_optimizer.py -q`

Expected: FAIL，缺少 `ResponsesImageOptimizer` / `ResponsesImageDecisionCache`。

- [ ] **Step 3: 最小实现 slot 和 LRU cache**

提取仅处理 `role == "user" and isinstance(content, list)` 的 item；`type == "message"` 可作为兼容信号但不是门槛。每个 candidate 持有 `(message_index, part_index)`、源类型、原 detail、该 message 内全部 `input_text.text` 拼接出的查询。cache key 对 data URL 使用解码字节 SHA-256，其他来源使用来源字符串 SHA-256；再混入 query SHA-256、原 detail、模型族和 `IMAGE_POLICY_VERSION`。缓存值不包含来源或查询明文。

```python
if decision == "low" and part.get("detail") != "low":
    part["detail"] = "low"
    modified = True
```

- [ ] **Step 4: 运行适配器单测确认通过**

Run: `uv run pytest tests/test_responses_image_optimizer.py -q`

Expected: PASS。

- [ ] **Step 5: 提交提取、写回和 cache 基础**

```text
git add headroom/proxy/responses_image_optimizer.py tests/test_responses_image_optimizer.py
git commit -m "feat: add Responses image extraction and decision cache"
```

### Task 3: 复用图片路由器并实现保守策略

**Files:**
- Modify: `headroom/image/compressor.py`
- Modify: `headroom/proxy/responses_image_optimizer.py`
- Test: `tests/test_responses_image_optimizer.py`

**Consumes:** Task 2 candidate/cache，`OnnxTechniqueRouter.classify_query()` 和 `classify()` 的 `Technique` / `RouteDecision`。

**Produces:** 无图片重编码的 `ImageCompressor.classify_for_detail(image_bytes, query)` / `classify_query_for_detail(query)`，及 `safe`、`balanced`、`force-low`、`off` 决策。

- [ ] **Step 1: 写策略失败测试**

mock 路由器而非下载模型。验证 `safe` 不覆盖显式 `high/original`，`balanced` 不覆盖 `original`，`force-low` 覆盖所有有效 detail，`crop/transcode` 始终保留，URL/file_id 只走 query 分类，data URL 可走组合分类；无 query、异常和阈值未达标均保留。至少覆盖中英文概览和代码/表格/UI 细节查询。

```python
def test_safe_preserves_explicit_high_even_when_router_returns_full_low() -> None:
    result = _optimizer_with_route(Technique.FULL_LOW, 0.99).optimize(
        _payload(detail="high"), model="gpt-5", mode="safe", transport="http"
    )
    assert result.modified is False
    assert result.skip_reasons["explicit_high"] == 1
```

- [ ] **Step 2: 运行测试确认策略尚未接入路由器**

Run: `uv run pytest tests/test_responses_image_optimizer.py -q`

Expected: FAIL，safe/force-low 或路由调用断言失败。

- [ ] **Step 3: 添加最小的只读路由适配 API 与策略表**

`ImageCompressor` 新 API 只能委托 `_get_router()` 的分类方法，不调用 `compress()`、`_apply_compression()` 或 tile optimizer。适配器将 `Technique.FULL_LOW` 映射为候选 `low`，将 `PRESERVE`、`CROP`、`TRANSCODE` 映射为 `preserve`；阈值使用命名常量，data URL 阈值低于 URL/file_id 阈值。`off` 在进入分类器前返回 preserve，已有 `low` 不触发分类。

```python
def classify_query_for_detail(self, query: str) -> tuple[Technique, float]:
    return self._get_router().classify_query(query)

def classify_for_detail(self, image_bytes: bytes, query: str) -> RouteDecision:
    return self._get_router().classify(image_bytes, query)
```

- [ ] **Step 4: 运行策略和既有图片测试**

Run: `uv run pytest tests/test_responses_image_optimizer.py tests/test_image_compressor.py -q`

Expected: PASS。

- [ ] **Step 5: 提交策略适配器**

```text
git add headroom/image/compressor.py headroom/proxy/responses_image_optimizer.py tests/test_responses_image_optimizer.py
git commit -m "feat: route Responses images to conservative detail policy"
```

### Task 4: 集成 HTTP、原始字节与图片观测

**Files:**
- Modify: `headroom/proxy/handlers/openai.py`
- Modify: `headroom/proxy/metrics.py`
- Modify: `tests/test_proxy_byte_faithful_forwarding.py`
- Modify: `tests/test_responses_image_optimizer.py`

**Consumes:** Task 2/3 的 `ResponsesImageOptimizer` 结果、`BodyMutationTracker.mark_mutated()`、已有压缩 executor。

**Produces:** `/v1/responses` 在 memory 注入前用客户端原始 `body["input"]` 做图片决策；图片与文本独立执行、合并 mutation，并记录独立图片指标。

- [ ] **Step 1: 写 HTTP 集成失败测试**

通过现有 fake upstream 捕获 body bytes。断言图片关闭或详细任务时收到的字节与入站 bytes 完全相同；普通图片被降为 low 时只有 detail 变化且 mutation reason 为 `responses_image_detail_low`；图片分类异常仍允许工具输出文本压缩。验证 bypass 与 `image_optimize=False` 不实例化/调用分类器。

```python
def test_responses_image_no_mutation_preserves_original_request_bytes() -> None:
    raw = _openai_responses_body_bytes(detail="high")
    assert _post_through_proxy(raw).upstream_body == raw

def test_responses_image_mutation_uses_canonical_bytes() -> None:
    assert json.loads(_post_through_proxy(_openai_responses_body_bytes()).upstream_body)["input"][0]["content"][1]["detail"] == "low"
```

- [ ] **Step 2: 运行 HTTP 测试确认当前 handler 不会改 Responses 图片**

Run: `uv run pytest tests/test_proxy_byte_faithful_forwarding.py tests/test_responses_image_optimizer.py -q`

Expected: FAIL，优化 detail 的断言失败。

- [ ] **Step 3: 在 HTTP 请求准备层执行独立图片阶段**

在 `handle_openai_responses()` 创建 `ImageCompressionDecision`，在它允许且 `responses_image_mode != "off"` 时通过现有有界 compression executor 调用 image optimizer；然后再执行现有 `_compress_openai_responses_payload_in_executor()`。图片 `modified` 时调用 `body_mutation_tracker.mark_mutated("responses_image_detail_low")`，并把 `responses:image:detail_low` 加入 transforms；不把估算值加入 `tokens_saved`。为 metrics façade 添加一个不含敏感值的 `record_responses_image_optimization(result, transport)` 方法。

- [ ] **Step 4: 运行 HTTP 回归测试**

Run: `uv run pytest tests/test_proxy_byte_faithful_forwarding.py tests/test_openai_responses_compression_units.py tests/test_responses_image_optimizer.py -q`

Expected: PASS。

- [ ] **Step 5: 提交 HTTP 集成**

```text
git add headroom/proxy/handlers/openai.py headroom/proxy/metrics.py tests/test_proxy_byte_faithful_forwarding.py tests/test_responses_image_optimizer.py
git commit -m "feat: optimize Responses image detail over HTTP"
```

### Task 5: 集成已确认 WebSocket frame 且保持 frame 保真

**Files:**
- Modify: `headroom/proxy/handlers/openai.py`
- Modify: `tests/test_openai_codex_ws_lifecycle.py`
- Modify: `tests/test_responses_image_optimizer.py`

**Consumes:** Task 4 optimizer 与 metrics，已有 `response.create` 首帧/后续帧压缩代码。

**Produces:** 仅针对直接 Responses payload 和 fixture 确认的 `response.create.response` 内层 payload 的 WS detail 优化。

- [ ] **Step 1: 写 WS 失败测试**

使用现有 fake client/upstream 验证：同一 inner payload 的 HTTP 与 WS 得到相同 detail；未变更 `response.create` 字符串逐字转发；优化时只重新序列化该确认 wrapper；`response.cancel` 和未知 event 逐字转发。加入完整历史重发：首回合 low 决策、后续新查询不改变历史图片、cache hit 不重复调用分类器。

```python
@pytest.mark.asyncio
async def test_unknown_ws_frame_is_forwarded_verbatim() -> None:
    raw = '{ "type" : "response.cancel", "opaque" : true }'
    assert await _forward_one_frame(raw) == raw
```

- [ ] **Step 2: 运行 WS 测试确认当前实现不改图片 detail**

Run: `uv run pytest tests/test_openai_codex_ws_lifecycle.py tests/test_responses_image_optimizer.py -q`

Expected: FAIL，`detail == "low"` 断言失败。

- [ ] **Step 3: 在现有 WS `response.create` 处理点接入 image stage**

在 JSON 解析成功后仅识别 `(frame["type"] == "response.create" and isinstance(frame.get("response"), dict))` 与 fixture 已确认的直接 body。传入 inner payload、`transport="ws"`，并把 result 的 modified 与既有文本 modified 合并。只有 modified 时 `json.dumps` wrapper；否则保留原始 `str` frame。每个 frame 调用独立 metrics 入口，不能影响 `response.cancel`、服务端事件和 fallback 的 body 解析。

- [ ] **Step 4: 运行 WS 与 Responses 回归**

Run: `uv run pytest tests/test_openai_codex_ws_lifecycle.py tests/e2e_ws_responses_compression.py tests/test_openai_responses_compression_units.py tests/test_responses_image_optimizer.py -q`

Expected: PASS（若 e2e 需要外部服务，记录为 skip，不把 skip 表述为通过）。

- [ ] **Step 5: 提交 WS 集成**

```text
git add headroom/proxy/handlers/openai.py tests/test_openai_codex_ws_lifecycle.py tests/test_responses_image_optimizer.py
git commit -m "feat: optimize Responses image detail over websocket"
```

### Task 6: 完成 metrics、隐私、失败路径与全量验证

**Files:**
- Modify: `headroom/proxy/metrics.py`
- Modify: `headroom/proxy/request_logger.py`
- Modify: `tests/test_responses_image_optimizer.py`
- Modify: `tests/test_image_log_redaction.py`
- Modify: `tests/test_handler_outcome_tag_invariant.py`

**Consumes:** 前序 tasks 的 results、request logger redaction 与指标 façade。

**Produces:** 可观测但不泄露敏感内容的 Responses 图片统计；完整回归保护。

- [ ] **Step 1: 写失败路径与 redaction 测试**

断言每次图片结果记录 `openai_responses_image_units_total/low/preserved/already_low` 和 source/transport/skip reason；已知尺寸/模型才记录 `estimated_*`，未知信息为 `None` 而不是编造值。构造 data URL、带凭据 URL、file_id、查询，断言日志/metric labels 均不包含原文。模拟 decode、分类、executor timeout 和写回 slot 失效，全部 preserve 且文本压缩仍继续。

```python
def test_image_logs_never_contain_source_or_query(caplog: pytest.LogCaptureFixture) -> None:
    _optimizer_that_logs().optimize(_payload_with_secret_source(), model="gpt-5", mode="safe", transport="http")
    assert "secret-token" not in caplog.text
    assert "file_sensitive" not in caplog.text
```

- [ ] **Step 2: 运行测试确认新指标/redaction 尚不完整**

Run: `uv run pytest tests/test_responses_image_optimizer.py tests/test_image_log_redaction.py tests/test_handler_outcome_tag_invariant.py -q`

Expected: FAIL，缺失指标或泄露/失败路径断言。

- [ ] **Step 3: 最小实现独立观测与安全日志**

指标标签只允许 `transport`、`source_kind`、`reason`、`mode` 和模型族，不使用 URL、file ID、query 或 hash 作为 label。debug 日志最多写 source 类型、字节数、尺寸、detail、短 hash 前缀和理由类别。扩展 handler invariant，要求 Responses 图片必须经 `ImageCompressionDecision` 和适配器，而不能出现新的 `self.config.image_optimize` 内联 gate。

- [ ] **Step 4: 运行目标测试及全量回归**

Run: `uv run pytest tests/test_responses_image_optimizer.py tests/test_image_log_redaction.py tests/test_proxy_byte_faithful_forwarding.py tests/test_openai_responses_compression_units.py tests/test_openai_codex_ws_lifecycle.py -q`

Expected: PASS。

Run: `uv run pytest -q`

Expected: PASS，或列出与本改动无关的既有失败并取得继续处理授权。

- [ ] **Step 5: 按设计逐项复核并提交**

逐项核对：图片来源/顺序不变、HTTP/WS 等价、历史缓存稳定、关闭/异常透传、估算与精确 token 分离、已确认 frame 白名单、敏感信息不入日志。随后提交。

```text
git add headroom/proxy/metrics.py headroom/proxy/request_logger.py tests/test_responses_image_optimizer.py tests/test_image_log_redaction.py tests/test_handler_outcome_tag_invariant.py
git commit -m "feat: observe Responses image optimization safely"
```
