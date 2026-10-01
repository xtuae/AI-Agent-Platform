import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApiError } from "./lib/api";
import { AuthProvider } from "./lib/auth";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: true,
      retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
    },
  },
});

// The platform console (admin.<domain>, or /console) is a separate chunk with its own staff sign-in;
// tenant dashboards never load it. Which one runs never grants anything: the server checks tokens.
const ConsoleApp = lazy(() => import("./console/ConsoleApp"));
const isConsole = window.location.hostname.startsWith("admin.") || window.location.pathname.startsWith("/console");

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        {isConsole ? (
          <Suspense fallback={null}>
            <ConsoleApp />
          </Suspense>
        ) : (
          <AuthProvider>
            <App />
          </AuthProvider>
        )}
      </QueryClientProvider>
    </StrictMode>,
  );
}
