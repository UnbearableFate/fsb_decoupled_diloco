# 00 术语表(英文术语与缩写索引)

本页集中解释文档中出现的英文术语与缩写,并规定全书统一的中文译法。阅读建议:

- 首次出现的术语通常写作「中文(英文)」,之后正文尽量用中文,必要时保留英文;
- 表格中的**原文**是必须保留的英文(代码标识符、配置字段、文件内容字面量、命令行参数等),它们与程序一一对应,不能翻译;
- 遇到不认识的词,先来本页查。

## 一、分布式训练与 DiLoCo 基本概念

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| DiLoCo | DiLoCo(分布式低通信训练) | Distributed Low-Communication training。把训练拆成「本地大步 + 全局慢步」以降低通信频率的分布式训练范式 |
| Decoupled | 解耦 | learner 与 syncer 完全异步、互不等待 |
| Filesystem-backed | 文件系统承载 | 进程之间只通过共享文件系统通信,没有任何网络集合通信 |
| learner | 学习者(learner) | 在本地 GPU 上训练模型并周期性上传参数更新的进程,类似 DiLoCo 中的 worker |
| syncer | 同步器(syncer) | 收集各 learner 更新、执行外层合并与优化的协调进程,类似 DiLoCo 中的 coordinator |
| inner step / local step | 内层步/本地步 | learner 本地的一次优化器步进(含梯度累积) |
| outer step | 外层步 | syncer 的一次全局合并与优化器步进 |
| inner optimizer | 内层优化器 | learner 本地使用的优化器(本项目为 AdamW) |
| outer optimizer | 外层优化器 | syncer 使用的优化器(默认 Nesterov 动量 SGD) |
| pseudo-gradient | 伪梯度 | 全局参数与加权平均参数之差(θ − p̄),作为外层优化器的梯度 |
| global version | 全局版本号 | 全量模式下 syncer 每完成一次外层步进后的全局权重版本,从 0 递增 |
| global merge event | 全局合并事件号 | fragment 模式下的全局合并计数,每次合并任意分片都 +1 |
| fragment version | 分片版本号 | 某个分片自己的版本号,只在合并该分片时 +1 |
| base version | 基准版本 | 某份更新出发时 learner 加载的版本,用于计算陈旧度 |
| token | 词元(token) | 文本被分词器切分后的最小单元;「token 数」也是合并加权的依据。注意:与权限体系中的 token(令牌)是两个词 |
| staleness | 陈旧度 | 当前版本 − 基准版本,衡量一份更新的过时程度 |

## 二、协议与文件(learner ↔ syncer 的通信)

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| update / proposal | 更新 / 提议 | learner 完成一个本地区间后提交的参数包。「提议」强调它是提交给 syncer 的候选;两者指同一件事 |
| payload | 载荷 | 不可变的张量数据文件(如 `*.params.safetensors`),与描述它的元数据分离存放 |
| pointer | 指针 | 指向某份不可变数据文件的固定路径 JSON |
| proposal pointer | 提议指针 | learner 固定可见的提交标记(`updates/latest/learner_XXX.json`),新提议原子覆盖旧提议 |
| fixed pointer | 固定指针 | 路径固定、每 learner 恰好一份的提议指针 |
| latest.json | 全局指针文件 | syncer 发布的「当前全局权重」唯一入口,learner 轮询它 |
| canonical head | 权威头部 | HA 模式下每个 epoch 目录里不可变的头部 JSON,绑定权重路径与 SHA |
| heartbeat | 心跳 | learner 周期性写入的存活信号 JSON |
| JSONL | JSON 行文件 | 每行一个 JSON 对象的追加式日志文件 |
| CSV | CSV 逗号分隔文件 | 逗号分隔的指标表文件 |
| manifest | 清单 | 记录一批文件/事件的结构化清单(如 bootstrap 调度清单、清理清单) |
| frontier | 摄取水位 | SQLite 中记录的「该指针已摄取到哪份提议」,用于防止重放 |
| orphan | 孤儿文件 | 不再被任何指针/数据库引用的未发布文件,宽限期后被回收 |
| receipt | 回执 | 命令提交后立即返回的结构化结果(如 qsub 的 job ID) |

## 三、合并算法

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| quorum | 法定人数(quorum) | 一次合并所需更新的数量下限 `quorum_min` / 上限 `quorum_max` |
| grace window | 宽限窗口 | 达到下限后 syncer 再等待一小段时间,以收集更多更新 |
| adaptive grace | 自适应宽限 | 以「最快下一次上传的预计时间」为界动态缩短、绝不延长的宽限窗口 |
| selection policy | 选择策略 | 每个 learner 多份合格更新时选哪一份的规则(`most_recent_per_learner` / `oldest_pending`) |
| weighted merge | 加权合并 | 按 token 数与陈旧度加权平均各 learner 的参数更新 |
| drop reason | 丢弃原因 | 更新被丢弃时写入数据库的字面原因:`missing_file` / `too_stale` / `superseded` / `future_base` |

## 四、存活管理与停机

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| liveness | 存活管理 | 根据心跳年龄把 learner 分类为 `active` / `stale` / `dead` / `stopped` |
| watchdog | 看门狗 | 监测对方进展、超时后自保退出的守护逻辑 |
| terminal drain | 末端排空 | 所有 learner 明确停止后,syncer 放宽法定人数、尽量合并完剩余合法更新的收尾阶段 |
| input closed / closure | 输入闭合 | 已确认不会再有新输入的状态 |
| stop reason | 停止原因 | syncer 停机原因字面量:`stop_after_outer_steps` / `stop_after_global_tokens` / `input_exhausted` / `no_progress_timeout` / `completed` / `error` |
| input_exhausted | 输入耗尽 | 所有 learner 已停止且末端排空没有剩余合法提议时的正常停机原因 |
| fail closed | 失败即关闭(fail-closed) | 条件不满足时拒绝继续运行并报错退出,而不是降级继续 |
| fail fast | 快速失败 | 启动期发现配置/环境不可用时尽早报错 |

## 五、高可用(HA)与动态成员

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| HA | 高可用 | High Availability。通过 leader 选举与 epoch 隔离使 syncer 崩溃后可接管恢复 |
| leader | 领导者(leader) | 当前持有租约、唯一可提交事务的 syncer |
| candidate | 候选进程 | 竞争成为领导者的 syncer 进程 |
| successor | 后继者 | 崩溃后取得新 epoch、接管恢复的候选进程 |
| epoch | 纪元(epoch) | 单调递增、不复用的领导任期编号;所有 checkpoint 与权威文件按 epoch 分目录 |
| generation | 世代(generation) | 同一次不可变终态发布的尝试编号(注意与训练 epoch 无关) |
| LeaderToken | 领导者令牌 | 不可变的 `(run_id, epoch, owner_id)` 三元组,所有写事务必须携带 |
| lease | 租约 | 领导者周期性续约的有限有效期,防止僵死领导者占位 |
| renew | 续约 | 领导者在租约到期前刷新有效期 |
| fenced | 隔离的(fenced) | 旧领导者的写入被令牌校验拦截,无法与现任冲突 |
| fencing | 隔离机制 | 通过令牌 + 事务内重验阻止旧 epoch 写入的技术 |
| recovery | 恢复 | 崩溃后由后继者重建控制面并继续训练 |
| takeover | 接管 | 候选者获得租约并接替原领导者 |
| resume | 恢复(续跑) | 从持久数据库中恢复一个既有 run 并继续训练(legacy 全量模式) |
| membership | 成员管理 | 决定「哪些 learner 是当前成员」的机制,`static` 固定 / `dynamic` 动态 |
| incarnation | 化身(incarnation) | dynamic 模式下一次进程启动生成的实例身份 `learner_li_<uuid4>` |
| instance | 实例 | dynamic 模式下的进程化身,以 UUID 标识 |
| placement | 部署位置 | 物理位置(主机名 + CUDA 设备标识),一个位置同一时刻只属于一个实例 |
| stream | 数据流(stream) | 固定的虚拟数据分片;数据分片与随机种子以 stream 为准,不受在线增删成员影响 |
| stream pool | 数据流池 | 初始化时冻结的固定大小 stream 集合,不支持在线扩容 |
| registration | 注册 | learner 向 leader 提交身份与绑定信息的准入申请 |
| admission | 准入 | leader 审核注册后分配 epoch/token 并允许实例加入训练 |
| bootstrap | 引导 | 初始成员的确定过程(initializer 预建固定数量的注册请求与数据流) |
| scale request | 扩容请求 | 容量不足时 leader 发起的新 learner 启动请求 |
| capacity observation | 容量观测 | 周期性记录的有效贡献者数量观测值 |
| starvation | 饥饿 | 长时间没有合并发生(候选贡献者不足)的状态 |
| outbox | 发件箱 | 持久化的命令提交队列(记录 qsub 请求与 job 状态对账) |
| reservation | 预留 | 已提交但未完成的 job 占用的容量 |
| cooldown | 冷却期 | 一次扩容请求后的等待期,防止频繁扩容 |
| drain | 排空(关闭排空) | dynamic 关闭后的收尾阶段:停止准入、冻结终止上限、等待成员确认 |
| ack | 确认(ack) | 健康实例对关闭世代的确认回执 |
| revoke | 撤销 | 对超时未确认实例的强制驱逐 |
| close generation | 关闭世代 | 关闭操作发布的一次性世代编号,实例按它确认 |
| max terminal version | 终止版本上限 | 关闭事务冻结的允许最后合并的最大版本号 |

## 六、分片(fragment)模式

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| fragment | 分片(fragment) | 参数向量的一个互不重叠、完全覆盖的子集 |
| materialize | 物化 | 把所有分片拼回完整权重向量并保存为完整 checkpoint |
| round-robin | 轮转 | 按 事件号 mod 片数 循环选择目标分片 |
| balanced_tensor | 张量均衡装箱 | 以整个张量为单位贪心均衡分桶的策略 |
| scatter | 散布 | 把分片写回完整向量的对应区间 |

## 七、采纳策略(learner 侧)

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| adoption | 采纳 | learner 把新全局权重应用到本地模型的过程 |
| replace | 替换 | 直接覆盖本地权重并重置内层优化器 |
| rebase | 变基 | 把尚未发布的本地差值叠加到新全局权重上,保留优化器状态 |
| predict | 预测 | 用外层动量与本地差值预测下一个全局权重,真实新版到达时再对齐 |
| reconcile | 对齐 | 预测采纳后,把预测期间产生的本地进展合并到真实新版 |
| reference / anchor | 参照 / 锚点 | rebase/predict 保留的「发布点本地参数快照」 |
| inner poll | 区间中途轮询 | 在一个本地区间内就检查新版本并采纳 |

## 八、存储与一致性技术

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| safetensors | safetensors(张量文件格式) | 一种头部记录元数据的张量序列化格式,可安全加载 |
| atomic write | 原子写 | 「同目录临时文件 → fsync → 改名替换」,读者只会看到完整新文件 |
| os.replace | 改名替换 | 原子替换文件内容的系统调用 |
| fsync | 刷盘 | 把文件内容强制写回持久存储 |
| SQLite | SQLite(嵌入式数据库) | 权威提交记录的持久数据库 |
| rollback journal | 回滚日志 | SQLite 默认的崩溃安全日志模式(本仓库固定 `journal_mode=DELETE`) |
| WAL | 预写日志 | SQLite 的另一种日志模式(本仓库刻意不用) |
| transaction | 事务 | 一组要么全部生效要么全部回滚的数据库操作 |
| commit | 提交 | 事务生效 |
| rollback | 回滚 | 事务取消、恢复原状 |
| integrity check | 完整性检查 | 校验数据库内部一致性的 `PRAGMA integrity_check` |
| GC | 垃圾回收 | 引用驱动地删除不再被引用的历史文件 |
| checkpoint | 检查点 | 某一版本的全局权重文件(含外层优化器状态) |
| snapshot | 快照 | 某一时刻的完整数据副本 |
| digest | 摘要 | 文件的校验和(如 SHA-256) |
| fence | 隔离栅栏(fence) | 恢复时记录的旧心跳内容摘要,字节相同的旧心跳被忽略 |
| monotonic clock | 单调时钟 | 只增不减的计时器,用于计算时长(不受系统时间调整影响) |

## 九、模型与训练组件

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| param index | 参数索引 | 可训练参数 ↔ 扁平向量的映射契约(名称/形状/dtype/偏移) |
| fragment index | 分片索引 | 扁平向量划分为若干分片的契约 |
| flatten | 扁平化 | 把命名参数拼接成一维向量 |
| micro-batch | 微批 | 梯度累积中的单个前向/反向批次 |
| gradient accumulation | 梯度累积 | 多个微批梯度累加后统一步进 |
| autocast | 自动混合精度 | PyTorch 的 BF16/FP16 自动精度切换 |
| scheduler | 学习率调度器 | 按步数调整学习率的组件(warmup / cosine) |
| horizon | 步数上限 | 学习率调度的总步数(scheduler 的 horizon,与停止条件无关) |
| BF16 / FP32 / FP16 | BF16 / FP32 / FP16 | 浮点精度:bfloat16、float32(单精度)、float16 |
| dtype | 数据类型 | 张量的数值类型 |
| vocab | 词表 | 分词器/模型的词元表 |
| perplexity (ppl) | 困惑度 | 语言模型评估指标,loss 的指数 |

## 十、平台与工具

| 术语/缩写 | 中文译法 | 含义 |
|---|---|---|
| Miyabi | Miyabi(超算) | 本项目的目标超算系统 |
| PBS / qsub / qstat | PBS / 作业提交 / 作业查询 | 作业调度系统的提交与查询命令 |
| walltime | 墙钟时长 | 作业允许运行的总时长上限(`HH:MM:SS`) |
| group_list | 组列表 | PBS 的计费/权限组标识(提交前必须替换为字面值) |
| node | 节点 | 一台计算服务器 |
| login node | 登录节点 | 只做静态检查、不跑作业的入口节点 |
| compute node | 计算节点 | 运行作业的节点 |
| CUDA_VISIBLE_DEVICES | CUDA 可见设备列表 | 限制进程可见 GPU 的环境变量 |
| W&B | W&B(实验跟踪平台) | Weights & Biases,在线/离线实验指标平台 |
| lm-eval | lm-eval(评测工具) | Hugging Face 的 LM Evaluation Harness 评测框架 |
| HF | HF(Hugging Face) | Hugging Face 的模型/数据集生态 |
| run | 运行(run) | 一次完整的训练运行,共享同一个目录 |
| smoke | 冒烟 | 小规模快速验证 |
| matched | 配对对照 | 同配置的两种实现的对比实验(如 static vs dynamic) |
| p99 / p50 / p95 | 99%/50%/95% 分位 | 性能指标的百分位值 |
| ETA | 预计时间 | Estimated Time of Arrival,预计完成时间 |
| SIGKILL | 强制终止信号 | 无法被进程捕获、直接杀死的信号 |
| UUID | 通用唯一标识符 | 全局唯一的随机标识(如 `learner_li_<uuid4>`) |
| exit code | 退出码 | 进程退出时的整数值,0 表示成功 |
| stdout | 标准输出 | 进程的正常输出流 |
| dry-run | 预演 | 只输出将执行的操作、不真正执行 |

## 十一、状态字面量与丢弃原因(必须保留英文的值)

以下字符串直接写入文件或数据库,文档中一律用反引号原样引用:

- 更新状态:`pending`(待处理)/ `selected`(已选中)/ `applied`(已应用)/ `dropped`(已丢弃)/ `failed`(失败,当前未使用)
- learner 存活状态:`unknown` / `active` / `stopped` / `stale` / `dead`
- 丢弃原因:`missing_file`(文件缺失)/ `too_stale`(过时)/ `superseded`(被取代)/ `future_base`(基准超前)
- 停止原因:`stop_after_outer_steps` / `stop_after_global_tokens` / `input_exhausted` / `no_progress_timeout` / `completed` / `error`
- 采纳策略:replace / rebase_post_publish_delta / predict_post_publish_global
- 成员模式:`static` / `dynamic`;宽限模式:`fixed` / `adaptive_fastest_upload_eta`

## 阅读约定

- 代码标识符、配置字段、文件名、命令行参数:反引号内保留英文原样,不翻译;
- 首次出现的概念术语:中文(英文);再次出现:尽量用中文;
- 「全局版本号(global version)」这类带括号的写法,括号内即本术语表的统一英文。
