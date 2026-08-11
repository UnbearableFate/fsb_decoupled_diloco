"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { apiModuleIndex } from "../reference-data/api-index";
import { allSections } from "../site";

const searchItems = [
  ...allSections,
  ...apiModuleIndex.map((item) => ({
    href: item.route,
    label: item.module,
    description: item.summary,
    keywords: [
      "Python",
      "API",
      "reference",
      item.isPackage ? "package" : "module",
      item.module.replaceAll(".", "/"),
    ],
  })),
];

export function Search() {
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const normalized = query.trim().toLowerCase();
  const matches = useMemo(() => {
    if (!normalized) return allSections.slice(0, 4);
    return searchItems
      .filter((item) =>
        [item.label, item.description, ...item.keywords]
          .join(" ")
          .toLowerCase()
          .includes(normalized),
      )
      .slice(0, 10);
  }, [normalized]);

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", focusSearch);
    return () => window.removeEventListener("keydown", focusSearch);
  }, []);

  return (
    <div className="search">
      <label className="sr-only" htmlFor="docs-search">
        搜索文档章节或 Python 模块
      </label>
      <span className="search-icon" aria-hidden="true">
        /
      </span>
      <input
        id="docs-search"
        ref={inputRef}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setQuery("");
            event.currentTarget.blur();
          }
        }}
        placeholder="搜索章节或模块"
        autoComplete="off"
        aria-controls="docs-search-results"
      />
      <kbd>⌘ K</kbd>
      <div className="search-results" id="docs-search-results" role="list">
        <p>{normalized ? `匹配「${query.trim()}」` : "常用入口"}</p>
        {matches.length > 0 ? (
          matches.map((item) => (
            <a
              key={item.href}
              href={item.href}
              onClick={() => setQuery("")}
            >
              <span>{item.label}</span>
              <small>{item.description}</small>
            </a>
          ))
        ) : (
          <span className="search-empty">没有匹配的章节或模块。</span>
        )}
      </div>
    </div>
  );
}
