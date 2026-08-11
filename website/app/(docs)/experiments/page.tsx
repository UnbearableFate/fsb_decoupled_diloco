import type { Metadata } from "next";
import { Callout } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";

export const metadata: Metadata = {
  title: "Experiments",
  description: "为 FS-DiLoCo 的实验协议、结果和证据预留的文档章节。",
};

const toc = [
  { id: "reserved", label: "预留状态" },
  { id: "required-evidence", label: "发布所需证据" },
  { id: "slots", label: "实验页面空位" },
];

export default function ExperimentsPage() {
  return (
    <DocsPage
      eyebrow="RESERVED"
      title="Experiments"
      lede="本章已加入信息架构，但当前不发布实验结果。空位用于后续固定协议、运行身份、指标定义和结构化证据，避免把实现事实误写成经验结论。"
      toc={toc}
      previous={{ href: "/reference", label: "Reference" }}
    >
      <section id="reserved">
        <div className="reserved-hero">
          <span>EXPERIMENTS / 00</span>
          <h2>结果区已预留</h2>
          <p>
            这里将承载可复现的对照实验、消融、系统性能与故障恢复评估。
            在证据完整前，不填入数值、排名或方法优劣结论。
          </p>
          <div aria-hidden="true">
            <i />
            <i />
            <i />
            <i />
          </div>
        </div>
      </section>

      <section id="required-evidence">
        <h2>发布所需证据</h2>
        <p>每个实验条目至少需要以下信息，缺失项标记「待确认」，不自行补全。</p>
        <div className="evidence-grid">
          {[
            ["IDENTITY", "源码 commit、source fingerprint、run ID 与 descriptor SHA-256。"],
            ["PROTOCOL", "模型、数据 revision、seed、resolved config 与停止条件。"],
            ["RESOURCES", "PBS job ID、队列、节点/GPU、walltime 与运行环境 attestation。"],
            ["METRICS", "指标定义、单位、聚合方式、样本数和不确定性表达。"],
            ["CHECKER", "结构化状态、所有错误、requirement 绑定与完整 evidence path。"],
            ["COMPARABILITY", "任务、模型、数据、预算和统计协议是否可直接比较。"],
          ].map(([tag, detail]) => (
            <article key={tag}>
              <span>{tag}</span>
              <p>{detail}</p>
            </article>
          ))}
        </div>
        <Callout title="比较边界" tone="warning">
          <p>
            模型、任务、指标、seed 或预算不一致时，只能说明各自实验观察，
            不能据此宣布一种方法普遍优于另一种方法。
          </p>
        </Callout>
      </section>

      <section id="slots">
        <h2>实验页面空位</h2>
        <div className="empty-slots">
          <article><span>01</span><h3>Protocol baseline</h3><p>待补充</p></article>
          <article><span>02</span><h3>Membership study</h3><p>待补充</p></article>
          <article><span>03</span><h3>Fault recovery</h3><p>待补充</p></article>
          <article><span>04</span><h3>System profile</h3><p>待补充</p></article>
          <article><span>05</span><h3>Quality analysis</h3><p>待补充</p></article>
          <article><span>06</span><h3>Ablations</h3><p>待补充</p></article>
        </div>
      </section>
    </DocsPage>
  );
}
