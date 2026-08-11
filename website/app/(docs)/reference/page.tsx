import type { Metadata } from "next";
import { ApiEntry } from "../../components/ApiEntry";
import { Callout, CodeBlock } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";
import {
  apiModuleIndex,
  apiReferenceStats,
} from "../../reference-data/api-index";
import { sourceUrl } from "../../site";

export const metadata: Metadata = {
  title: "Reference",
  description: "FS-DiLoCo 的 CLI、配置、Python API 和运行目录参考。",
};

const toc = [
  { id: "python-modules", label: "完整 Python API" },
  { id: "cli", label: "CLI Reference" },
  { id: "config", label: "Configuration Reference" },
  { id: "python-api", label: "常用 Python API" },
  { id: "filesystem", label: "Filesystem Reference" },
  { id: "status", label: "状态与错误语义" },
];

const apiPackageGroups = apiModuleIndex
  .filter((item) => item.isPackage)
  .map((packageItem) => ({
    packageItem,
    modules: apiModuleIndex.filter(
      (item) => !item.isPackage && item.package === packageItem.module,
    ),
  }));

const tree = `/path/to/run/
├── .identity
├── .complete
├── run_config.resolved.yaml
├── control/
│   ├── run_descriptor.json
│   ├── run_source_manifest.json
│   ├── artifact_policy.json
│   ├── bootstrap_complete.json
│   ├── param_index.json
│   ├── run_config.resolved.yaml
│   ├── syncer_metadata.sqlite3
│   ├── latest.json
│   ├── summary.json
│   ├── registration_requests/
│   └── syncer_epochs/
├── weights/epochs/
├── optim/epochs/
├── updates/
│   ├── latest/
│   └── payloads/
├── metrics/
├── audit/
├── heartbeats/
└── logs/`;

export default function ReferencePage() {
  return (
    <DocsPage
      eyebrow="LOOKUP"
      title="Reference"
      lede="本章覆盖 fs_diloco 的全部当前 Python 模块，并保留 CLI、配置和运行目录契约。每个模块页面按类、类成员、函数、参数、返回值、异常和源码位置组织。"
      toc={toc}
      previous={{ href: "/architecture", label: "Architecture" }}
      next={{ href: "/experiments", label: "Experiments" }}
    >
      <section id="python-modules">
        <h2>完整 Python API</h2>
        <p>
          下列目录由当前 <code>fs_diloco/**/*.py</code> 源码生成。每个 Python 文件对应一个独立页面；
          包的 <code>__init__.py</code> 使用包路径作为页面地址。
        </p>
        <div className="api-coverage-strip">
          <div><strong>{apiReferenceStats.modules}</strong><span>模块页面</span></div>
          <div><strong>{apiReferenceStats.classes}</strong><span>类</span></div>
          <div><strong>{apiReferenceStats.functions}</strong><span>模块函数</span></div>
          <div><strong>{apiReferenceStats.methods}</strong><span>方法与属性</span></div>
          <div><strong>{apiReferenceStats.symbols}</strong><span>已记录元素</span></div>
        </div>
        <Callout title="覆盖边界" tone="note">
          <p>
            Reference 记录模块体直接声明的类、模块变量和函数，以及类字段、实例字段、方法与属性。
            <code>Raises</code> 列表只统计当前函数体直接执行的 <code>raise</code>；被调用函数仍可能抛出其他异常。
          </p>
        </Callout>
        <div className="api-package-catalog">
          {apiPackageGroups.map(({ packageItem, modules }) => (
            <section key={packageItem.module}>
              <header>
                <div>
                  <span>PACKAGE</span>
                  <h3><a href={packageItem.route}><code>{packageItem.module}</code></a></h3>
                </div>
                <strong>{modules.length} modules</strong>
              </header>
              <p>{packageItem.summary}</p>
              {modules.length > 0 ? (
                <div>
                  {modules.map((moduleItem) => (
                    <a href={moduleItem.route} key={moduleItem.module}>
                      <code>{moduleItem.module.split(".").at(-1)}</code>
                      <span>{moduleItem.summary}</span>
                    </a>
                  ))}
                </div>
              ) : (
                <small>该包当前只包含 <code>__init__.py</code>。</small>
              )}
            </section>
          ))}
        </div>
      </section>

      <section id="cli">
        <h2>CLI Reference</h2>
        <p>
          所有路径参数均应使用目标节点可解析的路径。运行期入口必须指向初始化时固化的
          <code>control/run_config.resolved.yaml</code>，不能替换为原始配置。
        </p>
        <div className="cli-list">
          <article>
            <header><code>python -m fs_diloco.tools.launch_independent_run</code><span>PRIMARY</span></header>
            <p>初始化一个 Full Protocol run，并可选地提交独立 Syncer 与 Learner PBS job。</p>
            <dl>
              <div><dt><code>--config PATH</code></dt><dd>必填。当前 YAML 配置。</dd></div>
              <div><dt><code>--run-id ID</code></dt><dd>可选。安全身份组件；省略时自动生成。</dd></div>
              <div><dt><code>--shared-root PATH</code></dt><dd>可选。运行根目录；省略时位于项目 <code>runs/</code> 下。</dd></div>
              <div><dt><code>--project-root PATH</code></dt><dd>默认当前目录。用于源码身份和 PBS 脚本路径。</dd></div>
              <div><dt><code>--submit</code></dt><dd>实际调用 <code>qsub</code>。省略时仍会初始化 run。</dd></div>
              <div><dt><code>--actor-queue QUEUE</code></dt><dd>提交时必填。所有独立 actor 的 PBS 队列。</dd></div>
              <div><dt><code>--syncer-walltime HH:MM:SS</code></dt><dd>提交时必填，至少 10 分钟。</dd></div>
              <div><dt><code>--learner-walltime HH:MM:SS</code></dt><dd>提交时必填，至少 10 分钟。</dd></div>
              <div><dt><code>--log-root PATH</code></dt><dd>提交时必填。新的绝对 actor 日志目录。</dd></div>
              <div><dt><code>--allow-dirty-snapshot</code></dt><dd>显式允许 dirty source bootstrap。</dd></div>
            </dl>
          </article>

          <article>
            <header><code>python -m fs_diloco.tools.init_run</code><span>BOOTSTRAP</span></header>
            <p>只初始化运行根目录，不生成独立 actor 提交 receipt。</p>
            <dl>
              <div><dt><code>--config PATH</code></dt><dd>必填。当前 YAML 配置。</dd></div>
              <div><dt><code>--run-id ID</code></dt><dd>覆盖配置中的 run ID。</dd></div>
              <div><dt><code>--shared-root PATH</code></dt><dd>覆盖配置中的 shared root。</dd></div>
              <div><dt><code>--project-root PATH</code></dt><dd>默认当前目录。</dd></div>
              <div><dt><code>--allow-dirty-snapshot</code></dt><dd>允许 dirty source bootstrap。</dd></div>
            </dl>
          </article>

          <article>
            <header><code>python -m fs_diloco.syncer</code><span>RUNTIME</span></header>
            <p>启动一个 fenced Syncer candidate。候选必须先验证 descriptor，再申请 leader lease。</p>
            <dl>
              <div><dt><code>--config PATH</code></dt><dd>必填，且必须等于 immutable resolved config。</dd></div>
              <div><dt><code>--shared-root PATH</code></dt><dd>必填。已完成 bootstrap 的运行根目录。</dd></div>
            </dl>
          </article>

          <article>
            <header><code>python -m fs_diloco.learner</code><span>RUNTIME</span></header>
            <p>启动 torch-free admission entrypoint；admission 成功后进入训练 runtime。</p>
            <dl>
              <div><dt><code>--config PATH</code></dt><dd>必填，且必须等于 immutable resolved config。</dd></div>
              <div><dt><code>--shared-root PATH</code></dt><dd>必填。已完成 bootstrap 的运行根目录。</dd></div>
              <div><dt><code>--learner-id ID</code></dt><dd>Static 模式必填。</dd></div>
              <div><dt><code>--logical-launch-id ID</code></dt><dd>Static launch identity；PBS wrapper 通常生成。</dd></div>
              <div><dt><code>--bootstrap-slot N</code></dt><dd>Dynamic 初始实例使用，与 launch request 互斥。</dd></div>
              <div><dt><code>--launch-request-id ID</code></dt><dd>Dynamic replacement 使用。</dd></div>
              <div><dt><code>--stream-id N</code></dt><dd>Dynamic replacement 的目标 stream。</dd></div>
              <div><dt><code>--replace-instance-id ID</code></dt><dd>可选。被替换的 dynamic instance。</dd></div>
            </dl>
          </article>

          <article>
            <header><code>python -m fs_diloco.tools.analysis SHARED_ROOT</code><span>READ ONLY</span></header>
            <p>汇总 authority，并可对 contributor 数、全局步和 terminal 记录执行断言。</p>
            <dl>
              <div><dt><code>--expected-learners N</code></dt><dd>要求当前或 terminal fence 数量等于 N。</dd></div>
              <div><dt><code>--expected-global-steps N</code></dt><dd>要求 latest committed version 等于 N。</dd></div>
              <div><dt><code>--require-terminal</code></dt><dd>要求 authority 存在 terminal 记录。</dd></div>
            </dl>
          </article>

          <article>
            <header><code>python -m fs_diloco.tools.request_static_replacement</code><span>MUTATING</span></header>
            <p>发布一份不可变 static replacement authorization。旧 fence 与新 attempt 参数全部必填。</p>
          </article>
          <article>
            <header><code>python -m fs_diloco.tools.request_terminal_close</code><span>MUTATING</span></header>
            <p>为 <code>manual</code> close policy 发布唯一不可变 close request。</p>
          </article>
          <article>
            <header><code>python -m fs_diloco.tools.resolve_scheduler_uncertainty</code><span>DRY-RUN FIRST</span></header>
            <p>预览或发布 dynamic scheduler operator request。只有附加 <code>--apply</code> 才写文件。</p>
          </article>
          <article>
            <header><code>python -m fs_diloco.tools.clean_run</code><span>DESTRUCTIVE WITH --execute</span></header>
            <p>默认生成已完成 run 的清理 inventory；<code>--execute --manifest PATH</code> 执行并保存 manifest。</p>
          </article>
        </div>
      </section>

      <section id="config">
        <h2>Configuration Reference</h2>
        <p>
          下表的默认值来自 <code>fs_diloco.core.config.Config</code> dataclass，
          不等同于三个仓库示例配置的具体值。所有浮点值必须有限。
        </p>
        <div className="table-wrap config-table">
          <table>
            <thead>
              <tr><th>段</th><th>当前字段与默认值</th><th>关键约束</th></tr>
            </thead>
            <tbody>
              <tr>
                <td><code>run</code></td>
                <td><code>name=full_protocol</code>、<code>run_id=null</code>、<code>shared_root=null</code>、源码身份字段。</td>
                <td>ID 只能使用安全字符；formal bootstrap 需要 commit 与 source fingerprint。</td>
              </tr>
              <tr>
                <td><code>model</code></td>
                <td><code>name_or_path=synthetic-tiny</code>、revision、tokenizer revision、dtype、compile、synthetic shape。</td>
                <td>dtype 只有 <code>float32</code> 或 <code>bfloat16</code>；远程模型必须 pin revision。</td>
              </tr>
              <tr>
                <td><code>data</code></td>
                <td><code>dataset_name=synthetic</code>、config/revision、<code>train_split=train</code>、<code>block_size=16</code>、cache、shuffle。</td>
                <td>远程数据必须 pin revision；block size 至少 2。</td>
              </tr>
              <tr>
                <td><code>sync</code></td>
                <td><code>num_learners=8</code>、<code>quorum_min=4</code>、<code>quorum_max=8</code>、<code>staleness_lambda=0.25</code>、停止目标。</td>
                <td><code>1 ≤ min ≤ max ≤ num_learners</code>；至少存在一个可达停止条件。</td>
              </tr>
              <tr>
                <td><code>syncer</code></td>
                <td><code>device=auto</code>、<code>compute_dtype=float32</code>、<code>publish_dtype=float32</code>。</td>
                <td>device 只有 <code>auto</code>、<code>cpu</code>、<code>cuda</code>。</td>
              </tr>
              <tr>
                <td><code>membership</code></td>
                <td><code>mode=static</code>、stream pool、bootstrap instances、admission/heartbeat timeout。</td>
                <td>Dynamic 中 <code>num_learners = stream_pool_size</code>；initial deadline 必须覆盖 request TTL。</td>
              </tr>
              <tr>
                <td><code>scaling</code></td>
                <td>开关、目标 contributor、低容量窗口、launch budget、reconcile timeout、PBS script/queue/walltime。</td>
                <td>只允许 dynamic；启用时 walltime 必填且至少 10 分钟，launch 与窗口关系受约束。</td>
              </tr>
              <tr>
                <td><code>terminal</code></td>
                <td>close policy、deadline、drain ack timeout、可见性 grace、terminal merge 上限。</td>
                <td>Policy 只有 <code>global_target_or_launch_budget</code>、<code>global_target</code>、<code>manual</code>、<code>deadline</code>。</td>
              </tr>
              <tr>
                <td><code>training</code></td>
                <td><code>inner_steps=100</code>、micro batch、gradient accumulation、max local steps、completion mode、seed、grad clip。</td>
                <td>步数和 batch 必须为正；<code>global_only</code> 必须有全局或 token 停止目标。</td>
              </tr>
              <tr>
                <td><code>inner_optimizer</code></td>
                <td><code>name=adamw</code>、lr、betas、eps、weight decay、<code>scheduler=none</code>、warmup、total steps、min ratio。</td>
                <td>只有 AdamW；scheduler 只有 <code>none</code> 或 <code>cosine</code>。</td>
              </tr>
              <tr>
                <td><code>outer_optimizer</code></td>
                <td><code>name=nesterov</code>、<code>lr=0.7</code>、<code>momentum=0.9</code>、weight decay、betas、eps。</td>
                <td>名称只有 <code>sgd</code>、<code>momentum</code>、<code>nesterov</code>、<code>adamw</code>。</td>
              </tr>
              <tr>
                <td><code>io</code></td>
                <td><code>tensor_dtype=float32</code>。</td>
                <td>只有 <code>float32</code> 或 <code>bfloat16</code>。</td>
              </tr>
              <tr>
                <td><code>learner</code></td>
                <td>inner poll、发布后采纳、global adoption strategy、发布后等待与 prediction reconcile timeout。</td>
                <td>策略与轮询/等待组合由统一 adoption validator 校验。</td>
              </tr>
              <tr>
                <td><code>leader</code></td>
                <td>lease、renew、clock skew、SQLite busy timeout、candidate wait、learner recovery wait。</td>
                <td>lease 至少是 renew interval 的 5 倍；各层等待时间必须覆盖下层边界。</td>
              </tr>
              <tr>
                <td><code>maintenance</code></td>
                <td>archive batch、recent dedup、quarantine 上限、publication orphan grace。</td>
                <td>所有计数为正；orphan grace 必须覆盖 leader 安全时间窗。</td>
              </tr>
            </tbody>
          </table>
        </div>
        <Callout title="Source of truth" tone="note">
          <p>
            字段类型、默认值和跨字段约束以
            <a href={sourceUrl("fs_diloco/core/config.py", 24)}>Config 与 _normalize_and_validate()</a>
            为准。YAML 示例用于具体运行，不定义兼容接口。
          </p>
        </Callout>
      </section>

      <section id="python-api">
        <h2>常用 Python API</h2>
        <p>
          以下条目保留面向常见任务的精选说明。完整类和函数列表位于上方模块目录；
          这里的「当前」表示与本站对应的源码 revision，不承诺跨 revision 向后兼容。
        </p>

        <ApiEntry
          id="api-load-config"
          signature="load_config(path: str | Path) -> Config"
          summary="读取一个 YAML mapping，拒绝未知字段，并执行完整结构与语义校验。"
          parameters={[
            { name: "path", type: "str | pathlib.Path", description: "待读取的当前 Full Protocol YAML 文件。" },
          ]}
          returns={<>经过规范化和验证的 <code>Config</code>。</>}
          raises={[
            { name: "ValueError", description: "文件不是 mapping、字段未知、类型错误或跨字段约束失败。" },
          ]}
          note={<>该函数不解析 run ID 和 shared root，也不绑定源码身份；运行初始化通常使用 <code>resolve_config()</code>。</>}
          source={sourceUrl("fs_diloco/core/config.py", 390)}
        />

        <ApiEntry
          id="api-resolve-config"
          signature="resolve_config(path, *, run_id=None, shared_root=None, project_root=None) -> Config"
          summary="解析运行期配置：应用显式覆盖和受支持的源码身份环境变量，生成缺省 run ID 与 shared root，并执行完整校验。"
          parameters={[
            { name: "path", type: "str | pathlib.Path", description: "输入 YAML 路径。" },
            { name: "run_id", type: "str | None", description: "可选运行 ID 覆盖。" },
            { name: "shared_root", type: "str | None", description: "可选运行根目录覆盖，支持 {run_id} 占位符。" },
            { name: "project_root", type: "str | pathlib.Path | None", description: "缺省 shared root 的项目基准目录。" },
          ]}
          returns={<>字段完整、路径已解析的 <code>Config</code>。</>}
          raises={[
            { name: "ValueError", description: "配置无效，或 formal identity 环境要求未满足。" },
          ]}
          source={sourceUrl("fs_diloco/core/config.py", 413)}
        />

        <ApiEntry
          id="api-source-identity"
          signature="capture_source_identity(project_root: str | Path) -> dict[str, Any]"
          summary="计算当前 Git commit、所覆盖源码是否 dirty，以及按文件内容构造的 SHA-256 source fingerprint。"
          parameters={[
            { name: "project_root", type: "str | pathlib.Path", description: "包含当前 Git checkout 的项目根目录。" },
          ]}
          returns={<>包含 <code>git_commit</code>、<code>git_dirty</code>、<code>source_scopes</code>、文件清单和 <code>source_fingerprint</code> 的字典。</>}
          raises={[
            { name: "subprocess.CalledProcessError", description: "Git 查询失败。" },
          ]}
          note="指纹使用源码内容，不依赖文件 mtime。"
          source={sourceUrl("fs_diloco/core/source_identity.py", 74)}
        />

        <ApiEntry
          id="api-initialize-run"
          signature="initialize_run(config, *, project_root, allow_dirty_snapshot=False, fault_hook=None) -> dict[str, Any]"
          summary="创建或验证一个 crash-recoverable、no-replace 的运行根目录，并发布版本 0、authority 与 immutable bootstrap marker。"
          parameters={[
            { name: "config", type: "Config", description: "已经解析且绑定源码身份的配置。" },
            { name: "project_root", type: "str | pathlib.Path", description: "用于 source lock 和脚本身份的项目根目录。" },
            { name: "allow_dirty_snapshot", type: "bool", description: "是否显式允许 dirty source。默认 false。" },
            { name: "fault_hook", type: "Callable[[str], None] | None", description: "仅用于受控 fault-injection 测试的回调。" },
          ]}
          returns="包含 descriptor、路径和 bootstrap 元数据的 JSON-compatible 字典。"
          raises={[
            { name: "ValueError", description: "配置缺少 resolved identity，或 dirty source 未被允许。" },
            { name: "FileExistsError", description: "目标身份与已存在或预留的运行根目录冲突。" },
            { name: "RuntimeError", description: "已有 manifest、identity 或对象完整性校验失败。" },
          ]}
          note="如果相同 identity 的完整 run 已存在，函数验证并返回该 run；不会覆盖对象。"
          source={sourceUrl("fs_diloco/tools/init_run.py", 51)}
        />

        <ApiEntry
          id="api-load-descriptor"
          signature="load_run_descriptor(shared_root, *, expected_run_id=None, expected_git_commit=None, expected_git_dirty=None, expected_source_fingerprint=None, expected_descriptor_sha256=None) -> LoadedRunDescriptor"
          summary="验证 descriptor 自哈希、协议/schema/mode、运行路径、resolved config、source manifest 与可选期望身份。"
          parameters={[
            { name: "shared_root", type: "str | pathlib.Path", description: "已完成 bootstrap 的运行根目录。" },
            { name: "expected_*", type: "optional identity fields", description: "提交命令携带的期望运行身份；提供后必须精确匹配。" },
          ]}
          returns={<>包含 <code>descriptor</code>、<code>config</code>、<code>RunPaths</code> 与 authority identity 的冻结对象。</>}
          raises={[
            { name: "RuntimeError", description: "任一 checksum、路径、版本、mode 或身份不匹配。" },
          ]}
          source={sourceUrl("fs_diloco/core/run_descriptor.py", 63)}
        />

        <ApiEntry
          id="api-weights"
          signature="normalized_update_weights(updates, *, current_version, staleness_lambda) -> dict[str, float]"
          summary="按 direct token 与版本陈旧度计算正权重，并归一化到总和 1。"
          parameters={[
            { name: "updates", type: "list[dict[str, Any]]", description: "每项包含唯一 update_id、正整数 tokens_this_update 和合法 base_global_version。" },
            { name: "current_version", type: "int", description: "当前非负 committed version。" },
            { name: "staleness_lambda", type: "float", description: "有限非负陈旧度惩罚系数。" },
          ]}
          returns="从 update ID 到归一化 float 权重的字典。"
          raises={[
            { name: "ValueError", description: "输入为空、身份重复、token/base 非法，或权重非有限正数。" },
          ]}
          source={sourceUrl("fs_diloco/protocol/merge.py", 17)}
        />

        <ApiEntry
          id="api-outer-step"
          signature="outer_optimizer_step(theta, grad, state, config) -> tuple[Tensor, dict[str, Tensor]]"
          summary="对扁平全局参数执行一次显式外层优化器更新。输入会 detach/clone，不原地修改调用方 tensor。"
          parameters={[
            { name: "theta", type: "torch.Tensor", description: "当前扁平全局参数。" },
            { name: "grad", type: "torch.Tensor", description: "与 theta 同形的外层 pseudo-gradient。" },
            { name: "state", type: "dict[str, torch.Tensor]", description: "step 以及 momentum 或 Adam moments。" },
            { name: "config", type: "OuterOptimizerSection", description: "当前外层优化器参数。" },
          ]}
          returns="新的 theta 与新的 optimizer state。"
          raises={[
            { name: "ValueError", description: "outer optimizer 名称不受支持。" },
          ]}
          note={<>支持 <code>sgd</code>、<code>momentum</code>、<code>nesterov</code> 和 <code>adamw</code>。</>}
          source={sourceUrl("fs_diloco/modeling/outer_optim.py", 23)}
        />

        <ApiEntry
          id="api-runpaths"
          signature="RunPaths(shared_root: pathlib.Path)"
          summary="集中定义运行目录的规范路径与 epoch-scoped object name。"
          parameters={[
            { name: "shared_root", type: "pathlib.Path", description: "规范运行根目录。" },
          ]}
          returns="冻结的路径 helper；属性和方法返回 pathlib.Path。"
          note="RunPaths 只构造路径，不证明对象存在或可信。读取前仍需对应的 identity、checksum 或 authority 校验。"
          source={sourceUrl("fs_diloco/storage/paths.py", 13)}
        />
      </section>

      <section id="filesystem">
        <h2>Filesystem Reference</h2>
        <p>
          目录由 <code>RunPaths</code> 定义。以下结构展示主要稳定表面；
          某些控制文件只有在对应生命周期阶段才出现。
        </p>
        <CodeBlock label="Run root">{tree}</CodeBlock>
        <div className="table-wrap">
          <table>
            <thead><tr><th>路径</th><th>契约</th><th>写入者</th></tr></thead>
            <tbody>
              <tr><td><code>.identity</code></td><td>运行根目录 identity 与自哈希。</td><td>Initializer；不可变。</td></tr>
              <tr><td><code>.complete</code></td><td>Bootstrap 对象 manifest；最后发布的可见性 marker。</td><td>Initializer；不可写。</td></tr>
              <tr><td><code>control/syncer_metadata.sqlite3</code></td><td>权威事务状态。</td><td>当前 fenced Syncer。</td></tr>
              <tr><td><code>control/latest.json</code></td><td>最新 committed version 的便利副本。</td><td>当前 Syncer，原子替换。</td></tr>
              <tr><td><code>control/syncer_epochs/</code></td><td>按 epoch + owner 隔离的 heartbeat、latest、membership 与 terminal publication。</td><td>对应 epoch 的 Syncer。</td></tr>
              <tr><td><code>updates/payloads/</code></td><td>Learner 完整参数 proposal payload。</td><td>对应 admitted Learner；不可变。</td></tr>
              <tr><td><code>updates/latest/</code></td><td>每个稳定 contributor 的最新 proposal pointer。</td><td>对应 admitted Learner；原子替换。</td></tr>
              <tr><td><code>weights/epochs/</code></td><td>按 Syncer epoch 发布的全局权重。</td><td>当前 Syncer；不可变。</td></tr>
              <tr><td><code>optim/epochs/</code></td><td>与全局权重同 publication 的 outer state。</td><td>当前 Syncer；不可变。</td></tr>
              <tr><td><code>metrics/</code></td><td>Actor JSONL 事件与 immutable runtime attestation。</td><td>各 actor。</td></tr>
              <tr><td><code>audit/</code></td><td>有界归档 batch、partition 与 command receipt。</td><td>当前 Syncer。</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="status">
        <h2>状态与错误语义</h2>
        <div className="status-grid">
          <article><code>PASS</code><p>Checker 的全部必需断言成立，并且证据 identity 完整。</p></article>
          <article><code>FAIL</code><p>已执行检查并发现一个或多个确定失败。</p></article>
          <article><code>BLOCKED</code><p>缺少运行条件或证据，无法完成所需 gate；不表示通过。</p></article>
          <article><code>REVIEW</code><p>结构化产物需要人工处置，不能作为成功证据。</p></article>
          <article><code>partial</code><p>Launcher 只提交了部分独立 actor；先读取 receipt，再人工处置。</p></article>
          <article><code>REFUSED</code><p>清理工具无法证明目标安全，不执行删除。</p></article>
        </div>
        <Callout title="保留原始机器值" tone="warning">
          <p>
            JSON 状态、enum、字段名和错误原文保持英文机器值。中文说明解释实际语义，
            不改写可供脚本读取的字面量。
          </p>
        </Callout>
      </section>
    </DocsPage>
  );
}
