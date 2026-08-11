import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import "./globals.css";

export async function generateMetadata(): Promise<Metadata> {
  const requestHeaders = await headers();
  const forwardedHost = requestHeaders.get("x-forwarded-host");
  const host = (forwardedHost ?? requestHeaders.get("host"))
    ?.split(",")[0]
    .trim();
  const forwardedProtocol = requestHeaders.get("x-forwarded-proto");
  const protocol =
    forwardedProtocol?.split(",")[0].trim() ??
    (host?.startsWith("localhost") ? "http" : "https");
  const socialImage = host ? `${protocol}://${host}/og.png` : undefined;

  return {
    title: {
      default: "FS-DiLoCo Documentation",
      template: "%s · FS-DiLoCo",
    },
    description:
      "面向 Miyabi 共享文件系统的 Decoupled DiLoCo 原型文档：概念、操作、架构与接口参考。",
    applicationName: "FS-DiLoCo Documentation",
    keywords: [
      "Decoupled DiLoCo",
      "distributed training",
      "shared filesystem",
      "Miyabi",
      "PBS",
    ],
    openGraph: socialImage
      ? {
          type: "website",
          title: "FS-DiLoCo Documentation",
          description:
            "Filesystem-based Decoupled DiLoCo concepts, operations, architecture, and reference.",
          images: [{ url: socialImage, width: 1731, height: 909 }],
        }
      : undefined,
    twitter: socialImage
      ? {
          card: "summary_large_image",
          title: "FS-DiLoCo Documentation",
          description:
            "Filesystem-based Decoupled DiLoCo concepts, operations, architecture, and reference.",
          images: [socialImage],
        }
      : undefined,
  };
}

export const viewport: Viewport = {
  colorScheme: "dark",
  themeColor: "#07110f",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
