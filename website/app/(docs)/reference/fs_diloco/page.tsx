import type { Metadata } from "next";
import { ApiModuleReference } from "../../../components/ApiModuleReference";
import { findApiModule } from "../../../reference-data/api";

export const metadata: Metadata = {
  title: "fs_diloco Python API",
  description: "fs_diloco 根包及其全部当前 Python 模块入口。",
};

export default function RootPackageReferencePage() {
  const moduleDoc = findApiModule("fs_diloco");
  if (!moduleDoc) throw new Error("generated reference is missing fs_diloco");
  return <ApiModuleReference moduleDoc={moduleDoc} />;
}
