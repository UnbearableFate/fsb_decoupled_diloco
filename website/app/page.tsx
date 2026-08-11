import type { Metadata } from "next";
import { ArchitectureFlow } from "./components/ArchitectureFlow";
import { Brand } from "./components/Brand";
import { CodeBlock } from "./components/Content";
import { SiteHeader } from "./components/SiteHeader";
import { allSections, repositoryUrl } from "./site";

export const metadata: Metadata = {
  title: "Filesystem Decoupled DiLoCo",
  description: "面向 Miyabi 共享文件系统的 Decoupled DiLoCo 原型文档。",
};

const quickstart = `python -m fs_diloco.tools.launch_independent_run \\
  --config configs/full_protocol.yaml \\
  --run-id example \\
  --shared-root /path/to/runs/example \\
  --project-root "$PWD"`;

export default function Home() {
  return (
    <div className="landing">
      <SiteHeader />
      <main>
        <section className="hero">
          <div className="hero-grid" aria-hidden="true" />
          <div className="hero-copy">
            <div className="status-pill">
              <span /> 研究原型 · Full Protocol
            </div>
            <h1>
              用共享文件系统协调
              <br />
              <span>解耦式分布训练</span>
            </h1>
            <p>
              FS-DiLoCo 面向 Miyabi 构建。Learner 独立训练并发布不可变参数，
              Syncer 通过带 fence 的唯一写权限完成选择、合并、记账与终态收敛。
            </p>
            <div className="hero-actions">
              <a className="button primary" href="/getting-started">
                开始使用 <span aria-hidden="true">→</span>
              </a>
              <a className="button secondary" href="/architecture">
                查看架构
              </a>
            </div>
            <div className="hero-metrics" aria-label="项目能力摘要">
              <div>
                <strong>1</strong>
                <span>可运行协议</span>
              </div>
              <div>
                <strong>1</strong>
                <span>成员协议</span>
              </div>
              <div>
                <strong>1</strong>
                <span>权威事务存储</span>
              </div>
            </div>
          </div>
          <div className="hero-terminal">
            <div className="terminal-bar">
              <span>launch_independent_run</span>
              <div aria-hidden="true">
                <i />
                <i />
                <i />
              </div>
            </div>
            <CodeBlock label="初始化并预览 PBS 提交命令">{quickstart}</CodeBlock>
            <div className="terminal-result">
              <span className="result-dot" />
              <div>
                <strong>Run descriptor sealed</strong>
                <small>source · config · model · dataset identity</small>
              </div>
            </div>
            <div className="terminal-lines" aria-hidden="true">
              <span style={{ width: "88%" }} />
              <span style={{ width: "66%" }} />
              <span style={{ width: "74%" }} />
            </div>
          </div>
        </section>

        <section className="landing-section path-section">
          <div className="section-heading">
            <span>CHOOSE A PATH</span>
            <h2>从当前任务进入文档</h2>
            <p>信息架构按学习路径、核心概念、操作指南和参考手册分层。</p>
          </div>
          <div className="path-grid">
            {allSections.slice(0, 6).map((item, index) => (
              <a href={item.href} key={item.href}>
                <span className="path-index">0{index + 1}</span>
                <h3>{item.label}</h3>
                <p>{item.description}</p>
                <small>打开章节 →</small>
              </a>
            ))}
          </div>
        </section>

        <section className="landing-section flow-section">
          <div className="section-heading split">
            <div>
              <span>ONE SHARED STATE</span>
              <h2>计算解耦，状态可审计</h2>
            </div>
            <p>
              大对象写入不可变文件；SQLite 保存权威事务状态；控制发布让 Learner
              无需直接写数据库即可跟随全局版本。
            </p>
          </div>
          <ArchitectureFlow />
          <div className="principle-row">
            <div>
              <span>01</span>
              <strong>先 admission，后 Torch</strong>
              <p>运行描述符与成员资格通过门禁后，进程才加载模型与 GPU 运行时。</p>
            </div>
            <div>
              <span>02</span>
              <strong>唯一事务写入者</strong>
              <p>Syncer 依赖 leader lease、epoch 与 owner fence 保护权威写操作。</p>
            </div>
            <div>
              <span>03</span>
              <strong>每份处理量有归宿</strong>
              <p>Cycle receipt 将有效、丢弃和保留的 token 关系写入持久状态。</p>
            </div>
          </div>
        </section>

        <section className="landing-section reference-cta">
          <div>
            <span className="eyebrow">SOURCE-GROUNDED REFERENCE</span>
            <h2>从教程切换到精确接口</h2>
            <p>
              Reference 按签名、参数、返回值、异常和说明组织公共 Python API，
              同时列出 CLI、配置段和运行目录契约。
            </p>
          </div>
          <div className="reference-code">
            <code>
              <span>def</span> resolve_config(
              <br />
              &nbsp;&nbsp;path: str | Path,
              <br />
              &nbsp;&nbsp;*, run_id: str | None = <b>None</b>,
              <br />
              &nbsp;&nbsp;shared_root: str | None = <b>None</b>,
              <br />
              ) -&gt; Config
            </code>
            <a href="/reference">浏览 Reference →</a>
          </div>
        </section>
      </main>
      <footer className="landing-footer">
        <Brand />
        <p>面向 Miyabi 共享文件系统的 Decoupled DiLoCo 研究原型。</p>
        <div>
          <a href="/overview">文档</a>
          <a href={repositoryUrl} target="_blank" rel="noreferrer">
            GitHub ↗
          </a>
        </div>
      </footer>
    </div>
  );
}
