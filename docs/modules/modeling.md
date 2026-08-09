# 模块参考：modeling

- `hf_model.py`：按 frozen model/tokenizer revision 加载 causal LM 与 tokenizer。
- `hf_data.py`：加载 text/synthetic split，构造 non-overlapping blocks；v4 resumable run 使用 deterministic indexed cursor，不接受 fake streaming resume。
- `param_index.py`：构建、验证参数索引，stable flatten/load；index wire version独立于 protocol version。
- `training.py`：inner optimizer/scheduler、train segment和共享数学 helper；不 import runtime learner。
- `outer_optim.py`：flat-vector outer optimizer state 初始化/step。

modeling 代码不拥有 filesystem authority。tensor path、immutability、digest 和 publication 位于 storage；cycle token fate 位于 protocol/accounting。
