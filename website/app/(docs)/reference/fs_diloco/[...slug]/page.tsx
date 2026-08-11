import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ApiModuleReference } from "../../../../components/ApiModuleReference";
import {
  apiManifest,
  findApiModule,
  moduleNameFromSegments,
} from "../../../../reference-data/api";

type ModulePageProps = {
  params: Promise<{ slug: string[] }>;
};

export async function generateMetadata({ params }: ModulePageProps): Promise<Metadata> {
  const { slug } = await params;
  const moduleDoc = findApiModule(moduleNameFromSegments(slug));
  if (!moduleDoc) return { title: "未找到 API 模块" };
  return {
    title: moduleDoc.module,
    description: moduleDoc.summary,
  };
}

export function generateStaticParams(): Array<{ slug: string[] }> {
  return apiManifest.modules
    .filter((moduleDoc) => moduleDoc.module !== "fs_diloco")
    .map((moduleDoc) => ({ slug: moduleDoc.module.split(".").slice(1) }));
}

export default async function ModuleReferencePage({ params }: ModulePageProps) {
  const { slug } = await params;
  const moduleDoc = findApiModule(moduleNameFromSegments(slug));
  if (!moduleDoc) notFound();
  return <ApiModuleReference moduleDoc={moduleDoc} />;
}
