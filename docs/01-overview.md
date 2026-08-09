# 概览

## 目标

本仓库验证一种共享文件系统上的 decoupled 训练结构：learner 只做局部训练和不可变对象发布，syncer 只在取得 fenced authority 后选择 proposal、执行外层优化并发布新 global version。系统优先保证崩溃后可解释、无双写和 token 可核对，而不是把缓存文件当作共识服务。

## 当前运行形态

生产协议只有 Full Protocol v4：

- static topology：固定 learner 集合，可显式授权替换某个 active attempt。
- dynamic topology：固定 stream pool，进程 incarnation 可替换，stream key 保持稳定。
- single candidate：常见部署，一次只提交一个 syncer candidate。
- multi-candidate：多个 candidate 可竞争；SQLite lease 保证唯一 leader。

learner 在 admission 完成前不 import torch、不分配 CUDA。learner 永不打开 authority SQLite；它只读取经过当前 epoch heartbeat 和 digest 绑定的控制文件。

## 一次有效合并

1. learner 从已确认的 global version 和持久 cursor 开始一个 cycle。
2. cycle 内的 replace/rebase 会在 segment accumulator 中明确转移 token fate。
3. learner 先发布不可变 tensor，再发布 typed proposal/receipt。
4. leader 摄取并验证 contributor fence、cursor/hash chain、payload identity。
5. authority 按持久 service credit 选择 quorum；selection 本身不消耗 credit。
6. syncer 计算加权参数与外层优化器状态，发布不可变 weight/optim。
7. 同一 fenced command 提交 version、selection、publication 和 token fate。
8. leader 发布 current epoch latest；learner 验证后采纳。

## 不支持的形态

当前分支不提供 classic full、Fragment V0、旧 resume、fixed latest/stop authority、旧 schema bootstrap 或动态 mutator proxy。旧完成 run 只能 query-only 分析、导出或评估；旧未完成 run 不可续训。

归档 tag `archive/classic-full-v1-final` 和 `archive/fragment-v0-final` 均指向 `a00a3d64a50f10a2478c3f4fe795e658d1b3b52f`。
