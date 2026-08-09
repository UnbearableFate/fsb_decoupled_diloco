# 运行流程

## 1. 初始化与提交

`tools.init_run`/`launch_independent_run` 先用 `resolve_config_v4` 完整验证 config、source identity、walltime 和 topology，再发布 immutable descriptor、resolved config、artifact policy、v4 DB 和 bootstrap marker。run root 已存在时不会覆盖。

launcher 先提交 syncer candidate，再提交一个 static learner array 或多个 dynamic bootstrap learner。每个已接受的 scheduler receipt 都进入结果；后续 qsub 失败返回 partial，不隐式删除已接受 job。

## 2. Candidate 生命周期

1. `runtime.syncer_entrypoint` 验证 descriptor/config identity。
2. 打开严格 authority，轮询获取 leader lease。
3. 独立 renewer 周期续租并发布 heartbeat。
4. leader reconcile predecessor publication，初始化 dynamic pool（如需要）。
5. `syncer_v4` 创建或恢复 global v0，随后循环 admission、receipt/proposal ingest、selection、merge、publish。
6. 达到 outer-step 或 direct-token target 后进入 terminal close。
7. 正常结束发布 terminal；异常先发布 error control 并 fence 当前 leader。

## 3. Learner 生命周期

1. `runtime.learner_entrypoint` 在 torch import 前验证 descriptor 和 resolved config。
2. static learner 发布包含 logical launch/attempt/generation 的 request；dynamic learner生成 fresh instance ID 并发布 bootstrap/launch request。
3. reader 只接受最高 live epoch、exact current pointer、request digest 和 contributor fence 匹配的 admission。
4. admission 再验证后才 import torch/CUDA，写 actor attestation 并恢复 authority 提供的 cursor/hash chain。
5. 每个 cycle 训练、结算 segment token、发布可选 proposal 和 mandatory receipt。
6. learner 等待 exact receipt acknowledgement、drain 或 terminal；没有 ack 时不开始无限后续 cycle。
7. latest 采纳只接受 current epoch digest chain；terminal drain 发布 exact final-cycle ack 后退出。

## 4. Merge 和提交

leader 每次 ingest 都验证 immutable object 与 current fence。公平 selector 使用持久 committed service count、最近服务版本和 stable key；tensor reduction 始终用稳定 key 顺序。publication intent 先于 tensor I/O，commit 再验证权威 fence、batch、对象和 theta identity。

## 5. 接管与替换

successor candidate 不信任 fixed cache：从 DB 恢复 latest、cursor、membership、terminal 和 pending intent，在自己的 epoch 重发 heartbeat/latest/admission/receipt ack。static active replacement 必须有 create-no-replace operator authorization；dynamic replacement 必须明确 current instance 和 launch request。旧进程即使恢复运行，也不能越过新 fence 提交。

## 6. 终态

`begin_terminal_close` 冻结 current contributor fences 和 cursor。leader 继续摄取冻结时已在途的最后 receipt/proposal，并只接受允许的连续 final cycle ack。超时 contributor 记录 hard-crash gap 上界；全部处理后 `finalize_terminal` 原子固化 final version、direct applied tokens 和原因。`summary.json`/`stop.json` 是发布后的可修复视图，不是终态写权威。
