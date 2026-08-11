import type { Metadata } from "next";
import { Callout, CodeBlock } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";

export const metadata: Metadata = {
  title: "Getting Started",
  description: "安装 FS-DiLoCo，初始化运行，提交 PBS actor，并检查终态。",
};

const toc = [
  { id: "prerequisites", label: "前置条件" },
  { id: "install", label: "安装" },
  { id: "choose-config", label: "选择配置" },
  { id: "initialize", label: "初始化运行" },
  { id: "submit", label: "提交 PBS actor" },
  { id: "verify", label: "检查结果" },
];

const install = `python -m venv .venv
source .venv/bin/activate
python -m pip install -e .`;

const initialize = `python -m fs_diloco.tools.launch_independent_run \\
  --config configs/full_protocol.yaml \\
  --run-id example \\
  --shared-root /path/to/runs/example \\
  --project-root "$PWD"`;

const submit = `python -m fs_diloco.tools.launch_independent_run \\
  --config configs/full_protocol.yaml \\
  --run-id example \\
  --shared-root /path/to/runs/example \\
  --project-root "$PWD" \\
  --submit \\
  --actor-queue regular-g \\
  --syncer-walltime 00:10:00 \\
  --learner-walltime 00:10:00 \\
  --log-root /absolute/path/to/logs/example`;

const verify = `python -m fs_diloco.tools.analysis /path/to/runs/example \\
  --expected-learners 8 \\
  --expected-global-steps 10 \\
  --require-terminal`;

export default function GettingStartedPage() {
  return (
    <DocsPage
      eyebrow="QUICKSTART"
      title="Getting Started"
      lede="本章完成一条最短可执行路径：安装项目、选择当前配置、创建不可变运行根目录、提交独立 PBS actor，并读取结构化结果。"
      toc={toc}
      previous={{ href: "/overview", label: "Overview" }}
      next={{ href: "/concepts", label: "Concepts" }}
    >
      <section id="prerequisites">
        <h2>前置条件</h2>
        <ul className="check-list">
          <li>
            <strong>Python 3.13 或更高版本。</strong>
            项目元数据把 <code>requires-python</code> 固定为 <code>&gt;=3.13</code>。
          </li>
          <li>
            <strong>所有 actor 可见的共享文件系统。</strong>
            <code>--shared-root</code> 必须解析到相同的运行目录。
          </li>
          <li>
            <strong>可用的 PBS 环境。</strong>
            提交前确认队列、literal group ID、节点资源和 walltime 均适用于当前 Miyabi 账户。
          </li>
          <li>
            <strong>默认使用干净源码。</strong>
            只有明确接受 dirty source snapshot 时才使用 <code>--allow-dirty-snapshot</code>。
          </li>
        </ul>
        <Callout title="计算节点边界" tone="warning">
          <p>
            登录节点只用于安装轻量依赖、静态检查、<code>qsub</code>、<code>qstat</code>
            和读取结果。模型加载、训练、Syncer 张量合并与运行期验证应在 PBS 计算节点执行。
          </p>
        </Callout>
      </section>

      <section id="install">
        <h2>安装</h2>
        <p>在项目根目录创建独立环境并安装当前源码。</p>
        <CodeBlock label="Shell">{install}</CodeBlock>
        <p>
          运行依赖包括 PyTorch、Transformers、Datasets、Safetensors 和 PyYAML。
          开发依赖位于 <code>.[dev]</code>，仅在需要运行测试或静态检查时安装。
        </p>
      </section>

      <section id="choose-config">
        <h2>选择配置</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>配置</th>
                <th>用途</th>
                <th>关键拓扑</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><code>full_protocol_functional.yaml</code></td>
                <td>CPU synthetic 功能路径。</td>
                <td>4 个 stream，4 个全局步，可执行容量恢复。</td>
              </tr>
              <tr>
                <td><code>full_protocol.yaml</code></td>
                <td>当前正式固定容量验收配置。</td>
                <td>8 个 Learner，50 个 inner step，10 个全局步。</td>
              </tr>
              <tr>
                <td><code>experiments/gpt2_wikitext2_8l_200x10.yaml</code></td>
                <td>GPT-2 / WikiText-2 容量恢复实验。</td>
                <td>8 个 stream，quorum 为 4，启用自动 replacement。</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p>
          配置解析器拒绝未知字段、错误类型和不满足跨字段约束的组合。
          复制示例后，只保留当前设计需要的字段。
        </p>
      </section>

      <section id="initialize">
        <h2>初始化运行</h2>
        <p>
          不带 <code>--submit</code> 运行 launcher。命令会验证配置和源码身份，
          创建新的不可变运行根目录，并在 JSON 输出中给出将要使用的 PBS 命令。
        </p>
        <CodeBlock label="Shell">{initialize}</CodeBlock>
        <Callout title="这不是 dry-run" tone="warning">
          <p>
            命令会实际创建 <code>/path/to/runs/example</code>。运行根目录必须是新的；
            如果相同 identity 的完整 run 已存在，Initializer 只验证并返回该 run；
            不同 identity 的路径碰撞会被拒绝。只想检查 YAML 时，可在 Python 中调用
            <code>load_config()</code>。
          </p>
        </Callout>
        <p>初始化完成后，重点核对 JSON 输出中的以下值：</p>
        <ul>
          <li><code>descriptor.descriptor_sha256</code> 与预期运行身份一致。</li>
          <li><code>descriptor.stream_pool_size</code> 与所选配置一致。</li>
          <li><code>syncer_qsub</code> 和每条 <code>learner_qsubs</code> 使用正确路径。</li>
        </ul>
      </section>

      <section id="submit">
        <h2>提交 PBS actor</h2>
        <p>
          正式提交需要显式提供队列、Syncer walltime、Learner walltime 和新的绝对日志目录。
          两个 walltime 都必须是 <code>HH:MM:SS</code>，且不少于 10 分钟。
        </p>
        <CodeBlock label="Shell">{submit}</CodeBlock>
        <p>
          上例中的 10 分钟只是配置允许的下界。应根据模型、数据、节点数和既有证据估计
          最短可行 walltime，并为启动、波动与有序退出保留余量。
        </p>
        <Callout title="部分提交不会自动撤销" tone="note">
          <p>
            如果 Syncer 已提交而后续某个 Learner 提交失败，launcher 会保存已接受的 job ID
            并返回 <code>partial</code>，但不会隐式执行 <code>qdel</code>。
            先读取 submission receipt，再决定补交或取消。
          </p>
        </Callout>
      </section>

      <section id="verify">
        <h2>检查结果</h2>
        <p>
          运行结束后，先用只读分析命令检查 authority 完整性、Learner 数量、全局版本和终态。
        </p>
        <CodeBlock label="Shell">{verify}</CodeBlock>
        <p>
          命令成功表示这些显式断言成立，不等同于完整实验验收。正式验收还应运行
          <code>scripts/miyabi/agent/check_independent_run.pbs</code>，并保存
          Checker 的结构化证据、源码身份、PBS job ID 与解析后的配置。
        </p>
        <div className="next-callout">
          <div>
            <small>NEXT</small>
            <strong>先理解一次 proposal 如何变成全局版本</strong>
          </div>
          <a href="/concepts">继续阅读 Concepts →</a>
        </div>
      </section>
    </DocsPage>
  );
}
