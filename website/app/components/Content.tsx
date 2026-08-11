import type { ReactNode } from "react";

export function CodeBlock({
  label,
  children,
}: {
  label?: string;
  children: string;
}) {
  return (
    <div className="code-block">
      {label ? <div className="code-label">{label}</div> : null}
      <pre>
        <code>{children}</code>
      </pre>
    </div>
  );
}

export function Callout({
  title,
  tone = "note",
  children,
}: {
  title: string;
  tone?: "note" | "warning" | "success";
  children: ReactNode;
}) {
  return (
    <aside className={`callout ${tone}`}>
      <strong>{title}</strong>
      <div>{children}</div>
    </aside>
  );
}

export function StatStrip({
  items,
}: {
  items: Array<{ value: string; label: string }>;
}) {
  return (
    <div className="stat-strip">
      {items.map((item) => (
        <div key={item.label}>
          <strong>{item.value}</strong>
          <span>{item.label}</span>
        </div>
      ))}
    </div>
  );
}
