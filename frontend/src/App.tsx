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
        <div className="app">
          <header className="appbar">
            <div className="brand">
              {/* The mark's black and dark-red segments vanish on a dark ground,
                  so it sits on a light safe-area tile rather than being
                  recoloured. */}
              <span className="brand-tile">
                <img src="/lbf-mark.png" alt="" />
              </span>
              <span>
                <div className="brand-name">Backup Status</div>
                <div className="brand-sub">LB Foster Infrastructure</div>
              </span>
            </div>

            <nav className="appnav">
              {NAV.map((n) => (
                <NavLink key={n.to} to={n.to} end={n.end}>
                  <n.icon />
                  <span className="nav-text">{n.label}</span>
                  {counts[n.key] && <span className="nav-count">{counts[n.key]}</span>}
                </NavLink>
              ))}
            </nav>

            {/* The page's own summary rides in the bar, since a top nav leaves
                no rail to put it in. */}
            {foot && (
              <div className="appbar-summary">
                <span className="summary-label">{foot.label}</span>
                {foot.rows.map((r) => (
                  <span className="summary-item" key={r.name}>
                    {r.state && (
                      <span
                        className={`dot sw-${meta(r.state).cls}`}
                        style={{ width: 6, height: 6 }}
                        aria-hidden
                      />
                    )}
                    {r.name}
                    {r.value && <b>{r.value}</b>}
                  </span>
                ))}
              </div>
            )}
          </header>

          <main className="page">
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
