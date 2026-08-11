import type { Metadata } from "next";
import { Callout, CodeBlock } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";

export const metadata: Metadata = {
  title: "User Guide",
  description: "配置、运行、观测、恢复和安全清理 FS-DiLoCo 运行。",
};

const toc = [
  { id: "configuration-workflow", label: "配置工作流" },
  { id: "launch-topologies", label: "启动拓扑" },
  { id: "observe", label: "观测运行" },
  { id: "operator-actions", label: "Operator 操作" },
  { id: "recovery", label: "故障与恢复" },
  { id: "cleanup", label: "完成后清理" },
];

const uncertainty = `python -m fs_diloco.tools.resolve_scheduler_uncertainty \\
  --shared-root /path/to/run \\
  --launch-request-id REQUEST_ID \\
  --action confirm_job_id \\
  --expected-state-sha256 STATE_SHA256 \\
  --scheduler-job-id PBS_JOB_ID \\
  --reason "confirmed from qstat" \\
  --evidence-source /path/to/qstat-evidence.json

# 核对 JSON 预览后，再附加 --apply`;

const manualClose = `python -m fs_diloco.tools.request_terminal_close \\
  --shared-root /path/to/run \\
  --reason "operator requested close" \\
  --expected-descriptor-sha256 DESCRIPTOR_SHA256`;

const cleanup = `# 1. 只生成删除清单
python -m fs_diloco.tools.clean_run \\
  --run-root /path/to/completed/run \\
  --evidence /path/to/matching-pass-evidence.json

# 2. 人工核对清单后执行，并把结果写到报告目录
python -m fs_diloco.tools.clean_run \\
  --run-root /path/to/completed/run \\
  --evidence /path/to/matching-pass-evidence.json \\
  --execute \\
  --manifest /path/to/report/cleanup-manifest.json`;

export default function UserGuidePage() {
  return (
    <DocsPage
      eyebrow="OPERATE"
      title="User Guide"
      lede="本章面向运行实施人员：如何调整当前配置、选择启动拓扑、读取权威状态、处理受限恢复动作，并在证据保存后清理大对象。"
      toc={toc}
      previous={{ href: "/concepts", label: "Concepts" }}
      next={{ href: "/architecture", label: "Architecture" }}
    >
      <section id="configuration-workflow">
        <h2>配置工作流</h2>
        <ol className="steps">
          <li>
            <span>1</span>
            <div>
              <strong>从最接近目标的当前配置复制。</strong>
              <p>
                功能路径使用 <code>full_protocol_functional.yaml</code>；正式固定容量路径使用
                <code>full_protocol.yaml</code>；GPT-2 容量恢复实验使用
                <code>experiments/gpt2_wikitext2_8l_200x10.yaml</code>。
              </p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>只修改一个明确的实验维度。</strong>
              <p>
                模型、数据、训练预算、quorum、外层优化器和容量策略会共同改变运行语义。
                不要把不相关变更放进同一配置。
              </p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>在创建运行根目录前完成解析。</strong>
              <p>
                <code>load_config()</code> 验证纯 YAML；<code>resolve_config()</code>
                还会解析 run ID、shared root 和环境中的源码身份。
              </p>
            </div>
          </li>
          <li>
            <span>4</span>
            <div>
              <strong>把 resolved config 视为运行契约。</strong>
              <p>
                初始化后，Learner 和 Syncer 只能使用
                <code>control/run_config.resolved.yaml</code>。不要原地编辑运行配置。
              </p>
            </div>
          </li>
        </ol>
        <Callout title="严格字段集合" tone="note">
          <p>
            解析器拒绝未知顶层段和未知字段。<code>scaling.enabled=false</code> 使用固定容量；
            启用 scaling 时，所有 launch budget、调度器和 walltime 约束必须同时成立。
          </p>
        </Callout>
      </section>

      <section id="launch-topologies">
        <h2>启动拓扑</h2>
        <h3>独立 PBS actor</h3>
        <p>
          推荐入口是 <code>fs_diloco.tools.launch_independent_run</code>。它先提交一个
          Syncer job，再按 bootstrap slot 逐个提交 Learner scalar job。此入口不使用 job array。
        </p>
        <h3>单个多节点 allocation</h3>
        <p>
          <code>scripts/miyabi/agent/run_full_protocol.pbs</code> 预留一个多节点 allocation，
          随后由 <code>run_full_protocol_allocation.sh</code> 启动各 rank。
          该路径适合受控验证和 fault scenario，不替代独立 actor 的运行模型。
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>检查项</th>
                <th>提交前要求</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>PBS 语法</td>
                <td><code>bash -n scripts/miyabi/agent/*.pbs</code> 成功。</td>
              </tr>
              <tr>
                <td>账户</td>
                <td>每个 <code>#PBS -W group_list=...</code> 使用有效 literal group ID。</td>
              </tr>
              <tr>
                <td>队列</td>
                <td>Launcher 的 <code>--actor-queue</code> 与当前资源策略一致。</td>
              </tr>
              <tr>
                <td>Walltime</td>
                <td>根据工作量估计，至少 10 分钟，并保留启动与退出余量。</td>
              </tr>
              <tr>
                <td>日志</td>
                <td><code>--log-root</code> 是新的绝对目录，且有足够空间。</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section id="observe">
        <h2>观测运行</h2>
        <p>按证据价值从高到低读取状态，不要只根据进程退出码判断成功。</p>
        <div className="definition-list wide">
          <div>
            <code>control/summary.json</code>
            <p>终态摘要；只有终态流程完成后才应存在并与 authority 一致。</p>
          </div>
          <div>
            <code>control/latest.json</code>
            <p>方便读取的当前全局版本副本；权威提交仍在 SQLite。</p>
          </div>
          <div>
            <code>control/syncer_metadata.sqlite3</code>
            <p>Lease、成员、proposal、selection、version、token fate 与 terminal 的权威状态。</p>
          </div>
          <div>
            <code>metrics/&lt;actor&gt;/...</code>
            <p>按 actor attempt 写入的 JSONL 事件和运行环境 attestation。</p>
          </div>
          <div>
            <code>audit/</code>
            <p>从活跃表归档的有界历史和 command receipt。</p>
          </div>
          <div>
            <code>submission_receipt.json</code>
            <p>独立提交入口保存的已接受 PBS job ID 与部分失败边界。</p>
          </div>
        </div>
        <p>
          快速检查使用 <code>python -m fs_diloco.tools.analysis SHARED_ROOT</code>。
          需要硬断言时附加 <code>--expected-learners</code>、
          <code>--expected-global-steps</code> 和 <code>--require-terminal</code>。
        </p>
      </section>

      <section id="operator-actions">
        <h2>Operator 操作</h2>
        <h3>解决 scheduler uncertainty</h3>
        <p>
          先使用默认 dry-run 输出核对目标路径、action 和 expected state hash。
          只有 PBS 证据明确时才附加 <code>--apply</code>。
        </p>
        <CodeBlock label="Shell">{uncertainty}</CodeBlock>
        <p>
          Action 只有 <code>confirm_job_id</code>、<code>mark_failed</code>、
          <code>mark_expired</code> 和 <code>record_external_cancel_evidence</code>。
          <code>confirm_job_id</code> 必须提供 job ID；其他 action 不接受 job ID。
        </p>
        <h3>请求 manual close</h3>
        <p>
          仅当 resolved config 使用 <code>terminal.admission_close_policy: manual</code> 时执行。
        </p>
        <CodeBlock label="Shell">{manualClose}</CodeBlock>
      </section>

      <section id="recovery">
        <h2>故障与恢复</h2>
        <div className="scenario-list">
          <article>
            <span>SYNCER</span>
            <h3>Leader 退出或 lease 失效</h3>
            <p>
              新候选等待旧 lease 穿过安全边界后获取新 epoch。启动时恢复最后一个 committed
              outer state，并处置未完成 publication。旧 owner 的后续写入被 fence 拒绝。
            </p>
          </article>
          <article>
            <span>LEARNER CAPACITY</span>
            <h3>实例丢失并需要替换</h3>
            <p>
              Capacity service 先持久化观测和 launch request，再由调度器提交新实例。
              Replacement admission 必须绑定旧 instance、目标 stream 和 launch request。
            </p>
          </article>
          <article>
            <span>SCHEDULER</span>
            <h3>PBS 提交结果不确定</h3>
            <p>
              保存 <code>qstat</code> 或调度器证据。用 expected state hash 发布 operator request，
              由当前 leader 摄取。不要绕过 authority 直接改数据库。
            </p>
          </article>
          <article>
            <span>TERMINAL</span>
            <h3>进入 drain</h3>
            <p>
              Learner 停止创建新 cycle，发布最后的 terminal ack；Syncer 等待可见性 grace、
              执行受限 terminal merge、处置剩余 proposal，再发布 terminal control。
            </p>
          </article>
        </div>
        <Callout title="停止条件" tone="warning">
          <p>
            无法确认运行是否仍在排队、活跃或可恢复时，不要发布替换、取消或清理请求。
            先保存现场证据并停止操作，直到 owner、run ID、descriptor 与 PBS job 状态一致。
          </p>
        </Callout>
      </section>

      <section id="cleanup">
        <h2>完成后清理</h2>
        <p>
          清理工具只接受已证明完成、属于当前项目且拥有匹配无错误
          <code>PASS</code> 证据的运行。默认只输出 inventory，不删除文件。
        </p>
        <CodeBlock label="Shell">{cleanup}</CodeBlock>
        <p>执行前至少保存：</p>
        <ul>
          <li>精确命令、resolved config 与源码身份。</li>
          <li>run ID、PBS job ID、Checker 状态与摘要指标。</li>
          <li>失败时的完整错误证据，或成功时的最小代表性证据。</li>
          <li>删除范围、文件数、字节数、可恢复性和 manifest 路径。</li>
        </ul>
        <p>
          不要清理活跃、排队、可恢复或 ownership 不明确的运行；不要删除当前恢复所需的
          database、checkpoint、源码、配置或未处置失败证据。
        </p>
      </section>
    </DocsPage>
  );
}
