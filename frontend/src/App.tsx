import { useMemo, useState } from "react";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { Icons, SidebarContext, SidebarInfo, meta } from "./components";
import Collectors from "./pages/Collectors";
import History from "./pages/History";
import Overview from "./pages/Overview";
import ServerDetail from "./pages/ServerDetail";
import Servers from "./pages/Servers";

const NAV = [
  { to: "/", label: "Last night", icon: Icons.overview, key: "overview", end: true },
  { to: "/servers", label: "Servers", icon: Icons.servers, key: "servers" },
  { to: "/history", label: "History", icon: Icons.history, key: "history" },
  { to: "/collectors", label: "Collectors", icon: Icons.sync, key: "collectors" },
];

export default function App() {
  const [foot, setFoot] = useState<SidebarInfo | null>(null);
  const [counts, setCounts] = useState<Record<string, string>>({});
  const ctx = useMemo(() => ({ setFoot, setCounts }), []);

  return (
    <BrowserRouter>
      <SidebarContext.Provider value={ctx}>
        <div className="layout">
          <aside className="sidebar">
            <div className="brand">
              <span className="mark">
                <Icons.shield />
              </span>
              <span className="brand-name">Backup Status</span>
            </div>
            <div className="nav-label">Monitor</div>
            <nav>
              {NAV.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.end}>
                  <n.icon />
                  <span className="nav-text">{n.label}</span>
                  {counts[n.key] && <span className="nav-count">{counts[n.key]}</span>}
                </NavLink>
              ))}
            </nav>
            {foot && (
              <div className="sidebar-foot">
                <div className="foot-label">{foot.label}</div>
                {foot.rows.map((r) => (
                  <div className="foot-row" key={r.name}>
                    {r.state && (
                      <span className={`dot sw-${meta(r.state).cls}`} style={{ width: 5, height: 5 }} />
                    )}
                    <span className="foot-name">{r.name}</span>
                    {r.value && <span className="foot-val">{r.value}</span>}
                  </div>
                ))}
              </div>
            )}
          </aside>
          <main className="main">
            <Routes>
              <Route path="/" element={<Overview />} />
              <Route path="/servers" element={<Servers />} />
              <Route path="/servers/:id" element={<ServerDetail />} />
              <Route path="/history" element={<History />} />
              <Route path="/collectors" element={<Collectors />} />
            </Routes>
          </main>
        </div>
      </SidebarContext.Provider>
    </BrowserRouter>
  );
}
