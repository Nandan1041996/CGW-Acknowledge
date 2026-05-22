import time
import sys
import os
import smtplib
from email.message import EmailMessage
import psycopg2
from psycopg2.extras import RealDictCursor
import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
TARGET_EMAIL = os.getenv("TARGET_EMAIL")

class AutomationError(Exception):
    def __init__(self, message, attachment=None):
        super().__init__(message)
        self.attachment = attachment

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT
    )

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS royalty_pass_logs (
            id SERIAL PRIMARY KEY,
            rcode TEXT,
            status TEXT,
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS material_credentials (
            material TEXT PRIMARY KEY,
            username TEXT,
            password TEXT,
            mobile_number TEXT
        )
    ''')

    conn.commit()
    conn.close()

def log_to_db(rcode, status, message):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO royalty_pass_logs (rcode, status, message)
            VALUES (%s, %s, %s)
        ''', (rcode, status, message))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] Failed to log to DB: {e}")

def send_failure_email(rcode, reason, attachment_path=None):
    if not SMTP_USER or not SMTP_PASS or not TARGET_EMAIL:
        print("[EMAIL] SMTP credentials missing, email not sent.")
        return
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Automation Failed for RoyaltyPass: {rcode}"
        msg['From'] = SMTP_USER
        msg['To'] = TARGET_EMAIL
        
        content = f"Dear Team,\n\nPlease note that Royalty pass {rcode} belong to auto acknowledgement gets failed due to below reason.Please acknowledge pass in manual way..\n\nReason for failure:\n{reason}\n\nPlease check the logs or the portal for more details.\n\nRegards,\nSAP Automations"
        msg.set_content(content)
        
        if attachment_path and os.path.exists(attachment_path):
            import mimetypes
            ctype, encoding = mimetypes.guess_type(attachment_path)
            if ctype is None or encoding is not None:
                ctype = 'application/octet-stream'
            maintype, subtype = ctype.split('/', 1)
            with open(attachment_path, 'rb') as fp:
                msg.add_attachment(fp.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(attachment_path))
                
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            
        print(f"[EMAIL] Failure email sent to {TARGET_EMAIL}")
    except Exception as e:
        print(f"[EMAIL ERROR] Failed to send email: {e}")

init_db()

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    import ddddocr
    ocr = ddddocr.DdddOcr(det=False, old=False, show_ad=False)
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False
    print("[WARN] ddddocr not installed. Run: pip install ddddocr")
    print("[WARN] Falling back to manual captcha entry.")


CONFIG = {
    "headless":          True,
    "viewport_width":    1366,
    "viewport_height":   768,
    "slow_mo":           200,
    "captcha_mode":      "ddddocr",
    "api_key_2captcha":  "",
    "max_login_retries": 5,
}

BASE_URL = "https://cgmatr.ncode.in/cgm-ilms/login.aspx"

CAPTCHA_IMG_SEL     = "#imgCaptcha"
CAPTCHA_INPUT_SEL   = "#txtCaptcha"
CAPTCHA_REFRESH_SEL = "span.glyphicon-refresh, .glyphicon-refresh, [onclick*='Captcha' i], [onclick*='captcha' i], img[src*='refresh' i]"
ALERT_CLOSE_SEL     = ".alert .close, .alert button[data-dismiss], .close"


def dismiss_alert(page) -> None:
    """Close the 'Invalid Captcha' (or any) alert banner if it is visible."""
    try:
        close_btn = page.locator(ALERT_CLOSE_SEL).first
        if close_btn.count() > 0 and close_btn.is_visible():
            close_btn.click(timeout=2000)
            print("[INFO] Alert banner dismissed.")
    except Exception:
        pass


def click_captcha_refresh(page) -> None:
    """
    Click the ↻ refresh icon that sits next to the captcha input.
    This is the ONLY correct way to get a new captcha — it tells the server
    to generate a new image and update the session answer atomically.
    Falls back to clicking the captcha image itself if the icon is not found.
    """
    clicked = False

    try:
        el = page.locator(CAPTCHA_REFRESH_SEL).first
        if el.count() > 0 and el.is_visible():
            el.click(timeout=2000)
            print("[CAPTCHA] Clicked ↻ refresh icon.")
            clicked = True
    except Exception:
        pass

    if not clicked:
        try:
            img = page.locator(CAPTCHA_IMG_SEL).first
            if img.count() > 0:
                img.click(timeout=2000)
                print("[CAPTCHA] Clicked captcha image as refresh fallback.")
        except Exception:
            pass

    page.wait_for_timeout(800)


def screenshot_captcha(page) -> bytes:
    """
    Single screenshot of the captcha <img> element — what the browser rendered,
    what the server session is expecting.  No network requests.
    """
    el = page.locator(CAPTCHA_IMG_SEL).first
    raw = el.screenshot()
    print(f"[CAPTCHA] Screenshot taken ({len(raw)} bytes).")
    return raw


def type_captcha(page, text: str) -> None:
    """
    Clear the captcha field and type the answer character-by-character using
    keyboard events.  page.fill() may normalise case on some ASP.NET pages;
    keyboard typing preserves every character exactly as-is.
    """
    inp = page.locator(CAPTCHA_INPUT_SEL)
    inp.click()
  
    inp.press("Control+a")
    inp.press("Delete")

    for ch in text:
        page.keyboard.type(ch)
    print(f"[CAPTCHA] Typed into field: '{text}'")


def solve_with_ddddocr(page) -> str:
    raw = screenshot_captcha(page)
    result = ocr.classification(raw)
    result = "".join(c for c in result if c.isalnum())
    print(f"[CAPTCHA] ddddocr result: '{result}'")
    return result


def solve_with_manual(page) -> str:
    captcha_path = Path("captcha_temp.png")
    screenshot_captcha(page)
    page.locator(CAPTCHA_IMG_SEL).first.screenshot(path=str(captcha_path))
    print(f"[CAPTCHA] Saved to: {captcha_path.resolve()}  — open it and type the text below.")
    return input("[CAPTCHA] Enter captcha text: ").strip()


def solve_with_2captcha(page) -> str:
    import requests, base64

    api_key = CONFIG["api_key_2captcha"]
    if not api_key:
        raise ValueError("api_key_2captcha is empty in CONFIG.")

    raw = screenshot_captcha(page)
    b64 = base64.b64encode(raw).decode()
    resp = requests.post("http://2captcha.com/in.php", data={
        "key": api_key, "method": "base64", "body": b64, "json": 1,
    }).json()

    if resp.get("status") != 1:
        raise RuntimeError(f"2captcha submission error: {resp}")

    captcha_id = resp["request"]
    print(f"[CAPTCHA] Sent to 2captcha (id={captcha_id}), waiting…")

    for _ in range(20):
        time.sleep(5)
        result = requests.get(
            f"http://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1"
        ).json()
        if result.get("status") == 1:
            print(f"[CAPTCHA] 2captcha answered: '{result['request']}'")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha error: {result}")

    raise TimeoutError("2captcha did not respond within 100 s.")


def solve_captcha(page) -> str:
    mode = CONFIG["captcha_mode"]
    if mode == "ddddocr":
        if not OCR_AVAILABLE:
            print("[WARN] ddddocr unavailable — switching to manual.")
            return solve_with_manual(page)
        return solve_with_ddddocr(page)
    elif mode == "2captcha":
        return solve_with_2captcha(page)
    else:
        return solve_with_manual(page)

def run(username: str, password: str, mobile_number: str, search_rcode: str, material: str = "LS"):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=CONFIG["headless"],
            slow_mo=CONFIG["slow_mo"],
        )
        context = browser.new_context(
            viewport={
                "width":  CONFIG["viewport_width"],
                "height": CONFIG["viewport_height"],
            }
        )
        page = context.new_page()

        print("\n[1] Opening login page...")
        page.goto(BASE_URL, wait_until="networkidle")

        print("[2] Entering credentials...")
        page.fill("#txtLoginUserName", username)
        page.fill("#txtLoginPassword", password)
        page.fill("#MobileNumber",     mobile_number)

        logged_in = False

        for attempt in range(1, CONFIG["max_login_retries"] + 1):
            print(f"\n[3] CAPTCHA attempt {attempt}/{CONFIG['max_login_retries']}...")

            page.fill("#txtLoginUserName", username)
            page.fill("#txtLoginPassword", password)
            page.fill("#MobileNumber",     mobile_number)

            print("[INFO] Clicking ↻ to get a fresh captcha from server...")
            click_captcha_refresh(page)

            captcha_text = solve_captcha(page)
            if not captcha_text:
                print("[WARN] Empty OCR result — skipping attempt.")
                continue

            type_captcha(page, captcha_text)

            print("[4] Clicking Login...")
            page.click("#LoginButton")

            try:
                page.wait_for_url("**/Operation.aspx**", timeout=8000)
                print("[4] Login successful!")
                logged_in = True
                break
            except PlaywrightTimeout:
                error_msg = ""
                for err_sel in [".alert", "#lblError", ".error", "[class*='error']"]:
                    try:
                        error_msg = page.inner_text(err_sel, timeout=1500).strip()
                        if error_msg:
                            break
                    except Exception:
                        continue
                print(f"[WARN] Login failed. Reason: '{error_msg or 'unknown'}'")

                if attempt < CONFIG["max_login_retries"]:
                 
                    dismiss_alert(page)
                    page.wait_for_timeout(500)

        if not logged_in:
            print("\n[ERROR] All login attempts failed.")
            page.screenshot(path="login_failed.png")
            print("[ERROR] Screenshot saved to login_failed.png")
            browser.close()
            raise AutomationError("All login attempts failed. Check login_failed.png.", attachment="login_failed.png")

        if material == 'LIG':
            print("\n[INFO] Material is LIG. Clicking Update Later...")
            try:
                update_later_btn = "#ctl00_ContentPlaceHolder1_cnt_btnUpdateLater_ST"
                page.wait_for_selector(update_later_btn, timeout=10000)
                page.click(update_later_btn)
                page.wait_for_load_state("networkidle")
                print("[INFO] Clicked Update Later.")
            except PlaywrightTimeout:
                print("[WARN] Update Later button not found or timeout, continuing...")

        print("\n[5] Clicking DC module...")
        dc_btn = "#ctl00_ContentPlaceHolder1_cnt_rptLeaseDashboard_ctl01_BtnLoginModule"
        page.wait_for_selector(dc_btn, timeout=15000)
        page.click(dc_btn)
        page.wait_for_load_state("networkidle")
        print("[5] DC module loaded.")

        print(f"\n[6] Entering R-Code: {search_rcode}")
        rcode_input = "#ContentPlaceHolder1_cnt_txtFilterRcode"
        page.wait_for_selector(rcode_input, timeout=10000)
        page.click(rcode_input)
        page.fill(rcode_input, search_rcode)

        print("[7] Clicking Search...")
        page.click("#ContentPlaceHolder1_cnt_btnSearch")
        page.wait_for_load_state("networkidle")
        print("[7] Search complete.")

        print("\n[8] Selecting first result row...")
        chk = "#ContentPlaceHolder1_cnt_gvRoyaltyGrid_chkSelect_0"
        try:
            page.wait_for_selector(chk, timeout=10000)
            page.check(chk)
            print("[8] Row selected.")
        except PlaywrightTimeout:
            browser.close()
            raise AutomationError("No Royalty pass code found")

        print("\n[9] Clicking Acknowledge...")
        page.wait_for_selector("#ContentPlaceHolder1_cnt_btnAcknowledge", timeout=5000)
        page.click("#ContentPlaceHolder1_cnt_btnAcknowledge")
        page.wait_for_load_state("networkidle")
        print("[9] Done.")

        print("\n══════════════════════════════════════")
        print("  Automation completed successfully!")
        print("══════════════════════════════════════\n")

        if not CONFIG["headless"]:
            input("Press Enter to close the browser...")

        browser.close()

def create_app():
    from flask import Flask, request, render_template_string, jsonify, redirect, url_for

    app = Flask(__name__)

    HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>CGM Automation Portal</title>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:       #0d0f14;
      --surface:  #13161e;
      --border:   #1f2433;
      --accent:   #f0a500;
      --accent2:  #e05c2a;
      --text:     #e8eaf0;
      --muted:    #5a6070;
      --success:  #3ecf8e;
      --error:    #f05c5c;
      --mono:     'IBM Plex Mono', monospace;
      --sans:     'Syne', sans-serif;
    }
    body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 2rem; overflow: hidden; }
    body::before { content: ''; position: fixed; inset: 0; background-image: linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px); background-size: 48px 48px; opacity: 0.4; pointer-events: none; }
    body::after { content: ''; position: fixed; top: -20%; right: -10%; width: 600px; height: 600px; border-radius: 50%; background: radial-gradient(circle, rgba(240,165,0,0.12) 0%, transparent 70%); pointer-events: none; }
    .card { position: relative; background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 3rem 3.5rem; width: 100%; max-width: 600px; box-shadow: 0 0 0 1px rgba(240,165,0,0.06), 0 32px 80px rgba(0,0,0,0.6); animation: slideUp 0.5s cubic-bezier(0.16,1,0.3,1) both; }
    @keyframes slideUp { from { opacity: 0; transform: translateY(24px); } to { opacity: 1; transform: translateY(0); } }
    .card::before { content: ''; position: absolute; top: 0; left: 0; width: 48px; height: 48px; border-top: 2px solid var(--accent); border-left: 2px solid var(--accent); border-radius: 4px 0 0 0; }
    .card::after { content: ''; position: absolute; bottom: 0; right: 0; width: 48px; height: 48px; border-bottom: 2px solid var(--accent2); border-right: 2px solid var(--accent2); border-radius: 0 0 4px 0; }
    .badge { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 0.65rem; letter-spacing: 0.15em; text-transform: uppercase; color: var(--accent); border: 1px solid rgba(240,165,0,0.3); padding: 4px 10px; border-radius: 2px; margin-bottom: 1.5rem; text-decoration: none; }
    .badge::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); animation: pulse 2s ease-in-out infinite; }
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
    h1 { font-size: 2rem; font-weight: 800; line-height: 1.1; letter-spacing: -0.02em; margin-bottom: 0.4rem; }
    h1 span { color: var(--accent); }
    label { display: block; font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.6rem; }
    input[type="text"], select { width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 2px; color: var(--text); font-family: var(--mono); font-size: 0.95rem; letter-spacing: 0.05em; padding: 0.85rem 1rem; outline: none; transition: border-color 0.2s, box-shadow 0.2s; margin-bottom: 1.8rem; }
    input[type="text"]:focus, select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(240,165,0,0.12); }
    button { width: 100%; background: var(--accent); color: #0d0f14; font-family: var(--sans); font-size: 0.9rem; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; border: none; border-radius: 2px; padding: 0.9rem 1.5rem; cursor: pointer; position: relative; overflow: hidden; transition: background 0.2s, transform 0.1s; }
    button:hover { background: #ffc833; }
    button:active { transform: scale(0.98); }
    button:disabled { background: var(--border); color: var(--muted); cursor: not-allowed; }
    button.loading::after { content: ''; position: absolute; inset: 0; background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.3) 50%, transparent 100%); animation: shimmer 1s linear infinite; }
    @keyframes shimmer { from { transform: translateX(-100%); } to { transform: translateX(100%); } }
    #status { margin-top: 1.5rem; font-family: var(--mono); font-size: 0.78rem; line-height: 1.7; letter-spacing: 0.03em; min-height: 1.5rem; color: var(--muted); transition: color 0.3s; }
    #status.running { color: var(--accent); } #status.success { color: var(--success); } #status.error { color: var(--error); }
    .divider { height: 1px; background: var(--border); margin: 1.8rem 0; }
    .nav-links { position: absolute; top: 1rem; right: 1rem; display: flex; gap: 1rem; font-family: var(--mono); font-size: 0.8rem; }
    .nav-links a { color: var(--muted); text-decoration: none; transition: color 0.2s; }
    .nav-links a:hover { color: var(--accent); }
  </style>
</head>
<body>
  <div class="nav-links">
      <a href="/credentials">Manage Credentials</a>
  </div>
  <div class="card">
    <div class="badge">CGM Automation</div>
    <h1>Enter <span>RoyaltyPass&#8209;Code</span></h1>

    <label for="rcode">RoyaltyPass Code</label>
    <input type="text" id="rcode" placeholder="e.g. ML310400012500041366" autocomplete="off" spellcheck="false" />

    <label for="material">Material</label>
    <select id="material">
      <option value="LS">Limestone Domestic (LS)</option>
      <option value="LIG">Lignite Captive (LIG)</option>
    </select>

    <button id="runBtn" onclick="runAutomation()">Submit</button>

    <div class="divider"></div>
    <div id="status">Awaiting R-Code input...</div>
  </div>

  <script>
    async function runAutomation() {
      const rcode = document.getElementById('rcode').value.trim();
      const material = document.getElementById('material').value;
      const btn   = document.getElementById('runBtn');
      const status = document.getElementById('status');

      if (!rcode) {
        status.textContent = '✖ Please enter a valid RoyaltyPass Code.';
        status.className = 'error';
        return;
      }

      btn.disabled = true;
      btn.classList.add('loading');
      btn.textContent = 'Running...';
      status.textContent = '▶ Starting automation for: ' + rcode;
      status.className = 'running';

      try {
        const res  = await fetch('/run', {
          method:  'POST',
          headers: { 'Content-Type': 'application/json' },
          body:    JSON.stringify({ rcode, material }),
        });
        const data = await res.json();

        if (data.success) {
          status.textContent = '✔ Automation completed successfully for: ' + rcode;
          status.className = 'success';
        } else {
          status.textContent = '✖ Failed: ' + (data.error || 'Unknown error');
          status.className = 'error';
        }
      } catch (err) {
        status.textContent = '✖ Request error: ' + err.message;
        status.className = 'error';
      } finally {
        btn.disabled = false;
        btn.classList.remove('loading');
        btn.textContent = 'Submit';
      }
    }
    document.getElementById('rcode').addEventListener('keydown', e => { if (e.key === 'Enter') runAutomation(); });
  </script>
</body>
</html>
    """

    CREDENTIALS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Manage Credentials</title>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Syne:wght@400;700;800&display=swap" rel="stylesheet"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:       #0d0f14;
      --surface:  #13161e;
      --border:   #1f2433;
      --accent:   #f0a500;
      --accent2:  #e05c2a;
      --text:     #e8eaf0;
      --muted:    #5a6070;
      --success:  #3ecf8e;
      --error:    #f05c5c;
      --mono:     'IBM Plex Mono', monospace;
      --sans:     'Syne', sans-serif;
    }
    body { background: var(--bg); color: var(--text); font-family: var(--sans); min-height: 100vh; padding: 4rem 2rem; overflow-y: auto; }
    body::before { content: ''; position: fixed; inset: 0; background-image: linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px); background-size: 48px 48px; opacity: 0.4; pointer-events: none; z-index: -1;}
    .container { max-width: 800px; margin: 0 auto; }
    .card { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 2rem; margin-bottom: 2rem; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
    h1, h2 { font-weight: 800; color: var(--accent); margin-bottom: 1.5rem; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 1.5rem; font-family: var(--mono); font-size: 0.9rem; }
    th, td { padding: 1rem; text-align: left; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; font-size: 0.75rem; }
    label { display: block; font-family: var(--mono); font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); margin-bottom: 0.6rem; margin-top: 1rem; }
    input[type="text"] { width: 100%; background: var(--bg); border: 1px solid var(--border); border-radius: 2px; color: var(--text); font-family: var(--mono); font-size: 0.95rem; padding: 0.6rem 1rem; outline: none; margin-bottom: 1rem; }
    input[type="text"]:focus { border-color: var(--accent); }
    button { background: var(--accent); color: #0d0f14; font-family: var(--sans); font-size: 0.9rem; font-weight: 700; border: none; border-radius: 2px; padding: 0.6rem 1.2rem; cursor: pointer; transition: background 0.2s; }
    button:hover { background: #ffc833; }
    .nav-links { margin-bottom: 2rem; font-family: var(--mono); font-size: 0.9rem; }
    .nav-links a { color: var(--accent); text-decoration: none; }
  </style>
</head>
<body>
  <div class="container">
    <div class="nav-links"><a href="/">&larr; Back to Portal</a></div>
    <h1>Manage Material Credentials</h1>
    <div class="card">
        <table>
            <tr><th>Material</th><th>Username</th><th>Password</th><th>Mobile</th></tr>
            {% for row in rows %}
            <tr>
                <td>{{ row.material }}</td>
                <td>{{ row.username }}</td>
                <td>{{ row.password }}</td>
                <td>{{ row.mobile_number }}</td>
            </tr>
            {% endfor %}
        </table>
    </div>
    
    <div class="card">
        <h2>Add / Update Credentials</h2>
        <form method="POST" action="/credentials">
            <label>Material (e.g. LS, LIG)</label>
            <input type="text" name="material" required>
            <label>Username</label>
            <input type="text" name="username" required>
            <label>Password</label>
            <input type="text" name="password" required>
            <label>Mobile Number</label>
            <input type="text" name="mobile_number" required>
            <button type="submit">Save</button>
        </form>
    </div>
  </div>
</body>
</html>
    """

    def get_credentials(material):
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM material_credentials WHERE material = %s", (material,))
        row = cursor.fetchone()
        conn.close()
        return row

    @app.route("/")
    def index():
        return render_template_string(HTML)
        
    @app.route("/credentials", methods=["GET", "POST"])
    def credentials():
        if request.method == "POST":
            material = request.form.get("material").strip().upper()
            username = request.form.get("username").strip()
            password = request.form.get("password").strip()
            mobile_number = request.form.get("mobile_number").strip()
            if material and username and password and mobile_number:
                conn = get_db_connection()
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO material_credentials (material, username, password, mobile_number) 
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (material) 
                    DO UPDATE SET username = EXCLUDED.username, password = EXCLUDED.password, mobile_number = EXCLUDED.mobile_number
                """, (material, username, password, mobile_number))
                conn.commit()
                conn.close()
            return redirect(url_for("credentials"))

        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM material_credentials ORDER BY material")
        rows = cursor.fetchall()
        conn.close()
        return render_template_string(CREDENTIALS_HTML, rows=rows)

    @app.route("/run", methods=["POST"])
    def run_automation_ui():
        data = request.get_json(force=True)
        rcode = (data.get("rcode") or "").strip()
        material = (data.get("material") or "LS").strip().upper()

        if not rcode:
            return jsonify({"success": False, "error": "R-Code is required."}), 400

        creds = get_credentials(material)
        if not creds:
            return jsonify({"success": False, "error": f"Credentials not found for material {material}."}), 400

        try:
            run(creds['username'], creds['password'], creds['mobile_number'], rcode, material=material)
            log_to_db(rcode, "SUCCESS", "Automation completed successfully")
            return jsonify({"success": True})
        except AutomationError as exc:
            error_msg = str(exc)
            log_to_db(rcode, "FAILED", error_msg)
            send_failure_email(rcode, error_msg, exc.attachment)
            return jsonify({"success": False, "error": error_msg}), 500
        except Exception as exc:
            error_msg = str(exc)
            log_to_db(rcode, "FAILED", error_msg)
            send_failure_email(rcode, error_msg)
            return jsonify({"success": False, "error": error_msg}), 500

    @app.route("/api/acknowledge", methods=["POST"])
    def api_acknowledge():
        data = request.get_json(force=True)
        search_rcode = (data.get("search_rcode") or "").strip()
        material = (data.get("material") or "LS").strip().upper()

        if not search_rcode:
            return jsonify({"success": False, "error": "search_rcode is required."}), 400

        creds = get_credentials(material)
        if not creds:
            return jsonify({"success": False, "error": f"Credentials not found for material {material}."}), 400

        import threading

        def background_task(usr, pwd, mob, rcode, mat):
            try:
                run(usr, pwd, mob, rcode, material=mat)
                log_to_db(rcode, "SUCCESS", "Automation completed successfully via API")
            except AutomationError as exc:
                error_msg = str(exc)
                log_to_db(rcode, "FAILED", f"API Error: {error_msg}")
                send_failure_email(rcode, f"API Request failed: {error_msg}", exc.attachment)
            except Exception as exc:
                error_msg = str(exc)
                log_to_db(rcode, "FAILED", f"API Error: {error_msg}")
                send_failure_email(rcode, f"API Request failed: {error_msg}")

        thread = threading.Thread(target=background_task, args=(creds['username'], creds['password'], creds['mobile_number'], search_rcode, material))
        thread.start()

        return jsonify({
            "success": True,
            "message": f"Automation for {search_rcode} (Material: {material}) has been started in the background."
        }), 202

    @app.route("/api/status/<rcode>", methods=["GET"])
    def api_status(rcode):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('''
                SELECT status, message, created_at 
                FROM royalty_pass_logs 
                WHERE rcode = %s 
                ORDER BY created_at DESC 
                LIMIT 1
            ''', (rcode,))
            row = cursor.fetchone()
            conn.close()

            if row:
                return jsonify({
                    "rcode": rcode,
                    "status": row[0],
                    "message": row[1],
                    "timestamp": row[2]
                })
            else:
                return jsonify({
                    "rcode": rcode,
                    "status": "PENDING_OR_NOT_FOUND",
                    "message": "No logs found for this R-Code. It might still be processing or it doesn't exist."
                }), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app

if __name__ == "__main__":
    if len(sys.argv) > 1:
        rcode = sys.argv[1]
        material = sys.argv[2] if len(sys.argv) > 2 else "LS"
        try:
            # For CLI we will need credentials from DB. Since create_app hasn't run, we can fetch them here.
            conn = get_db_connection()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM material_credentials WHERE material = %s", (material,))
            creds = cursor.fetchone()
            conn.close()
            
            if not creds:
                raise ValueError(f"Credentials not found for material {material}")
                
            run(creds['username'], creds['password'], creds['mobile_number'], rcode, material=material)
            log_to_db(rcode, "SUCCESS", "Automation completed successfully via CLI")
        except AutomationError as e:
            error_msg = str(e)
            log_to_db(rcode, "FAILED", error_msg)
            send_failure_email(rcode, error_msg, e.attachment)
            sys.exit(1)
        except Exception as e:
            error_msg = str(e)
            log_to_db(rcode, "FAILED", error_msg)
            send_failure_email(rcode, error_msg)
            sys.exit(1)
    else:
        app = create_app()
        try:
            from waitress import serve
            print("Starting Waitress production server on 0.0.0.0:5000...")
            serve(app, host="0.0.0.0", port=5000)
        except ImportError:
            print("[WARN] Waitress not installed. Run 'pip install waitress'. Falling back to Flask dev server.")
            app.run(host="0.0.0.0", port=5000, debug=False)
