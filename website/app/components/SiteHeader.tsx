import { Brand } from "./Brand";
import { Search } from "./Search";
import { repositoryUrl } from "../site";

const topLinks = [
  ["文档", "/overview"],
  ["架构", "/architecture"],
  ["Reference", "/reference"],
  ["Experiments", "/experiments"],
] as const;

export function SiteHeader() {
  return (
    <header className="site-header">
      <div className="header-inner">
        <Brand />
        <nav className="top-nav" aria-label="主导航">
          {topLinks.map(([label, href]) => (
            <a key={href} href={href}>
              {label}
            </a>
          ))}
        </nav>
        <div className="header-tools">
          <Search />
          <a
            className="github-link"
            href={repositoryUrl}
            target="_blank"
            rel="noreferrer"
          >
            GitHub <span aria-hidden="true">↗</span>
          </a>
        </div>
        <details className="mobile-menu">
          <summary aria-label="打开导航">菜单</summary>
          <div>
            {topLinks.map(([label, href]) => (
              <a key={href} href={href}>
                {label}
              </a>
            ))}
            <a href={repositoryUrl} target="_blank" rel="noreferrer">
              GitHub ↗
            </a>
          </div>
        </details>
      </div>
    </header>
  );
}
