import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
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
import "./App.css";

const NAV_LINKS = [
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
  { to: "/logs", label: "Logs" },
  { to: "/settings", label: "⚙️ Settings" },
];

export default function App() {
  return (
    <BrowserRouter>
      <nav className="nav">
        <span className="nav-brand">Perp Hyper Arb</span>
        <ul className="nav-links">
          {NAV_LINKS.map(({ to, label }) => (
            <li key={to}>
              <NavLink
                to={to}
                end={to === "/"}
                className={({ isActive }) => (isActive ? "active" : "")}
              >
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
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
          <Route path="/logs" element={<Logs />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
