import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { can, onSessionChange, refreshSession, type Role, type Session } from "./api";
import { setTimezone } from "./format";
import { startLiveUpdates } from "./stream";

interface AuthState {
  session: Session | null;
  ready: boolean; // the initial "is there a refresh cookie?" check has finished
}

const AuthContext = createContext<AuthState>({ session: null, ready: false });

export function AuthProvider({ children }: { children: ReactNode }) {
  const client = useQueryClient();
  const [state, setState] = useState<AuthState>({ session: null, ready: false });

  useEffect(() => {
    const off = onSessionChange((session) => {
      if (session) setTimezone(session.tenant.timezone);
      else client.clear(); // never show one login's data to the next
      setState({ session, ready: true });
    });
    void refreshSession().then((s) => setState({ session: s, ready: true }));
    return () => {
      off();
    };
  }, [client]);

  // one live-update connection per signed-in tab
  const userId = state.session?.user.id;
  useEffect(() => {
    if (!userId) return;
    return startLiveUpdates(client);
  }, [userId, client]);

  return <AuthContext.Provider value={state}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

export function useCan(role: Role): boolean {
  return can(useAuth().session?.user.role, role);
}
