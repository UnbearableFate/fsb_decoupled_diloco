# plan03-1 精简代码库以及重构

## 目的

把仓库收敛成只有一种可运行、可配置、可分析、可测试的 filesystem Decoupled DiLoCo 协议：`Full Protocol`。它就是当前已经实现的 Full Protocol v4 语义，但产品名、模块名、类型名、配置入口、schema 文件名和运行目录不再携带 `v4` 世代后缀。

## 过程

1. 分析当前fs_diloco 代码中Full Protocol v4 的路径.
2. 将相同含义的代码进行归并,比如将fs_diloco/core/config.py中被Full Protocol v4 的路径依赖的函数或代码行归并到fs_diloco/core/config_4.py里
3. 移除所有用不到的代码.
4. 将v4系列代码改名, 不再携带 `v4` 世代后缀。
5. 整理fs_diloco 代码,进行重构以符合最佳代码范式.
6. 以模块为单位构建单元测试, 修正和测试
6. 以功能为单位构建4 learner + 1 syncer的测试, 测试已经实现过的节点状态动态变化容错等功能,对测试代码进行多agent review,然后测试
7. 进行8 learner + 1 syncer 的 50 local steps * 10 global steps的测试.
8. 更新docs