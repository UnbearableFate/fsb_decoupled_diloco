import type {
  ApiCallableDoc,
  ApiClassDoc,
  ApiFieldDoc,
  ApiModuleDoc,
} from "../reference-data/api";
import {
  adjacentApiModules,
  apiManifest,
  apiSourceUrl,
  findApiModule,
  moduleChildren,
} from "../reference-data/api";
import { DocsPage } from "./DocsPage";

function slugId(value: string): string {
  return value.replaceAll(/[^A-Za-z0-9_-]/g, "-").toLowerCase();
}

function InlineCodeText({ text }: { text: string }) {
  return text.split(/(`[^`]+`)/g).map((part, index) =>
    part.startsWith("`") && part.endsWith("`") ? (
      <code key={`${part}-${index}`}>{part.slice(1, -1)}</code>
    ) : (
      part
    ),
  );
}

function VisibilityTag({ value }: { value: string }) {
  const label =
    value === "public" ? "PUBLIC" : value === "dunder" ? "DUNDER" : "INTERNAL";
  return <span className={`api-visibility ${value}`}>{label}</span>;
}

function SourceDocstring({ value }: { value: string | null }) {
  return (
    <div className="source-docstring">
      <h5>Source docstring</h5>
      {value ? (
        <pre>{value}</pre>
      ) : (
        <p>源码未提供独立 docstring；职责说明由当前签名和实现结构生成。</p>
      )}
    </div>
  );
}

function FieldTable({
  fields,
  moduleDoc,
}: {
  fields: ApiFieldDoc[];
  moduleDoc: ApiModuleDoc;
}) {
  return (
    <div className="table-wrap api-member-table">
      <table>
        <thead>
          <tr>
            <th>成员</th>
            <th>类型</th>
            <th>默认值或表达式</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {fields.map((field) => (
            <tr key={`${field.kind}-${field.name}-${field.line}`}>
              <td>
                <a href={apiSourceUrl(moduleDoc, field.line)}>
                  <code>{field.name}</code>
                </a>
                <VisibilityTag value={field.visibility} />
              </td>
              <td><code>{field.type}</code></td>
              <td>{field.default === null ? "—" : <code>{field.default}</code>}</td>
              <td><InlineCodeText text={field.summary} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CallableReference({
  callable,
  moduleDoc,
  owner,
}: {
  callable: ApiCallableDoc;
  moduleDoc: ApiModuleDoc;
  owner?: string;
}) {
  const id = slugId(owner ? `${owner}-${callable.name}` : callable.name);
  return (
    <article className="api-callable" id={id}>
      <header>
        <div>
          <span className="api-kind">{callable.kind}</span>
          <VisibilityTag value={callable.visibility} />
        </div>
        <a href={apiSourceUrl(moduleDoc, callable.line)}>源码 L{callable.line} ↗</a>
      </header>
      <h4><code>{callable.qualifiedName}</code></h4>
      <pre className="api-signature"><code>{callable.signature}</code></pre>
      <p><InlineCodeText text={callable.summary} /></p>

      {callable.decorators.length > 0 ? (
        <div className="api-decorators">
          <strong>Decorators</strong>
          {callable.decorators.map((decorator) => (
            <code key={decorator}>@{decorator}</code>
          ))}
        </div>
      ) : null}

      <section className="api-detail-block">
        <h5>Parameters</h5>
        {callable.parameters.length > 0 ? (
          <dl className="api-parameter-list">
            {callable.parameters.map((parameter) => (
              <div key={`${parameter.kind}-${parameter.name}`}>
                <dt>
                  <code>{parameter.name}</code>
                  <span>{parameter.type}</span>
                  <small>{parameter.kind}</small>
                </dt>
                <dd>{parameter.description}</dd>
              </div>
            ))}
          </dl>
        ) : (
          <p>无显式参数。</p>
        )}
      </section>

      <section className="api-detail-block api-return-block">
        <h5>Returns</h5>
        <p><code>{callable.returns.type}</code> — {callable.returns.description}</p>
      </section>

      <section className="api-detail-block">
        <h5>Raises</h5>
        {callable.raises.length > 0 ? (
          <div className="api-raises">
            {callable.raises.map((errorName) => (
              <code key={errorName}>{errorName}</code>
            ))}
          </div>
        ) : (
          <p>当前函数体没有直接 <code>raise</code>。被调用函数仍可能抛出异常。</p>
        )}
      </section>

      <SourceDocstring value={callable.docstring} />
    </article>
  );
}

function ClassReference({
  classDoc,
  moduleDoc,
}: {
  classDoc: ApiClassDoc;
  moduleDoc: ApiModuleDoc;
}) {
  return (
    <article className="api-class" id={slugId(`class-${classDoc.name}`)}>
      <header className="api-class-header">
        <div>
          <span className="api-kind">class</span>
          <VisibilityTag value={classDoc.visibility} />
          <h3><code>{classDoc.name}</code></h3>
        </div>
        <a href={apiSourceUrl(moduleDoc, classDoc.line)}>源码 L{classDoc.line} ↗</a>
      </header>
      <pre className="api-signature"><code>{classDoc.signature}</code></pre>
      <p><InlineCodeText text={classDoc.summary} /></p>

      {classDoc.bases.length > 0 || classDoc.decorators.length > 0 ? (
        <div className="api-class-meta">
          {classDoc.bases.length > 0 ? (
            <div><strong>Bases</strong>{classDoc.bases.map((base) => <code key={base}>{base}</code>)}</div>
          ) : null}
          {classDoc.decorators.length > 0 ? (
            <div><strong>Decorators</strong>{classDoc.decorators.map((decorator) => <code key={decorator}>@{decorator}</code>)}</div>
          ) : null}
        </div>
      ) : null}

      <SourceDocstring value={classDoc.docstring} />

      {classDoc.fields.length > 0 ? (
        <section className="api-class-section">
          <h4>类成员与实例成员</h4>
          <FieldTable fields={classDoc.fields} moduleDoc={moduleDoc} />
        </section>
      ) : null}

      {classDoc.methods.length > 0 ? (
        <section className="api-class-section api-methods">
          <h4>方法与属性</h4>
          {classDoc.methods.map((method) => (
            <CallableReference
              callable={method}
              moduleDoc={moduleDoc}
              owner={classDoc.name}
              key={`${method.kind}-${method.name}-${method.line}`}
            />
          ))}
        </section>
      ) : null}
    </article>
  );
}

function ModuleLinks({
  title,
  moduleNames,
}: {
  title: string;
  moduleNames: string[];
}) {
  if (moduleNames.length === 0) return null;
  return (
    <div className="module-relation">
      <h3>{title}</h3>
      <div>
        {moduleNames.map((moduleName) => {
          const target = findApiModule(moduleName);
          return target ? (
            <a href={target.route} key={moduleName}><code>{moduleName}</code></a>
          ) : (
            <code key={moduleName}>{moduleName}</code>
          );
        })}
      </div>
    </div>
  );
}

export function ApiModuleReference({ moduleDoc }: { moduleDoc: ApiModuleDoc }) {
  const children = moduleChildren(moduleDoc.module);
  const adjacent = adjacentApiModules(moduleDoc.module);
  const toc = [
    { id: "module-overview", label: "模块概览" },
    ...(children.length > 0 ? [{ id: "package-children", label: "下级模块" }] : []),
    ...(moduleDoc.variables.length > 0 ? [{ id: "module-variables", label: "模块变量" }] : []),
    ...(moduleDoc.classes.length > 0 ? [{ id: "module-classes", label: "类" }] : []),
    ...(moduleDoc.functions.length > 0 ? [{ id: "module-functions", label: "函数" }] : []),
    { id: "module-relations", label: "模块关系" },
  ];
  const methodCount = moduleDoc.classes.reduce(
    (total, classDoc) => total + classDoc.methods.length,
    0,
  );
  const fieldCount = moduleDoc.classes.reduce(
    (total, classDoc) => total + classDoc.fields.length,
    0,
  );

  return (
    <DocsPage
      eyebrow={moduleDoc.isPackage ? "PYTHON PACKAGE" : "PYTHON MODULE"}
      title={moduleDoc.module}
      lede={moduleDoc.summary}
      toc={toc}
      previous={
        adjacent.previous
          ? { href: adjacent.previous.route, label: adjacent.previous.module }
          : { href: "/reference", label: "Reference" }
      }
      next={
        adjacent.next
          ? { href: adjacent.next.route, label: adjacent.next.module }
          : { href: "/experiments", label: "Experiments" }
      }
    >
      <section id="module-overview">
        <nav className="api-breadcrumb" aria-label="API 路径">
          <a href="/reference">Reference</a>
          {moduleDoc.module.split(".").map((part, index, parts) => {
            const name = parts.slice(0, index + 1).join(".");
            const target = findApiModule(name);
            return (
              <span key={name}>
                <b>/</b>
                {target ? <a href={target.route}>{part}</a> : <code>{part}</code>}
              </span>
            );
          })}
        </nav>
        <div className="api-module-meta">
          <VisibilityTag value={moduleDoc.visibility} />
          <span>{moduleDoc.isPackage ? "PACKAGE" : "MODULE"}</span>
          <span>{moduleDoc.lineCount} 行</span>
          <a href={apiSourceUrl(moduleDoc)}>查看完整源码 ↗</a>
        </div>
        <div className="api-module-stats">
          <div><strong>{moduleDoc.classes.length}</strong><span>类</span></div>
          <div><strong>{moduleDoc.functions.length}</strong><span>函数</span></div>
          <div><strong>{methodCount}</strong><span>方法与属性</span></div>
          <div><strong>{fieldCount + moduleDoc.variables.length}</strong><span>数据成员</span></div>
        </div>
        <div className="api-import-path">
          <span>IMPORT PATH</span>
          <code>{moduleDoc.module}</code>
        </div>
        <SourceDocstring value={moduleDoc.docstring} />
        <p className="api-source-identity">
          本页由当前 Python AST 生成，对应源码 revision <code>{apiManifest.sourceRevision.slice(0, 12)}</code>，
          文件 SHA-256 为 <code>{moduleDoc.sourceSha256}</code>。
        </p>
      </section>

      {children.length > 0 ? (
        <section id="package-children">
          <h2>下级模块</h2>
          <div className="api-module-grid">
            {children.map((child) => (
              <a href={child.route} key={child.module}>
                <span>{child.isPackage ? "PACKAGE" : "MODULE"}</span>
                <code>{child.module}</code>
                <p>{child.summary}</p>
              </a>
            ))}
          </div>
        </section>
      ) : null}

      {moduleDoc.variables.length > 0 ? (
        <section id="module-variables">
          <h2>模块变量与常量</h2>
          <p>包含模块体直接声明的常量、类型别名和运行期对象。</p>
          <FieldTable fields={moduleDoc.variables} moduleDoc={moduleDoc} />
        </section>
      ) : null}

      {moduleDoc.classes.length > 0 ? (
        <section id="module-classes">
          <h2>类</h2>
          <div className="api-class-list">
            {moduleDoc.classes.map((classDoc) => (
              <ClassReference
                classDoc={classDoc}
                moduleDoc={moduleDoc}
                key={`${classDoc.name}-${classDoc.line}`}
              />
            ))}
          </div>
        </section>
      ) : null}

      {moduleDoc.functions.length > 0 ? (
        <section id="module-functions">
          <h2>函数</h2>
          <div className="api-function-list">
            {moduleDoc.functions.map((callable) => (
              <CallableReference
                callable={callable}
                moduleDoc={moduleDoc}
                key={`${callable.name}-${callable.line}`}
              />
            ))}
          </div>
        </section>
      ) : null}

      <section id="module-relations">
        <h2>模块关系</h2>
        <p>
          下列关系来自当前源码中的直接和延迟 <code>import</code>。运行期动态调用仍以源码和
          Architecture 章节为准。
        </p>
        <div className="module-relations">
          <ModuleLinks title="依赖模块" moduleNames={moduleDoc.dependencies} />
          <ModuleLinks title="被以下模块导入" moduleNames={moduleDoc.usedBy} />
        </div>
        {moduleDoc.dependencies.length === 0 && moduleDoc.usedBy.length === 0 ? (
          <p>当前源码中没有解析到其他已记录模块的直接 import 关系。</p>
        ) : null}
      </section>
    </DocsPage>
  );
}
