export const repositoryUrl =
  "https://github.com/UnbearableFate/fsb_decoupled_diloco";

export const sourceRevision = "df1d6db3f08549c28914114e99634ee237f24944";

export type SectionItem = {
  href: string;
  label: string;
  description: string;
  keywords: string[];
};

export type NavGroup = {
  label: string;
  items: SectionItem[];
};

export const navGroups: NavGroup[] = [
  {
    label: "开始",
    items: [
      {
        href: "/overview",
        label: "Overview",
        description: "项目定位、适用范围与阅读路径",
        keywords: ["概览", "full protocol", "项目定位"],
      },
      {
        href: "/getting-started",
        label: "Getting Started",
        description: "安装、初始化、提交与验证",
        keywords: ["快速开始", "安装", "pbs", "qsub"],
      },
    ],
  },
  {
    label: "理解系统",
    items: [
      {
        href: "/concepts",
        label: "Concepts",
        description: "协议角色、proposal、fence 与 token 记账",
        keywords: ["概念", "learner", "syncer", "membership", "proposal"],
      },
      {
        href: "/architecture",
        label: "Architecture",
        description: "控制面、数据面、提交流程与终态收敛",
        keywords: ["架构", "sqlite", "filesystem", "leader lease"],
      },
    ],
  },
  {
    label: "运行与开发",
    items: [
      {
        href: "/user-guide",
        label: "User Guide",
        description: "配置、运行、观测、恢复与清理",
        keywords: ["用户指南", "配置", "运维", "恢复", "清理"],
      },
      {
        href: "/reference",
        label: "Reference",
        description: "CLI、配置、Python API 与目录结构",
        keywords: ["参考", "api", "cli", "config", "runpaths"],
      },
    ],
  },
  {
    label: "研究",
    items: [
      {
        href: "/experiments",
        label: "Experiments",
        description: "为实验协议、结果与证据预留",
        keywords: ["实验", "结果", "证据", "benchmark"],
      },
    ],
  },
];

export const allSections = navGroups.flatMap((group) => group.items);

export function sourceUrl(path: string, line?: number): string {
  const anchor = line ? `#L${line}` : "";
  return `${repositoryUrl}/blob/${sourceRevision}/${path}${anchor}`;
}
