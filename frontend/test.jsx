
/* ══════════════════════════════════════════════════════════════
   MapMantra — React Application
   ══════════════════════════════════════════════════════════════ */
const { useState, useEffect, useCallback, useRef, useMemo } = React;
const API = window.location.origin;

// ── API Helper ────────────────────────────────────────────────
async function api(endpoint, opts = {}) {
  try {
    const r = await fetch(`${API}${endpoint}`, { ...opts, headers: { 'Content-Type': 'application/json', ...opts.headers } });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  } catch (e) { console.error(`API ${endpoint}:`, e); return null; }
}
async function apiForm(endpoint, formData) {
  try {
    const r = await fetch(`${API}${endpoint}`, { method: 'POST', body: formData });
    if (!r.ok) throw new Error(r.statusText);
    return r.json();
  } catch (e) { console.error(`API ${endpoint}:`, e); return null; }
}

// ── Hooks ─────────────────────────────────────────────────────
function useApi(endpoint, deps = []) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const fetch_ = useCallback(async () => {
    if (!endpoint) return;
    setLoading(true);
    setError(null);
    const d = await api(endpoint);
    if (d === null) {
      setError('Connection refused or API returned an error.');
    } else {
      setData(d);
    }
    setLoading(false);
  }, [endpoint, ...deps]);
  useEffect(() => { fetch_(); }, [fetch_]);
  return { data, loading, error, refetch: fetch_ };
}
function usePolling(endpoint, interval = 30000) {
  const { data, loading, error, refetch } = useApi(endpoint);
  useEffect(() => { const id = setInterval(refetch, interval); return () => clearInterval(id); }, [refetch, interval]);
  return { data, loading, error, refetch };
}


// ── Utils ─────────────────────────────────────────────────────
const riskColor = s => s >= 60 ? 'var(--red)' : s >= 30 ? 'var(--amber)' : 'var(--green)';
const riskLevel = s => s >= 60 ? 'high' : s >= 30 ? 'medium' : 'low';
const statusColor = s => ({completed:'var(--green)',pass:'var(--green)',satisfied:'var(--green)',assigned:'var(--blue)',
  in_progress:'var(--blue)',pending:'var(--text-3)',partial:'var(--amber)',overdue:'var(--red)',failed:'var(--red)',
  fail:'var(--red)',conflict:'var(--red)',superseded:'var(--text-4)'}[s] || 'var(--text-3)');
const priorityColor = p => ({high:'var(--red)',medium:'var(--amber)',low:'var(--green)'}[p] || 'var(--text-3)');
const agentColor = a => ({ingestion:'var(--cyan)',extractor:'var(--purple)',router:'#14b8a6',validator:'var(--amber)',
  supervisor:'var(--text-3)',conflict_detector:'var(--red)',threat_mapper:'var(--purple)',risk_engine:'var(--green)'}[a] || 'var(--text-3)');
function timeAgo(iso) {
  if (!iso) return '';
  try { const d = new Date(iso), diff = (Date.now() - d) / 1000;
    if (diff < 60) return 'just now'; if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff/3600)}h ago`; return `${Math.floor(diff/86400)}d ago`;
  } catch { return ''; }
}
function daysRemaining(d) {
  if (!d) return { days: null, color: 'var(--text-3)' };
  const diff = Math.ceil((new Date(d) - Date.now()) / 86400000);
  return { days: diff, color: diff < 0 ? 'var(--red)' : diff <= 7 ? 'var(--amber)' : 'var(--green)' };
}

// ── Shared Components ─────────────────────────────────────────
function Skeleton({ w = '100%', h = '16px', r = '6px' }) {
  return <div className="skeleton" style={{ width: w, height: h, borderRadius: r }} />;
}
function Badge({ label, color, bg }) {
  return <span className="badge" style={{ background: bg || `${color}18`, color, border: `1px solid ${color}30` }}>{label}</span>;
}
function AnimatedBar({ pct, color, delay = 0 }) {
  const [w, setW] = useState(0);
  useEffect(() => { const t = setTimeout(() => setW(Math.min(pct, 100)), 100 + delay); return () => clearTimeout(t); }, [pct, delay]);
  return <div className="dept-bar"><div className="dept-bar-fill" style={{ width: `${w}%`, background: color }} /></div>;
}
function GlassCard({ children, className = '', style, onClick, onMouseMove }) {
  return <div className={`glass-card ${className}`} style={style} onClick={onClick} onMouseMove={onMouseMove}>{children}</div>;
}

// ── Sidebar ───────────────────────────────────────────────────
function Sidebar({ page, navigate, counts }) {
  const links = [
    { id: 'dashboard', icon: '▣', label: 'Dashboard', section: 'OVERVIEW' },
    { id: 'inbox', icon: '↓', label: 'Regulation Inbox', section: 'OVERVIEW', badge: counts.newRegs, badgeType: 'amber' },
    { id: 'maps', icon: '◈', label: 'MAP Board', section: 'COMPLIANCE' },
    { id: 'conflicts', icon: '⚑', label: 'Conflicts', section: 'COMPLIANCE', badge: counts.conflicts, badgeType: 'red' },
    { id: 'threat', icon: '◉', label: 'Threat Intel', section: 'COMPLIANCE' },
    { id: 'audit', icon: '≡', label: 'Audit Trail', section: 'REPORTS' },
    { id: 'risk', icon: '◎', label: 'Risk Report', section: 'REPORTS' },
  ];
  let lastSection = '';
  return (
    <nav className="sidebar">
      <div className="sb-brand">
        <div className="sb-logo">MM</div>
        <div><div className="sb-title">MapMantra</div><div className="sb-subtitle">Regulatory Intelligence</div></div>
      </div>
      <div style={{ flex: 1, padding: '8px 0', overflowY: 'auto' }}>
        {links.map(l => {
          const showSection = l.section !== lastSection;
          lastSection = l.section;
          return (
            <React.Fragment key={l.id}>
              {showSection && <div className="sb-section"><div className="sb-section-label">{l.section}</div></div>}
              <div style={{ padding: '0 8px' }}>
                <div className={`sb-link ${page === l.id ? 'active' : ''}`} onClick={() => navigate(l.id)}>
                  <span className="sb-icon">{l.icon}</span><span>{l.label}</span>
                  {l.badge > 0 && <span className={`sb-badge sb-badge-${l.badgeType}`}>{l.badge}</span>}
                </div>
              </div>
            </React.Fragment>
          );
        })}
      </div>
    </nav>
  );
}

// ── Topbar ─────────────────────────────────────────────────────
function Topbar({ page, navigate, dashboard, onExportCSV, onDownloadReport }) {
  const [showNotif, setShowNotif] = useState(false);
  const [showRisk, setShowRisk] = useState(false);
  const risk = dashboard?.risk || {};
  const score = risk.global_score || 0;
  const activity = dashboard?.recent_activity || [];
  const departments = risk.departments || {};
  const fin = risk.financial_exposure || {};
  const bd = risk.breakdown || {};
  const rows = [
    { label: 'Base Score', count: 1, mult: 10, positive: true },
    { label: 'Overdue High Priority', count: bd.overdue_high || 0, mult: 15, positive: true },
    { label: 'Overdue Medium Priority', count: bd.overdue_medium || 0, mult: 10, positive: true },
    { label: 'Overdue Low Priority', count: bd.overdue_low || 0, mult: 5, positive: true },
    { label: 'Pending High Priority', count: bd.pending_high || 0, mult: 2, positive: true },
    { label: 'Unresolved Conflicts', count: bd.conflicts_unresolved || 0, mult: 20, positive: true },
    { label: 'Completed this month', count: bd.completed_this_month || 0, mult: -2, positive: false },
  ];
  const tabs = [
    { id: 'dashboard', label: 'Dashboard' }, { id: 'maps', label: 'MAP Board' }, { id: 'inbox', label: 'Circulars' },
    { id: 'conflicts', label: 'Conflicts' }, { id: 'threat', label: 'Threat Intel' }, { id: 'audit', label: 'Audit Trail' },
  ];

  useEffect(() => {
    const close = (e) => { if (!e.target.closest('.risk-pill-wrap,.notif-wrap')) { setShowNotif(false); setShowRisk(false); } };
    document.addEventListener('click', close); return () => document.removeEventListener('click', close);
  }, []);

  return (
    <header className="topbar">
      <div className="tb-tabs">
        {tabs.map(t => <button key={t.id} className={`tb-tab ${page===t.id?'active':''}`} onClick={()=>navigate(t.id)}>{t.label}</button>)}
      </div>
      <div className="tb-right">
        <button className="btn btn-outline" style={{fontSize:12,padding:'6px 14px'}} onClick={onExportCSV}>Export CSV</button>
        <button className="btn btn-outline" style={{fontSize:12,padding:'6px 14px'}} onClick={onDownloadReport}>Download Report</button>
      </div>

      <div className="grid-3 mb-4">
        <GlassCard style={{ textAlign:'center', borderColor: score>=60?'rgba(239,68,68,0.2)':'transparent', boxShadow: score>=60?'var(--glow-red)':'none' }}>
          <div className="metric-label">Overall Risk Score</div>
          <div className="metric-value" style={{ color: riskColor(score), fontSize: 48 }}>{score}</div>
          <div className="metric-desc">out of 100</div>
        </GlassCard>
        <GlassCard style={{ textAlign:'center' }}>
          <div className="metric-label">Penalty Exposure</div>
          <div style={{ fontSize:24, fontWeight:900, color:'var(--amber)', margin:'10px 0' }}>₹{fin.min_cr||0}Cr — ₹{fin.max_cr||0}Cr</div>
          <div className="metric-desc">{fin.overdue_count||0} overdue MAPs contributing</div>
        </GlassCard>
        <GlassCard style={{ textAlign:'center' }}>
          <div className="metric-label">Closed This Month</div>
          <div className="metric-value" style={{ color:'var(--green)' }}>{bd.completed_this_month||0} <span style={{fontSize:20}}>↓</span></div>
          <div className="metric-desc">Score reduced by {(bd.completed_this_month||0)*2} points</div>
        </GlassCard>
      </div>

      <GlassCard className="mb-4">
        <div className="section-title">How this score is calculated</div>
        {rows.map((r,i) => {
          const pts = r.count * r.mult;
          const bg = r.positive && r.count > 0 ? 'rgba(239,68,68,0.05)' : !r.positive && r.count > 0 ? 'rgba(16,185,129,0.05)' : 'transparent';
          const ptColor = pts > 0 ? 'var(--red)' : pts < 0 ? 'var(--green)' : 'var(--text-4)';
          return <div key={i} className="flex items-center justify-between" style={{ background:bg, borderRadius:'var(--radius-sm)', padding:'10px 14px', margin:'2px 0' }}>
            <span className="text-sm">{r.label}</span>
            <div className="flex gap-4 items-center"><span className="text-sm" style={{color:'var(--text-3)'}}>{r.count} ×{r.mult}</span>
              <span className="font-bold" style={{color:ptColor,width:50,textAlign:'right'}}>{pts>0?'+':''}{pts}</span></div>
          </div>;
        })}
        <div className="flex items-center justify-between" style={{ borderTop:'2px solid var(--glass-border)', paddingTop:10, marginTop:4 }}>
          <span className="font-bold text-lg">Total (clamped 0-100)</span>
          <span style={{ fontSize:22, fontWeight:900, color:riskColor(score) }}>{score}</span>
        </div>
      </GlassCard>

      <div className="section-title">Risk by Department</div>
      <div className="grid-3 mb-4">
        {['IT','Compliance','Legal','HR','Risk','Operations'].map(name => {
          const d = departments[name] || {};
          return <GlassCard key={name} style={{ textAlign:'center' }}>
            <div className="text-sm font-bold mb-2">{name}</div>
            <div style={{fontSize:28,fontWeight:900,color:riskColor(d.score||0)}}>{d.score||0}</div>
            <div style={{margin:'10px auto',height:6,background:'rgba(255,255,255,0.04)',borderRadius:3,overflow:'hidden'}}>
              <div style={{height:'100%',width:`${Math.min(d.score||0,100)}%`,background:riskColor(d.score||0),borderRadius:3,transition:'width 0.8s'}}/>
            </div>
            <div className="flex justify-between text-xs" style={{color:'var(--text-4)',gap:8,justifyContent:'center'}}>
              <span style={{color:'var(--red)',display:'flex',alignItems:'center',gap:4}}><span style={{width:6,height:6,borderRadius:'50%',background:'var(--red)',display:'inline-block'}}></span>{d.overdue||0}</span>
              <span style={{color:'var(--blue)',display:'flex',alignItems:'center',gap:4}}><span style={{width:6,height:6,borderRadius:'50%',background:'var(--blue)',display:'inline-block'}}></span>{d.in_progress||0}</span>
              <span style={{color:'var(--green)',display:'flex',alignItems:'center',gap:4}}><span style={{width:6,height:6,borderRadius:'50%',background:'var(--green)',display:'inline-block'}}></span>{d.completed||0}</span>
            </div>
          </GlassCard>;
        })}
      </div>

      <div className="section-title">Estimated Regulatory Exposure</div>
      <div className="grid-2">
        <GlassCard style={{textAlign:'center'}}><div className="text-xs" style={{color:'var(--text-4)'}}>Minimum Exposure</div><div style={{fontSize:24,fontWeight:900,color:'var(--amber)',margin:'8px 0'}}>₹{fin.min_cr||0} Crores</div></GlassCard>
        <GlassCard style={{textAlign:'center'}}><div className="text-xs" style={{color:'var(--text-4)'}}>Maximum Exposure</div><div style={{fontSize:24,fontWeight:900,color:'var(--red)',margin:'8px 0'}}>₹{fin.max_cr||0} Crores</div></GlassCard>
      </div>
      <div className="text-xs mt-2" style={{color:'var(--text-4)',textAlign:'center'}}>Based on RBI (₹2Cr—₹50Cr), SEBI (₹1Cr—₹25Cr), and IRDAI (₹50L—₹5Cr) penalty ranges per overdue MAP.</div>
    </div>
  );
}

// ══════════════════════════════════════════════════════════════
// APP — Router + Layout
// ══════════════════════════════════════════════════════════════

// ── Dummy Screens (Since they were missing from the file) ──
function DashboardScreen() { return <div style={{padding: 20}}><h2>Dashboard</h2><p>Dashboard coming soon...</p></div>; }
function InboxScreen() { return <div style={{padding: 20}}><h2>Regulation Inbox</h2><p>Inbox coming soon...</p></div>; }
function MapBoardScreen() { return <div style={{padding: 20}}><h2>MAP Board</h2><p>MAP Board coming soon...</p></div>; }
function MapDetailScreen() { return <div style={{padding: 20}}><h2>MAP Detail</h2><p>MAP Detail coming soon...</p></div>; }
function ConflictsScreen() { return <div style={{padding: 20}}><h2>Conflicts</h2><p>Conflicts coming soon...</p></div>; }
function ThreatIntelScreen() { return <div style={{padding: 20}}><h2>Threat Intel</h2><p>Threat Intel coming soon...</p></div>; }
function AuditTrailScreen() { return <div style={{padding: 20}}><h2>Audit Trail</h2><p>Audit Trail coming soon...</p></div>; }
function RiskReportScreen() { return <div style={{padding: 20}}><h2>Risk Report</h2><p>Please refer to the Topbar for the Risk Report overview.</p></div>; }

function App() {
  const [page, setPage] = useState('dashboard');
  const { data: dashboard } = usePolling('/api/dashboard', 15000);
  const counts = { newRegs: dashboard?.new_circulars || 0, conflicts: dashboard?.conflicts || 0 };

  const navigate = (p) => setPage(p);

  const screens = {
    dashboard: <DashboardScreen dashboard={dashboard} navigate={navigate} />,
    inbox: <InboxScreen navigate={navigate} />,
    maps: <MapBoardScreen navigate={navigate} />,
    map_detail: <MapDetailScreen navigate={navigate} />,
    conflicts: <ConflictsScreen />,
    threat: <ThreatIntelScreen navigate={navigate} />,
    audit: <AuditTrailScreen />,
    risk: <RiskReportScreen />,
  };

  const exportCSV = async () => {
    const maps = await api('/api/maps?limit=1000');
    if (!maps || !maps.length) { alert('No MAP data to export.'); return; }
    const headers = ['ID','Department','Action','Status','Priority','Deadline','Metric','Verification'];
    const rows = maps.map(m => [
      m.id,
      m.department || '',
      `"${(m.action_description||'').replace(/"/g,'""')}"`,
      m.status || '',
      m.priority || '',
      (m.deadline_date||'').slice(0,10),
      `"${(m.compliance_metric||'').replace(/"/g,'""')}"`,
      `"${(m.verification_method||'').replace(/"/g,'""')}"`
    ]);
    const csv = [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
    const blob = new Blob([csv], {type:'text/csv;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `mapmantra_maps_${new Date().toISOString().slice(0,10)}.csv`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  };

  const downloadReport = async () => {
    const [risk, maps, conflicts] = await Promise.all([
      api('/api/risk-report'),
      api('/api/maps?limit=1000'),
      api('/api/conflicts'),
    ]);
    const now = new Date().toISOString().slice(0,10);
    const total = maps ? maps.length : 0;
    const byStatus = {};
    (maps||[]).forEach(m => { byStatus[m.status] = (byStatus[m.status]||0)+1; });
    const byDept = {};
    (maps||[]).forEach(m => { byDept[m.department] = (byDept[m.department]||0)+1; });
    const lines = [
      `MAPMANTRA — COMPLIANCE STATUS REPORT`,
      `Generated: ${now}`,
      `${'='.repeat(60)}`,
      ``,
      `EXECUTIVE SUMMARY`,
      `-`.repeat(40),
      `Overall Risk Score : ${risk?.global_score || 'N/A'} / 100`,
      `Total MAPs         : ${total}`,
      `Completed          : ${byStatus['completed']||0}`,
      `In Progress        : ${byStatus['in_progress']||0}`,
      `Overdue            : ${byStatus['overdue']||0}`,
      `Partial            : ${byStatus['partial']||0}`,
      `Conflicts (Open)   : ${(conflicts||[]).filter(c=>c.resolution==='pending_legal').length}`,
      ``,
      `STATUS BREAKDOWN BY DEPARTMENT`,
      `-`.repeat(40),
      ...Object.entries(byDept).map(([d,c]) => `  ${d.padEnd(20)} ${c} MAPs`),
      ``,
      `FINANCIAL EXPOSURE`,
      `-`.repeat(40),
      `Minimum: INR ${risk?.financial_exposure?.min_cr||0} Crores`,
      `Maximum: INR ${risk?.financial_exposure?.max_cr||0} Crores`,
      ``,
      `${'='.repeat(60)}`,
      `This report was auto-generated by MapMantra AI Compliance System.`,
    ];
    const blob = new Blob([lines.join('\n')], {type:'text/plain;charset=utf-8;'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url;
    a.download = `mapmantra_compliance_report_${now}.txt`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  };

  return (
    <div className="app-layout">
      <Sidebar page={page} navigate={navigate} counts={counts} />
      <Topbar page={page} navigate={navigate} dashboard={dashboard} onExportCSV={exportCSV} onDownloadReport={downloadReport} />
      <main className="main-area">
        {screens[page] || screens.dashboard}
      </main>
    </div>
  );
}

// ── Render ─────────────────────────────────────────────────────
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
