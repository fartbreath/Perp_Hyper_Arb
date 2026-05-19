import { BrowserRouter, Routes, Route, NavLink, useLocation } from "react-router-dom";
import { BASE_URL } from "./api/client";
import Dashboard from "./pages/Dashboard";
import Trades from "./pages/Trades";
import Performance from "./pages/Performance";
import Signals from "./pages/Signals";
import Risk from "./pages/Risk";
import Markets from "./pages/Markets";
import Settings from "./pages/Settings";
import Logs from "./pages/Logs";
import Positions from "./pages/Positions";
import Pending from "./pages/Pending";
import Fills from "./pages/Fills";
import Events from "./pages/Events";
import ModelAgentPage from "./pages/ModelAgent";
import ModelPaperTradesPage from "./pages/ModelPaperTrades";
import OPEPage from "./pages/OPE";
import ModelCPage from "./pages/ModelC";
import ModelDPage from "./pages/ModelD";
import "./App.css";

const FLAT_NAV: { to: string; label: string }[] = [
  { to: "/", label: "Dashboard" },
  { to: "/trades", label: "Trades" },
  { to: "/pending", label: "Pending" },
  { to: "/positions", label: "Positions" },
  { to: "/performance", label: "Performance" },
  { to: "/signals", label: "Signals" },
  { to: "/risk", label: "Risk" },
  { to: "/markets", label: "Markets" },
  { to: "/fills", label: "Fills" },
  { to: "/events", label: "Events" },
];

// Dropdown group: Model Sim — shadow agent + paper trading + OPE + simulators
const MODEL_SIM_ROUTES = ["/model", "/model-paper", "/ope", "/model-c", "/model-d"];
const MODEL_SIM_LINKS: { to: string; label: string }[] = [
  { to: "/model", label: "Shadow Agent" },
  { to: "/model-paper", label: "Paper Trades" },
  { to: "/ope", label: "OPE Surface" },
  { to: "/model-c", label: "Model C Simulator" },
  { to: "/model-d", label: "Model D Simulator" },
];

// Dropdown group: Models — SHAP reports and explainability, one per model
const MODEL_REPORT_LINKS: { href: string; label: string; badge?: string }[] = [
  { href: `${BASE_URL}/reports/model_a_v0_shap.html`, label: "Model A — Entry Quality", badge: "A" },
  { href: `${BASE_URL}/reports/model_b_v0_shap.html`, label: "Model B — Exit Gate (v0)", badge: "B" },
  { href: `${BASE_URL}/reports/model_b_v1_shap.html`, label: "Model B v1 — Exit Gate (v5 features)", badge: "B1" },
  { href: `${BASE_URL}/reports/model_c_v0_shap.html`, label: "Model C — Divergence", badge: "C" },
  // Model D (Config Policy) — added when model_d_v0.pkl is available
  { href: `${BASE_URL}/reports/model_d_v0_shap.html`, label: "Model D — Config Policy", badge: "D" },
];

function NavDropdown({
  label,
  routes,
  children,
}: {
  label: string;
  routes?: string[];
  children: React.ReactNode;
}) {
  const location = useLocation();
  const hasActive = routes?.some((r) =>
    r === "/" ? location.pathname === "/" : location.pathname.startsWith(r)
  );
  return (
    <li className={`nav-dropdown${hasActive ? " has-active" : ""}`}>
      <span className="nav-dropdown-trigger">{label}</span>
      <div className="nav-dropdown-menu">{children}</div>
    </li>
  );
}

function Nav() {
  return (
    <nav className="nav">
      <span className="nav-brand">Perp Hyper Arb</span>
      <ul className="nav-links">
        {FLAT_NAV.map(({ to, label }) => (
          <li key={to}>
            <NavLink to={to} end={to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
              {label}
            </NavLink>
          </li>
        ))}

        {/* ▼ Model Sim — shadow agent status + paper trades */}
        <NavDropdown label="Model Sim" routes={MODEL_SIM_ROUTES}>
          <span className="menu-section-label">Live Simulation</span>
          {MODEL_SIM_LINKS.map(({ to, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>
              {label}
            </NavLink>
          ))}
        </NavDropdown>

        {/* ▼ Models — SHAP explainability reports, one per model */}
        <NavDropdown label="Models">
          <span className="menu-section-label">Explainability (SHAP)</span>
          {MODEL_REPORT_LINKS.map(({ href, label }) => (
            <a key={href} href={href} target="_blank" rel="noreferrer">
              {label} ↗
            </a>
          ))}
        </NavDropdown>

        <li>
          <NavLink to="/logs" className={({ isActive }) => (isActive ? "active" : "")}>
            Logs
          </NavLink>
        </li>
        <li>
          <NavLink to="/settings" className={({ isActive }) => (isActive ? "active" : "")}>
            ⚙️ Settings
          </NavLink>
        </li>
      </ul>
    </nav>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Nav />
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/pending" element={<Pending />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/signals" element={<Signals />} />
          <Route path="/risk" element={<Risk />} />
          <Route path="/markets" element={<Markets />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/fills" element={<Fills />} />
          <Route path="/events" element={<Events />} />
          <Route path="/model" element={<ModelAgentPage />} />
          <Route path="/model-paper" element={<ModelPaperTradesPage />} />
          <Route path="/ope" element={<OPEPage />} />
          <Route path="/model-c" element={<ModelCPage />} />
          <Route path="/model-d" element={<ModelDPage />} />
          <Route path="/logs" element={<Logs />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
