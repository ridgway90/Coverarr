import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { Spinner } from "./components/ui";
import { api, type AuthStatus } from "./lib/api";
import AuthPage from "./pages/AuthPage";
import CollectionDetailPage from "./pages/CollectionDetailPage";
import CollectionsPage from "./pages/CollectionsPage";
import DashboardPage from "./pages/DashboardPage";
import ItemDetailPage from "./pages/ItemDetailPage";
import JobsPage from "./pages/JobsPage";
import LibraryPage from "./pages/LibraryPage";
import ReviewPage from "./pages/ReviewPage";
import SettingsPage from "./pages/SettingsPage";

export default function App() {
  const qc = useQueryClient();
  const auth = useQuery({ queryKey: ["auth"], queryFn: () => api.get<AuthStatus>("/api/auth/status") });

  useEffect(() => {
    const onUnauthorized = () => qc.invalidateQueries({ queryKey: ["auth"] });
    window.addEventListener("coverarr:unauthorized", onUnauthorized);
    return () => window.removeEventListener("coverarr:unauthorized", onUnauthorized);
  }, [qc]);

  if (auth.isLoading) return <Spinner label="Loading" />;
  if (!auth.data?.authenticated) {
    return <AuthPage setup={auth.data?.setup_required ?? false} />;
  }

  return (
    <Routes>
      <Route element={<Layout username={auth.data.username ?? ""} />}>
        <Route index element={<DashboardPage />} />
        <Route path="library/:libraryId" element={<LibraryPage />} />
        <Route path="items/:itemId" element={<ItemDetailPage />} />
        <Route path="collections" element={<CollectionsPage />} />
        <Route path="collections/:collectionId" element={<CollectionDetailPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
