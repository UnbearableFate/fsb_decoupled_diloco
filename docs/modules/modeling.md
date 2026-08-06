# 模块参考：`fs_diloco/modeling`

## `hf_model.py`：模型与 tokenizer

- `CausalLMOutput` 是 tiny 模型的 `{loss, logits}` 兼容返回类型。
- `TinyCausalLM` 是 embedding 加无 bias linear head；`forward()` 返回全序列 logits，传 labels 时用 `logits[:, :-1]` 对 `labels[:, 1:]` 做均值 cross entropy。
- `SyntheticTokenizer` 只有 `vocab_size/eos_token/pad_token`，没有 `__call__` 或 `eos_token_id`；它只服务 synthetic data 路径。
- `is_synthetic_model()` 只识别 `synthetic-tiny/tiny-synthetic/tiny-local` 三个字面值。
- `model_dtype()` 把 BF16/FP16 别名映射到对应 torch dtype；任何其他字符串（包括拼写错误）静默得到 FP32。
- `load_causal_lm_and_tokenizer()` 对 synthetic 创建 tiny 模型并转 model dtype；真实模型用 HF `AutoTokenizer`/`AutoModelForCausalLM.from_pretrained(dtype=...)`，tokenizer 没有 pad 但有 EOS 时以 EOS 补 pad，可选 `torch.compile`。compile 后返回包装模型。
- `choose_device()` 只按 `torch.cuda.is_available()` 选首个 CUDA 或 CPU，不解析设备编号。

## `hf_data.py`：数据到无限 batch 流

| 名称 | 行为 |
|---|---|
| `Batch.to(device)` | 只移动 `input_ids/labels`，计数保持 Python int；返回新对象。 |
| `synthetic_batches()` | 私有 `torch.Generator`，seed=`training.seed + learner_index*100003`；无限产生 `[micro_batch, block]` long tensor，labels 是 clone，token 数含全部输入位置。`synthetic_num_batches` 不参与。 |
| `_chunks(tokens, block_size)` | 产生不重叠定长 Python list，尾部不足一块直接丢弃。 |
| `load_text_split(data_config, split)` | 调 `datasets.load_dataset`。原始 name 恰为 `wikitext` 且设置环境变量时先重定向；首次失败且实际 name 不含 `/` 时尝试 `Salesforce/wikitext`。fallback 也失败时重新抛**第一次**异常。 |
| `text_rows_to_blocks()` | 顺序消费所有 row；只接受 mapping 的非空 `text`；tokenize 时 `add_special_tokens=False`，若 tokenizer 有 `eos_token_id` 则每个文本后追加 EOS；拼成单流再 `_chunks`。 |
| `_splitmix64()` | 64-bit wrapping SplitMix64 混合器，用于稳定生成 learner/epoch shuffle seed。 |
| `_batched_blocks()` | 零块报错。shuffle=false 时按索引取模无限循环；true 时先混合 global seed 与 learner ID，每个 epoch 用独立 `random.Random` 重排全体块，再按 `micro_batch_size` 取块构造 tensor。 |
| `wikitext_batches()` | 先加载 train split，再 `dataset.shard(..., contiguous=True)`，物化 blocks，最后建立上述无限 iterator。即使 `data.streaming=true`，token/block 阶段仍物化全部块。 |
| `build_batch_iterator()` | `data.dataset_name == "synthetic"` 才走 synthetic；否则一律走 WikiText 风格 text 管线。synthetic vocab 优先取 tokenizer 的 `vocab_size`。 |
| `build_stream_batch_iterator()` | dynamic入口；严格要求`0 ≤ stream_id < stream_pool_size`，随后把stream ID映射为learner index、固定pool映射为num shards并复用同一synthetic/WikiText实现。active成员增减不改变shard或shuffle seed；replacement复用stream时数据映射保持不变，restart语义由membership stream epoch另行记录。 |

## `param_index.py`：模型/扁平向量契约

index 按 `model.named_parameters()` 顺序记录 `name/shape/dtype/numel/offset`，顶层记录 format/model/trainable-only/total-numel。

- `dtype_name()` 直接返回 `str(torch.dtype)`。
- `build_param_index()` 默认跳过 `requires_grad=false` 参数；不要求至少一个参数。
- `save_param_index()` 原子写 JSON；`load_param_index()` 只先校验 format version，完整结构在后续函数使用时验证。
- `_named_param_map()` 把 `named_parameters()` 转 dict；缺名会自然抛 `KeyError`。
- `flatten_trainable_params()` 按 index 查参数、detach/flatten/转 device/dtype 后 `cat`；空 index 返回目标设备上的空 tensor。
- `trainable_params_l2_norm()` 逐参数用目标 dtype 求 norm，再对这些 norm 求二次 norm，避免完整 flat 副本；无 trainable 参数返回 CPU 标量零。
- `load_flat_into_model()` 校验总 numel，逐段转成目标参数 device/dtype 后 `copy_`；strict 模式还校验当前参数 shape，非 strict 仍按当前参数 shape reshape。
- `flat_to_named_tensors()` 校验总 numel，返回 detach 后的 CPU views/张量字典；不强制 clone。
- `named_tensors_to_flat()` 按 index 的名字/numel 校验并转目标 dtype/device；空 index 返回空 tensor。
- `validate_compatible_index()` 精确比较四个顶层字段以及完整 `params` list，因此参数顺序、dtype、shape、offset 任一差异都会失败。

## `outer_optim.py`：显式外层优化器

- `OuterOptimizerConfig` 保存 name/lr/momentum/weight-decay/betas/eps；`config_from_any()` 从属性对象复制，list betas 转 tuple，但不提前校验长度/范围。
- `init_outer_state()` 总是建 CPU int64 `step=0`；SGD/momentum/Nesterov 另建与 theta 同形的 `momentum`，AdamW 建 `exp_avg/exp_avg_sq`；未知 name 报错。
- `outer_optimizer_step()` 先 clone theta/grad，grad 转 theta dtype，step 加一并放 theta device。SGD 系先做耦合 L2 decay；纯 `sgd` 或 momentum=0 使用 grad 并把 buffer 清零；momentum 用 `b=μb+g`；Nesterov 用 `g+μb`。AdamW 先做 `theta*(1-lr*wd)`，再更新矩、做逐 step bias correction 和 `addcdiv`。返回的新 theta/state 不原地修改调用方输入。
- `state_to_tensors()` 返回 CPU tensor dict，键为 `theta` 加 state；指定 dtype 时只转换浮点项，int step 保持整数。
- `state_from_tensors()` 要求 `theta` 键；theta 默认加载为 FP32，指定 dtype 时所有浮点 state 同步转换，整数保持原 dtype；缺 step 的旧文件补 device 上 int64 零。

outer checkpoint 自身原子保存 theta 与 optimizer state，但 global weight 是另一个文件；full publication 在两文件都写成后用 SQLite 事务把它们共同纳入版本，不能把两个文件称作单文件原子快照。
