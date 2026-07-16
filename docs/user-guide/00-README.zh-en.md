# FS DiLoCo Miyabi 文档总览 / Documentation Index

本文档入口按内容逻辑拆分中英文说明。每个主题文件均先给中文说明，再给英文说明。

This index splits the bilingual documentation by topic. Each topic file contains Chinese first, then English.

## 阅读顺序 / Reading Order

- [系统概览 / System Overview](01-overview.zh-en.md): 介绍项目定位、核心角色、共享目录和当前里程碑边界。 / Project scope, roles, shared-root behavior, and milestone boundaries.
- [训练使用流程 / Training Workflows](02-training.zh-en.md): 说明登录节点静态检查、1/2/9 节点 PBS 运行、synthetic smoke、手动启动和 resume。 / Static checks, 1/2/9-node PBS runs, synthetic smoke, manual launch, and resume.
- [数据流与优化语义 / Dataflow and Semantics](03-dataflow-and-semantics.zh-en.md): 覆盖初始化、learner upload、syncer 聚合、global adoption、merge 权重和 outer optimizer。 / Initialization, learner uploads, syncer aggregation, global adoption, merge weights, and outer optimizer.
- [存储布局与元数据 / Storage Layout and Metadata](04-storage-and-schema.zh-en.md): 说明 run 目录、原子发布、param index、tensor 文件和 SQLite 数据模型。 / Run layout, atomic publication, param index, tensor files, and SQLite data model.
- [模块设计 / Module Design](05-modules.zh-en.md): 按模块列出 Python package 中每个组件的职责边界。 / Responsibilities and boundaries of each Python package module.
- [配置参数参考 / Configuration Reference](06-configuration.zh-en.md): 逐项说明 YAML 配置组和关键参数含义。 / Detailed YAML config groups and parameter meanings.
- [观测、排查与限制 / Operations, Troubleshooting, and Limits](07-operations.zh-en.md): 说明 analysis/SQLite 检查、常见问题、实验建议和已知限制。 / Analysis and SQLite checks, troubleshooting, experiment suggestions, and known limitations.

## 相关文档 / Related Docs

- [Miyabi runbook](../miyabi_runbook.md)
- [Design notes](../design.md)
- [Experiments](../experiments.md)
