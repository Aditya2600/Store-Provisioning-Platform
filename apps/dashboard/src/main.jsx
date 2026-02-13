import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { 
  Store, 
  ShoppingCart, 
  Trash2, 
  RefreshCw, 
  ExternalLink, 
  Plus, 
  AlertCircle,
  Clock,
  Activity,
  Server
} from "lucide-react";
import "./index.css";

const API_BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

function StatusBadge({ status }) {
  const icon = useMemo(() => {
    switch(status) {
      case "Ready": return <Activity size={14} />;
      case "Provisioning": return <RefreshCw size={14} className="loader" />;
      default: return <AlertCircle size={14} />;
    }
  }, [status]);

  return (
    <span className={`status-badge ${status}`}>
      <span className="status-dot"></span>
      {status}
    </span>
  );
}

function StatCard({ icon: Icon, label, value }) {
  return (
    <div className="stat-card">
      <div className="stat-icon">
        <Icon size={24} />
      </div>
      <div className="stat-info">
        <h3>{label}</h3>
        <p>{value}</p>
      </div>
    </div>
  );
}

function App() {
  const [stores, setStores] = useState([]);
  const [engine, setEngine] = useState("woocommerce");
  const [storeId, setStoreId] = useState("");
  const [err, setErr] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [isCreating, setIsCreating] = useState(false);

  const api = useMemo(() => ({
    async list() {
      const r = await fetch(`${API_BASE}/stores`);
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    async create(payload) {
      const r = await fetch(`${API_BASE}/stores`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
    async del(id) {
      const r = await fetch(`${API_BASE}/stores/${id}`, { method: "DELETE" });
      if (!r.ok) throw new Error(await r.text());
      return r.json();
    },
  }), []);

  async function refresh() {
    setIsLoading(true);
    setErr("");
    try {
      setStores(await api.list());
    } catch (e) {
      setErr(String(e));
    } finally {
      setIsLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000); // reduced polling for smoother feel
    return () => clearInterval(t);
  }, []);

  async function onCreate(e) {
    e.preventDefault();
    setErr("");
    if (!storeId) return;
    
    setIsCreating(true);
    try {
      await api.create({ engine, storeId });
      setStoreId("");
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setIsCreating(false);
    }
  }

  async function onDelete(id) {
    if(!confirm(`Are you sure you want to delete ${id}?`)) return;
    setErr("");
    try {
      await api.del(id);
      await refresh();
    } catch (e) {
      setErr(String(e));
    }
  }

  const activeStores = stores.filter(s => s.phase === 'Ready').length;
  const provisioningStores = stores.filter(s => s.phase === 'Provisioning').length;

  return (
    <div className="app-container">
      <header>
        <h1>Store Provisioning Platform</h1>
        <p className="subtitle">Orchestrate isolated e-commerce environments on Kubernetes</p>
      </header>

      {err && (
        <div className="error-msg">
          <AlertCircle size={20} />
          <span>{err}</span>
        </div>
      )}

      <div className="stats-bar">
        <StatCard icon={Store} label="Total Stores" value={stores.length} />
        <StatCard icon={Activity} label="Active" value={activeStores} />
        <StatCard icon={Server} label="Provisioning" value={provisioningStores} />
      </div>

      <div className="dashboard-grid">
        <aside className="create-card">
          <h2><Plus size={20} /> New Store</h2>
          <form onSubmit={onCreate} className="form-group">
            <div className="form-group">
              <label>Store Engine</label>
              <select value={engine} onChange={(e) => setEngine(e.target.value)}>
                <option value="woocommerce">WooCommerce (WordPress)</option>
                <option value="medusa">MedusaJS (Coming Soon)</option>
              </select>
            </div>
            
            <div className="form-group">
              <label>Store Identifier</label>
              <input
                placeholder="e.g. fashion-store-1"
                value={storeId}
                onChange={(e) => setStoreId(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ''))}
                maxLength={32}
              />
            </div>

            <button 
              type="submit" 
              className="primary-btn" 
              disabled={!storeId || isCreating}
            >
              {isCreating ? <RefreshCw className="loader" size={18} /> : <ShoppingCart size={18} />}
              {isCreating ? "Provisioning..." : "Launch Store"}
            </button>
          </form>
        </aside>

        <section className="stores-section">
          <div className="section-header">
            <h2>Your Stores</h2>
            <button onClick={refresh} className="refresh-btn" disabled={isLoading}>
              <RefreshCw size={18} className={isLoading ? "loader" : ""} />
            </button>
          </div>

          {stores.length === 0 ? (
            <div className="empty-state">
              <Store size={48} style={{ opacity: 0.2, marginBottom: '1rem' }} />
              <p>No stores deployed yet. Create your first store to get started.</p>
            </div>
          ) : (
            stores.map((s) => (
              <div key={s.storeId} className="store-card">
                <div className="store-info">
                  <div className="store-header">
                    <span className="store-name">{s.storeId}</span>
                    <span className="engine-badge">{s.engine}</span>
                  </div>
                  <div className="store-meta">
                    <span className="meta-item">
                      <Clock size={14} /> 
                      {s.createdAt ? new Date(s.createdAt).toLocaleTimeString() : 'Just now'}
                    </span>
                    <StatusBadge status={s.phase} />
                  </div>
                  {s.lastError && (
                     <div style={{color: 'var(--error)', fontSize: '0.8rem', marginTop: '0.5rem'}}>
                       Error: {s.lastError}
                     </div>
                  )}
                </div>
                
                <div className="store-actions">
                  {s.url && (
                    <a href={s.url} target="_blank" rel="noreferrer" className="link-btn">
                      Visit Store <ExternalLink size={14} />
                    </a>
                  )}
                  <button 
                    onClick={() => onDelete(s.storeId)} 
                    className="delete-btn"
                    title="Delete Store"
                  >
                    <Trash2 size={18} />
                  </button>
                </div>
              </div>
            ))
          )}
        </section>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
