# 模块参考：modeling

- `hf_model.py`：把 strict Full-v4 config 中的 model revision 和 `tokenizer_revision or model.revision` 原样传给 Hugging Face loader，再构造 causal LM 与 tokenizer。
- `hf_data.py`：把 frozen dataset revision 同时传给 primary 与 WikiText fallback loader，构造 text/synthetic non-overlapping blocks；v4 resumable run 使用 deterministic indexed cursor，不接受 fake streaming resume。
- `param_index.py`：构建、验证参数索引，stable flatten/load；index wire version独立于 protocol version。
- `training.py`：inner optimizer/scheduler、train segment和共享数学 helper；不 import runtime learner。
- `outer_optim.py`：flat-vector outer optimizer state 初始化/step。

`hf_identity.py` 在 Full v4 producer 调用前检查 actor cwd 与 environment override；任何显式 local spelling、现存相对 file/directory 或 symlink 都因缺少 descriptor-bound content manifest 而 fail closed。这样合法 Hub ID 不会在不同 actor cwd 中被重新解释成未冻结的 local bytes。Torch baseline 与 classic/fragment query-only reader 保留原有 local checkpoint/dataset 兼容性；调用方必须显式启用 Full v4 identity gate。

modeling 代码不拥有 filesystem authority。tensor path、immutability、digest 和 publication 位于 storage；cycle token fate 位于 protocol/accounting。
