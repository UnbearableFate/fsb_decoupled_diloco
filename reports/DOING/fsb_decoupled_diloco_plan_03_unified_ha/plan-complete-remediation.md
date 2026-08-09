# Plan 03 plan-complete 审查意见汇总与修缮计划

- 全量审查 target：`e0f4c9b88eab4a462c49e9746d8ac5da8fb8c6b0`
- Codex 报告：`reports/DOING/code_review/fsb_decoupled_diloco_plan_03_unified_ha/plan-complete/gpt-5.6-sol_e0f4c9b88eab4a462c49e9746d8ac5da8fb8c6b0.md`
- Claude 调用：请求模型 `claude-opus-5`、session `ebee1631-f8b3-4fd5-864c-57e0005d6033`，在输入/输出 token 均为 0 时收到 HTTP 429 session-limit；按用户指示与 `plans/AGENTS.md` 记为 `skipped-session-limit`，不阻断 Codex 门禁。
- 汇总时间：2026-08-10 06:46 JST

## 去重结论与处置顺序

只有 Codex 产生实际 finding；Claude 没有进入审查内容，因而不存在跨 reviewer 冲突或需要合并的重复 finding。以下四项均接受修复。状态字段使用门禁规定的 `fixed` 处置，括号说明当前进度；只有相应 RED、实现、验证和增量复审全部完成后才可把 plan 视为完成。

### H1 — Hugging Face revision 没有传入 loader

- 状态：`fixed`（loader/config/descriptor参数闭环已完成，job `2514544` 完整回归通过）。
- 修改范围：`fs_diloco/modeling/hf_model.py`、`fs_diloco/modeling/hf_data.py`。
- RED：mock `transformers`/`datasets` producer，断言 model、tokenizer、primary dataset 与 WikiText fallback 都收到 exact revision；tokenizer 未单独配置时必须继承 model revision。
- 实现：model 使用 `model.revision`，tokenizer 使用 `tokenizer_revision or model.revision`；dataset primary 与 fallback 都使用 `data.revision`。
- 验证：focused loader tests、完整 PBS G2、随后同一 clean target 的全部 P6 formal evidence。

### M1 — Hub-backed Full v4 允许可移动或空 revision

- 状态：`fixed`（Hub commit pin与 local-input fail-closed边界均已完成，job `2514544` 完整回归通过）。
- 修改范围：`fs_diloco/core/config_v4.py` 与 11 个 repository-owned GPT-2/WikiText Full v4 config；baseline 和 query-only legacy 行为保持不变。
- RED：unpinned/标签式 revision 的 Full v4 配置被拒绝；synthetic Full v4 与 unpinned Torch baseline 继续可用；所有 tracked non-synthetic Full v4 config 都有 40 位小写 Hub commit SHA。
- 实现：仅 `FULL_V4` 对 Hub-backed non-synthetic model/data 强制 immutable SHA；有效 tokenizer revision 为显式 tokenizer pin 或 model pin。GPT-2/model+tokenizer 固定到 `607a30d783dfa663caf39e06633721c8d4cfcd7e`，Salesforce/WikiText 固定到 `b08601e04326c79dfdd32d625aee71d232d685c3`。
- 验证：config focused tests、所有 tracked config parse、initializer identity tests、完整 PBS G2 和正式 P6 ladder。

### M2 — final tree 仍有旧选择/API/layout 兼容面

- 状态：`fixed`（compatibility/dead surfaces已删除，architecture/authority/runtime完整回归通过）。
- 修改范围：删除 `protocol.merge` 的无 caller 旧选择 helper、`LeaderSession.record_proposal` alias、`DynamicAdmission` 扁平 accessor、syncer unnamespaced receipt fallback 和 admission early-P4 layout scan；旧测试改为 canonical typed API。
- RED：AST/static boundary test 断言上述生产定义/fallback 不存在；现有 authority/admission/runtime tests 继续验证 canonical 路径。
- 验证：architecture、protocol、authority、runtime、legacy query-only 与完整 PBS G2；正式 runtime gates 证明 fresh-v4 路径无回归。

### L1 — 重复且过时的常量和 fixed-path/helper 死代码

- 状态：`fixed`（专属 param-index version 与无 caller helper清理已完成，static/full回归通过）。
- 修改范围：param index 改用 `PARAM_INDEX_FORMAT_VERSION`；`core/constants.py` 只保留真实共享的 run-root 常量；删除无 caller 的 fixed global/optim path、旧目录初始化、旧聚合 iterator 与 `wait_for_file`。
- RED：AST/static boundary test 断言只有 `core.versions` 拥有 protocol/format version，param index 使用专属 version，dead methods/functions 不存在。
- 验证：param-index roundtrip、architecture/full suite、Ruff/compile/static gates。

## 冻结和复审策略

1. 先在旧 target 上运行新增 RED，并把预期失败写入 `failures.md`；再修改 production/test/config。
2. 在 PBS compute node 跑完整 G2，修复所有由更严格 config boundary 暴露的 fixture；通过后创建修缮 review-target commit。
3. 以 `e0f4c9b88eab4a462c49e9746d8ac5da8fb8c6b0` 为 base 对修缮增量做 Codex review；Claude 只有可核验 session-limit 才能再次跳过。
4. 修缮改变 formal source fingerprint 和 ENV-01 identity，不能复用旧 P6 evidence。对最终 clean target 重跑 G0–G10、质量/文档检查、八行 requirement matrix 与 tracked-evidence Checker，再创建 plan-final commit。

## 增量审查追加处置

- Target `397e2b7d96d339b69bbb8f6f9e78a024346786c8` 的 Codex 增量审查产生 M1（path-prefix local input 没有 content identity）和 L1（repository pin test 过度写死 GPT-2/WikiText）。Claude 调用 `94d79ec2-b5c9-445f-8911-3ef9fe9acb06` 在 input/output token 均为 0 时返回 HTTP 429 session-limit，按用户规则记为 nonblocking skip。
- M1 状态：`fixed`。RED job `2514532` 证明八种 local spelling 会绕过 target validator；修复后 Full v4 对 local non-synthetic input 明确 fail closed，直至 schema/descriptor拥有 content manifest，synthetic 与 Torch baseline边界不变。job `2514544` 的八个 RED 与完整 suite 全部通过。
- L1 状态：`fixed`。repository config test 先通用验证 immutable SHA，只对 `gpt2`/`wikitext` 断言本仓库 exact pin，并以其他 Hub model/dataset regression证明没有写死 producer identity；job `2514544` 通过。
- 第二次增量 target `3cd7dab0ca846a9914b8e6784c7fc11e818a7ade` 的 Codex review发现 config-only prefix check无法阻止 actor cwd下同名 relative directory或 local dataset env override；Claude session `83654c81-6cd6-49b3-ac73-d5b2179cdfe0` 同样在 0 token时返回可核验 HTTP 429，nonblocking skip。
- Loader-local M1 状态：`fixed`。RED job `2514566` 已证明四个实际 producer 分支会读取未冻结 local bytes；修复在 producer 调用紧邻处拒绝任何解析为现存 path/symlink 的 model、resolved dataset、WikiText fallback 或 environment override，并把 gate 显式限制在 Full v4 runtime/current-v4 query 路径。Torch baseline 与 classic/fragment query-only local input 兼容性由两个反向 regression 保留。PBS job `2514597` 通过 673 个 focused tests 与完整 suite 的 805 passed/2 skipped；结构化 gate 仅因修复尚未 commit 而按预期 `BLOCKED`。
