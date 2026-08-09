"""Bounded Playwright browser automation for Dashboard AI."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict

from .base import AsyncHandler
from .runtime_user import get_default_runtime_user, get_runtime_user_home, mkdir_with_owner, wrap_argv_for_user


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SESSION_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}
_RUNNER = r"""
const fs = require('fs');
const { chromium } = require('playwright');
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {ok:false, session_id:cfg.session_id, steps:[], screenshots:[]};
const locator = (p,s) => {
  let l;
  if (s.role) l=p.getByRole(s.role, s.name === undefined ? {} : {name:s.name, exact:!!s.exact});
  else if (s.label) l=p.getByLabel(s.label, {exact:!!s.exact});
  else if (s.text) l=p.getByText(s.text, {exact:!!s.exact});
  else if (s.testid) l=p.getByTestId(s.testid);
  else l=p.locator(s.css || s.selector);
  return s.nth === undefined ? l : l.nth(s.nth);
};
(async () => {
 let context, video;
 try {
  const launch = {headless:true, viewport:cfg.viewport};
  if (cfg.executable_path) launch.executablePath = cfg.executable_path;
  if (cfg.video_dir) launch.recordVideo = {dir:cfg.video_dir, size:cfg.video_size};
  context = await chromium.launchPersistentContext(cfg.profile_dir, launch);
  const pages=context.pages(); const page=pages[0] || await context.newPage();
  video=page.video(); const started=Date.now();
  page.setDefaultTimeout(cfg.timeout_ms);
  if (cfg.start_url) await page.goto(cfg.start_url, {waitUntil:'domcontentloaded'});
  for (let i=0;i<cfg.steps.length;i++) {
   if(cfg.deadline_ms && Date.now()-started > cfg.deadline_ms) throw Error('recording duration limit reached');
   const s=cfg.steps[i], op=s.op; let value=null;
   try {
    const l = ['click','fill','press','select','check','uncheck','assert','read'].includes(op) ? locator(page,s) : null;
    if(op==='goto') await page.goto(s.url,{waitUntil:s.wait_until||'domcontentloaded'});
    else if(op==='click') await l.click();
    else if(op==='fill') await l.fill(String(s.value??''));
    else if(op==='press') await l.press(s.key);
    else if(op==='select') value=await l.selectOption(s.value);
    else if(op==='check') await l.check();
    else if(op==='uncheck') await l.uncheck();
    else if(op==='wait') s.ms ? await page.waitForTimeout(s.ms) : await page.waitForLoadState(s.state||'domcontentloaded');
    else if(op==='assert') { const kind=s.kind||'visible'; if(kind==='visible') await l.waitFor({state:'visible'}); else if(kind==='hidden') await l.waitFor({state:'hidden'}); else if(kind==='text'){const got=await l.textContent();if(!String(got||'').includes(String(s.value)))throw Error(`expected text ${s.value}`); } else if(kind==='url'){if(!page.url().includes(String(s.value)))throw Error(`expected URL ${s.value}`);} else throw Error('unsupported assertion'); }
    else if(op==='read') { if(s.attribute) value=await l.getAttribute(s.attribute); else if(s.kind==='value') value=await l.inputValue(); else if(s.kind==='count') value=await l.count(); else value=(await l.innerText()).slice(0,8000); }
    else if(op==='screenshot') { const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality,fullPage:!!s.full_page}); out.screenshots.push({step:i,data:b.toString('base64'),mime_type:'image/jpeg'}); }
    else throw Error(`unsupported operation: ${op}`);
    out.steps.push({i,op,ok:true,...(value===null?{}:{value})});
   } catch(e) { out.steps.push({i,op,ok:false,error:String(e.message||e).slice(0,500)}); if(cfg.screenshot_on_failure){const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality});out.screenshots.push({step:i,data:b.toString('base64'),mime_type:'image/jpeg',failure:true});} throw e; }
  }
  if(cfg.final_screenshot){const b=await page.screenshot({type:'jpeg',quality:cfg.image_quality});out.screenshots.push({step:'final',data:b.toString('base64'),mime_type:'image/jpeg'});}
  out.ok=true; out.url=page.url(); out.title=await page.title();
 } catch(e) {out.error=String(e.message||e).slice(0,1000);}
 finally {if(context) await context.close().catch(()=>{}); if(video){try{out.video_path=await video.path();}catch{}}}
 process.stdout.write(JSON.stringify(out));
})().catch(e=>{process.stdout.write(JSON.stringify({ok:false,error:String(e)}));process.exitCode=1});
"""


class BrowserRunHandler(AsyncHandler):
    @property
    def command_name(self) -> str:
        return "browser_run"

    async def execute(self, message: Dict[str, Any]) -> Dict[str, Any]:
        session_id = str(message.get("session_id") or "default")
        if not _SAFE_ID.fullmatch(session_id):
            raise ValueError("session_id must contain only letters, numbers, dot, dash, or underscore")
        steps = message.get("steps") or []
        if not isinstance(steps, list) or len(steps) > 30:
            raise ValueError("steps must be an array containing at most 30 actions")
        encoded = json.dumps(steps)
        if len(encoded) > 64_000:
            raise ValueError("browser action payload is too large")
        if sum(1 for step in steps if isinstance(step, dict) and step.get("op") == "screenshot") > 4:
            raise ValueError("at most four explicit screenshots are allowed per run")
        timeout_ms = min(max(int(message.get("timeout_ms") or 10_000), 250), 30_000)
        width = min(max(int((message.get("viewport") or {}).get("width", 960)), 320), 1920)
        height = min(max(int((message.get("viewport") or {}).get("height", 540)), 240), 1080)
        user = get_default_runtime_user(message)
        root = Path(get_runtime_user_home(message)) / ".local" / "share" / "portacode" / "browser"
        profile = root / "profiles" / session_id
        video_dir = root / "recordings" / session_id if message.get("record_video") else None
        if not profile.exists():
            mkdir_with_owner(profile, user)
        if video_dir:
            if not video_dir.exists():
                mkdir_with_owner(video_dir, user)
        executable = "/usr/bin/chromium-browser" if Path("/usr/bin/chromium-browser").exists() else None
        config = {
            "session_id": session_id, "profile_dir": str(profile), "video_dir": str(video_dir) if video_dir else None,
            "video_size": {"width": min(width, 854), "height": min(height, 480)},
            "viewport": {"width": width, "height": height}, "executable_path": executable,
            "start_url": message.get("start_url"), "steps": steps, "timeout_ms": timeout_ms,
            "deadline_ms": 45_000 if video_dir else None,
            "image_quality": min(max(int(message.get("image_quality") or 65), 30), 85),
            "final_screenshot": bool(message.get("final_screenshot", True)),
            "screenshot_on_failure": bool(message.get("screenshot_on_failure", True)),
        }
        env = dict(os.environ)
        npm = await asyncio.create_subprocess_exec("npm", "root", "-g", stdout=asyncio.subprocess.PIPE)
        npm_out, _ = await npm.communicate()
        env["NODE_PATH"] = os.pathsep.join(filter(None, [
            "/opt/portacode-playwright/node_modules",
            npm_out.decode().strip(),
        ]))
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/ms-playwright")
        argv = wrap_argv_for_user(["node", "-e", _RUNNER], user, login=False)
        lock = _SESSION_LOCKS.setdefault((user, session_id), asyncio.Lock())
        async with lock:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
            try:
                run_limit = min(75 if video_dir else 300, 20 + len(steps) * timeout_ms / 1000)
                stdout, stderr = await asyncio.wait_for(proc.communicate(json.dumps(config).encode()), timeout=run_limit)
            except asyncio.TimeoutError:
                proc.kill(); await proc.wait()
                raise RuntimeError("Browser run exceeded its bounded execution time")
        try:
            result = json.loads(stdout)
        except Exception as exc:
            raise RuntimeError(f"Playwright returned an invalid response: {stderr.decode(errors='replace')[:500]}") from exc
        result.update({"event": "browser_run_response", "success": True})
        return result
