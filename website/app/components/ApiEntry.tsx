import type { ReactNode } from "react";

type ApiParameter = {
  name: string;
  type: string;
  description: ReactNode;
};

export function ApiEntry({
  id,
  signature,
  summary,
  parameters,
  returns,
  raises,
  note,
  source,
}: {
  id: string;
  signature: string;
  summary: ReactNode;
  parameters: ApiParameter[];
  returns: ReactNode;
  raises?: Array<{ name: string; description: ReactNode }>;
  note?: ReactNode;
  source: string;
}) {
  return (
    <article className="api-entry" id={id}>
      <header>
        <code>{signature}</code>
        <a href={source}>源码 ↗</a>
      </header>
      <div className="api-body">
        <p className="api-summary">{summary}</p>
        <section>
          <h4>Parameters</h4>
          {parameters.length ? (
            <dl>
              {parameters.map((parameter) => (
                <div key={parameter.name}>
                  <dt>
                    <code>{parameter.name}</code> <span>({parameter.type})</span>
                  </dt>
                  <dd>{parameter.description}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p>无。</p>
          )}
        </section>
        <section>
          <h4>Returns</h4>
          <p>{returns}</p>
        </section>
        {raises?.length ? (
          <section>
            <h4>Raises</h4>
            <dl>
              {raises.map((item) => (
                <div key={item.name}>
                  <dt><code>{item.name}</code></dt>
                  <dd>{item.description}</dd>
                </div>
              ))}
            </dl>
          </section>
        ) : null}
        {note ? (
          <aside>
            <strong>Note</strong>
            <div>{note}</div>
          </aside>
        ) : null}
      </div>
    </article>
  );
}
