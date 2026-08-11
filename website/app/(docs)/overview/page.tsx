import type { Metadata } from "next";
import { ArchitectureFlow } from "../../components/ArchitectureFlow";
import { Callout, StatStrip } from "../../components/Content";
import { DocsPage } from "../../components/DocsPage";
import { sourceUrl } from "../../site";

export const metadata: Metadata = {
  title: "Overview",
  description: "了解 FS-DiLoCo 的定位、范围、系统角色与文档阅读路径。",
};

const toc = [
  { id: "what-it-is", label: "项目是什么" },
  { id: "supported-scope", label: "当前支持范围" },
  { id: "system-roles", label: "系统角色" },
  { id: "choose-path", label: "选择阅读路径" },
  { id: "documentation-boundary", label: "文档边界" },
];

export default function OverviewPage() {
  return (
    <DocsPage
      eyebrow="START HERE"
      title="Overview"
      lede="FS-DiLoCo 是一个面向 Miyabi 共享文件系统的 Decoupled DiLoCo 研究原型。本章先界定系统做什么、当前实现支持什么，以及应当从哪一章继续阅读。"
      toc={toc}
      next={{ href: "/getting-started", label: "Getting Started" }}
    >
      <section id="what-it-is">
        <h2>项目是什么</h2>
        <p>
          仓库只有一种可运行协议：<strong>Full Protocol</strong>。多个 Learner
          各自执行本地训练，并把完整参数 proposal 发布为不可变对象。一个当前 Syncer
          获得带 fence 的 leader lease 后，负责摄取 proposal、选择批次、执行外层优化、
          发布全局版本、记录 token 去向并完成终态收敛。
        </p>
        <p>
          系统不依赖常驻协调服务。共享文件系统同时承载不可变张量对象、控制发布、
          审计记录和 SQLite 权威状态。这一边界让训练 actor 可以通过独立 PBS 作业运行，
          同时把所有提交决定集中到唯一的事务写入者。
        </p>
        <StatStrip
          items={[
            { value: "Full", label: "唯一运行协议" },
            { value: "static / dynamic", label: "成员模式" },
            { value: "SQLite + files", label: "共享状态载体" },
          ]}
        />
        <ArchitectureFlow />
      </section>

      <section id="supported-scope">
        <h2>当前支持范围</h2>
        <div className="feature-grid">
          <article>
            <span>01</span>
            <h3>严格运行身份</h3>
            <p>
              初始化时固化配置、源码指纹、模型、tokenizer、数据集与运行描述符。
              Actor 必须加载相同的 resolved config。
            </p>
          </article>
          <article>
            <span>02</span>
            <h3>两种 Membership</h3>
            <p>
              <code>static</code> 使用固定 Learner 身份；<code>dynamic</code> 使用固定 stream
              pool，并持久化 admission、容量观测和 PBS launch reservation。
            </p>
          </article>
          <article>
            <span>03</span>
            <h3>可恢复唯一写入</h3>
            <p>
              Syncer 候选竞争 leader lease。epoch、owner 与 contributor fence
              阻止过期进程继续提交权威状态。
            </p>
          </article>
          <article>
            <span>04</span>
            <h3>可审计处理量</h3>
            <p>
              Cycle receipt 与 authority 行记录 proposal、有效 token、本地丢弃 token、
              提交版本和终态处置。
            </p>
          </article>
        </div>
        <Callout title="当前版本策略" tone="note">
          <p>
            配置必须使用当前 <code>config_schema_version</code>。项目当前阶段不提供旧配置、
            旧协议或旧运行目录的兼容层。
          </p>
        </Callout>
      </section>

      <section id="system-roles">
        <h2>系统角色</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>角色</th>
                <th>主要职责</th>
                <th>允许写入的状态</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><strong>Initializer</strong></td>
                <td>解析并验证配置，绑定源码身份，创建全局版本 0。</td>
                <td>新的运行根目录及其不可变 bootstrap 内容。</td>
              </tr>
              <tr>
                <td><strong>Learner</strong></td>
                <td>申请 admission、本地训练、发布参数 proposal 与 cycle receipt。</td>
                <td>自己的请求、不可变 payload、pointer、receipt、ack 与遥测。</td>
              </tr>
              <tr>
                <td><strong>Syncer</strong></td>
                <td>获取 leader lease，摄取、选择、合并、提交、维护和终态化。</td>
                <td>SQLite authority、版本对象与当前 epoch 的控制发布。</td>
              </tr>
              <tr>
                <td><strong>Checker / operator</strong></td>
                <td>读取证据、发布受限操作请求或在完成后执行安全清理。</td>
                <td>独立证据文件或指定的不可变 operator request。</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p className="source-note">
          查看源码：
          <a href={sourceUrl("fs_diloco/runtime/learner_entrypoint.py", 39)}>Learner admission</a>
          <span>·</span>
          <a href={sourceUrl("fs_diloco/runtime/syncer_entrypoint.py", 146)}>Syncer entrypoint</a>
          <span>·</span>
          <a href={sourceUrl("fs_diloco/tools/init_run.py", 268)}>Run initialization</a>
        </p>
      </section>

      <section id="choose-path">
        <h2>选择阅读路径</h2>
        <div className="link-panels">
          <a href="/getting-started">
            <small>首次运行</small>
            <strong>安装、初始化并验证一个运行</strong>
            <span>Getting Started →</span>
          </a>
          <a href="/concepts">
            <small>理解协议</small>
            <strong>掌握 proposal、fence、merge 与 token 语义</strong>
            <span>Concepts →</span>
          </a>
          <a href="/user-guide">
            <small>日常操作</small>
            <strong>配置 PBS 运行、恢复、观测与清理</strong>
            <span>User Guide →</span>
          </a>
          <a href="/reference">
            <small>查精确信息</small>
            <strong>查询 CLI 参数、配置段、API 与文件路径</strong>
            <span>Reference →</span>
          </a>
        </div>
      </section>

      <section id="documentation-boundary">
        <h2>文档边界</h2>
        <p>
          本站描述当前仓库中的代码、配置和运行契约，不把计划文档当作已经实现的功能。
          性能、收敛质量和方法比较需要固定模型、数据、seed、资源、预算与 Checker 证据，
          因此不会从架构事实推导实验结论。
        </p>
        <Callout title="Experiments 已预留" tone="warning">
          <p>
            <a href="/experiments">Experiments</a> 当前只保留结构化空位。
            后续实验必须附运行身份、配置、PBS 作业、指标定义和证据路径后再发布。
          </p>
        </Callout>
      </section>
    </DocsPage>
  );
}
