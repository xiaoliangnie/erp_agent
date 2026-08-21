import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AuthGate } from "./components/AuthGate";
import { Loading } from "./components/PageState";
import { ROUTES } from "./routes";

// 看板和台账都要吃整年明细，单独切块，别让只想开对话页的人先下载图表代码。
const DashboardPage = lazy(() => import("./pages/dashboard/DashboardPage"));
const LedgerPage = lazy(() => import("./pages/ledger/LedgerPage"));
const SpuPage = lazy(() => import("./pages/spu/SpuPage"));
const PurchasePage = lazy(() => import("./pages/purchase/PurchasePage"));
const ContractPage = lazy(() => import("./pages/contract/ContractPage"));
const StatusPage = lazy(() => import("./pages/status/StatusPage"));

export function App() {
  return (
    <AuthGate>
      <Suspense fallback={<Loading label="正在加载页面…" />}>
        <Routes>
          <Route path="/" element={<Navigate to={ROUTES.dashboard} replace />} />
          <Route path={ROUTES.dashboard} element={<DashboardPage />} />
          <Route path={ROUTES.ledger} element={<LedgerPage />} />
          <Route path={ROUTES.spu} element={<SpuPage />} />
          <Route path={ROUTES.baihuo} element={<SpuPage board="baihuo" />} />
          <Route path={ROUTES.purchase} element={<PurchasePage />} />
          <Route path={ROUTES.contract} element={<ContractPage />} />
          <Route path={ROUTES.exchange} element={<Navigate to={ROUTES.status} replace />} />
          <Route path={ROUTES.chat} element={<Navigate to={ROUTES.status} replace />} />
          <Route path={ROUTES.workbench} element={<Navigate to={ROUTES.status} replace />} />
          <Route path={ROUTES.status} element={<StatusPage />} />
          <Route path="*" element={<Navigate to={ROUTES.dashboard} replace />} />
        </Routes>
      </Suspense>
    </AuthGate>
  );
}
