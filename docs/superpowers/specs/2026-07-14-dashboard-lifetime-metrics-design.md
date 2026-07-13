# Dashboard Lifetime 持久化指标设计

## 状态

- 日期：2026-07-14
- 状态：待实现
- 部署范围：单容器、单 worker
- 关联 Issue：<https://github.com/headroomlabs-ai/headroom/issues/2137>

## 背景

Headroom Dashboard 当前以 `/stats` 的进程内指标作为 Session 主数据源。代理重启后，请求数、Token、Cache Hit 等运行期计数会重新开始，因此用户会看到 Dashboard 大量指标接近归零。现有 `SavingsTracker` 已通过 `proxy_savings.json` 持久化压缩 savings、Historical checkpoints 和 60 分钟无活动窗口的 `display_session`，但这些数据不足以重建完整 Dashboard。

本设计不改变现有 Session 和 Historical 的主要语义，而是新增 Lifetime 范围，展示从首次可用持久化数据开始持续累计的指标。Lifetime 不因代理重启或 60 分钟无活动而清零。

## 目标

1. 新增 `Session | Lifetime | Historical` 三个 Dashboard 范围。
2. Lifetime 的累计指标跨正常重启和容器重建保持。
3. Lifetime 只展示真实持久化的累计指标，不混入实时状态或单请求数据。
4. 保持 `/stats`、`/stats-history` 和现有 `/metrics` series 兼容。
5. 复用 `SavingsTracker` 现有的锁、批量保存、原子替换和 shutdown flush。
6. 支持 `proxy_savings.json` schema v4 到 v5 的安全迁移。
7. 对模型维度进行有界存储，同时给新模型积累到 Top 100 的机会。

## 非目标

本次不实现以下内容：

- 不新增 `HEADROOM_PROXY_TRUSTED_DASHBOARD_HOSTS` 或修改 Requests 安全过滤。
- 不修改 Recent Requests 的空状态、恢复方式或持久化方式。
- 不新增 Codex WebSocket 诊断卡。
- 不在 Lifetime 展示 Latency、Overhead、TTFB 或 Throughput。
- 不在 Lifetime 展示活跃请求、活跃 WebSocket、relay task、队列深度或当前速率。
- 不增加新的 `/metrics` series，也不 hydrate 现有 runtime counters。
- 不为多 worker、多容器或共享文件系统实现分布式锁。
- 不增加外部 Prometheus、VictoriaMetrics 或数据库依赖。
- 不提供 Lifetime 手动重置功能。

## 现状与兼容边界

### Session

Session 页面继续读取 `/stats`。当前页面主要消费 `PrometheusMetrics` 的进程内计数，因此大部分数值在代理重启后归零。后端已有的 60 分钟 `display_session` 保持不变，但本次不将它改造成 Session 页面所有组件的统一数据源。

Session 标题下增加简短范围说明：

```text
Current proxy process · runtime counters reset after restart
```

### Historical

Historical 继续读取 `/stats-history`，保留现有 savings checkpoints、日/周/月 rollup、导出和 CLI filtering 汇总能力。本次不扩大 Historical 的指标范围。

### Lifetime

Lifetime 新增 `/stats-lifetime` 数据源。其累计指标不会因代理重启或 60 分钟无活动而清零。只有 `proxy_savings.json` 被删除、损坏且无法恢复，或用户启用 stateless 模式时，Lifetime 才不可用或重新开始。

## 方案选择

### 采用：扩展 `proxy_savings.json` 到 schema v5

该方案复用现有持久化基础设施，避免增加第二套锁、原子写和错误恢复逻辑。新状态以 `lifetime_metrics` 节点写入同一文件。

### 未采用：独立 `proxy_metrics.json`

独立文件的职责边界更清晰，但会产生两套保存节奏、错误状态和 shutdown flush，并可能出现 savings 文件与 metrics 文件版本不同步。

### 未采用：外部时序数据库

Prometheus/VictoriaMetrics 适合长期监控，但不应成为单容器 Dashboard 正常工作的前置依赖。

## 架构

```text
HTTP / WS 请求处理
        |
        v
PrometheusMetrics.record_*
        |------------------------------|
        v                              v
现有 runtime counters          PersistentMetricsState
        |                              |
        v                              v
Session /stats                 SavingsTracker 标记 dirty
                                       |
                                       v
                             proxy_savings.json schema v5
                                       |
                                       v
                              Lifetime /stats-lifetime
```

### `PersistentMetricsState`

新增 `headroom/proxy/persistent_metrics.py`。该模块负责：

- 定义和规范化 Lifetime 聚合状态。
- 接收请求、失败、限流、Token、Cache、费用和 waste signal 增量。
- 计算百分比、Top 模型和 API 快照。
- 执行模型候选压缩。
- 不执行文件 IO，不拥有线程锁。

### `SavingsTracker`

`SavingsTracker` 继续负责：

- 持有单 worker 进程内锁。
- 加载、迁移和校验 schema v5。
- 将 `PersistentMetricsState` 序列化到 `proxy_savings.json`。
- 批量保存、临时文件写入和原子替换。
- shutdown flush 和持久化健康状态。

### `PrometheusMetrics`

`PrometheusMetrics` 继续拥有现有 runtime counters。其记录入口同时向 `PersistentMetricsState` 发送相同请求增量，但不会用持久化状态初始化 runtime counters。

这保证：

- `/stats` 的重启归零行为保持。
- `/metrics` 现有 runtime series 的重启归零行为保持。
- `/stats-lifetime` 独立返回跨重启累计值。

## schema v5

示意结构如下，最终字段名应在实现中保持稳定并由测试锁定：

```json
{
  "schema_version": 5,
  "lifetime": {},
  "display_session": {},
  "history": [],
  "projects": {},
  "lifetime_metrics": {
    "started_at": null,
    "last_activity_at": null,
    "full_fidelity_started_at": null,
    "requests": {
      "total": 0,
      "cached": 0,
      "failed": 0,
      "rate_limited": 0,
      "by_provider": {},
      "by_stack": {}
    },
    "tokens": {
      "input": 0,
      "output": 0,
      "attempted_input": 0,
      "saved": 0
    },
    "prefix_cache": {
      "requests": 0,
      "hit_requests": 0,
      "cache_read_tokens": 0,
      "cache_write_tokens": 0,
      "cache_write_5m_tokens": 0,
      "cache_write_1h_tokens": 0,
      "uncached_input_tokens": 0,
      "bust_count": 0,
      "bust_tokens": 0,
      "misses_by_reason": {},
      "by_provider": {}
    },
    "cost": {
      "input_usd": 0.0,
      "compression_savings_usd": 0.0,
      "cache_savings_usd": 0.0
    },
    "waste_signals": {},
    "models": {
      "tracked": {},
      "other": {}
    },
    "persistence": {
      "last_saved_at": null
    }
  }
}
```

所有加载值必须经过非负整数、有限非负浮点数和受控字符串键规范化。无效值回退到零，不能让 NaN、Infinity 或错误类型污染累计状态。

## 指标定义

### 请求

- `requests.total`：与现有 `/stats.requests.total` 相同口径的已记录代理请求数。
- `requests.cached`：现有响应缓存命中请求数。
- `requests.failed`：现有失败请求计数。
- `requests.rate_limited`：现有限流请求计数。
- `requests.by_provider`、`requests.by_stack`：累计请求分布。

失败和限流保持现有记录入口语义，不重新定义是否包含在 `requests.total` 中。页面不新增容易产生分母歧义的失败率卡，只展示明确计数。

### Token

- `tokens.input`：实际发送或计费口径下的输入 Token 累计。
- `tokens.output`：输出 Token 累计。
- `tokens.attempted_input`：计算压缩 savings 百分比所需的压缩前分母。
- `tokens.saved`：代理及已确认纳入现有总 savings 口径的 Token 累计。

### Prefix Cache

必须保存计算比例所需的分子和分母，而不是保存最终百分比：

- 总请求与命中请求。
- cache read、write、5m write、1h write 和 uncached input Token。
- cache bust 数量和重写 Token。
- miss reason 和 provider 分布。

### 费用

沿用现有价格估算口径，累计输入费用、压缩节省和 Cache 节省。LiteLLM 不可用时保持现有降级行为，不为 Lifetime 引入新的价格来源。

### Waste Signals

只持久化现有受控 waste signal 名称和 Token 累计，不保存原始内容。

### 其他维度的上限

模型使用单独的候选缓冲策略。其余字符串维度使用更小的固定上限，防止异常或伪造标签让 JSON 无限增长：

- Provider 最多 32 个具名值，超出后按请求数最少者合并到 `other`。
- Stack 最多 64 个具名值，超出后按请求数最少者合并到 `other`。
- Prefix Cache provider 共用 Provider 上限。
- Cache miss reason 和 Waste Signal 只接受代码中已知枚举；未知值统一进入 `other`。

所有 `other` 桶都不参与具名条目上限。

## 派生比例

所有比例在读取快照时计算：

```text
token_savings_percent = saved / attempted_input
cache_hit_rate = hit_requests / prefix_cache.requests
ttl_1h_percent = cache_write_1h_tokens / (cache_write_1h_tokens + cache_write_5m_tokens)
ttl_5m_percent = cache_write_5m_tokens / (cache_write_1h_tokens + cache_write_5m_tokens)
```

分母为零时 API 返回 `null`，Dashboard 显示 `—`，不得显示误导性的 `0%`。

## 模型维度的有界存储

### 对外展示

Lifetime API 和 Dashboard 最多展示：

```text
Token 最高的 100 个具名模型 + other
```

排序分数为：

```text
observed_tokens = input_tokens + output_tokens
```

不能使用 `tokens_saved` 作为排序分数，否则使用量大但没有压缩 savings 的模型会被错误淘汰。

### 内部候选缓冲

内部最多跟踪 200 个具名模型，以便新模型在 Token 较少时仍能继续累计：

- 排名前 100 的模型作为具名结果返回。
- 排名 101 至 200 的模型暂时汇总显示在 `other`，但内部身份和完整累计仍保留。
- 候选模型后续进入前 100 时，自动恢复为具名模型，并携带此前完整累计。
- 当第 201 个具名模型出现时，按 `observed_tokens` 排序，保留前 100，将其余 101 个永久合并到 `models.other`，重新释放约 100 个候选槽位。
- schema v4 无法确定模型身份的旧累计直接迁移到 `models.other`，不保留 `unknown` 桶。

对外 `other` 的值为：

```text
永久 other + 当前排名 101 至 200 的候选模型之和
```

排序相同时依次按更早的 `last_activity_at`、模型名排序，保证压缩结果确定且测试可复现。

## 写入策略

- 每次现有顶层请求记录完成后，同时更新 runtime 和 Lifetime 内存状态。
- 继续使用每 25 个顶层请求批量保存一次完整状态。
- 正常 SIGTERM、容器 stop 和应用 shutdown 强制 flush。
- Codex WS 诊断专用 counters 不进入 Lifetime，也不触发独立磁盘写入。
- 保存成功后更新 `last_saved_at` 并清零 pending 计数。
- 保存失败时保留 dirty 状态和内存累计，下一个批次继续重试。

正常 shutdown 不丢失尾部数据。强制断电、进程崩溃或 `SIGKILL` 最多可能丢失最后 24 个尚未 flush 的顶层请求累计；这是减少每请求 JSON 序列化和 fsync 的明确取舍。

## 原子保存与异常恢复

### 保存

沿用现有同目录临时文件、flush、fsync 和原子替换流程。只有原子替换成功后才更新持久化健康状态。

### 读取失败

如果 JSON 无法解析或顶层结构无效：

1. 记录 warning，不阻止代理启动。
2. 尝试将原文件保留为 `proxy_savings.json.corrupt-<timestamp>`。
3. 使用空状态继续运行。
4. `/stats-lifetime.persistence.healthy` 返回 `false`，Dashboard 显示持久化警告。

如果损坏文件无法重命名，仍不得覆盖原始错误原因；日志和 API 状态必须说明恢复失败。

如果文件的 `schema_version` 高于当前代码支持的版本，当前进程不得用旧结构覆盖该文件。代理继续提供 runtime 功能，但 Lifetime 持久化进入只读 degraded 状态；`/stats-lifetime.persistence.error` 明确返回 unsupported schema，直到用户升级程序或人工恢复文件。

### 写入失败

写入失败不影响代理请求。Lifetime 页面可继续显示内存中的最新累计，但必须显示尚未成功持久化的警告和 pending 数量。

### Stateless

`--stateless` 模式下 `/stats-lifetime` 返回 `persistence.enabled = false`。Dashboard 不展示全零 Lifetime 卡片，而是显示：

```text
Lifetime metrics unavailable in stateless mode
```

### 更新版本回滚

schema v4 代码不认识 `lifetime_metrics`，回滚到旧版本后旧代码可能在下一次保存时删除 v5-only 字段。现有 savings、history 和 display session 仍保留，但完整 Lifetime 指标可能需要从回滚后的首次 v5 启动重新累计。该限制应在发布说明中明确。

## v4 到 v5 迁移

迁移过程只执行一次，并保留现有字段：

- `lifetime.requests` 迁移到 `lifetime_metrics.requests.total`。
- `lifetime.tokens_saved` 迁移到 `lifetime_metrics.tokens.saved`。
- `lifetime.total_input_tokens` 迁移到 `tokens.input`。
- `tokens.attempted_input` 使用 `lifetime.total_input_tokens + lifetime.tokens_saved` 初始化，与现有压缩 savings 分母口径一致。
- `lifetime.cache_read_tokens` 迁移到 Prefix Cache read Token。
- 现有输入费用、压缩节省和 Cache 节省迁移到对应费用字段。
- 无法恢复的 Output Token、Failed、Rate Limited、Cache requests 和 hit requests 从零开始。
- 旧模型归属无法可靠重建，迁移到 `models.other`。

时间范围：

- `started_at`：优先使用最早 Historical checkpoint；其次使用现有 display session 开始时间；最后使用迁移时间。
- `last_activity_at`：优先使用最新 checkpoint 或 display session 最后活动时间。
- `full_fidelity_started_at`：固定为首次初始化 schema v5 的时间。

Lifetime 页面应提示：

```text
Lifetime data since <started_at>
Full metric coverage since <full_fidelity_started_at>
```

## `/stats-lifetime` API

新增：

```http
GET /stats-lifetime
```

该端点只返回聚合值，不返回 Request ID、Provider URL、错误正文、原始请求内容或 Recent Requests，因此不复用 `/stats` 的敏感请求明细权限逻辑。

示例：

```json
{
  "scope": "lifetime",
  "schema_version": 5,
  "generated_at": "2026-07-14T10:00:00Z",
  "started_at": "2026-07-01T00:00:00Z",
  "last_activity_at": "2026-07-14T09:59:30Z",
  "full_fidelity_started_at": "2026-07-14T00:00:00Z",
  "requests": {},
  "tokens": {},
  "prefix_cache": {},
  "cost": {},
  "waste_signals": {},
  "by_model": {},
  "persistence": {
    "enabled": true,
    "healthy": true,
    "last_saved_at": "2026-07-14T09:59:00Z",
    "pending_records": 3,
    "error": null
  }
}
```

端点失败原则：

- 读取内存快照不执行磁盘 IO。
- 持久化降级不返回 HTTP 500；通过 `persistence` 状态表达。
- 只有应用状态本身不可用时才返回服务错误。

## Dashboard 设计

### 导航和轮询

- 保持 `Session | Lifetime | Historical`。
- Session 继续按现有频率轮询 `/stats`。
- Lifetime 仅在选中时轮询 `/stats-lifetime`，建议频率低于 Session。
- Historical 继续读取 `/stats-history`。

### Lifetime 组件

Lifetime 展示：

- Requests、Failed、Rate Limited、Cached。
- Input、Output、Attempted Input 和 Saved Tokens。
- Token Savings 百分比。
- 输入费用、压缩节省和 Cache 节省。
- Provider、Stack 和 Top 100 Model + Other 分布。
- Prefix Cache requests、hits、read/write Token、TTL mix、Cache Bust 和 miss attribution。
- Waste Signals。
- 数据开始时间、完整覆盖开始时间和持久化健康状态。

Lifetime 不展示：

- Recent Requests。
- 活跃请求、活跃 WebSocket、relay task、队列或当前速率。
- Latency、Overhead、TTFB、Throughput。
- Codex WS units/frame 诊断数据。
- 单请求详情和原始日志。

### Session 补充

Session 保持现有全部组件，并增加：

1. 标题下范围说明：`Current proxy process · runtime counters reset after restart`。
2. Request Health：Completed、Failed、Rate Limited、Cached 明确计数。
3. Live Activity：Active Requests、Active WebSockets、Relay Tasks、Compression Queued、Compression Running、Queue Timeouts。

Request Health 优先复用现有 `/stats.requests`。Live Activity 以 additive 方式整理 `/stats` 已有 `proxy_inbound` 和 health collector 数据，不改变已有字段。

本次明确不修改 Recent Requests 空状态，也不增加 Codex WS 诊断卡。

## `/metrics` 兼容性

本次不增加任何 `/metrics` series。

- 现有 runtime series 保持名称、语义和重启归零行为。
- 现有 `headroom_persistent_savings_*` series 继续从现有 savings lifetime 字段导出。
- 新增的 Lifetime request、error、output-token 和 cache-hit 聚合只通过 `/stats-lifetime` 提供。
- `PrometheusMetrics` 不从 `PersistentMetricsState` hydrate runtime counters。

该选择意味着外部 Prometheus 暂时无法查询所有新 Lifetime 指标，但避免扩大本次兼容面，后续可独立设计。

## 测试设计

### `PersistentMetricsState` 单元测试

- 所有计数正确累加，负数和无效值被规范化。
- 百分比由分子和分母计算，零分母返回 `null`。
- Provider、Stack、Cache reason 和 Waste Signal 的键规范化。
- 模型排名使用 input + output Token，而不是 tokens saved。
- 101 至 200 名候选计入 API `other`，身份仍保留。
- 候选模型增长到前 100 后恢复具名并保留完整累计。
- 第 201 个模型触发确定性压缩，永久 other 与总计保持一致。
- schema v4 旧模型数据进入 other，不产生 unknown。

### 持久化测试

- schema v5 round trip。
- 每 25 个顶层请求批量保存。
- shutdown flush 保存尾部记录。
- 重新创建 `SavingsTracker` 后 Lifetime 值保持。
- 60 分钟 display session 变化不影响 Lifetime。
- 保存失败后 dirty 状态保留并可重试。
- JSON 损坏备份和 degraded 状态。
- stateless 模式不写文件并明确返回 disabled。
- future/unknown schema 不被静默破坏。

### API 测试

- `/stats-lifetime` 返回稳定结构和派生比例。
- `/stats-lifetime` 不包含 Recent Requests 或敏感字段。
- 持久化失败仍返回聚合快照和 degraded 状态。
- `/stats` 与 `/stats-history` 现有响应保持兼容。
- `/metrics` 不新增 series，现有 persistent savings series 继续工作。

### Dashboard 测试

- 三个范围正确切换和按需轮询。
- Lifetime 不渲染 Live、Recent、Performance 和 Codex WS 组件。
- 零分母显示 `—`。
- stateless 和 degraded 状态显示明确提示。
- Session 显示范围说明、Request Health 和 Live Activity。
- Recent Requests 现有空状态保持不变。

## 发布与运维

- Docker 继续挂载 Headroom workspace，例如 `/data/headroom:/home/nonroot/.headroom`。
- `proxy_savings.json` 仍位于已挂载 workspace，因此容器重建后可读取。
- 单 worker 部署不需要额外配置。
- 发布说明应指出 v5-only Lifetime 数据在回滚到 schema v4 代码后可能丢失。
- 若需要跨多容器、任意历史查询或告警，仍建议在独立方案中接入外部时序数据库。

## 验收标准

1. 代理处理请求后，Lifetime 页面显示累计请求、Token、费用和 Cache 指标。
2. 正常重启代理或重建容器后，Lifetime 数值保持并继续累加。
3. Session runtime 指标仍按当前行为在重启后归零，页面明确说明范围。
4. 60 分钟无活动不会重置 Lifetime。
5. Historical 页面和 `/stats-history` 行为不变。
6. Lifetime 不显示任何实时状态、Recent Requests、Performance 或 Codex WS 诊断组件。
7. Dashboard 最多展示 100 个具名模型和 other，新模型可通过候选区积累并进入前 100。
8. `/metrics` 不新增 series，现有 series 输出保持兼容。
9. 持久化失败不会中断代理流量，并在 Lifetime 页面明确显示 degraded 状态。
