import type { Metadata } from "next";
import { Callout, CodeBlock } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";
import { sourceUrl } from "../../site";

export const metadata: Metadata = {
  title: "Concepts",
  description: "理解 Full Protocol、proposal、membership、fence、merge 与 token 记账。",
};

const toc = [
  { id: "full-protocol", label: "Full Protocol" },
  { id: "planes", label: "控制面与数据面" },
  { id: "membership", label: "Membership 与 admission" },
  { id: "fencing", label: "Lease 与 fence" },
  { id: "proposal-merge", label: "Proposal 与合并" },
  { id: "accounting", label: "Token 记账" },
  { id: "adoption", label: "全局版本采纳" },
];

const weightFormula = `staleness_i = max(0, current_version - base_version_i)
raw_weight_i = tokens_i / (1 + λ × staleness_i)
weight_i = raw_weight_i / Σ raw_weight_j

p̄ = Σ weight_i × local_params_i
pseudo_gradient = θ_current - p̄
θ_next = outer_optimizer(θ_current, pseudo_gradient)`;

export default function ConceptsPage() {
  return (
    <DocsPage
      eyebrow="CORE MODEL"
      title="Concepts"
      lede="本章给出理解运行行为所需的最小概念集合。概念名称沿用源码中的稳定术语，避免在同一对象上使用多套表达。"
      toc={toc}
      previous={{ href: "/getting-started", label: "Getting Started" }}
      next={{ href: "/user-guide", label: "User Guide" }}
    >
      <section id="full-protocol">
        <h2>Full Protocol</h2>
        <p>
          Full Protocol 的 proposal payload 保存 Learner 当前的完整扁平参数向量，
          不是梯度分片或增量文件。Syncer 选择多个 proposal 后，对参数向量做加权平均，
          再把当前全局参数与该平均值之差交给外层优化器。
        </p>
        <p>
          每轮提交同时产生新的全局权重对象和 outer optimizer state。
          两者在 SQLite authority 中以同一个 publication identity 提交，
          再通过 <code>latest</code> 控制发布向 Learner 可见。
        </p>
        <Callout title="唯一运行协议" tone="note">
          <p>
            仓库中的当前运行入口只支持 Full Protocol。不要从历史计划或旧术语推断还存在
            fragment、legacy 或兼容模式。
          </p>
        </Callout>
      </section>

      <section id="planes">
        <h2>控制面与数据面</h2>
        <div className="concept-pair">
          <article>
            <span>CONTROL PLANE</span>
            <h3>权威状态与可见性</h3>
            <p>
              <code>control/syncer_metadata.sqlite3</code> 保存 lease、成员、proposal 状态、
              selection batch、committed version、token fate 和 terminal 状态。
            </p>
            <p>
              epoch-scoped JSON 发布对 Learner 暴露 heartbeat、latest、drain 和 terminal，
              但不取代 SQLite 提交点。
            </p>
          </article>
          <article>
            <span>DATA PLANE</span>
            <h3>不可变张量与证据</h3>
            <p>
              <code>weights/</code>、<code>optim/</code> 和 <code>updates/payloads/</code>
              保存 Safetensors 或编码后的 outer state。路径、大小和 SHA-256 绑定到 authority row。
            </p>
            <p>
              receipt、attestation 和 audit batch 保存可独立复核的运行事实。
            </p>
          </article>
        </div>
      </section>

      <section id="membership">
        <h2>Membership 与 admission</h2>
        <p>
          Learner 在导入 Torch 和加载模型之前，必须完成运行描述符验证和 admission。
          Admission 返回 contributor fence、稳定 contributor key 与恢复 cursor。
        </p>
        <div className="comparison-grid">
          <article>
            <h3><code>static</code></h3>
            <ul>
              <li>身份集合由 <code>sync.num_learners</code> 固定。</li>
              <li>Learner ID 形如 <code>learner_000</code>。</li>
              <li>替换现有绑定需要精确匹配旧 fence 的 operator authorization。</li>
              <li>配置中不得声明 inert <code>scaling</code> 段。</li>
            </ul>
          </article>
          <article>
            <h3><code>dynamic</code></h3>
            <ul>
              <li>逻辑并行度由固定 <code>stream_pool_size</code> 定义。</li>
              <li>实例 ID 每次 admission 都是新的 UUID。</li>
              <li>Leader 持久化容量观测、launch request 和 scheduler reconciliation。</li>
              <li>启动实例或 replacement 必须绑定 bootstrap slot 或 launch reservation。</li>
            </ul>
          </article>
        </div>
      </section>

      <section id="fencing">
        <h2>Lease 与 fence</h2>
        <p>
          Leader lease 决定当前唯一 Syncer。Lease token 包含 run、epoch 和 owner；
          authority 在每个受保护写操作前验证 token 尚未被替代，也没有越过考虑时钟偏差后的安全边界。
        </p>
        <p>
          Contributor fence 保护 Learner 身份。Static fence 绑定 learner、logical launch、attempt
          和 generation；dynamic fence 绑定 instance、stream 与 generation。
          Selection 后、commit 前再次检查 fence，从而拒绝已经失效的 proposal。
        </p>
        <Callout title="Fence conflict 不会提交半个版本" tone="success">
          <p>
            如果 commit 前发现成员 fence 已变化，MergeService 记录冲突并重新加载最后一个已提交
            outer state，不发布该次候选版本。
          </p>
        </Callout>
      </section>

      <section id="proposal-merge">
        <h2>Proposal 与合并</h2>
        <p>
          一个可选择 proposal 至少带有唯一 <code>update_id</code>、正整数 direct token、
          不晚于当前版本的 <code>base_global_version</code>、payload 路径与内容身份。
          Batch 选择满足配置的 quorum，并确保 contributor 不重复。
        </p>
        <CodeBlock label="当前合并权重与外层更新">{weightFormula}</CodeBlock>
        <p>
          <code>λ</code> 对应 <code>sync.staleness_lambda</code>。当 <code>λ = 0</code> 时，
          权重只与 direct token 成正比。当前外层优化器支持 <code>sgd</code>、
          <code>momentum</code>、<code>nesterov</code> 和 <code>adamw</code>。
        </p>
        <p className="source-note">
          查看源码：
          <a href={sourceUrl("fs_diloco/protocol/merge.py", 9)}>weighting</a>
          <span>·</span>
          <a href={sourceUrl("fs_diloco/runtime/services/merge.py", 59)}>merge_once()</a>
          <span>·</span>
          <a href={sourceUrl("fs_diloco/modeling/outer_optim.py", 23)}>outer_optimizer_step()</a>
        </p>
      </section>

      <section id="accounting">
        <h2>Token 记账</h2>
        <p>
          Learner 使用 <code>TrainingSegmentAccumulator</code> 记录一个 upload cycle。
          每个完成的本地 step 必须包含正数 token 和 example，step 序号必须连续，loss 必须有限。
        </p>
        <div className="definition-list">
          <div>
            <code>processed_tokens</code>
            <p>本 cycle 实际完成训练的全部 token。</p>
          </div>
          <div>
            <code>effective_tokens</code>
            <p>仍属于最终 proposal 有效 segment 的 token。</p>
          </div>
          <div>
            <code>local_discarded_tokens</code>
            <p>因 destructive base replacement 而没有进入 proposal 的 token。</p>
          </div>
          <div>
            <code>retained_tokens_since_base</code>
            <p>重基或预测路径保留的有效 ancestry 上界。</p>
          </div>
        </div>
        <p>
          永久约束是 <code>processed_tokens = effective_tokens + local_discarded_tokens</code>。
          当 <code>effective_tokens = 0</code> 时，cycle 只能发布 receipt，不能承诺 proposal。
        </p>
      </section>

      <section id="adoption">
        <h2>全局版本采纳</h2>
        <p>
          Learner 通过 <code>learner.global_adoption_strategy</code> 选择如何处理更新的全局版本。
          当前值只有以下三种：
        </p>
        <div className="definition-list wide">
          <div>
            <code>replace</code>
            <p>加载新全局权重；按策略要求重建 inner optimizer，并重新开始有效 segment。</p>
          </div>
          <div>
            <code>rebase_post_publish_delta</code>
            <p>发布后把已训练的局部变化重基到更新的全局参数上，保留可解释的有效工作。</p>
          </div>
          <div>
            <code>predict_post_publish_global</code>
            <p>利用当前 outer momentum 与本地变化构造预测全局点，随后与真实提交版本对账。</p>
          </div>
        </div>
        <Callout title="配置组合会被整体校验" tone="note">
          <p>
            策略名称只是入口。轮询时机、发布后等待、outer optimizer 和 prediction timeout
            还必须满足各策略的跨字段约束。不要通过绕过 <code>resolve_config()</code>
            直接构造未验证运行配置。
          </p>
        </Callout>
      </section>
    </DocsPage>
  );
}
