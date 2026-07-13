# Codex Responses 文本 Part 与小输出批量压缩设计

## 背景

Codex CLI 通过 OpenAI Responses API 提交本地工具结果时，工具输出存在两种形状：

```json
{
  "type": "custom_tool_call_output",
  "call_id": "call_xxx",
  "output": "stdout"
}
```

```json
{
  "type": "custom_tool_call_output",
  "call_id": "call_xxx",
  "output": [
    {"type": "input_text", "text": "执行元数据"},
    {"type": "input_text", "text": "stdout"},
    {"type": "input_image", "image_url": "data:image/png;base64,..."}
  ]
}
```

Responses 适配器已经能够把数组中的 `input_text.text` 提取为独立、可写的压缩单元，并保持图片等非文本 part 不变。剩余问题是 512B 下限目前应用于每个单元：Codex 会产生大量单个小于 512B、但合计明显超过 512B 的工具输出，因此所有单元都可能在进入 ContentRouter 前被标记为 `size_floor`，最终 `tokens_saved=0`。

直接降低全局下限会让单个微小输出也进入压缩器。把整组过线后的所有单元下限直接设为 0，则仍会对每个小单元分别调用 ContentRouter，放大 tokenizer、内容分类和压缩模型开销，并可能超过 Codex WebSocket 的 5 秒压缩预算。

## 目标

- 保留 Responses 路径现有的 512B 压缩下限。
- 512B 表示一次压缩任务是否值得执行，而不是每个小文本槽位必须单独达到的大小。
- 单个文本达到 512B 时继续使用现有单元压缩、缓存、并发和写回路径。
- 多个小文本合计达到 512B 时，将它们组成有界批次，一次调用 ContentRouter，并按稳定 ID 写回各自原槽位。
- 支持字符串 `output`，以及 `function_call_output`、`custom_tool_call_output`、`local_shell_call_output`、`apply_patch_call_output` 中数组形式的 `output`。
- 保持原有 part 顺序、数量、类型及所有非文本字段不变。
- 继续应用角色保护、工具排除、`headroom_retrieve`/CCR 保护和最终 token 变小校验。
- 失败时安全透传，不因批次解析失败而丢失、交换或合并工具输出。

## 非目标

- 不降低全局或 Rust Responses 路径的 512B 常量。
- 不进行 TCP、HTTP 或 WebSocket 网络分包重组。
- 不删除 Responses item，也不把多个 `call_id` 合并成一个 item。
- 不改变图片、加密内容或其他非文本 part。
- 不为批次结果复用现有单元缓存；批次结果依赖同批其他文本，不能安全写入单元缓存键。
- 不在本次改动中为所有压缩器增加通用向量化 API。

## 方案比较

### 方案一：把 512B 降为 256B

实现最简单，但 100B 左右的输出仍会全部跳过；继续降低则会让大量不值得压缩的微小单元进入模型。该方案没有表达“单个小、整体大”的真实条件，因此不采用。

### 方案二：整组超过 512B 后把每个单元下限设为 0

能够消除 `size_floor`，但仍然是 N 个单元执行 N 次压缩。对于数百个 Codex 工具输出，这会形成大量串行并发批次，并可能触发请求级超时。该方案只聚合了准入判断，没有聚合压缩工作，因此不采用。

### 方案三：受保护的结构化批次

小单元按原始顺序装入有界批次。每个文本由唯一标记包围，标记先通过现有 tag protector 转换为占位符，文本内容仍暴露给 ContentRouter。整个批次只调用一次 `router.compress()`，随后恢复标记、校验 ID 和顺序，再把各段结果写回原槽位。

该方案保留 512B 门槛，减少压缩器调用次数，并能在解析失败时整体回退。采用此方案。

## 数据模型与边界

新增 provider-neutral 批次模块，避免继续扩大 OpenAI handler 内已经较长的单元处理方法：

```python
@dataclass(frozen=True)
class CompressionBatchEntry:
    entry_id: str
    routed: RoutedCompressionUnit


@dataclass(frozen=True)
class CompressionBatch:
    entries: tuple[CompressionBatchEntry, ...]
    text_bytes: int
```

批次模块负责：

1. 按原始顺序区分大单元和小单元。
2. 将小单元贪心装入批次。
3. 构造、保护并解析结构化批次信封。
4. 把一次 RouterCompressionResult 转换为按 entry ID 对齐的单元结果。

OpenAI handler 仍负责：

1. 从 Responses payload 提取可写槽位。
2. 在工具排除和 CCR 保护之后创建 `RoutedCompressionUnit`。
3. 调度大单元任务和批次任务。
4. 按槽位把被接受的结果写回深拷贝 payload。
5. 汇总 token、transform、分类和调试指标。

## 批次规则

- `MIN_BATCH_BYTES = 512`，与 `OPENAI_RESPONSES_ROUTER_MIN_BYTES` 保持一致并由调用方传入。
- `MAX_BATCH_BYTES = 2048`，限制单次批次的模型输入和最坏延迟。
- `MAX_BATCH_UNITS = 16`，避免一个批次包含过多边界。
- `MAX_BATCH_BYTES` 和 `MAX_BATCH_UNITS` 都是上限，不是开始压缩前必须达到的条件；批次只要合计达到 `MIN_BATCH_BYTES` 就具备执行资格。
- 单元大小按 `len(text.encode("utf-8", errors="replace"))` 计算，不使用字符数代替字节数。
- 单个单元达到 512B 时不进入批次，继续走现有单元路径。
- 小单元按请求中的原始顺序贪心装箱；达到单位数或字节上限后关闭当前批次。
- 只有 `provider`、`endpoint`、`role`、`cache_zone`、`mutable`、`context`、`question` 和 `bias` 等路由属性兼容的单元才能进入同一批次。
- 只有原始文本合计达到 512B 的批次才执行压缩。
- 尾部不足 512B 的小单元保持 `size_floor`，不调用 ContentRouter。
- 请求结束时必须 flush 当前未满批次：即使只有 4 个单元、合计 600B，也应执行一次批量压缩；如果合计只有 450B，则按 `size_floor` 安全透传。
- 如果批次先达到 16 个单元但合计仍不足 512B，则这些极小单元整体按 `size_floor` 透传并开始新批次，避免为了少量潜在 token 节省构造过大的标记信封。
- 批次可以包含不同 `call_id`，但每个 entry 必须保留独立 ID、原槽位和写回结果；任何 ID 丢失、重复或重排都会导致整个批次回退。
- 已排除工具、`headroom_retrieve` 输出、非 live cache zone、不可变单元及受保护角色不进入批次。

例如：

```text
150B + 140B + 130B + 120B = 540B
```

旧逻辑执行 0 次路由并全部 `size_floor`；逐项放开方案执行 4 次路由；本方案形成一个 540B 批次并执行 1 次路由。

## 批次信封

每个批次使用内容哈希生成确定性的 nonce，并构造唯一自定义标签：

```text
<headroom-batch-a1b2-u0>
first text
</headroom-batch-a1b2-u0>
<headroom-batch-a1b2-u1>
second text
</headroom-batch-a1b2-u1>
```

调用顺序：

```text
原始批次信封
  -> protect_tags(..., compress_tagged_content=True)
  -> 一次 router.compress(protected_text)
  -> 确认所有占位符仍存在
  -> restore_tags(...)
  -> 按精确标签解析 entry
  -> 校验 ID 集合、数量和顺序
  -> 逐 entry 做 token 变小校验
  -> 写回被接受的 entry
```

预保护标签是必要的：ContentRouter 默认会把完整自定义标签块视为不可压缩内容；`compress_tagged_content=True` 只保护标签标记，让标签之间的文本参与一次整体压缩。

原始文本可能包含任意 XML、日志或 shell 字符。解析器只识别包含批次 nonce 的精确标签，不把信封当作通用 XML 文档解析。

## 接受与回退规则

批次在以下任一情况下整体回退为原文本：

- Router 抛出异常。
- Router 返回原信封或空内容。
- 任一 tag protector 占位符丢失。
- 批次标签缺失、重复、重排或无法唯一解析。
- 解析出的 entry 数量或 ID 与输入不一致。

结构校验通过后，对每个 entry 独立执行 tokenizer 校验：

- `tokens_after < tokens_before`：接受该 entry 的替换。
- `tokens_after >= tokens_before`：该 entry 保留原文本并记录 `rejected_not_smaller`。
- 至少一个 entry 被接受时，批次记为 modified。
- 没有 entry 被接受时，payload 保持不变。

因此一次批次调用可以部分应用，但绝不会把一个 entry 的结果写入另一个槽位。

## 缓存与并发

- 大单元继续使用 `_openai_responses_unit_cache_key` 和现有单元缓存。
- 小单元批次不读写单元缓存，因为同一文本在不同批次上下文中可能得到不同结果。
- 每个批次作为一个 executor job；批次 job 与大单元 job 共用现有 Responses 压缩并发上限。
- handler 按原始 routed-unit 索引保存结果，完成顺序不影响写回顺序。
- 16 个单元的上限把最坏任务数从 N 次调用降低到约 `ceil(N / 16)`，同时避免构造超大批次。

## 可观测性

- 批次内每个 entry 继续贡献自己的 `tokens_before`、`tokens_after` 和 `tokens_saved`。
- 批次实际调用 Router 后，所有 entry 都计入 `attempted_input_tokens`，即使某个 entry 最终因不变小而保留原文。
- 被接受的 entry 记录 `applied`；未变小的 entry 记录 `rejected_not_smaller`。
- 结构校验失败时记录 `batch_invalid`，并包含 batch size、unit count 和失败原因，但不记录完整敏感文本。
- transforms 增加 `router:openai:responses:batch:<strategy>`，同时保留压缩策略名称。
- 调试日志记录 batch ID、entry 槽位、原始/压缩字节数和 Router 调用耗时。

## 与数组 output 的关系

数组输出继续采用多槽提取：

1. 字符串 `output` 生成 `("output", None)` 槽位。
2. 数组中满足 `type == "input_text"` 且 `text` 为字符串的元素生成 `("output_part", part_index)` 槽位。
3. 图片及其他非文本 part 不生成压缩单元。
4. 批次 entry 保存原槽位，写回时只更新对应 `text` 字段。
5. part 数量、顺序、类型和非文本字段保持不变。

## 测试策略

### 批次模块单元测试

- 四个分别小于 512B、合计超过 512B 的单元形成一个批次。
- 批次最多包含 16 个单元且不超过 2048B；尾部不足 512B 时保持未批处理。
- 多字节文本按 UTF-8 字节数进入正确分支。
- 两个以上 entry 只调用一次 Router，并按稳定 ID 返回对应结果。
- 占位符丢失、标签缺失、标签重排和重复 ID 都整体回退。
- 某个 entry 压缩后不变小时只回退该 entry，其他变小 entry 仍可应用。

### Responses 适配器回归测试

- 多个小字符串工具输出合计超过 512B 时产生非零节省，Router 调用次数为 1。
- 多个小 `input_text` part 合计超过 512B 时按原槽位写回，并保留图片和元数据字段。
- 合计低于 512B 时 Router 不运行，所有单元仍归类为 `size_floor`。
- 单个大输出继续走现有缓存路径，不进入批次。
- 工具排除、CCR 保护、字符串 output、数组 output 和 cross-turn dedup 测试继续通过。

### 超时回归

- 使用可计数 Router 验证 381 个小单元最多形成 `ceil(381 / 16) = 24` 个 Router job，而不是 381 个 job。
- 测试批次 job 遵守现有并发上限，结果按输入顺序应用。

## 成功条件

- 保留 512B 常量，且门槛按 UTF-8 字节数执行。
- 多个小单元合计达到 512B 后不再全部 `size_floor`。
- 每个满批次只调用一次 ContentRouter。
- 批次解析失败时 payload 与原输入一致。
- 不同 `call_id` 的结果始终写回各自原槽位。
- 数组 output 的 part 数量、顺序、类型和非文本字段保持不变。
- 新增测试先在旧实现上因缺少批次能力而失败，完成实现后通过。
- 现有 Responses 压缩测试与相关 compression-unit 测试全部通过。
