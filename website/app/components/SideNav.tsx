"use client";

import { useLayoutEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import { apiModuleIndex } from "../reference-data/api-index";
import { navGroups } from "../site";

const referenceSidebarScrollKey = "fs-diloco.reference-sidebar-scroll";

const apiPackageGroups = apiModuleIndex
  .filter((item) => item.isPackage)
  .map((packageItem) => ({
    packageItem,
    modules: apiModuleIndex.filter(
      (item) => !item.isPackage && item.package === packageItem.module,
    ),
  }));

export function SideNav() {
  const pathname = usePathname();
  const navRef = useRef<HTMLElement>(null);
  const isReferencePage = pathname.startsWith("/reference");

  useLayoutEffect(() => {
    if (!isReferencePage) return;
    const sidebar = navRef.current?.closest<HTMLElement>(".docs-sidebar");
    if (!sidebar) return;

    const restoreScrollPosition = () => {
      try {
        const savedPosition = Number.parseInt(
          window.sessionStorage.getItem(referenceSidebarScrollKey) ?? "",
          10,
        );
        if (Number.isFinite(savedPosition)) sidebar.scrollTop = savedPosition;
      } catch {
        // Storage restrictions must not prevent the navigation from rendering.
      }
    };
    const saveScrollPosition = () => {
      try {
        window.sessionStorage.setItem(
          referenceSidebarScrollKey,
          String(sidebar.scrollTop),
        );
      } catch {
        // Storage restrictions only disable position persistence for this session.
      }
    };

    restoreScrollPosition();
    const restoreFrame = window.requestAnimationFrame(restoreScrollPosition);
    sidebar.addEventListener("scroll", saveScrollPosition, { passive: true });
    window.addEventListener("pagehide", saveScrollPosition);

    return () => {
      window.cancelAnimationFrame(restoreFrame);
      saveScrollPosition();
      sidebar.removeEventListener("scroll", saveScrollPosition);
      window.removeEventListener("pagehide", saveScrollPosition);
    };
  }, [isReferencePage]);

  return (
    <nav ref={navRef} className="side-nav" aria-label="文档导航">
      {navGroups.map((group) => (
        <section key={group.label}>
          <h2>{group.label}</h2>
          {group.items.map((item) => {
            const active = pathname === item.href;
            return (
              <a
                key={item.href}
                href={item.href}
                className={active ? "active" : undefined}
                aria-current={active ? "page" : undefined}
              >
                <span>{item.label}</span>
                <small>{item.description}</small>
              </a>
            );
          })}
        </section>
      ))}
      {isReferencePage ? (
        <section className="api-module-tree">
          <h2>Python API</h2>
          {apiPackageGroups.map(({ packageItem, modules }) => {
            const packageActive = pathname === packageItem.route;
            const packageSelected =
              packageActive || pathname.startsWith(`${packageItem.route}/`);
            return (
              <details key={packageItem.module} open={packageSelected}>
                <summary>
                  <span>{packageItem.module.replace(/^fs_diloco\.?/, "") || "fs_diloco"}</span>
                  <small>{modules.length}</small>
                </summary>
                <a
                  href={packageItem.route}
                  className={packageActive ? "active" : undefined}
                  aria-current={packageActive ? "page" : undefined}
                >
                  <span>包概览</span>
                  <small>{packageItem.module}</small>
                </a>
                {modules.map((moduleItem) => {
                  const active = pathname === moduleItem.route;
                  return (
                    <a
                      href={moduleItem.route}
                      className={active ? "active" : undefined}
                      aria-current={active ? "page" : undefined}
                      key={moduleItem.route}
                    >
                      <span>{moduleItem.module.split(".").at(-1)}</span>
                      <small>
                        {moduleItem.counts.classes}C · {moduleItem.counts.functions}F · {moduleItem.counts.methods}M
                      </small>
                    </a>
                  );
                })}
              </details>
            );
          })}
        </section>
      ) : null}
    </nav>
  );
}
