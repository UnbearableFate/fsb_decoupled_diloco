# Plan 03 plan-complete remediation 第二次增量审查（Codex / GPT-5.6-sol）

- Base commit：`397e2b7d96d339b69bbb8f6f9e78a024346786c8`
- Target commit：`3cd7dab0ca846a9914b8e6784c7fc11e818a7ade`
- Ancestry：已验证 base 是 target 的 ancestor。
- 审查范围：target 相对 base 的 Full-v4 local-input validation、config tests与三份同步文档；同时沿实际 loader 解析路径复核上一轮 M1 是否完全关闭。
- 结论：`CHANGES_REQUIRED`（1 Medium；无 Critical/High/Low）。

## 已确认正确

- `/`、`./`、`../` 和 `file://` 明确 path spelling 已在 Full-v4 config boundary fail closed，错误说明准确指出缺少 descriptor-bound content identity。
- synthetic 和 Torch baseline profile仍保持原边界；其他 Hub ID 在 exact 40-lowercase-hex model/tokenizer/dataset commits下可通过。
- repository config oracle已改为 generic immutable SHA + GPT-2/WikiText条件式 exact assertion，不再把所有未来 Hub producer写死为当前两项。
- RED job `2514532` 的八项在 target逻辑下通过；job `2514544` 为 `673` focused、`799 passed, 2 skipped` full。文档与实现一致。

## Medium

### M1 — 相对 Hub ID 可被同名 local path 或 dataset env override重新解释，config-only path check不完整

证据：

- `_is_explicit_local_reference` 只识别四个前缀；合法 Hub spelling 如 `organization/model`、`Salesforce/wikitext` 也可能在 actor cwd 下恰好存在为相对目录。
- Transformers `from_pretrained(name, ...)` 会优先把存在的目录当作 local model；`datasets.load_dataset(name, ...)` 同样接受 local directory/script。当前 `hf_model.py`/`hf_data.py` 在实际调用前没有检查 resolved `Path(name).exists()`。
- `hf_data.load_text_split` 还允许 `FS_DILOCO_HF_WIKITEXT_REPO` 在 config validation之后替换 repository spelling。launcher 默认值是 Hub ID `Salesforce/wikitext`，但操作者可把它设为 absolute 或 ambiguous relative local path；该 override不在 descriptor 中。

影响：config 可以声明并冻结 Hub commit，随后 actor cwd 中一个同名 relative directory或 local env override让 loader读取本地 bytes；revision kwarg对 local directory不提供 content identity。descriptor仍声称 Hub revision，实际 producer bytes却未冻结，上一轮 ENV-01 finding仍存在一个 loader-time分支。

修复建议：在 model/data loader紧邻实际 producer调用处做第二层 fail-closed检查：对 `Path(reference).expanduser().exists()` 的任何 file/dir/symlink拒绝 Full-v4 load，并拒绝 local env override；显式前缀仍由 config层给出早期诊断。这个 runtime check必须覆盖 ambiguity发生的 actor cwd。新增 mock/temporary-directory RED：相对 `organization/model`、相对 `Salesforce/wikitext`、absolute path、symlink，以及 `FS_DILOCO_HF_WIKITEXT_REPO` local override 均不得到达 Transformers/datasets producer；不存在的 Hub ID仍传 exact revision。若未来需要 local input，应先在 descriptor增加 content manifest而不是删除此 check。

## 验证与结论

补齐 loader-time RED和防线后，重跑 loader/config/initializer/migration/checker与完整 PBS G2。此修改仍属于同一 fresh-input identity边界，应冻结新 target并做最后一次增量复审。当前 target为 `CHANGES_REQUIRED`；config层修复正确但不能单独控制 loader如何解释 ambiguous relative string。
