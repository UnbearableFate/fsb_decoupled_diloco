# Plan 03 plan-complete remediation 增量审查（Codex / GPT-5.6-sol）

- Base commit：`e0f4c9b88eab4a462c49e9746d8ac5da8fb8c6b0`
- Target commit：`397e2b7d96d339b69bbb8f6f9e78a024346786c8`
- Ancestry：已验证 base 是 target 的 ancestor。
- 审查范围：完整 `git diff base..target` 中的 production source、11 个 Full-v4 config、Plan 03 checker、tests 与同步文档；plan-complete 原始审查/失败 artifact 只核验身份和处置链，不把大体积 source manifest 当作实现代码复审。
- 结论：`CHANGES_REQUIRED`（1 Medium、1 Low；无 Critical/High）。

## 已确认的修缮

- `hf_model` 与 `hf_data` 已把 config/descriptor 声明的 model、有效 tokenizer 和 dataset revision 传给所有 primary/fallback loader；mock RED 覆盖参数闭环。
- Full-v4 Hub 输入使用 exact 40 位小写 SHA；Torch baseline 在 profile return boundary 前退出，synthetic smoke 不受影响。repository GPT-2/WikiText pins 与本机现有 cache ref/snapshot basename一致。
- frozen P0→current checker 在 strict migration 前加入同一组明确的 repository-owned identity projection，随后仍用 whole-payload equality；没有放宽 unknown key 或 baseline byte boundary。
- old unfair selector、proposal alias、flat admission accessor、early layout fallback、generic version constant 与无 caller fixed paths/helpers 均已删除；current fair selector、typed resume、canonical fence namespace 和 epoch publication path未改变。
- param index 改用独立 `PARAM_INDEX_FORMAT_VERSION` 且没有 wire bump。SQLite DDL、business transaction、lease fence、publication/GC/terminal 状态机均不在 diff 中。

## Medium

### M1 — 路径前缀被当作 Full-v4 local identity 逃生口，但 descriptor 没有冻结这些 bytes

证据：

- `fs_diloco/core/config_v4.py` 的 `_is_explicit_local_reference` 对 `/`、`./`、`../`、`file://` 返回 true，`_validate_full_input_revisions` 因而允许 non-synthetic model/dataset 的 revision 为 `None`。
- `fs_diloco/tools/init_run.py` 对 local input 仍只把 path/revision 写入 descriptor，没有 directory/file manifest 或 content digest；replacement 在同一路径读取到不同 bytes 时 authority identity不会变化。
- `hf_model` 把 path 直接交给 Transformers，`file://` 不是该 API 声明的 local-directory spelling；当前文档却把它列为支持的 exemption。

影响：一个 fresh Full-v4 config 可以通过 strict validation，但实际 model/data 既可能不可加载（`file://`）也可能在 actor/replacement 间原地改变而不改变 descriptor。这重新产生 H1/M1 要关闭的“声明 identity 与实际 bytes 不同”问题，只是从 Hub branch 换成了 mutable local path。

建议：当前 schema 没有 local content digest 字段，因此 Full-v4 应对 non-synthetic local path fail closed，并明确提示需要先发布到 immutable Hub commit 或未来增加 content-manifest identity。只保留 synthetic exemption；Torch baseline继续按其 profile兼容。新增 RED 覆盖 absolute、dot-relative、parent-relative 和 `file://` model/data path，证明它们不能绕过 ENV-01。若决定正式支持 local input，则必须先设计 descriptor-bound content manifest，而不是按字符串前缀豁免。

## Low

### L1 — repository pin test 会把未来所有 non-synthetic model误判为 GPT-2

证据：`tests/test_config.py::test_every_hub_backed_full_config_pins_immutable_input_commits` 只判断“不在 synthetic name set”，随后无条件要求 `GPT2_COMMIT`；dataset 同样对任意 non-synthetic name要求 WikiText commit。

影响：以后增加一个正确固定到其他 commit 的 Full-v4 repository config时，测试会给出错误的 identity 期望。它是 fail-safe false positive，不影响当前 runtime correctness。

建议：先对所有 Hub input 做通用 40-lowercase-hex断言，再仅在 `name_or_path == "gpt2"` / `dataset_name == "wikitext"` 时断言 repository-specific exact pins；增加一个非 GPT-2 synthetic config object 或 helper-level test防止 test oracle重新写死。

## 验证要求

1. 先加 local-path RED，确认 target 会接受这些未冻结输入；再删除 implicit local exemption、修正文档和 generic repository-pin oracle。
2. 重新运行 config/loader/migration/checker/initializer关联测试及完整 PBS G2；P0 frozen boundary仍须 PASS。
3. 因 M1 修改 fresh-run identity/public config boundary，冻结新 target 后应以本 target 为 base 做一次短增量复审，再开始 formal G0–G10 evidence regeneration。

## 最终结论

`CHANGES_REQUIRED`。Hub pin传播与旧代码清理本身正确，但 Full-v4 path-prefix exemption仍允许未冻结的实际输入 bytes，必须 fail closed或引入完整 local content identity后才能批准。
