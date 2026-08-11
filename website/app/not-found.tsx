import { SiteHeader } from "./components/SiteHeader";

export default function NotFound() {
  return (
    <div className="not-found">
      <SiteHeader />
      <main>
        <span>404 / PATH NOT FOUND</span>
        <h1>没有找到这个文档路径</h1>
        <p>返回 Overview，或使用页首搜索进入当前章节。</p>
        <a className="button primary" href="/overview">返回 Overview →</a>
      </main>
    </div>
  );
}
