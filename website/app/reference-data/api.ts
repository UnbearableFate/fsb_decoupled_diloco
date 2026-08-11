import manifestData from "./api-manifest.json";

export type ApiParameterDoc = {
  name: string;
  type: string;
  default: string | null;
  kind: string;
  description: string;
};

export type ApiCallableDoc = {
  name: string;
  qualifiedName: string;
  kind: string;
  visibility: string;
  signature: string;
  summary: string;
  docstring: string | null;
  parameters: ApiParameterDoc[];
  returns: { type: string; description: string };
  raises: string[];
  decorators: string[];
  line: number;
  endLine: number;
};

export type ApiFieldDoc = {
  name: string;
  kind: string;
  visibility: string;
  type: string;
  default: string | null;
  summary: string;
  line: number;
};

export type ApiClassDoc = {
  name: string;
  kind: "class";
  visibility: string;
  signature: string;
  summary: string;
  docstring: string | null;
  bases: string[];
  decorators: string[];
  fields: ApiFieldDoc[];
  methods: ApiCallableDoc[];
  line: number;
  endLine: number;
};

export type ApiModuleDoc = {
  module: string;
  route: string;
  sourcePath: string;
  sourceSha256: string;
  isPackage: boolean;
  visibility: string;
  summary: string;
  docstring: string | null;
  exports: string[];
  variables: ApiFieldDoc[];
  classes: ApiClassDoc[];
  functions: ApiCallableDoc[];
  dependencies: string[];
  usedBy: string[];
  lineCount: number;
};

export type ApiReferenceManifest = {
  schemaVersion: number;
  sourceRevision: string;
  stats: {
    modules: number;
    packages: number;
    classes: number;
    functions: number;
    methods: number;
    fields: number;
    variables: number;
    symbols: number;
  };
  modules: ApiModuleDoc[];
};

export const apiManifest = manifestData as unknown as ApiReferenceManifest;

const modulesByName = new Map(
  apiManifest.modules.map((moduleDoc) => [moduleDoc.module, moduleDoc]),
);

export function findApiModule(moduleName: string): ApiModuleDoc | undefined {
  return modulesByName.get(moduleName);
}

export function moduleNameFromSegments(segments: string[]): string {
  return ["fs_diloco", ...segments].join(".");
}

export function moduleChildren(moduleName: string): ApiModuleDoc[] {
  const prefix = `${moduleName}.`;
  return apiManifest.modules.filter((moduleDoc) => {
    if (!moduleDoc.module.startsWith(prefix)) return false;
    return !moduleDoc.module.slice(prefix.length).includes(".");
  });
}

export function adjacentApiModules(moduleName: string): {
  previous?: ApiModuleDoc;
  next?: ApiModuleDoc;
} {
  const index = apiManifest.modules.findIndex(
    (moduleDoc) => moduleDoc.module === moduleName,
  );
  if (index < 0) return {};
  return {
    previous: index > 0 ? apiManifest.modules[index - 1] : undefined,
    next:
      index < apiManifest.modules.length - 1
        ? apiManifest.modules[index + 1]
        : undefined,
  };
}

export function apiSourceUrl(moduleDoc: ApiModuleDoc, line?: number): string {
  const anchor = line ? `#L${line}` : "";
  return `https://github.com/UnbearableFate/fsb_decoupled_diloco/blob/${apiManifest.sourceRevision}/${moduleDoc.sourcePath}${anchor}`;
}
