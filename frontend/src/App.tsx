import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { createBrowserRouter, RouterProvider } from "react-router";

import { AuthProvider } from "./auth/AuthProvider";
import { ApiError } from "./lib/api";
import { routes } from "./routes";

const router = createBrowserRouter(routes);

function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Never retry client errors (401/403/404/422); retry transient failures twice.
        retry: (failureCount, error) =>
          !(error instanceof ApiError && error.status < 500) && failureCount < 2,
        refetchOnWindowFocus: false,
      },
    },
  });
}

export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <RouterProvider router={router} />
      </AuthProvider>
    </QueryClientProvider>
  );
}
