# Codex Responses 图片输入优化设计

## 背景

Headroom 当前的 OpenAI 图片优化主要覆盖 Chat Completions：从
`messages[].content[]` 提取 `type == "image_url"` 的图片，使用查询分类器与图片分析器选择
策略，并在适合概览理解时设置 `detail="low"`。

目标客户端使用不同的请求协议：

- `@openai/codex@0.144.1` 通过 OpenAI Responses API 发送请求。
- ChatGPT App `26.707.31428` 可能使用 Responses HTTP 或 WebSocket，并可能以 data URL、
  HTTPS URL 或 `file_id` 引用上传图片。

Responses 图片输入位于 `input[].content[]`，形状为 `type == "input_image"`。当前
`/v1/responses` HTTP 与 WebSocket 路径只优化文本和工具输出，不会将这些图片交给现有
图片优化器，因此 `--image-optimize` 对目标客户端的 Responses 图片输入没有效果。

OpenAI 对 Responses 与 Chat Completions 使用相同的 `detail` 语义。设置 `detail="low"`
不会缩小客户端发送的 URL、base64 或文件引用，但会让 OpenAI 服务端以低分辨率视觉输入
处理图片，从而减少视觉 token。它会影响模型可见细节，因此不能无条件应用于代码、终端、
网页、表格和 UI 截图。

## 目标

- 支持 Codex CLI 与 ChatGPT App 通过 Responses HTTP/WS 上传的图片输入。
- 复用 Headroom 现有查询分类和图片分析能力，而不是维护第二套策略模型。
- 仅在高置信度、非细节任务中将 `input_image.detail` 设置为 `low`。
- 保持图片 URL、base64、`file_id`、part 顺序、数量和未知字段不变。
- 保证历史图片的转换结果跨回合稳定，避免破坏 OpenAI 前缀缓存。
- 图片优化与文本压缩独立开关、独立统计、独立失败处理。
- 无修改时保持当前的原始请求体或原始 WebSocket frame 透传行为。

## 非目标

- 不在第一版中缩放、裁剪、转码或重新编码图片字节。
- 不在第一版中自动执行 OCR `transcode`。
- 不下载 HTTPS 图片，不调用 OpenAI Files API 获取 `file_id` 内容。
- 不修改图片生成或图片编辑请求与响应。
- 不递归修改未经真实客户端流量确认的未知 WebSocket frame。
- 不将视觉 token 估算冒充为上游返回的精确 token savings。

## 已确认的协议形状

Responses 图片输入的标准形状为：

```json
{
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {"type": "input_text", "text": "这张图片是什么？"},
        {
          "type": "input_image",
          "image_url": "data:image/png;base64,...",
          "detail": "auto"
        }
      ]
    }
  ]
}
```

用户消息 item 的 `type` 可能是 `"message"`，也可能省略。提取器必须同时接受
`role == "user" && content 为数组` 与显式 `type == "message"` 的形状，不能把
`type` 作为唯一判定条件。

图片来源可能是以下之一：

```json
{"type": "input_image", "image_url": "data:image/png;base64,..."}
```

```json
{"type": "input_image", "image_url": "https://example.com/image.png"}
```

```json
{"type": "input_image", "file_id": "file_..."}
```

WebSocket 第一帧至少支持以下两种已知形状：

```json
{"type": "response.create", "response": {"model": "...", "input": []}}
```

```json
{"model": "...", "input": []}
```

实现前必须分别捕获 Codex CLI `0.144.1` 与 ChatGPT App `26.707.31428` 的真实图片
请求，移除认证信息、URL 凭据和图片正文后固化为 golden fixtures。只有经过 fixture 确认的
其他 WebSocket frame 才纳入第一版。

## 方案选择

### 方案一：所有图片强制 `low`

优点是实现小、节省稳定。缺点是会让代码、报错、表格、小字体和 UI 定位任务明显退化，
不适合作为默认行为。

### 方案二：Responses 原生适配与保守路由

从 Responses payload 原位提取图片候选项，复用 Headroom 分类能力，仅写回
`input_image.detail`。图片内容可用时组合查询分类与 SigLIP 信号；内容不可用时使用更高
阈值的查询分类。默认保真，失败时透传。

这是选定方案。

### 方案三：代理下载、缩放和重新编码图片

可以同时减少带宽和视觉 token，但会引入外部访问、认证、隐私、延迟、格式兼容和截图
失真风险。该能力应作为未来独立功能设计，不属于本次范围。

## 架构

```text
Codex CLI / ChatGPT App
        |
        +-- Chat Completions image_url --> 现有 ImageCompressor
        |
        +-- Responses input_image
              +-- HTTP /v1/responses
              +-- WS response.create / 已确认 frame
                         |
              ResponsesImageOptimizer
                         |
          提取图片、所属用户消息、查询和槽位
                         |
            决策缓存 --> 图片策略判定
                         |
             preserve 或 detail="low"
                         |
                原位写回并转发上游
```

图片优化不应嵌入只受 `config.optimize` 控制的文本压缩函数。Responses 请求准备层必须分别
执行：

1. `image_optimize` 图片策略；
2. `optimize` 文本/工具输出压缩；
3. 合并两者的 mutation、日志和指标。

这样关闭文本压缩时仍可独立启用图片优化。

## 组件设计

### ResponsesImageExtractor

职责：

- 识别 Responses payload 或已知 WebSocket 包装层。
- 遍历 `input` 中的用户 `message` item。
- 提取每个 `input_image` 的来源、原始 `detail`、所在槽位和所属消息文本。
- 标记候选项属于最新用户回合还是历史回合。
- 不复制、记录或返回可被日志直接打印的完整 base64。

查询必须来自图片所在的用户消息，而不是把当前请求的全部文本拼成一个查询。多张图片位于
同一消息时共享该消息的查询，但每张图片独立决策和写回。

### ImagePolicyAdapter

职责：把现有图片路由器的结果收敛为 Responses 第一版允许的两个动作：

- `preserve`
- `low`

不得通过构造 Chat payload 再调用 `ImageCompressor.compress()`。现有 `compress()` 只识别
Chat/Anthropic/Google 图片结构，并且在无法取得图片字节时直接跳过 ML 路由。适配层应直接
复用以下分类能力：

- data URL 可成功解码：`classify(image_bytes, query)`；
- HTTPS URL 或 `file_id`：`classify_query(query)`；
- 无查询或分类失败：`preserve`。

Responses 第一版将 `crop` 和 `transcode` 映射为 `preserve`，避免改变图片内容或用 OCR 文本
替换图片。

### ResponsesImageDecisionCache

职责：保持重发完整历史时的转换确定性。

缓存键包含：

```text
SHA-256(
  图片来源字符串或解码后图片字节哈希
  + 所属用户消息的 input_text
  + 原始 detail
  + 模型族
  + 图片策略版本
)
```

缓存值只保存决策、置信度分档和原因类别，不保存图片或完整用户文本。

处理规则：

- 最新用户回合：执行分类并写入缓存。
- 历史回合命中缓存：重放原决策，不重新分类。
- 历史回合未命中缓存：使用该历史消息自己的查询确定性重算并缓存。
- 不允许当前回合查询影响历史图片决策。

确定性保证限定在相同策略版本、模型族和分类器可用的正常运行条件内。代理重启后会通过
稳定键和相同策略重算；如果分类器在重算时不可用，安全透传优先于缓存命中，允许该次请求
失去图片优化并记录 `classifier_unavailable`。第一版不引入持久化图片决策存储。

该行为与 Responses 工具输出的结果缓存思路一致：客户端重发原始历史时，代理需要稳定重放
之前的转换，而不能只处理最新槽位后把历史内容恢复为未优化状态。

### ResponsesImageRewriter

职责：在防御性结构检查通过后，仅设置目标 `input_image` 的 `detail`。

允许的写回：

```json
{"type": "input_image", "image_url": "...", "detail": "low"}
```

禁止修改：

- `image_url`；
- base64 内容；
- `file_id`；
- part 数量和顺序；
- 非目标 part；
- 未知字段。

只有字段实际新增或改变时才返回 `modified=True`。

### ResponsesImageOptimizer

职责：编排提取、决策缓存、分类、写回、耗时统计和安全日志，并返回一个明确的结果对象，
至少包括：

- 更新后的 payload；
- 是否修改；
- 候选项数量；
- `low`、`preserve`、`already_low` 数量；
- 原因分类；
- 估算视觉 token；
- 阶段耗时。

## 最新回合与历史回合

用户消息中的普通 `input_text` 不属于现有 Responses 文本压缩候选项。当前文本适配器只压缩
`custom_tool_call_output`、`function_call_output`、`local_shell_call_output` 和
`apply_patch_call_output`。

工具输出会在完整 `input` 上重新应用精确结果缓存，因此不能简单照搬“只遍历最后一个 item”
规则。图片采用相同原则：

- 只让最新回合产生与当前交互有关的新策略决策；
- 对历史回合稳定重放或确定性恢复旧决策；
- 永远不让后续查询改变历史图片的 detail。

## 策略模式

新增 Responses 图片策略模式：

| 模式 | 行为 |
| --- | --- |
| `off` | 不执行 Responses 图片优化 |
| `safe` | 默认；尊重显式 `high/original`，仅高置信度地覆盖省略值或 `auto` |
| `balanced` | 仍尊重 `original`；对普通场景允许较宽松地把省略值或 `auto` 改为 `low` |
| `force-low` | 用户明确选择时，将支持的图片设为 `low`，包括显式高质量值 |

`--image-optimize` 是总开关，模式决定 Responses 图片策略的激进程度。默认模式为 `safe`。

第一版策略表：

| 条件 | 决策 |
| --- | --- |
| 已经是 `low` | 保持不变，记录 `already_low` |
| `safe/balanced` 且显式 `original` | `preserve` |
| `safe` 且显式 `high` | `preserve` |
| 无查询 | `preserve` |
| 查询指向代码、报错、小字、表格、图表、UI 定位或截图细节 | `preserve` |
| data URL 检测到小细节、文档或复杂布局 | `preserve` |
| 路由结果为高置信度 `full_low` | `low` |
| 路由结果为 `crop/transcode` | `preserve` |
| 分类异常、超时或不确定 | `preserve` |
| `force-low` | `low`，除非 part 结构无效 |

精确置信度阈值在实施计划中通过现有路由器输出分布和测试 fixture 确定，不在设计中引入未经
验证的数字。data URL 的组合分类阈值应低于只有查询信号的 URL/`file_id` 阈值。

## 原始字节与序列化

HTTP 处理器继续保存解析后的 payload 与解码后的原始 JSON 请求体：

- 图片与文本均未修改：转发原始请求体，不重新 `json.dumps()`。
- 任一优化修改 payload：序列化更新后的 JSON，并由 mutation tracker 记录原因。

WebSocket 同理：

- 未修改：发送原始 frame 字符串。
- 修改：只替换已确认包装层中的 inner Responses payload 后重新序列化该 frame。

重新序列化不会重新编码图片；base64 字符串内容保持不变。该规则避免无修改请求因空格、
字段顺序或转义方式变化而触发 Codex Desktop 上游兼容问题，也避免无意义复制大型 base64。

## 指标与 token 估算

新增带 Responses 命名空间的独立图片指标：

- `openai_responses_image_units_total`
- `openai_responses_image_units_low`
- `openai_responses_image_units_preserved`
- `openai_responses_image_units_already_low`
- `openai_responses_image_skip_reason`
- `openai_responses_image_source_kind=data_url|url|file_id`
- `openai_responses_image_transport=http|ws`
- `openai_responses_estimated_image_tokens_before`
- `openai_responses_estimated_image_tokens_after`
- `openai_responses_estimated_image_tokens_saved`

图片 token 是估算值，不能直接混入现有精确 `tokens.saved`。当前图片计数中固定的 85/765
假设不适用于所有 GPT-5.x 模型、尺寸和自定义模型别名。估算器必须：

- 按已知模型族和 detail 规则计算；
- 未知模型或未知尺寸时标记为 unknown，不填造精确节省；
- 在 dashboard 中明确标记 estimated；
- 仍使用 `image_units_low` 判断功能是否实际生效。

## 性能与资源

- 分类运行在现有有界压缩 executor，不阻塞事件循环。
- HTTP 与 WS 使用独立的图片分类超时；超时后原样透传。
- 决策缓存避免完整历史和相同图片重复推理。
- 可在代理启动后预加载 ONNX 查询分类器，但预加载失败不能影响代理就绪。
- SigLIP 仍保持可选；不可用时降级为查询分类并提高保守程度。
- 对单请求图片数量设置上限，超出上限的图片保持原样并记录原因，防止恶意或异常请求耗尽
  推理资源。

## 安全与隐私

- 不记录 base64、图片字节、完整图片 URL、URL 查询参数、文件 ID 或完整用户查询。
- 调试日志只记录来源类型、字节数、尺寸、detail、哈希前缀和原因类别。
- 复用现有请求日志 redaction，新增 Responses 图片 fixture 验证。
- 决策缓存只保存不可逆哈希和非敏感决策元数据。
- 不发起额外网络请求获取图片。

## 错误处理

- data URL 解码失败：保持原样。
- 图片格式不受支持：保持原样。
- 路由器未安装、模型下载失败或推理异常：保持原样。
- 分类或 executor 超时：保持原样。
- 写回时槽位不存在、索引变化或类型不匹配：不修改该 part。
- 未知 WebSocket frame：原样透传。
- 图片优化失败不得阻断 Codex/ChatGPT 请求，也不得影响随后执行的文本压缩。

## 集成顺序

### HTTP `/v1/responses`

1. 读取并解析请求，保留原始 JSON 字节。
2. 计算 bypass、图片和文本独立决策。
3. 执行 Responses 图片优化。
4. 执行现有 Responses 文本/工具输出压缩。
5. 合并 mutation tracker、transform 标签和指标。
6. 无修改时发送原始字节，有修改时发送更新后的 JSON。

图片分类应读取客户端原始用户消息，不读取后续 memory 注入生成的内部文本。

### WebSocket `/v1/responses`

1. 解析已确认的客户端到上游 JSON frame。
2. 解包直接 payload 或 `response.create.response`。
3. 对 inner payload 执行与 HTTP 相同的图片优化和文本压缩。
4. 仅在 inner payload 修改时重新包装并序列化。
5. 服务端到客户端事件、图片生成结果和未知 frame 全部透传。

## 测试设计

### 真实客户端 fixture

- Codex CLI `0.144.1` 上传图片的脱敏 HTTP payload。
- ChatGPT App `26.707.31428` 上传图片的脱敏 HTTP/WS payload。
- fixture 必须删除认证、账号标识、URL 凭据和图片正文，只保留协议结构与可控测试图片。

### 提取与写回

- data URL、HTTPS URL、`file_id`。
- 多张图片、多个用户消息、未知 part 和混合 content。
- 直接 Responses payload 与 `response.create.response`。
- 写回只改变目标 `detail`，其他字段、part 数量和顺序不变。
- 无修改时返回原 payload 对象语义并保留原始请求字节路径。

### 策略

- 普通照片与概览问题得到 `low`。
- 代码、终端、报错、表格、图表、小字和 UI 定位保持原样。
- `low/high/original/auto/省略` 全部分支。
- `safe/balanced/force-low/off` 全部模式。
- data URL 组合分类与 URL/`file_id` 查询分类使用不同保守程度。
- `crop/transcode` 在第一版映射为 `preserve`。
- 中文与英文查询至少各覆盖概览和细节保护场景。

### 历史与缓存

- 第一回合将最新图片决定为 `low`。
- 第二回合重发完整历史时，历史图片重放相同决策。
- 新回合查询不会改变历史图片决策。
- 决策缓存命中不重复调用分类器。
- 清空缓存后，相同历史消息确定性恢复相同结果。
- 改变图片策略版本会产生新缓存键。

### HTTP/WS 与失败路径

- HTTP 和 WS 对相同 inner payload 产生相同决策。
- 未知 WS frame 原样透传。
- 分类超时、异常、模型不可用和写回结构变化时安全透传。
- 图片失败不阻止文本压缩。
- bypass 与 `--no-image-optimize` 不调用分类器。

### 隐私与指标

- base64、图片 URL 凭据、文件 ID 和完整查询不进入日志。
- 修改和未修改的计数分类正确。
- 未知模型不产生伪精确 token savings。
- 图片估算不污染现有精确文本 savings。

### 回归

- 现有 Chat Completions 图片优化测试继续通过。
- 现有 Responses 文本工具输出、CCR、工具排除和缓存测试继续通过。
- Codex HTTP/WS 原始字节透传测试继续通过。

## 发布策略

1. 首先以 `safe` 模式和独立指标发布。
2. 使用 fixture 与本地代理验证两个目标客户端都命中适配层。
3. 观察 `openai_responses_image_units_low`、保留原因、超时和请求延迟。
4. 在未发现截图质量回归后再开放 `balanced`。
5. `force-low` 始终要求用户显式选择。

## 成功条件

- Codex CLI 与 ChatGPT App 的真实图片请求均能被识别。
- 普通照片在安全条件下写入 `detail="low"`。
- 代码、终端、网页、表格和 UI 截图默认不降画质。
- 图片内容、来源、part 数量、顺序和未知字段保持不变。
- HTTP 与 WebSocket 对相同 payload 产生一致结果。
- 在相同策略版本和正常分类器条件下，历史图片决策不会随新回合改变，上游转换前缀保持
  稳定。
- 图片优化关闭、跳过、失败和成功原因均可观测。
- 未修改请求继续使用原始请求体或 frame。
- 现有文本压缩、CCR 与 Chat 图片优化无回归。
