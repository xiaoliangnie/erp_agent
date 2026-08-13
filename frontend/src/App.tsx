import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Loading } from "./components/PageState";
import { ROUTES } from "./routes";

// 看板和台账都要吃整年明细，单独切块，别让只想开对话页的人先下载图表代码。
const DashboardPage = lazy(() => import("./pages/dashboard/DashboardPage"));
const LedgerPage = lazy(() => import("./pages/ledger/LedgerPage"));
const ContractPage = lazy(() => import("./pages/contract/ContractPage"));
const ExchangePage = lazy(() => import("./pages/exchange/ExchangePage"));
const ChatPage = lazy(() => import("./pages/chat/ChatPage"));

export function App() {
  return (
    <Suspense fallback={<Loading label="正在加载页面…" />}>
      <Routes>
        <Route path="/" element={<Navigate to={ROUTES.dashboard} replace />} />
        <Route path={ROUTES.dashboard} element={<DashboardPage />} />
        <Route path={ROUTES.ledger} element={<LedgerPage />} />
        <Route path={ROUTES.contract} element={<ContractPage />} />
        <Route path={ROUTES.exchange} element={<ExchangePage />} />
        <Route path={ROUTES.chat} element={<ChatPage />} />
        <Route path="*" element={<Navigate to={ROUTES.dashboard} replace />} />
      </Routes>
    </Suspense>
  );
}
