# 模块参考:fs_diloco/modeling

模型构建、数据管道、参数索引、外层优化器。

---

## modeling/hf_model.py — 模型与 tokenizer

- **`CausalLMOutput`**(dataclass)— `{loss, logits}`,让 TinyCausalLM 与 HF 模型输出接口一致。
- **`TinyCausalLM(vocab_size=128, hidden_size=32)`** — 冒烟用微型 LM:Embedding + 无偏置 lm_head,forward 里做 shift-1 交叉熵;无外网/无 GPU 环境即可测全链路。
- **`SyntheticTokenizer(vocab_size)`** — 配套占位 tokenizer(只有 vocab_size/eos/pad 属性)。
- **`is_synthetic_model(name_or_path) -> bool`** — `synthetic-tiny / tiny-synthetic / tiny-local` 判定。
- **`model_dtype(dtype_name) -> torch.dtype`** — `bf16/fp16/其他 → bfloat16/float16/float32`。
- **`load_causal_lm_and_tokenizer(config) -> (model, tokenizer)`** — 合成模型或 HF `AutoModelForCausalLM/AutoTokenizer`(按需 trust_remote_code、torch_dtype、pad_token 兜底为 eos、可选 `torch.compile`)。
- **`choose_device() -> torch.device`** — 有 CUDA 用 cuda,否则 cpu。

## modeling/hf_data.py — 数据管道

- **`Batch`**(dataclass)— `{input_ids, labels, num_tokens, num_examples}` + `.to(device)`;labels 即 input_ids 副本(causal LM 内部做 shift)。
- **`synthetic_batches(*, vocab_size, block_size, micro_batch_size, seed, learner_index) -> Iterator[Batch]`** — 无限随机 token 流;每 learner 种子隔离(`seed + index·100003`)。
- **`_chunks(tokens, block_size)`**(私有)— token 流切成整块,尾部丢弃。
- **`_batched_blocks(blocks, micro_batch_size)`**(私有)— 对块列表做无限轮转取批(索引取模,永不 StopIteration)。
- **`wikitext_batches(data_config, tokenizer, *, learner_index, num_learners, micro_batch_size, block_size)`** — 真实数据管道:
  1. `load_dataset`(支持 `$FS_DILOCO_HF_WIKITEXT_REPO` 重定向;`wikitext` 失败自动回退 `Salesforce/wikitext`);
  2. **`dataset.shard(num_shards=num_learners, index=learner_index, contiguous=True)`** — 每 learner 拿到互不重叠的连续数据分片;
  3. 全部文本 tokenize 成一条流(样本间插 eos)→ 切块 → 无限轮转批。
- **`build_batch_iterator(config, tokenizer, *, learner_index, num_learners)`** — 按 `data.dataset_name` 分派合成/真实管道。

## modeling/param_index.py — 参数索引与扁平向量互转

param index 是"模型 ↔ 扁平向量"映射的**契约**,JSON 结构:`{format_version, model_name_or_path, trainable_only, total_numel, params: [{name, shape, dtype, numel, offset}]}`,顺序 = `named_parameters()` 声明顺序。

- **`torch_dtype_name(dtype)`** — `str(dtype)`。
- **`build_param_index(model, *, model_name_or_path, trainable_only=True) -> dict`** — 遍历可训练参数累积 offset。
- **`save_param_index / load_param_index`** — 原子写 / 读取(校验 format_version)。
- **`flatten_trainable_params(model, param_index, *, dtype=float32, device="cpu") -> Tensor`** — 按 index 顺序把各参数 reshape(-1) 拼接为扁平向量。
- **`trainable_params_l2_norm(model, *, dtype=float32) -> Tensor`** — 逐个可训练参数计算 L2 norm,再以同一 dtype 汇总为全局 norm;不物化完整扁平参数副本。
- **`load_flat_into_model(model, flat, param_index, *, strict_shape=True)`** — 逆操作(`@torch.no_grad`):按 offset 切片、reshape、`param.copy_()` 写回;总长或形状不符抛错。
- **`flat_to_named_tensors(flat, param_index) -> dict[name, Tensor]`** — 扁平向量 → 命名张量(存全局权重用)。
- **`named_tensors_to_flat(named_tensors, param_index, *, device) -> Tensor`** — 逆向(加载全局权重用),逐张量校验 numel。
- **`validate_compatible_index(param_index, other)`** — 严格比对 format_version / model / trainable_only / total_numel / **逐条 params**;不一致抛 `ValueError`。learner 启动时用它确保本地模型与 syncer 发布的契约完全一致。

## modeling/outer_optim.py — 显式扁平向量外层优化器

不用 `torch.optim`,直接在扁平向量上实现,使优化器状态可精确 safetensors 序列化、跨 resume 位级一致。所有步进都是函数式的(输入 θ/grad/state,返回新 θ/新 state,不原地修改调用方张量)。

- **`OuterOptimizerConfig`**(dataclass)— `{name, lr, momentum, weight_decay, betas, eps}`。
- **`config_from_any(config)`** — 从任意带同名属性的对象(如 `core.config.OuterOptimizerSection`)构造,list betas 转 tuple。
- **`init_outer_state(theta, config) -> dict[str, Tensor]`** — `{step: 0}` + 按优化器补 `momentum`(sgd 系)或 `exp_avg/exp_avg_sq`(adamw),全零初始化。
- **`outer_optimizer_step(theta, grad, state, config) -> (theta', state')`** —
  - `sgd/momentum/nesterov`:可选 weight_decay 加到 grad;momentum buffer `b ← μ·b + g`;nesterov 用 `g + μ·b`,momentum 用 `b`,纯 sgd(或 μ=0)直接用 `g`;`θ ← θ − lr·update`;
  - `adamw`:解耦 weight decay(`θ ← θ·(1−lr·wd)`)、一阶/二阶矩 EMA、偏置校正、`θ ← θ − lr·m̂/(√v̂+ε)`;
  - step 计数随 state 持久化,保证 resume 后偏置校正连续。
- **`state_to_tensors(theta, state)`** / **`state_from_tensors(tensors, *, device) -> (theta, state)`** — 与 safetensors 的互转;theta 与状态存在同一文件里(见 `storage/tensor_codec.py: save_outer_state`),使"权重 + 优化器状态"构成单一原子快照。
