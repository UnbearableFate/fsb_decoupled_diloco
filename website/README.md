# FS-DiLoCo Documentation Site

这是 Filesystem Decoupled DiLoCo 的网页文档源文件。站点按 Overview、Getting Started、
Concepts、User Guide、Architecture、Reference 和 Experiments 组织；Experiments 当前只保留
结构化空位。Reference 由项目当前 Python AST 生成，为 `fs_diloco` 下的每个 `.py` 文件
提供独立页面。

## 本地预览

需要 Node.js 22.13 或更高版本。

```bash
npm install
npm run dev
```

默认开发地址由启动命令输出。

## 验证

```bash
npm run reference:generate
npm run build
npm test
```

`npm run build` 会先重新生成 `app/reference-data/api-manifest.json` 和
`app/reference-data/api-index.ts`。生成器记录模块、类、字段、方法、函数、参数、返回类型、
直接抛出的异常和源码位置；不要手工修改这两个生成文件。

内容事实以项目根目录中的当前源码、配置和 PBS 脚本为准。修改运行接口后，应同步更新
对应教程、Reference 和渲染测试。

## 代码与文档同步及定位

本节用于在 `fs_diloco` 新增、修改、重命名或删除代码后，快速找到对应的网页文档，并判断
哪些内容由生成器维护、哪些内容需要手工更新。

### Python 源码与 Reference 页面

`scripts/generate_reference.py` 会扫描项目根目录中的全部 `fs_diloco/**/*.py` 文件。普通模块
去掉 `.py` 后直接映射到 Reference 路径；包的 `__init__.py` 映射到包路径。

| Python 源码或元素 | Reference 路径 |
| --- | --- |
| `fs_diloco/modeling/hf_data.py` | `/reference/fs_diloco/modeling/hf_data` |
| `fs_diloco/modeling/__init__.py` | `/reference/fs_diloco/modeling` |
| `fs_diloco/__init__.py` | `/reference/fs_diloco` |
| `Batch` 类 | `/reference/fs_diloco/modeling/hf_data#class-batch` |
| `Batch.to` 方法 | `/reference/fs_diloco/modeling/hf_data#batch-to` |
| `text_rows_to_blocks` 函数 | `/reference/fs_diloco/modeling/hf_data#text_rows_to_blocks` |

每个模块页面记录模块变量、类、类字段、实例字段、方法、属性、函数、签名、参数、返回类型、
直接执行的 `raise`、docstring、源码行号和模块依赖。新增模块时，还应在
`scripts/generate_reference.py` 的 `MODULE_SUMMARIES` 中补充准确的模块职责说明，避免使用通用
后备文案。

生成链路如下：

| 位置 | 职责 | 维护方式 |
| --- | --- | --- |
| `scripts/generate_reference.py` | 解析 Python AST，计算源码身份并生成 Reference 数据 | 手工维护生成规则和模块摘要 |
| `app/reference-data/api-manifest.json` | 保存完整模块、符号、签名和依赖数据 | 运行生成器，禁止手工编辑 |
| `app/reference-data/api-index.ts` | 为 Reference 总览、左侧导航和搜索提供轻量索引 | 运行生成器，禁止手工编辑 |
| `app/reference-data/api.ts` | 提供模块查询、相邻页面和源码链接 | 接口结构变化时手工维护 |
| `app/components/ApiModuleReference.tsx` | 渲染模块、类、成员和函数页面 | 页面结构变化时手工维护 |
| `app/(docs)/reference/fs_diloco/[...slug]/page.tsx` | 将 Reference URL 解析为 Python 模块 | 路由规则变化时手工维护 |

### 按代码变更定位手写文档

AST 生成只能同步 API 表面，教程、概念、架构和操作语义仍需手工维护。

| 代码变更 | 优先检查的网页文档源码 |
| --- | --- |
| CLI 入口、参数或副作用 | `app/(docs)/reference/page.tsx`、`app/(docs)/getting-started/page.tsx`、`app/(docs)/user-guide/page.tsx` |
| 配置字段、默认值或跨字段校验 | `app/(docs)/reference/page.tsx`、`app/(docs)/getting-started/page.tsx` |
| 协议对象、不变量、选择或合并语义 | `app/(docs)/concepts/page.tsx`、`app/(docs)/architecture/page.tsx`、`app/(docs)/reference/page.tsx` |
| Learner、Syncer 或服务编排流程 | `app/(docs)/architecture/page.tsx`、`app/(docs)/user-guide/page.tsx` |
| 文件布局、持久化对象或状态语义 | `app/(docs)/reference/page.tsx`、`app/(docs)/architecture/page.tsx` |
| 安装、启动、恢复或故障排查流程 | `app/(docs)/getting-started/page.tsx`、`app/(docs)/user-guide/page.tsx` |
| 实验协议或结果 | `app/(docs)/experiments/page.tsx` |
| 顶部导航、章节名称或搜索入口 | `app/site.ts` 和相关页面 |

重命名或删除模块、类、方法或函数时，应搜索旧模块名、旧符号名和旧 URL，清除手写页面中的
失效引用。不要为了保留旧链接而在 Reference 中增加兼容路由。

### 同步流程

1. 在项目根目录完成代码修改，并同步更新源码中的英文 docstring。Reference 会直接显示源码
   docstring；缺少 docstring 时只能根据签名和实现结构生成保守说明。
2. 在最终生成前提交并推送项目根目录中的代码。生成器读取工作区文件内容，但
   `sourceRevision` 来自当前 `HEAD` 历史中最后一个修改 `fs_diloco` 的提交；如果
   `fs_diloco` 仍有未提交修改，页面内容、文件 SHA-256 和 GitHub 源码链接可能不对应。
3. 在 `website/` 中运行 `npm run reference:generate`，检查两个生成文件的差异，并使用上面的
   路径规则打开受影响的模块页面。
4. 根据变更类型更新手写章节。只修改 API 签名并不足以同步概念、架构、操作步骤或状态语义。
5. 对重命名和删除执行全文搜索，确认旧名称和旧 URL 已从页面、导航、测试和示例中移除。
6. 运行完整验证：

   ```bash
   npm test
   npx tsc --noEmit
   npm run lint
   ```

`npm test` 会重新生成 Reference、构建站点、核对 Python 文件与模块页面的一一对应关系，并
逐页验证全部生成路由。提交文档站点时，应同时包含手写页面和重新生成的 Reference 数据。

### Git 管理边界

`website/` 是项目根仓库直接管理的普通目录，不是独立 Git 仓库，也不是 Git submodule。不要
在 `website/` 中再次运行 `git init` 或创建 `website/.git`。所有状态检查、暂存和提交都应从
项目根目录执行，例如：

```bash
git status --short -- website .gitignore
git add website .gitignore
```

源码、配置、锁文件、Reference 生成结果、测试、`build/sites-vite-plugin.ts` 和
`.openai/hosting.json` 必须纳入版本控制。`node_modules/`、`dist/`、`.next/`、`.vinext/`、
`.wrangler/`、环境变量文件和 TypeScript 增量构建缓存属于本地生成内容，必须保持忽略。
