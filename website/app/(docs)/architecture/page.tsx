import type { Metadata } from "next";
import { ArchitectureFlow } from "../../components/ArchitectureFlow";
import { Callout, CodeBlock } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";
import { sourceUrl } from "../../site";

export const metadata: Metadata = {
  title: "Architecture",
  description: "FS-DiLoCo 的控制面、数据面、提交协议、恢复边界和终态流程。",
};

const toc = [
  { id: "high-level", label: "高层结构" },
  { id: "bootstrap", label: "Bootstrap" },
  { id: "learner-path", label: "Learner 路径" },
  { id: "syncer-path", label: "Syncer 路径" },
  { id: "commit-protocol", label: "版本提交协议" },
  { id: "high-availability", label: "高可用与恢复" },
  { id: "terminal", label: "终态收敛" },
  { id: "boundaries", label: "依赖边界" },
];

const commitProtocol = `select batch (SQLite transaction)
    ↓
compute weighted parameters + outer optimizer step
    ↓
prepare publication (paths, sizes, SHA-256, theta identity)
    ↓
publish immutable weight and optimizer objects
    ↓
commit merge (revalidate leader + contributor fences)
    ↓
publish epoch pointer and control/latest.json`;

export default function ArchitecturePage() {
  return (
    <DocsPage
      eyebrow="INTERNALS"
      title="Architecture"
      lede="FS-DiLoCo 把大对象发布与权威状态提交分开：文件系统负责不可变对象和可读控制面，SQLite 负责唯一事务决定。本章沿一次运行的真实调用路径展开。"
      toc={toc}
      previous={{ href: "/user-guide", label: "User Guide" }}
      next={{ href: "/reference", label: "Reference" }}
    >
      <section id="high-level">
        <h2>高层结构</h2>
        <ArchitectureFlow />
        <p>
          每个 Learner 与 Syncer 都是独立 actor。Actor 之间不通过 RPC 交换训练状态；
          它们只通过同一个运行根目录协作。SQLite 文件也位于共享文件系统中，
          但只有持有当前 leader token 的 Syncer 能打开写入 session。
        </p>
        <div className="layer-stack">
          <div>
            <span>ENTRYPOINTS</span>
            <p><code>fs_diloco.learner</code> · <code>fs_diloco.syncer</code> · operator tools</p>
          </div>
          <div>
            <span>RUNTIME SERVICES</span>
            <p>admission · merge · terminal · maintenance · dynamic capacity</p>
          </div>
          <div>
            <span>PROTOCOL</span>
            <p>proposal · selection · token accounting · scheduler · contributor fence</p>
          </div>
          <div>
            <span>STORAGE</span>
            <p>authority · immutable object store · control publication · run initializer</p>
          </div>
          <div>
            <span>MODELING</span>
            <p>Hugging Face model/data · parameter index · inner/outer optimizer</p>
          </div>
        </div>
      </section>

      <section id="bootstrap">
        <h2>Bootstrap</h2>
        <ol className="steps compact">
          <li>
            <span>1</span>
            <div><strong>解析配置。</strong><p><code>resolve_config()</code> 生成完整运行身份和绝对 shared root。</p></div>
          </li>
          <li>
            <span>2</span>
            <div><strong>绑定源码。</strong><p><code>capture_source_identity()</code> 记录 commit、dirty 状态和 source fingerprint。</p></div>
          </li>
          <li>
            <span>3</span>
            <div><strong>固化外部输入。</strong><p>模型、tokenizer 与数据 revision 解析为 immutable identity。</p></div>
          </li>
          <li>
            <span>4</span>
            <div><strong>构建版本 0。</strong><p>参数索引、全局权重、outer state、SQLite schema 和 artifact policy 写入 staging root。</p></div>
          </li>
          <li>
            <span>5</span>
            <div><strong>无替换发布。</strong><p>Initializer 通过 hard link、fsync、identity reservation 和最后的 <code>.complete</code> marker 发布运行。</p></div>
          </li>
        </ol>
        <p>
          Actor 只有在 <code>.complete</code> 是不可写常规文件、manifest 自哈希成立且运行 identity
          匹配时才把目录视为可见。
        </p>
      </section>

      <section id="learner-path">
        <h2>Learner 路径</h2>
        <div className="sequence">
          {[
            ["01", "Descriptor gate", "读取 immutable descriptor，核对 config 路径与期望源码身份。"],
            ["02", "Admission", "发布 static 或 dynamic request，等待带 contributor fence 的响应。"],
            ["03", "Runtime import", "再次验证 admission 后才导入 Torch、加载模型并写 attestation。"],
            ["04", "Local train", "从 cursor 构造确定性数据 shard，执行 inner optimizer step 并累计 token。"],
            ["05", "Publish cycle", "写入不可变 local parameter payload、proposal pointer 与 cycle receipt。"],
            ["06", "Ingestion barrier", "等待 receipt ack、drain 或 terminal，避免覆盖尚未摄取的 cycle。"],
          ].map(([index, title, detail]) => (
            <div key={index}>
              <span>{index}</span>
              <strong>{title}</strong>
              <p>{detail}</p>
            </div>
          ))}
        </div>
        <Callout title="Torch-free admission" tone="success">
          <p>
            Admission 入口在运行身份和成员资格成立前不导入 Torch。
            无效、过期或不属于 descriptor scope 的 actor 不会占用模型与 GPU 资源。
          </p>
        </Callout>
      </section>

      <section id="syncer-path">
        <h2>Syncer 路径</h2>
        <p>
          Syncer 候选先打开 strict authority，循环申请 leader lease。成功后立即发布 heartbeat，
          启动独立 lease renewer，并构造当前 epoch 的 <code>ControlPublisher</code>。
        </p>
        <p>主循环组合五类服务：</p>
        <div className="service-grid">
          <article><strong>Admission</strong><p>摄取 membership request，签发或拒绝 contributor fence。</p></article>
          <article><strong>Merge</strong><p>选择 quorum、读取 proposal、执行外层优化并提交版本。</p></article>
          <article><strong>Terminal</strong><p>判定 close、发布 drain、收集 ack、执行受限尾部合并。</p></article>
          <article><strong>Maintenance</strong><p>归档已处置行，清理孤儿 publication，并保持活跃扫描有界。</p></article>
          <article><strong>Dynamic capacity</strong><p>记录容量窗口、创建 launch reservation，并与 PBS 状态对账。</p></article>
        </div>
      </section>

      <section id="commit-protocol">
        <h2>版本提交协议</h2>
        <CodeBlock label="MergeService.merge_once()">{commitProtocol}</CodeBlock>
        <p>
          <code>prepare_publication</code> 先在 authority 中绑定目标版本、selection batch、
          对象路径、大小、payload SHA-256 和 theta SHA-256。对象写入成功后，
          <code>commit_merge</code> 在事务内重新验证 leader lease、selection 与 contributor fence。
        </p>
        <p>
          SQLite commit 是版本成为权威事实的时刻。随后发布的 JSON pointer 用于高效读取；
          如果进程在两者之间退出，接管 Syncer 可从 committed row 重建控制发布。
        </p>
        <p className="source-note">
          查看源码：
          <a href={sourceUrl("fs_diloco/runtime/services/merge.py", 59)}>MergeService.merge_once()</a>
          <span>·</span>
          <a href={sourceUrl("fs_diloco/storage/control.py", 71)}>ControlPublisher.publish_latest()</a>
        </p>
      </section>

      <section id="high-availability">
        <h2>高可用与恢复</h2>
        <div className="invariant-grid">
          <article>
            <span>INV-01</span>
            <h3>单一 Leader</h3>
            <p>一个 run 同时只有一份有效 lease；安全边界考虑配置的最大时钟偏差。</p>
          </article>
          <article>
            <span>INV-02</span>
            <h3>Epoch 隔离</h3>
            <p>Heartbeat、latest、drain 与 terminal 都位于 epoch + owner 命名空间。</p>
          </article>
          <article>
            <span>INV-03</span>
            <h3>可幂等重放</h3>
            <p>Authority command ID 绑定 immutable request；不同内容复用 ID 会触发冲突。</p>
          </article>
          <article>
            <span>INV-04</span>
            <h3>恢复自提交点</h3>
            <p>接管从 latest committed version 和 contributor progress 恢复，不猜测临时文件。</p>
          </article>
        </div>
        <p>
          准备完成但未提交的 publication 不等同于版本。接管路径通过 authority 行和对象身份
          判定继续、清理或隔离；不会把仅存在于文件系统的权重对象提升为全局版本。
        </p>
      </section>

      <section id="terminal">
        <h2>终态收敛</h2>
        <div className="terminal-timeline">
          {[
            ["CLOSE", "关闭新 admission", "由 global target、launch budget、deadline 或 manual request 触发。"],
            ["DRAIN", "发布 drain generation", "Learner 停止创建新 cycle，并报告最后 receipt/update。"],
            ["WAIT", "等待可见性窗口", "允许已经发布但尚未摄取的 registration、receipt 与 proposal 进入 authority。"],
            ["MERGE", "执行受限尾部合并", "最多执行 terminal.max_terminal_merges；可使用低于正常 quorum 的剩余合法集合。"],
            ["FINALIZE", "处置剩余状态", "每份 proposal 和 processed token 获得 durable fate，随后发布 terminal 与 summary。"],
          ].map(([tag, title, detail]) => (
            <div key={tag}>
              <span>{tag}</span>
              <section><strong>{title}</strong><p>{detail}</p></section>
            </div>
          ))}
        </div>
      </section>

      <section id="boundaries">
        <h2>依赖边界</h2>
        <ul className="boundary-list">
          <li><code>protocol/</code> 不依赖 Torch 或文件系统，保存纯协议值对象与算法。</li>
          <li><code>storage/</code> 实现 authority、不可变发布和目录契约，不执行模型训练。</li>
          <li><code>runtime/</code> 组合协议、存储和 modeling，并承担 actor 生命周期。</li>
          <li><code>modeling/</code> 封装 Hugging Face 输入、参数编解码和优化器数学。</li>
          <li>Operator 工具不绕过 leader：需要改变运行状态时，只发布受限不可变请求。</li>
        </ul>
        <Callout title="架构来源" tone="note">
          <p>
            本章的角色与调用路径通过 CodeGraph 从当前源码提取，并用 entrypoint、runtime service、
            authority、control publisher 和 architecture tests 交叉核对。
          </p>
        </Callout>
      </section>
    </DocsPage>
  );
}
