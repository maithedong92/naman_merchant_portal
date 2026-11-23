import json
from pathlib import Path
import re
import subprocess
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.models.user import User, UserRole

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT_DIR / "app" / "templates"
STATIC_DIR = ROOT_DIR / "app" / "static"

ALL_TEMPLATES = [
    "base.html",
    "login.html",
    "orders.html",
    "admin_dashboard.html",
    "reports.html",
    "inventory.html",
    "users.html",
    "system_status.html",
]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def super_admin_user():
    return User(
        id="sec-t3-admin-1",
        username="sec_admin",
        email="sec_admin@namanmarket.com",
        role=UserRole.SUPER_ADMIN.value,
        is_active=True,
        is_superuser=True,
    )


def _read_template(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _extract_js_function(source: str, func_name: str) -> str:
    match = re.search(rf"(?:window\.{func_name}\s*=\s*(?:async\s+)?function|(?:async\s+)?function\s+{func_name})\s*\(", source)
    assert match, f"Could not locate JS function {func_name}"
    paren_depth = 0
    param_end = -1
    for idx in range(match.end() - 1, len(source)):
        ch = source[idx]
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
            if paren_depth == 0:
                param_end = idx
                break
    assert param_end != -1, f"Unbalanced parameter list in {func_name}"
    brace_start = source.find("{", param_end)
    assert brace_start != -1
    depth = 0
    for idx in range(brace_start, len(source)):
        ch = source[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : idx + 1]
    raise AssertionError(f"Unbalanced braces while extracting {func_name}")


def _run_node_json(script: str):
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    return json.loads(proc.stdout.strip())


def test_static_assets_built_and_vendored():
    tailwind_css = STATIC_DIR / "css" / "tailwind.css"
    chart_js = STATIC_DIR / "vendor" / "chart.umd.min.js"

    assert tailwind_css.is_file(), "Compiled static Tailwind CSS file must exist"
    css_text = tailwind_css.read_text(encoding="utf-8")
    assert len(css_text.encode("utf-8")) > 10_000
    assert "shadow-2xs" in css_text
    assert "backdrop-blur-xs" in css_text
    assert "shadow-apple-card" in css_text
    assert "shadow-apple-dropdown" in css_text

    assert chart_js.is_file(), "Vendored Chart.js UMD file must exist"
    assert chart_js.stat().st_size > 100_000
    chart_head = chart_js.read_text(encoding="utf-8")[:512]
    assert "chart.js" in chart_head.lower()


def test_no_external_js_cdns_and_local_assets_rendered(client, super_admin_user):
    for name in ALL_TEMPLATES:
        html = _read_template(name)
        assert "cdn.tailwindcss.com" not in html, f"{name} still references cdn.tailwindcss.com"
        assert "cdn.jsdelivr.net" not in html, f"{name} still references cdn.jsdelivr.net"

    assert '<link rel="stylesheet" href="/static/css/tailwind.css">' in _read_template("base.html")
    assert '<link rel="stylesheet" href="/static/css/tailwind.css">' in _read_template("login.html")
    assert '<script src="/static/vendor/chart.umd.min.js"></script>' in _read_template("reports.html")

    login_res = client.get("/admin/login")
    assert login_res.status_code == 200
    assert "/static/css/tailwind.css" in login_res.text
    assert "cdn.tailwindcss.com" not in login_res.text

    routes = [
        "/admin",
        "/admin/orders",
        "/admin/reports",
        "/admin/inventory",
        "/admin/users",
        "/system-status",
    ]
    with patch("app.web.router._get_authenticated_web_user", new_callable=AsyncMock, return_value=super_admin_user):
        for route in routes:
            res = client.get(route)
            assert res.status_code == 200, f"Expected 200 on {route}, got {res.status_code}"
            assert "/static/css/tailwind.css" in res.text
            assert "cdn.tailwindcss.com" not in res.text
            assert "cdn.jsdelivr.net" not in res.text
            if route == "/admin/reports":
                assert "/static/vendor/chart.umd.min.js" in res.text


def test_base_and_login_csrf_header_and_session_storage():
    base_html = _read_template("base.html")
    login_html = _read_template("login.html")

    # SEC-M01: X-Requested-With on _performTokenRefresh, authFetch, logoutAdmin, handleLogin
    refresh_fn = _extract_js_function(base_html, "_performTokenRefresh")
    auth_fetch_fn = _extract_js_function(base_html, "authFetch")
    logout_fn = _extract_js_function(base_html, "logoutAdmin")
    login_fn = _extract_js_function(login_html, "handleLogin")

    for fn_name, body in [
        ("_performTokenRefresh", refresh_fn),
        ("authFetch", auth_fetch_fn),
        ("logoutAdmin", logout_fn),
        ("handleLogin", login_fn),
    ]:
        assert "'X-Requested-With': 'XMLHttpRequest'" in body, f"{fn_name} missing X-Requested-With header"

    # SEC-L05: naman_user_profile stored in sessionStorage and cleaned from localStorage
    assert 'sessionStorage.getItem("naman_user_profile")' in base_html
    assert 'sessionStorage.setItem("naman_user_profile"' in base_html
    assert 'sessionStorage.removeItem("naman_user_profile")' in base_html
    assert 'localStorage.getItem("naman_user_profile")' not in base_html
    assert 'localStorage.setItem("naman_user_profile"' not in base_html
    assert 'localStorage.removeItem("naman_user_profile")' in base_html

    assert 'sessionStorage.setItem("naman_user_profile"' in login_html
    assert 'localStorage.getItem("naman_user_profile")' not in login_html
    assert 'localStorage.setItem("naman_user_profile"' not in login_html
    assert 'localStorage.removeItem("naman_user_profile")' in login_html


def test_login_open_redirect_validation_and_port_redaction():
    login_html = _read_template("login.html")
    assert "Cổng chuẩn 3002" not in login_html
    assert "3002" not in login_html
    assert "resolveSafeRedirect" in login_html

    resolve_fn = _extract_js_function(login_html, "resolveSafeRedirect")
    node_script = f"""
const window = {{ location: {{ origin: 'https://ump.namanmarket.com' }} }};
{resolve_fn}
const fallback = '/admin/orders';
const results = {{
  ext_https: resolveSafeRedirect('https://evil.com/phish', fallback),
  proto_rel: resolveSafeRedirect('//evil.com/admin', fallback),
  triple_slash: resolveSafeRedirect('///evil.com', fallback),
  backslash: resolveSafeRedirect('/\\\\evil.com', fallback),
  crlf: resolveSafeRedirect('/admin\\nLocation: https://evil.com', fallback),
  nul: resolveSafeRedirect('/admin\\x00evil', fallback),
  js_scheme: resolveSafeRedirect('javascript:alert(1)', fallback),
  prefix_bypass: resolveSafeRedirect('/admin-evil', fallback),
  status_bypass: resolveSafeRedirect('/system-status-evil', fallback),
  other_path: resolveSafeRedirect('/api/v1/auth/me', fallback),
  valid_admin: resolveSafeRedirect('/admin', fallback),
  valid_orders: resolveSafeRedirect('/admin/orders?status=PENDING#top', fallback),
  valid_status: resolveSafeRedirect('/system-status?page=2', fallback),
}};
console.log(JSON.stringify(results));
"""
    results = _run_node_json(node_script)
    for bad_key in (
        "ext_https",
        "proto_rel",
        "triple_slash",
        "backslash",
        "crlf",
        "nul",
        "js_scheme",
        "prefix_bypass",
        "status_bypass",
        "other_path",
    ):
        assert results[bad_key] == "/admin/orders", f"{bad_key} bypassed redirect validation: {results[bad_key]}"

    assert results["valid_admin"] == "/admin"
    assert results["valid_orders"] == "/admin/orders?status=PENDING#top"
    assert results["valid_status"] == "/system-status?page=2"


def test_orders_xss_upload_download_encoding_and_grabmart_badge():
    base_html = _read_template("base.html")
    orders_html = _read_template("orders.html")

    # Duplicate escapeHtml removed; delegates to window.escapeHtml
    assert "function escapeHtml(" not in orders_html

    # formatDateTime escapes invalid date fallback and catch fallback
    format_dt_fn = _extract_js_function(orders_html, "formatDateTime")
    assert format_dt_fn.count("window.escapeHtml(String(dateStr))") >= 2

    # Behavioral check via Node: escapeHtml handles 0/false and formatDateTime escapes XSS payloads
    escape_fn = _extract_js_function(base_html, "escapeHtml")
    node_script = f"""
const window = {{}};
{escape_fn}
{format_dt_fn}
const out = {{
  esc_zero: window.escapeHtml(0),
  esc_false: window.escapeHtml(false),
  esc_null: window.escapeHtml(null),
  dt_xss: formatDateTime('<img src=x onerror=alert(1)>'),
  dt_empty: formatDateTime(''),
}};
console.log(JSON.stringify(out));
"""
    out = _run_node_json(node_script)
    assert out["esc_zero"] == "0"
    assert out["esc_false"] == "false"
    assert out["esc_null"] == ""
    assert out["dt_xss"] == "&lt;img src=x onerror=alert(1)&gt;"
    assert out["dt_empty"] == "--"

    # File upload accepts only PDF/PNG/JPEG/WEBP (no image/*)
    assert 'accept=".pdf,image/*"' not in orders_html
    assert orders_html.count('accept=".pdf,image/png,image/jpeg,image/webp"') == 4

    # Blob preview allowlist in downloadHaravanFile
    assert "SAFE_PREVIEW_TYPES = new Set(['application/pdf', 'image/png', 'image/jpeg', 'image/webp'])" in orders_html
    dl_fn = _extract_js_function(orders_html, "downloadHaravanFile")
    assert "res.headers.get('content-type')" in dl_fn
    assert "SAFE_PREVIEW_TYPES.has(" in dl_fn
    assert "application/octet-stream" in dl_fn

    # Behavioral check via Node: SVG/HTML must NOT call window.open and must download as application/octet-stream
    dl_node_script = f"""
const SAFE_PREVIEW_TYPES = new Set(['application/pdf', 'image/png', 'image/jpeg', 'image/webp']);
async function runCase(contentType) {{
  let openedUrl = null;
  let downloaded = null;
  const createdBlobs = [];
  const window = {{
    open(url) {{ openedUrl = url; return {{}}; }}
  }};
  const document = {{
    createElement() {{ return {{ href: '', download: '', click() {{ downloaded = {{ href: this.href, download: this.download }}; }}, remove() {{}} }}; }},
    body: {{ appendChild() {{}} }}
  }};
  class FakeBlob {{
    constructor(parts, opts) {{ this.parts = parts; this.type = (opts && opts.type) || ''; createdBlobs.push(this.type); }}
  }}
  const Blob = FakeBlob;
  const URL = {{
    createObjectURL(b) {{ return 'blob:' + b.type; }},
    revokeObjectURL() {{}}
  }};
  const setTimeout = () => 0;
  const showToast = () => {{}};
  const authFetch = async () => ({{
    ok: true,
    headers: {{ get(k) {{ return k.toLowerCase() === 'content-type' ? contentType : ''; }} }},
    blob: async () => new FakeBlob(['data'], {{ type: contentType }})
  }});
  {dl_fn}
  await downloadHaravanFile('ord1', 'order');
  return {{ openedUrl, downloaded, createdBlobs }};
}}
(async () => {{
  const res = {{
    svg: await runCase('image/svg+xml'),
    html: await runCase('text/html; charset=utf-8'),
    pdf: await runCase('application/pdf')
  }};
  console.log(JSON.stringify(res));
}})();
"""
    dl_out = _run_node_json(dl_node_script)
    assert dl_out["svg"]["openedUrl"] is None
    assert dl_out["svg"]["downloaded"]["href"] == "blob:application/octet-stream"
    assert dl_out["html"]["openedUrl"] is None
    assert dl_out["html"]["downloaded"]["href"] == "blob:application/octet-stream"
    assert dl_out["pdf"]["openedUrl"] == "blob:application/pdf"

    # Numeric coercion and encodeURIComponent on dynamic order IDs
    assert "Number(rec.fulfilled_items_count || 0)" in orders_html
    assert "Number(rec.total_items_count || 0)" in orders_html
    assert "${encodeURIComponent(orderId)}" in orders_html
    assert "${encodeURIComponent(currentCancellingOrderId)}" in orders_html
    assert "${encodeURIComponent(currentHaravanOrderId)}" in orders_html

    # Ensure no unencoded orderId or currentHaravanOrderId or currentCancellingOrderId in authFetch template URLs
    unencoded = re.findall(r"authFetch\(`[^`]*\$\{(?:orderId|currentCancellingOrderId|currentHaravanOrderId)\}[^`]*`", orders_html)
    assert not unencoded, f"Found unencoded path parameters in orders.html: {unencoded}"

    # Preserve GrabMart canonical badge check
    assert "c === 'GRABMART' || c === 'GRAB' || c === 'GRAB_MART'" in orders_html


def test_admin_dashboard_redaction_oauth_allowlist_and_encoding():
    dash_html = _read_template("admin_dashboard.html")

    for forbidden in (
        "103.175.248.250:6767",
        "103.175.248.250",
        ":3001",
        "Port 3002",
        "3001+",
        "10045",
        "/api/v1/pos/pricing/channels",
    ):
        assert forbidden not in dash_html, f"Sensitive infrastructure detail {forbidden!r} still present in admin_dashboard.html"

    assert "Number(report.total_local_products || 0)" in dash_html
    assert "/api/v1/channels/${encodeURIComponent(channelCode)}/features" in dash_html

    oauth_fn = _extract_js_function(dash_html, "startShopeeMartOAuth")
    assert ".shopeemobile.com" in oauth_fn
    assert ".shopee.com" in oauth_fn
    assert ".shopee.vn" in oauth_fn

    oauth_node_script = f"""
async function testOAuthUrl(candidateUrl) {{
  let opened = null;
  let toastErr = null;
  const document = {{ getElementById: () => null }};
  const window = {{
    location: {{ origin: 'https://ump.namanmarket.com', href: '' }},
    open: (url) => {{ opened = url; return {{}}; }}
  }};
  const showToast = (msg, level) => {{ if (level === 'error') toastErr = msg; }};
  const authFetch = async () => ({{
    ok: true,
    json: async () => ({{ success: true, data: {{ auth_url: candidateUrl }} }})
  }});
  {oauth_fn}
  await startShopeeMartOAuth();
  return {{ opened, toastErr }};
}}
(async () => {{
  const res = {{
    evil_host: await testOAuthUrl('https://evil-shopeemobile.com/oauth'),
    evil_suffix: await testOAuthUrl('https://shopeemobile.com.evil.com/oauth'),
    http_scheme: await testOAuthUrl('http://partner.shopeemobile.com/oauth'),
    valid_shopeemobile: await testOAuthUrl('https://partner.shopeemobile.com/api/v2/shop/auth_partner'),
    valid_shopee_vn: await testOAuthUrl('https://open.shopee.vn/auth')
  }};
  console.log(JSON.stringify(res));
}})();
"""
    oauth_out = _run_node_json(oauth_node_script)
    assert oauth_out["evil_host"]["opened"] is None
    assert oauth_out["evil_suffix"]["opened"] is None
    assert oauth_out["http_scheme"]["opened"] is None
    assert oauth_out["valid_shopeemobile"]["opened"] == "https://partner.shopeemobile.com/api/v2/shop/auth_partner"
    assert oauth_out["valid_shopee_vn"]["opened"] == "https://open.shopee.vn/auth"


def test_reports_inventory_users_system_status_hardening():
    reports_html = _read_template("reports.html")
    assert "function escapeHtml(" not in reports_html
    assert "c === 'GRABMART' || c === 'GRAB' || c === 'GRAB_MART'" in reports_html

    inventory_html = _read_template("inventory.html")
    assert "Number(s.matched_skus_count || 0)" in inventory_html
    assert "Number(s.prices_updated || 0)" in inventory_html
    assert "Number(s.blocked_skus_count || 0)" in inventory_html
    assert "Number(cat.total_fetched || 0)" in inventory_html
    assert "Number(cat.products_created || 0)" in inventory_html
    assert "Number(cat.products_updated || 0)" in inventory_html
    assert "Number(cat.inventories_updated || 0)" in inventory_html
    assert "Number(match.matched_channels_count || rec.total_poppies_channels || 0)" in inventory_html
    assert "Number(rec.total_poppies_channels || 0)" in inventory_html

    users_html = _read_template("users.html")
    assert "naman_internal_app" not in users_html
    assert "`/api/v1/users/${userId}`" not in users_html
    assert users_html.count("/api/v1/users/${encodeURIComponent(userId)}") == 4

    sys_html = _read_template("system_status.html")
    assert "`/api/v1/system/errors/${errorId}/" not in sys_html
    assert "/api/v1/system/errors/${encodeURIComponent(errorId)}/resolve" in sys_html
    assert "/api/v1/system/errors/${encodeURIComponent(errorId)}/reopen" in sys_html
