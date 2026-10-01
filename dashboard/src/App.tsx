import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router";
import { Layout } from "./components/Layout";
import { Spinner } from "./components/ui/misc";
import { useAuth } from "./lib/auth";
import { useModules } from "./modules";
import { Login } from "./pages/Login";
import { TodayPage } from "./pages/Today";

// Today and Login ship in the main bundle (the first screens anyone sees); the rest load on demand.
// Module screens come from the module registry, for the modules this tenant has.
const ConversationsPage = lazy(() => import("./pages/Conversations"));
const ContactsPage = lazy(() => import("./pages/Contacts"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const CostsPage = lazy(() => import("./pages/Costs"));

function Loading() {
  return (
    <div className="flex justify-center py-16">
      <Spinner />
    </div>
  );
}

export function App() {
  const { session, ready } = useAuth();
  const routes = useModules().flatMap((m) => m.routes ?? []);
  if (!ready) return <Loading />;
  if (!session) return <Login />;
  return (
    <BrowserRouter>
      <Suspense fallback={<Loading />}>
        <Routes>
          {routes
            .filter((r) => r.bare)
            .map((r) => (
              <Route key={r.path} path={r.path} element={<r.element />} />
            ))}
          <Route element={<Layout />}>
            <Route index element={<TodayPage />} />
            {routes
              .filter((r) => !r.bare)
              .map((r) => (
                <Route key={r.path} path={r.path} element={<r.element />} />
              ))}
            <Route path="conversations" element={<ConversationsPage />} />
            <Route path="conversations/:id" element={<ConversationsPage />} />
            <Route path="contacts" element={<ContactsPage />} />
            <Route path="customers" element={<Navigate to="/contacts" replace />} />
            <Route path="costs" element={<CostsPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
