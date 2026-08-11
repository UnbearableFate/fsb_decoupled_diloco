import type { ReactNode } from "react";
import { SiteHeader } from "./SiteHeader";
import { SideNav } from "./SideNav";

export function DocsShell({ children }: { children: ReactNode }) {
  return (
    <div className="docs-root">
      <SiteHeader />
      <div className="mobile-docs-nav">
        <details>
          <summary>浏览全部章节</summary>
          <SideNav />
        </details>
      </div>
      <div className="docs-grid">
        <aside className="docs-sidebar">
          <SideNav />
        </aside>
        {children}
      </div>
    </div>
  );
}
