export function ArchitectureFlow() {
  return (
    <div className="architecture-flow" aria-label="FS-DiLoCo 训练数据流">
      <div className="flow-learners">
        <span className="flow-label">并行计算节点</span>
        {["Learner 000", "Learner 001", "Learner …"].map((label, index) => (
          <div className="flow-node" key={label}>
            <i>{String(index + 1).padStart(2, "0")}</i>
            <span>{label}</span>
            <small>local train · publish</small>
          </div>
        ))}
      </div>
      <div className="flow-arrow" aria-hidden="true">
        <span>immutable proposal</span>
        <b>→</b>
      </div>
      <div className="flow-store">
        <span className="flow-label">共享文件系统</span>
        <div className="store-stack">
          <div>
            <strong>OBJECTS</strong>
            <small>weights · updates · receipts</small>
          </div>
          <div>
            <strong>AUTHORITY</strong>
            <small>SQLite · lease · fence</small>
          </div>
          <div>
            <strong>CONTROL</strong>
            <small>latest · drain · terminal</small>
          </div>
        </div>
      </div>
      <div className="flow-arrow" aria-hidden="true">
        <span>selected batch</span>
        <b>→</b>
      </div>
      <div className="flow-syncer">
        <span className="flow-label">唯一写入者</span>
        <div className="syncer-core">
          <i>Σ</i>
          <strong>Syncer</strong>
          <small>select · merge · commit</small>
        </div>
        <div className="version-pill">global v → v + 1</div>
      </div>
    </div>
  );
}
