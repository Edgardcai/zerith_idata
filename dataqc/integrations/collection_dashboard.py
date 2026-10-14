"""Add the collection dashboard as a first-class workbench workspace."""
from .legacy_ui import replace


def integrate_dashboard(app):
    app.HTML = replace(app.HTML, '<nav class="workspace-switch" aria-label="质检模块切换">',
        '<nav class="workspace-switch" aria-label="质检模块切换">\n      <button id="collectionDashboardWorkspaceBtn" class="workspace-btn" type="button">数据看板</button>')
    app.HTML = replace(app.HTML, '  <main id="collectionWorkspace">',
        '  <div id="collectionDashboardWorkspace" class="cross-platform-workspace hidden"><iframe id="collectionDashboardFrame" title="数据采集看板" style="height:calc(100vh - 110px);min-height:640px;border:0"></iframe></div>\n  <main id="collectionWorkspace">')
    app.HTML = replace(app.HTML, '    function setWorkspace(name) {', '''    function setWorkspace(name) {
      const dashboard = name === 'collection-dashboard';
      if(dashboard) history.replaceState(null,'','#collection-dashboard');
      else if(location.hash === '#collection-dashboard') history.replaceState(null,'',location.pathname+location.search);
      document.getElementById('collectionDashboardWorkspace').classList.toggle('hidden', !dashboard);
      document.getElementById('collectionDashboardWorkspaceBtn').classList.toggle('active', dashboard);
      const dashboardFrame = document.getElementById('collectionDashboardFrame');
      if(dashboard && !dashboardFrame.getAttribute('src')) dashboardFrame.src='/collection-dashboard?embedded=1';''')
    app.HTML = replace(app.HTML, '    setWorkspace("collection");', '''    document.getElementById('collectionDashboardWorkspaceBtn').onclick=()=>setWorkspace('collection-dashboard');
    setWorkspace(location.hash === '#collection-dashboard' ? 'collection-dashboard' : 'collection');''')
