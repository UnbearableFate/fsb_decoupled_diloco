import type { ReactNode } from "react";

export type TocItem = {
  id: string;
  label: string;
};

type PagerLink = {
  href: string;
  label: string;
};

export function DocsPage({
  eyebrow,
  title,
  lede,
  toc,
  previous,
  next,
  children,
}: {
  eyebrow: string;
  title: string;
  lede: string;
  toc: TocItem[];
  previous?: PagerLink;
  next?: PagerLink;
  children: ReactNode;
}) {
  return (
    <>
      <main className="docs-main">
        <header className="article-header">
          <span className="eyebrow">{eyebrow}</span>
          <h1>{title}</h1>
          <p>{lede}</p>
        </header>
        <article className="prose">{children}</article>
        <nav className="article-pager" aria-label="相邻章节">
          {previous ? (
            <a href={previous.href}>
              <small>上一章</small>
              <span>← {previous.label}</span>
            </a>
          ) : (
            <span />
          )}
          {next ? (
            <a href={next.href} className="next">
              <small>下一章</small>
              <span>{next.label} →</span>
            </a>
          ) : null}
        </nav>
      </main>
      <aside className="toc" aria-label="本页目录">
        <p>本页内容</p>
        {toc.map((item) => (
          <a key={item.id} href={`#${item.id}`}>
            {item.label}
          </a>
        ))}
        <div className="toc-rule" />
        <span>内容与当前源码同步</span>
      </aside>
    </>
  );
}
