import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { Layout } from "./components/Layout";
import { Spinner } from "./components/ui/misc";
import { useAuth } from "./lib/auth";
import { Login } from "./pages/Login";
import { TodayPage } from "./pages/Today";

// Today and Login ship in the main bundle (the first screens anyone sees); the rest load on demand.
const OrdersPage = lazy(() => import("./pages/Orders"));
const DeliveryListPage = lazy(() => import("./pages/DeliveryList"));
const ConversationsPage = lazy(() => import("./pages/Conversations"));
const CustomersPage = lazy(() => import("./pages/Customers"));
const SettingsPage = lazy(() => import("./pages/Settings"));

function Loading() {
  return (
    <div className="flex justify-center py-16">
      <Spinner />
    </div>
  );
}

export function App() {
  const { session, ready } = useAuth();
  if (!ready) return <Loading />;
  if (!session) return <Login />;
  return (
    <BrowserRouter>
      <Suspense fallback={<Loading />}>
        <Routes>
          <Route path="/orders/delivery-list" element={<DeliveryListPage />} />
          <Route element={<Layout />}>
            <Route index element={<TodayPage />} />
            <Route path="orders" element={<OrdersPage />} />
            <Route path="conversations" element={<ConversationsPage />} />
            <Route path="conversations/:id" element={<ConversationsPage />} />
            <Route path="customers" element={<CustomersPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
