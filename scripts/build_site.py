"""Build the Workers static-asset directory (.site/) from static/: index.html at /, the rest under /static/."""
import pathlib, re, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
SITE = ROOT / ".site"
shutil.rmtree(SITE, ignore_errors=True)
(SITE / "static").mkdir(parents=True)
for f in (ROOT / "static").iterdir():
    if f.is_dir():  # fonts/, img/
        shutil.copytree(f, SITE / "static" / f.name)
    elif f.is_file():
        shutil.copy(f, SITE / ("index.html" if f.name == "index.html" else f"static/{f.name}"))
# Same security headers the FastAPI middleware sets, so static assets get them too.
csp = re.search(r'CSP = \(("(?:[^"\\]|\\.)*"\s*)+\)', (ROOT / "src/app.py").read_text()).group(0)
csp = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', csp))
(SITE / "_headers").write_text(f"""/*
  Content-Security-Policy: {csp}
  X-Content-Type-Options: nosniff
  X-Frame-Options: DENY
  Referrer-Policy: no-referrer
  Permissions-Policy: geolocation=(), camera=(), microphone=()
  Cross-Origin-Opener-Policy: same-origin
  Strict-Transport-Security: max-age=31536000; includeSubDomains

/static/fonts/*
  Cache-Control: public, max-age=31536000, immutable

/static/img/*
  Cache-Control: public, max-age=2592000
""")
print(f"built {SITE}")
