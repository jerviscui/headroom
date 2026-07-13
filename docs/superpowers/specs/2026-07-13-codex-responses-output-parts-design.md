# Codex Responses 文本 Part 压缩设计

## 背景

Codex CLI 通过 Responses API 提交本地工具结果时，`custom_tool_call_output.output`
既可能是字符串，也可能是内容 part 数组。实际请求显示，普通 shell 命令结果使用数组：

```json
{
  "type": "custom_tool_call_output",
  "call_id": "call_xxx",
  "output": [
    {"type": "input_text", "text": "执行元数据"},
    {"type": "input_text", "text": "stdout"}
  ]
}
```

当前 Responses 适配器只提取字符串形式的 `output`，因此数组中的长文本不会形成
`CompressionUnit`，也不会进入 ContentRouter。仪表盘表现为代理请求正常，但压缩节省始终为零。

## 目标

- 支持 `function_call_output`、`custom_tool_call_output`、`local_shell_call_output`
  和 `apply_patch_call_output` 中数组形式的 `output`。
- 将数组中的每个字符串 `input_text.text` 作为独立、可写的压缩单元。
- 保留原有字符串 `output` 行为。
- 保持 part 顺序、数量、类型和非文本字段不变。
- 继续应用现有角色保护、工具排除、CCR 输出保护和 512 字符下限。

## 非目标

- 不进行 TCP、HTTP 或 WebSocket 网络分包重组。
- 不按 `call_id` 合并多个 Responses item。
- 不把多个文本 part 合并为一个 part。
- 不压缩图片、加密内容或其他非文本 part。
- 不降低全局压缩下限。

## 设计

Responses 适配器把当前单槽提取逻辑扩展为多槽提取：

1. `output` 是字符串时，生成现有的 `("output", None)` 槽。
2. `output` 是数组时，遍历数组元素。
3. 元素是字典、`type == "input_text"` 且 `text` 是字符串时，生成
   `("output_part", part_index)` 槽。
4. 其他元素保持不透明，不生成压缩单元。

写回逻辑根据槽类型更新原字符串字段，或者更新指定数组元素的 `text` 字段。ContentRouter
仍独立决定每个文本 part 是压缩、保持原样还是因小于下限而跳过。

## 数据流

```text
Codex JSON 请求
  -> 提取 output 字符串或 output[].input_text.text
  -> 构造 CompressionUnit
  -> ContentRouter 压缩
  -> 写回原槽位
  -> 保持原 JSON 结构转发上游
```

## 错误处理

- 数组元素缺少 `type`、`text` 或类型不正确时直接跳过。
- 写回时槽位不再存在或结构发生变化时保持原 payload，不修改其他 part。
- ContentRouter 的异常与现有路径一致，继续安全透传原文本。

## 测试

- 回归测试使用实际 Codex 形状：第一个 `input_text` 为 47 字符元数据，第二个为超过
  512 字符的可压缩文本；断言元数据保持不变、长文本被替换、产生非零节省。
- 测试数组中的非文本 part 保持字节等价。
- 保留并运行现有字符串 `output`、排除工具和 CCR 保护测试。

## 成功条件

- 回归测试在修改生产代码前因数组输出未被处理而失败。
- 实现后该测试通过，并且现有 Responses 压缩测试全部通过。
- 修改后的 payload 仍保留原有 part 数量、顺序和类型。
