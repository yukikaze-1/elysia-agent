# describe_image 视觉工具 测试清单

## 准备

```bash
# 确保 MIMO API Key 已配置
echo $MIMO_API_KEY

# 确保 DEEPSEEK_API_KEY 已配置
echo $DEEPSEEK_API_KEY

# 启动服务
python -m Module.input
```

---

## 1. 基础流程 — DeepSeek + 图片

- [ ] LLM 设为 **DeepSeek**（`deepseek/deepseek-v4-flash`）
- [ ] 发送一张图片 + 文字 "图片里有什么？"
- [ ] **预期**：DeepSeek 调用 `describe_image` 工具
- [ ] **预期**：工具返回 MIMO 的图片描述
- [ ] **预期**：DeepSeek 基于描述生成自然语言回复
- [ ] **预期**：前端显示完整对话流程，包括工具卡片

## 2. 缓存验证

- [ ] 发送**同一张图片** + 不同文字 "再看看这张图"
- [ ] **预期**：服务端日志显示 `describe_image 缓存命中: xxxx`
- [ ] **预期**：不再调用 MIMO API（速度快）
- [ ] **预期**：返回相同的描述文字

## 3. MIMO 直接看图 — 不触发工具

- [ ] LLM 切换到 **MIMO**（`mimo/mimo-v2.5`）
- [ ] 发送图片 + "图片里有什么？"
- [ ] **预期**：MIMO 直接看图回复，**不**调用 `describe_image` 工具
- [ ] **预期**：回复速度和之前 Phase 1 一样

## 4. 纯文本消息 — 不触发工具

- [ ] LLM 保持 **DeepSeek**
- [ ] 发送纯文本 "你好"（不带图片）
- [ ] **预期**：正常回复，不调用 `describe_image`
- [ ] **预期**：消息中不出现 `[用户发送了一张图片...]` 提示

## 5. 多张图片

- [ ] LLM 设为 DeepSeek
- [ ] 上传 2 张不同图片
- [ ] 发送 "这是谁？这是哪里？"
- [ ] **预期**：DeepSeek 分别调用 `describe_image(image_index=0)` 和 `describe_image(image_index=1)`
- [ ] **预期**：两张图片都正确描述
- [ ] **预期**：两张图片分别缓存

## 6. 无文字只发图

- [ ] LLM 设为 DeepSeek
- [ ] 不输入文字，仅选择图片后点发送
- [ ] **预期**：消息注入 `[用户发送了一张图片。使用 describe_image 工具查看。]`
- [ ] **预期**：DeepSeek 调用 `describe_image`，基于描述给出回复

## 7. 工具失败处理

- [ ] 临时注释掉 MIMO_API_KEY（或设置错误 API Key）
- [ ] 发送图片
- [ ] **预期**：`describe_image` 返回失败
- [ ] **预期**：DeepSeek 看到工具失败，告知用户无法查看图片
- [ ] 恢复 MIMO_API_KEY

## 8. 不是图片的文件

- [ ] 尝试粘贴/拖拽非图片文件（如 .pdf, .txt）
- [ ] **预期**：上传被拒绝或预览不显示
- [ ] **预期**：不会导致崩溃

## 9. 缓存过期/手动清理

- [ ] 查看 `data/cache/vision/` 目录
- [ ] 确认缓存文件存在（JSON 格式）
- [ ] 删除缓存文件后重新发同一张图
- [ ] **预期**：重新调用 MIMO，生成新缓存

## 10. 调试页面

- [ ] 打开 `/debug` 页面
- [ ] Session 管理中查看消息
- [ ] **预期**：用户消息的 `content` 数组显示 `type: "image"` 和 `type: "text"` part
- [ ] **预期**：`describe_image` 出现在工具列表中

---

## 回归（确保原有功能未破坏）

- [ ] 纯文本对话（DeepSeek）：正常一问一答
- [ ] MIMO 直接看图对话：正常
- [ ] 模型切换 DeepSeek ↔ MIMO：正常
- [ ] TTS 语音播放：正常
- [ ] 麦克风 STT 输入：正常
- [ ] Code block / Markdown 渲染：正常
- [ ] 工具调用（web_search / web_fetch 等）：正常
- [ ] Session 持久化 / 恢复：正常
