/**
 * 页面路径与导航标题，只写一处。
 *
 * 路径用 ASCII：中文路径在地址栏、日志和分享链接里会变成 percent-encoding，
 * 不好读也不好搜。页面标题仍用中文业务叫法。
 */
export const ROUTES = {
  dashboard: "/dashboard",
  ledger: "/ledger",
  spu: "/spu",
  baihuo: "/baihuo",
  purchase: "/purchase",
  contract: "/contract",
  exchange: "/exchange",
  chat: "/chat",
  workbench: "/workbench",
  status: "/status",
} as const;

export interface NavItem {
  path: string;
  label: string;
  /** 看板与台账共用统计年度，跳转时把 ?year= 带过去。 */
  keepsYear: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { path: ROUTES.dashboard, label: "采购看板", keepsYear: true },
  { path: ROUTES.ledger, label: "交期提醒台账", keepsYear: true },
  { path: ROUTES.spu, label: "鞋服SPU", keepsYear: false },
  { path: ROUTES.baihuo, label: "自营百货", keepsYear: false },
  { path: ROUTES.purchase, label: "建立采购单", keepsYear: false },
  { path: ROUTES.contract, label: "采购合同", keepsYear: false },
  { path: ROUTES.status, label: "工作台", keepsYear: false },
];
